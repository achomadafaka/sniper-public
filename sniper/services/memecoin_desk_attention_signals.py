"""Tape-side attention signals for open desk positions (pure; no I/O, no clock, no network).

Operator order 2026-10-22 (ENGINE ATTENTION SIGNALS), design accepted by Alpha / Beta / Gamma. Five signals computed from the
committed U tape: creator_sell, sell_burst, buyer_exodus, volume_spike, holder_dump. Definitions are Gamma-pre-registered;
every threshold is a PROVISIONAL knob (MEMECOIN_DESK_ATTN_*) that Gamma fixes from the outcome-free dev-day rates.

Rules baked in:
  * our own wallets never count (our fill and exit would trigger the signals on themselves);
  * sell_burst counts DISTINCT NON-CREATOR sellers (the creator's own sell is the other half of the auto rule);
  * only swaps with received_at < cut are read (the committed prefix), strictly;
  * unknown is not calm: this module returns events only; blind / stale tape is the worker's explicit 'unknown' row;
  * an event carries closed numbers and enums plus tape evidence (signature, slot, ordinal); no text from a coin can pass
    validate_event, so nothing the coin's author wrote reaches the model prompt;
  * event_id is a hash of (position, signal, window key, source): a restart replays nothing twice.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, fields

SOURCE = "tape"
# The Mayhem agent is a fee-free automated wallet (73% of the swaps on mayhem coins): not demand. Its trades are never counted by the FLOW
# signals (sell_burst, buyer_exodus, volume_spike). Pinned equal to research/state.MAYHEM_AGENT_IDS by a test.
AGENTS = frozenset(("BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",))
PARAMS_VERSION = "attn-params-provisional-v0"
SIGNALS = ("creator_sell", "sell_burst", "buyer_exodus", "volume_spike", "holder_dump")
_SLUG = re.compile(r"[A-Za-z0-9]{1,128}\Z")


@dataclass(frozen=True)
class Params:
    creator_sell_min_usd: float = 25.0
    sell_burst_window_s: float = 10.0
    sell_burst_ratio: float = 2.0
    sell_burst_min_sellers: int = 3
    sell_burst_min_usd: float = 50.0
    exodus_active_s: float = 30.0
    exodus_quiet_s: float = 15.0
    exodus_min_buyers: int = 5
    volume_window_s: float = 10.0
    volume_baseline_s: float = 120.0
    volume_up_ratio: float = 4.0
    volume_down_ratio: float = 4.0
    volume_min_usd: float = 100.0
    holder_dump_pct: float = 50.0
    holder_dump_min_usd: float = 25.0


def params_from_env(environ):
    """Params from MEMECOIN_DESK_ATTN_<FIELD> (upper case). A bad value raises ValueError: a typo never falls back."""
    out = {}
    for f in fields(Params):
        raw = environ.get("MEMECOIN_DESK_ATTN_" + f.name.upper())
        if raw is None or str(raw).strip() == "":
            continue
        value = (int if f.type in ("int", int) else float)(str(raw).strip())
        if not math.isfinite(value) or value <= 0:
            raise ValueError("MEMECOIN_DESK_ATTN_%s must be a positive finite number" % f.name.upper())
        out[f.name] = value
    return Params(**out)


def _event(signal, value, swap, key):
    return {"signal": signal, "value": value, "observed_at": swap["received_at"], "key": key,
            "evidence": {"signature": swap["signature"], "slot": swap["slot"], "ordinal": swap["ordinal"]}}


def _swap_key(swap):
    return "%s:%s" % (swap["signature"], swap["ordinal"])


def detect(swaps, *, creator, ours, opened_at, cut, params, holders=None, ours_sigs=frozenset(), agents=AGENTS):
    """Events firing at `cut` for ONE position. `swaps` = the mint's swaps (dicts: received_at, slot, signature, ordinal,
    is_buy, usd, actor, token_atomic). Only received_at < cut counts; our wallets are dropped first. creator_sell, sell_burst and
    holder_dump look only at swaps after `opened_at`; `ours` (our wallet actors) and `ours_sigs` (our fill transaction signatures) are dropped first; volume_spike and buyer_exodus also use the history before entry."""
    p = params
    rows = sorted((s for s in swaps if s["received_at"] < cut and s["actor"] not in ours and s["signature"] not in ours_sigs),
                  key=lambda s: (s["received_at"], s["slot"], s["ordinal"]))
    flow = [s for s in rows if s["actor"] not in agents]            # demand/supply signals never count the fee-free agent
    post = [s for s in rows if s["received_at"] > opened_at]
    post_flow = [s for s in post if s["actor"] not in agents]
    out = []

    for s in post:                                                        # creator_sell: one event per qualifying sell
        if not s["is_buy"] and s["actor"] == creator and s["usd"] >= p.creator_sell_min_usd:
            out.append(_event("creator_sell", {"usd": s["usd"]}, s, _swap_key(s)))

    win = [s for s in post_flow if cut - p.sell_burst_window_s <= s["received_at"] < cut]
    sells = [s for s in win if not s["is_buy"] and s["actor"] != creator]
    buy_usd = sum(s["usd"] for s in win if s["is_buy"])
    sell_usd = sum(s["usd"] for s in sells)
    sellers = {s["actor"] for s in sells}
    if (len(sellers) >= p.sell_burst_min_sellers and sell_usd >= p.sell_burst_min_usd
            and (buy_usd == 0 or sell_usd / buy_usd >= p.sell_burst_ratio)):
        out.append(_event("sell_burst", {"sellers": len(sellers), "sell_usd": sell_usd, "buy_usd": buy_usd,
                                         "ratio": None if buy_usd == 0 else sell_usd / buy_usd,
                                         "window_s": p.sell_burst_window_s}, sells[-1], _swap_key(sells[-1])))

    buys = [s for s in flow if s["is_buy"]]
    if buys:
        last = buys[-1]
        quiet = cut - last["received_at"]
        buyers = {s["actor"] for s in buys if last["received_at"] - p.exodus_active_s <= s["received_at"] <= last["received_at"]}
        if quiet >= p.exodus_quiet_s and len(buyers) >= p.exodus_min_buyers:
            out.append(_event("buyer_exodus", {"buyers": len(buyers), "quiet_s": quiet}, last, _swap_key(last)))

    w, b = p.volume_window_s, p.volume_baseline_s
    vol = sum(s["usd"] for s in flow if cut - w <= s["received_at"] < cut)
    base = sum(s["usd"] for s in flow if cut - w - b <= s["received_at"] < cut - w) / (b / w)
    direction = None
    if vol >= p.volume_min_usd and (base == 0 or vol >= p.volume_up_ratio * base):
        direction = "up"
    elif base >= p.volume_min_usd and vol <= base / p.volume_down_ratio:
        direction = "down"
    if direction is not None and flow:
        out.append(_event("volume_spike", {"direction": direction, "window_usd": vol, "baseline_usd": base,
                                           "ratio": None if base == 0 else vol / base},
                          flow[-1], "%s:%d" % (direction, int(cut // w))))

    if holders:
        for actor, balance in holders.items():
            if actor in ours or not balance or balance <= 0:
                continue
            mine = [s for s in post if not s["is_buy"] and s["actor"] == actor]
            sold = sum(s["token_atomic"] for s in mine)
            pct = sold / balance * 100
            if mine and pct >= p.holder_dump_pct and sum(s["usd"] for s in mine) >= p.holder_dump_min_usd:
                out.append(_event("holder_dump", {"actor": actor, "sold_pct": round(pct, 4),
                                                  "usd": sum(s["usd"] for s in mine)}, mine[-1],
                                  "%s:%s" % (actor, _swap_key(mine[-1]))))
    return out


def event_id(position_id, event, version=""):
    """Stable across restarts and re-scans: the same trigger on the same position under the same thresholds version is
    the same id. The version is part of it, so a dry (unset) measurement never blocks the fire once thresholds are frozen."""
    body = "%s|%s|%s|%s|%s" % (position_id, event["signal"], event["key"], SOURCE, version)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


_NUM = "num"
_VALUE = {"creator_sell": {"usd": _NUM},
          "sell_burst": {"sellers": "int", "sell_usd": _NUM, "buy_usd": _NUM, "ratio": "num?", "window_s": _NUM},
          "buyer_exodus": {"buyers": "int", "quiet_s": _NUM},
          "volume_spike": {"direction": "dir", "window_usd": _NUM, "baseline_usd": _NUM, "ratio": "num?"},
          "holder_dump": {"actor": "slug", "sold_pct": _NUM, "usd": _NUM}}


def _ok(kind, v):
    if kind == "int":
        return type(v) is int and 0 <= v < 2**53
    if kind == "num?" and v is None:
        return True
    if kind in (_NUM, "num?"):
        return type(v) in (int, float) and math.isfinite(v) and 0 <= v < 1e15
    if kind == "dir":
        return v in ("up", "down")
    if kind == "slug":
        return isinstance(v, str) and bool(_SLUG.match(v))
    return False


def validate_event(event):
    """Closed key set and value types per signal; ValueError otherwise. Called before any write."""
    try:
        kinds = _VALUE[event["signal"]]
        value, evidence = event["value"], event["evidence"]
        if (set(value) != set(kinds) or any(not _ok(kinds[k], value[k]) for k in kinds)
                or set(evidence) != {"signature", "slot", "ordinal"}
                or not (isinstance(evidence["signature"], str) and _SLUG.match(evidence["signature"]))
                or not _ok("int", evidence["slot"]) or not _ok("int", evidence["ordinal"])
                or not _ok(_NUM, event["observed_at"])):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("attention_event") from None
    return event
