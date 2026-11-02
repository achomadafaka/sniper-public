"""The exit dust-route guard: never sell a bag for dust while our own market read still prices it.

WHY (Gamma MED, operator "fix everything", 2026-10-02). After the 15:11Z restart Kyber valued the
WHOLE DAODUN bag (position 68544, chain 56) at 0.0002 WBNB (~$0.16) while it traded at ~$44. The
ladder's dust check fired but still chose Kyber, the runner marked -99.8%, fired b_stop INSTANT
("first read sells, confirmation skipped") and SENT 9 sells with a ~0.00014 WBNB floor. Only the
reverts saved it. RIZOWEEN 71056 (4663, 2026-10-06): the route paid $3.67 for a $50 bag five
seconds after entry; the operator sold it by hand 36 minutes later for $10.13.

THE RULE (rebuilt 2026-10-06 on Gamma's MED against d219672fb and operator ruling b2ae12c0), in the
executors (every exit_fn caller: sweep, mirror, the rest), right before the floor-sanity gate:
  1. CANDIDATE: the exit's PRE-slippage quote unit price sits at or under MEMECOIN_EXIT_DUST_FRACTION
     of its reference (the last good executable price younger than MEMECOIN_EXIT_DUST_REF_MAX_AGE_S,
     else the entry price). Pre-slippage, because the signed floor carries the slippage rung: a thin
     -80% stop signed at a 60% rung reads 0.08 x entry and is no dust route. Not a candidate: the
     exit proceeds, with no market read at all.
  2. MARKET: our own NEWEST market mark for the coin -- Solana swap/curve/state reads younger than
     EVIDENCE_WINDOW_S, or the 4663 swap tape anchored on our entry fill. The quote above
     MARKET_FRACTION x that mark means the market is down too (a real collapse): the exit proceeds.
     At or under it: a dust route, HOLD.
  3. NO OWN MARK (8453 and 56 today, a V2 or Pons pool, an aged-out tape anchor, a pulled pool):
     HOLD. No third-party chart is ever asked; the operator accepted the holds.
HOLD: nothing is signed, the claim is released, status dust_route_hold, and Telegram is paged once
per episode with the force-sell action. A manual exit ("manual") is never held.
MEMECOIN_EXIT_DUST_GUARD=0 turns it all off.

The sweep keeps only its part: a b_stop/catastrophe never skips confirmation on a dust route, and a
dust-route preview does not overwrite last_executable_price (the guard's "last good" reference).
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("memecoin")

EVIDENCE_WINDOW_S = 300.0
MARKET_FRACTION = 0.25
# Our OWN reads of a Solana coin's pool, by exact name: no prefix may admit a future producer.
# Never fastlane_rpc / exact_route (our own exit quote, i.e. the dust route itself), never a
# post-close tape row (anchored on a previous position's exit fill).
OWN_MARK_SOURCES = ("fastlane_sol_swap", "fastlane_sol_curve", "fastlane_sol_state")
TAPE_CHAINS = ("4663",)
_LOCK = threading.Lock()
_PAGED = set()                 # position ids paged in the current hold episode


def _cfg():
    from sniper.settings import memecoin as cfg
    return cfg


def enabled():
    return bool(getattr(_cfg(), "MEMECOIN_EXIT_DUST_GUARD", True))


def fraction():
    return float(getattr(_cfg(), "MEMECOIN_EXIT_DUST_FRACTION", 0.10) or 0.10)


def _parse(iso):
    try:
        out = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def reference_unit_price(position, now_iso):
    """USD per token the dust line is drawn under: the last good executable price if younger than
    MEMECOIN_EXIT_DUST_REF_MAX_AGE_S, else the entry price; None when neither is usable."""
    max_age = float(getattr(_cfg(), "MEMECOIN_EXIT_DUST_REF_MAX_AGE_S", 3600.0) or 0.0)
    last, at, now = position.get("last_executable_price"), _parse(position.get("last_executable_at")), _parse(now_iso)
    try:
        if last and math.isfinite(float(last)) and float(last) > 0 and at and now and 0 <= (now - at).total_seconds() <= max_age:
            return float(last)
        entry = float(position.get("entry_price") or 0)
        return entry if math.isfinite(entry) and entry > 0 else None
    except (TypeError, ValueError):
        return None


def is_dust_unit(unit_price, ref_unit):
    """True when an executable unit price sits at or under the dust line. Unknown inputs: False."""
    try:
        return (unit_price is not None and ref_unit is not None
                and math.isfinite(float(unit_price)) and math.isfinite(float(ref_unit)) and float(ref_unit) > 0
                and 0 <= float(unit_price) <= fraction() * float(ref_unit))
    except (TypeError, ValueError):
        return False


def pre_slippage_usd(floor_usd, quote_payload):
    """The quote's PRE-slippage value in USD: the signed floor x expected / minimum output, both
    read from the same payload so their unit (quote-token atomic, USDC, lamports) cancels. An
    unreadable ratio, or an expected output under the minimum, returns the signed floor itself:
    the stricter reading the guard had before."""
    if floor_usd is None:
        return None
    payload = quote_payload or {}
    try:
        expected = int(payload.get("buyAmount", payload.get("outAmount")))
        minimum = int(payload.get("minBuyAmount", payload.get("otherAmountThreshold")))
    except (TypeError, ValueError, AttributeError):
        return floor_usd
    if minimum <= 0 or expected < minimum:
        return floor_usd
    return float(floor_usd) * expected / minimum


def _tape_mark(conn, position, now_iso):
    """The 4663 swap-tape mark anchored on our entry fill (memecoin_evm_tape_marks.exit_mark)."""
    if str(position.get("network_id") or "") not in TAPE_CHAINS or position.get("id") is None:
        return None
    now = _parse(now_iso)
    if now is None:
        return None
    from sniper.services.memecoin_evm_tape_marks import exit_mark
    return exit_mark(conn, position["id"], now.timestamp())


def newest_own_mark(conn, position, now_iso, *, tape_fn=None):
    """(price USD per token, source) from our OWN newest market read of this coin, or None.

    Solana: the newest valid row among OWN_MARK_SOURCES no older than EVIDENCE_WINDOW_S (a zero,
    negative, non-finite, quarantined or future row is absent). 4663: the swap-tape mark. Every
    other chain: None."""
    chain = str(position.get("network_id") or "")
    if chain in TAPE_CHAINS:
        mark = (tape_fn or _tape_mark)(conn, position, now_iso)
        price = _positive((mark or {}).get("price"))
        return (price, str(mark.get("source") or "tape")) if price else None
    if chain != "solana":
        return None
    from sniper.persistence import db_memecoin as m
    now = _parse(now_iso)
    if now is None:
        return None
    now = now.astimezone(timezone.utc)
    since = (now - timedelta(seconds=EVIDENCE_WINDOW_S)).isoformat()
    until = (now + timedelta(seconds=1)).isoformat()        # coarse; the age test below is exact
    for row in m.recent_position_marks(conn, chain, position.get("mint"), since, until,
                                       sources=OWN_MARK_SOURCES, limit=20):     # newest first
        at, price = _parse(row.get("sweep_ts")), _positive(row.get("mid"))
        if (row.get("source") in OWN_MARK_SOURCES and price and at is not None
                and 0 <= (now - at).total_seconds() <= EVIDENCE_WINDOW_S):
            return price, row["source"]
    return None


def _market_down_too(conn, position, unit, now_iso, tape_fn):
    """(True when our newest own mark shows the market down too, the mark or None)."""
    mark = newest_own_mark(conn, position, now_iso, tape_fn=tape_fn)
    return (mark is not None and float(unit) > MARKET_FRACTION * mark[0]), mark


def sweep_dust_route(position, exact_mid, now_iso, conn=None, *, tape_fn=None):
    """The sweep's question: does this exact preview (pre-slippage) sit on a dust route? Then a
    b_stop/catastrophe keeps its confirmation and last_executable_price is not overwritten. With a
    connection, a candidate whose market is down too is no dust route. False when off."""
    if not enabled() or exact_mid is None:
        return False
    if not is_dust_unit(exact_mid, reference_unit_price(position, now_iso)):
        return False
    if conn is None:
        return True
    try:
        return not _market_down_too(conn, position, exact_mid, now_iso, tape_fn)[0]
    except Exception:
        logger.exception("dust guard: sweep market read failed pos=%s", position.get("id"))
        return True


def _page(position, text, page_fn):
    try:
        if page_fn is not None:
            page_fn(text)
        else:
            from sniper.services.memecoin_alerts import send_alert
            send_alert("dust_route_hold", text)
    except Exception:
        logger.exception("dust guard: page failed pos=%s", position.get("id"))


def hold_reason(conn, position, *, exit_reason, floor_usd, sell_qty, now_iso, quote_payload=None,
                expected_usd=None, page_fn=None, tape_fn=None):
    """A reason to HOLD this exit (sign nothing), or None to let it proceed. Never raises.
    `expected_usd`, when the caller already holds the pre-slippage value in USD (the flap.sh quote
    exit), replaces the payload ratio; an unusable one falls back to it."""
    known_dust = False
    try:
        if not enabled() or str(exit_reason or "") == "manual" or str(exit_reason or "").startswith("manual_"):
            return None
        pid = position.get("id")
        ref_unit = reference_unit_price(position, now_iso)
        qty = float(sell_qty or 0)
        if floor_usd is None or ref_unit is None or qty <= 0:
            return None                              # cannot judge: today's behaviour
        if not math.isfinite(qty) or not math.isfinite(float(floor_usd)) or float(floor_usd) < 0:
            return "dust_route_hold: invalid quote units"
        value = _positive(expected_usd)
        if value is None or value < float(floor_usd):
            value = pre_slippage_usd(float(floor_usd), quote_payload)
        unit = value / qty
        if not is_dust_unit(unit, ref_unit):
            with _LOCK:
                _PAGED.discard(pid)                  # the episode is over
            return None
        known_dust = True
        down_too, mark = _market_down_too(conn, position, unit, now_iso, tape_fn)
        if down_too:
            logger.warning("[memecoin] dust candidate on pos=%s but our own market read is down too "
                           "(%s %.6g): selling as today", pid, mark[1], mark[0])
            return None
        market = ("market %.6g (%s)" % mark) if mark else "no own market mark"
        reason = ("dust_route_hold: exit quote $%.4f for %.6g tokens <= %.0f%% of reference $%.4f; %s"
                  % (value, qty, fraction() * 100, ref_unit * qty, market))
        logger.warning("[memecoin] %s pos=%s exit_reason=%s", reason, pid, exit_reason)
        with _LOCK:
            first = pid not in _PAGED
            _PAGED.add(pid)
        if first:
            _page(position, "DUST ROUTE HOLD pos %s %s on %s: the best exit pays $%.2f for a bag "
                  "referenced at $%.2f; %s. Not selling; the bag keeps its slot and exposure. "
                  "Force-sell: the dashboard's Sell at any price on position %s "
                  "(POST /api/memecoin/positions/%s/close {\"any_price\": true})."
                  % (pid, position.get("ticker") or position.get("mint"), position.get("network_id"),
                     value, ref_unit * qty, market, pid, pid),
                  page_fn)
        return reason
    except Exception:
        logger.exception("dust guard failed pos=%s known_dust=%s", position.get("id"), known_dust)
        return "dust_route_hold: evidence failure" if known_dust else None
