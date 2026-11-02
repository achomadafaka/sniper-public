"""Flask blueprint for the memecoin copy-mirror dashboard tab (paper)."""
from __future__ import annotations

import collections
import logging
import json
import html
import math
import os
import statistics
import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, Response, jsonify, redirect, request, stream_with_context

logger = logging.getLogger(__name__)

def _handle_of(conn, uid):
    """The wallet handle for a notification. Falls back to the uid so an alert is never blank."""
    try:
        row = conn.execute(
            "SELECT handle FROM memecoin_wallets WHERE source_user_id=?", (uid,)).fetchone()
        return (row[0] if row and row[0] else uid)
    except Exception:
        return uid


def _notify_operator(action, detail):
    """Report an operator mutation to Telegram. Best-effort: a notification must never fail the
    action it describes, and every alert path here already swallows its own errors."""
    try:
        from sniper.services.memecoin_alerts import alert_operator_action
        alert_operator_action(action, detail)
    except Exception:
        logger.exception("operator notification failed: %s %s", action, detail)


import time as _time

# Short TTL cache for the /real/positions DexScreener batch. The endpoint is polled every 4s and
# DexScreener isn't cached upstream, so every poll paid a full external round-trip (~0.7s) — the "UI
# is hyper slow" complaint. A 6s cache keyed by the open-position set makes repeat polls instant while
# keeping the display within one poll of live. Display-only; the funded runner uses exact quotes.
_DEX_BATCH_CACHE = {"key": None, "data": {}, "ts": 0.0}

# Throttle the web-side chart-mark write (write_marks scans the whole marks table — ~0.6s). The chart
# is fine with a ~25s mark cadence; this keeps the 4s poll fast.
_WEB_MARK_WRITE = {"ts": 0.0}
_WEB_MARK_WRITE_INTERVAL_S = 25.0


def _cached_dex_batch(keys, ttl_s=10.0):
    """10s (2026-08-24, was 1): the 1s TTL let one open dashboard drive ~60 batched calls a minute
    at this host's single residential IP, around the clock. DexScreener answered that on 2026-08-24
    by tarpitting our data routes — the edge completes the TLS handshake in 17 ms, then holds the
    connection open and returns nothing at all (75 s, zero bytes, no status), while a static 404 on
    the SAME host still comes back in 65 ms. That is a rate-limit response aimed at us, not an
    outage, and it escalated from an explicit 403 earlier the same day.

    10s is safe because this feeds DISPLAY only: mcap, liquidity, volume and the chart price.
    Every execution path — entry fills, exit triggers, the funded sweep — reads exact route quotes
    and never this cache. The earlier 1s was chosen to stop the ticker lagging; a display number
    10 s old is not lag worth being rate-limited for."""
    k = tuple(sorted((str(n or ""), str(mm)) for n, mm in keys))
    now = _time.monotonic()
    if _DEX_BATCH_CACHE["key"] == k and (now - _DEX_BATCH_CACHE["ts"]) < ttl_s:
        return _DEX_BATCH_CACHE["data"]
    data = dexscreener_stats_batch_keyed(keys) or {}
    _DEX_BATCH_CACHE.update(key=k, data=data, ts=now)
    return data


from sniper.persistence import db_memecoin as m
from sniper.routes.memecoin_csrf import csrf_ok, csrf_token, guard_mutations
# module-level (patchable) price fns — the /real/positions endpoint uses these so tests can inject
# and so live GeckoTerminal can price the DexScreener-blind 4663 chain per request (2026-07-18)
from sniper.execution.solana_quote import dexscreener_stats_batch_keyed, quote_for, modeled_fill


# TOKEN NAMES FOR ROWS WITH NO TICKER (2026-09-28, operator): sniper/services/memecoin_coin_names.py.
# The polls never wait: a coin neither the cache nor the ledger names is looked up in the background.
def _coin_names(conn, keys, *, now=None):
    from sniper.services import memecoin_coin_names as coin_names
    return coin_names.names(conn, keys, now=now)


# A closed position's supply never changes: read it once per process (one indexed mark read each).
_CLOSED_SUPPLY = {}


def _closed_supply(conn, p):
    key = (p["id"], p.get("mint"))
    if key not in _CLOSED_SUPPLY:
        _CLOSED_SUPPLY[key] = m.position_display_supply(conn, p)
    return _CLOSED_SUPPLY[key]


def _unnamed_keys(rows):
    return [(p.get("network_id") or "solana", p["mint"]) for p in rows
            if p.get("mint") and not str(p.get("ticker") or "").strip()]
from sniper.execution.geckoterminal_quote import gt_stats_batch_keyed, gt_pool_stats_for
from sniper.services import memecoin_radar_view as _radar
from sniper.services import memecoin_missed_entry_grader as _missed_entries
from sniper.services import memecoin_latency_report as _entry_latency
from sniper.services import memecoin_shadow_signals as _shadow_signals
from sniper.services import memecoin_wallet_recommender as _wallet_recommender
from sniper.services import memecoin_position_intel as _pos_intel
from sniper.services import memecoin_exit_tournament as _exit_tournament
from sniper.services.memecoin_marks import stored_position_marks
from sniper.services.memecoin_runner import read_heartbeat, HEARTBEAT_PATH
from sniper.feeds.fomo_feed import FomoAuth, fomo_get as _fomo_get


def _live_quote_map(keys):
    """`[(network_id, mint)] -> {accounting_mark_key: {liquidity, market_cap, volume_24h, price}}`.

    The radar quote daemon that filled `memecoin_radar_quote` has been stopped since 2026-08-21, so
    `radar_quote_map` returns {} and any surface that trusted it reads every coin as unpriced. This
    prices a BOUNDED list of coins from the same live batch `enrich_live_quotes` uses -- DexScreener
    first, GeckoTerminal for the DexScreener-blind 4663 chain. Callers pass only the rows they are
    about to display or score, never the whole universe. Fail-open: a dead provider returns {} and
    the caller keeps reading those coins as unknown.
    """
    keys = list({(str(n or "solana"), mm) for n, mm in keys})
    if not keys:
        return {}
    live = {}
    try:
        live = _cached_dex_batch(keys, ttl_s=15) or {}
    except Exception:
        logger.exception("live quote map: DexScreener batch failed")
    missing = [k for k in keys if not quote_for(live, k[1], k[0])]
    gt = {}
    if missing:
        try:
            gt = gt_stats_batch_keyed(missing, ttl_s=15) or {}
        except Exception:
            logger.exception("live quote map: GeckoTerminal batch failed")
    out = {}
    for net, mint in keys:
        q = quote_for(live, mint, net) or gt.get((net, mint)) or {}
        if not q:
            continue
        out[m.accounting_mark_key(net, mint)] = {
            "price": q.get("price"),
            "liquidity": q.get("liquidity"),
            "market_cap": q.get("market_cap") or q.get("mcap"),
            "volume_24h": q.get("volume_24h"),
        }
    return out

# A heartbeat older than this ⇒ runner considered down. Poll is ~1.5s, so 120s
# tolerates transient stalls without false-greening a dead loop.
RUNNER_STALE_S = 120.0


def _source_health(hb):
    """Cached relay health is separate from a successful runner queue drain."""
    from sniper.settings import memecoin as cfg
    source = cfg.MEMECOIN_SIGNAL_SOURCE
    result = {"source": source, "status": "unknown", "last_event_at": None, "accounts": []}
    if source != "j7":
        return result
    hb = hb if isinstance(hb, dict) else {}
    feed = hb.get("j7_feed")
    feed = feed if isinstance(feed, dict) else {}
    stamp = feed.get("last_event_at")
    if isinstance(stamp, str):
        try:
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")).utcoffset() is not None:
                result["last_event_at"] = stamp
        except ValueError:
            pass
    accounts = feed.get("accounts", [feed] if feed.get("status") else [])
    states = {"connected", "connect_failed", "connect_error", "auth_error", "reconnecting",
              "disconnected", "error", "starting", "connecting"}
    for row in accounts if isinstance(accounts, list) else []:
        row = row if isinstance(row, dict) else {}
        error = row.get("last_error")
        # Older heartbeat errors may contain credentials; publish only safe cause labels.
        error = (("Invalid token" if "invalid token" in error.lower() else
                  "Token expired" if "expired" in error.lower() else "Connection failed")
                 if isinstance(error, str) and error else None)
        result["accounts"].append({
            "status": row.get("status") if isinstance(row.get("status"), str)
                      and row["status"] in states else "unknown",
            "last_error": error,
            **{key: row.get(key) if type(row.get(key)) is int and row[key] >= 0 else None
               for key in ("tracked_count", "pump_tracked_count")}})
    stamp = hb.get("ts")
    age = _time.time() - stamp if type(stamp) in (int, float) else None
    if age is None or not math.isfinite(age) or age < 0:
        return result
    if age >= RUNNER_STALE_S:
        result["status"] = "stale"
    elif result["accounts"]:
        connected = sum(account["status"] == "connected" for account in result["accounts"])
        if connected:
            result["status"] = "connected" if connected == len(result["accounts"]) else "degraded"
        elif all(account["status"] not in {"unknown", "starting", "connecting"}
                 for account in result["accounts"]):
            result["status"] = "down"
    return result


def _ui_build():
    """The dashboard build the server would serve now: static/app.js mtime, the same value the
    HTML shell stamps into its ?v= query and into window.__UI_BUILD."""
    import os as _os
    try:
        here = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        return str(int(_os.path.getmtime(_os.path.join(here, "static", "app.js"))))
    except OSError:
        return ""


# wallet-address cache (path, mtime) -> address; /real/status polls ~4s and the address is immutable.
_ADDR_CACHE = {}


# How stale a `rails_blind` audit row may be before the card stops reporting it. The sweep
# upserts one row per position/reason/minute, so anything older than a few minutes means the
# sweep is no longer finding the position blind (or is not running).
_RAILS_BLIND_MAX_AGE_S = 240.0

# How fresh a persisted exact route quote must be to price the card. Beyond this the card falls
# back to the modeled haircut and says so.
_MEASURED_MARK_MAX_AGE_S = 300.0


def _mark_age_seconds(now_iso, observed_at):
    """Seconds from observed_at to now_iso, or None when either is missing or unparseable. A naive
    stamp is the ledger's UTC (DR-03, 2026-09-27): aware-minus-naive used to raise TypeError and read
    as age unknown, so a naive exact_route mark was never fresh."""
    now, observed = _utc_instant(now_iso), _utc_instant(observed_at)
    if now is None or observed is None:
        return None
    return (now - observed).total_seconds()


def _positive(value):
    """True for a finite number above zero. Null, zero and text are unknown, never a size."""
    try:
        return float(value) > 0 and float(value) != float("inf")
    except (TypeError, ValueError):
        return False


def _derived_market_cap(events, price):
    """Market cap at `price` from the supply another tracked trade on the coin implies, or None.

    The J7 relay sends market cap 0.0 on some trades (xp 2026-09-17: "bought at $0"). Zero is
    unknown, never a market cap. Supply is `market_cap / price` from a trade that carried both, so
    the value is the coin's own supply priced at the refused trade's price.
    """
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if not price > 0:
        return None
    for event in events or ():
        try:
            their_price, their_mcap = float(event["their_price"]), float(event["their_market_cap"])
        except (KeyError, TypeError, ValueError):
            continue
        if their_price > 0 and their_mcap > 0:
            return round(their_mcap / their_price * price)
    return None


def _select_display_mark(*, mid, mid_observed_at, liquidity, qty, now_iso,
                         exact_price=None, exact_observed_at=None):
    exact_age_s = _mark_age_seconds(now_iso, exact_observed_at)
    if (exact_price and float(exact_price) > 0 and exact_age_s is not None
            and 0 <= exact_age_s <= _MEASURED_MARK_MAX_AGE_S):
        return float(exact_price), "measured_route", exact_observed_at, round(exact_age_s, 1)

    mid_age_s = _mark_age_seconds(now_iso, mid_observed_at)
    mark_age_s = round(max(0.0, mid_age_s), 1) if mid_age_s is not None else None
    if mid and liquidity and liquidity > 0 and qty > 0:
        fallback, fallback_basis = (modeled_fill(mid, qty * mid, liquidity, "sell"),
                                    "modeled_slippage")
    else:
        fallback, fallback_basis = mid, "mid"

    # STALE-EXACT FLOOR (2026-08-24). Once the exact quote ages past the window above, this used to
    # DISCARD it and show a fresh model or raw mid instead — so the card read more optimistic than
    # the rails on the same position, because the funded sweep keeps its last resolved exact and
    # takes the WORSE of it and nominal. On a thin pool the mid is roughly double what the bag
    # fetches, which is the direction that talks an operator out of a sell that was already right.
    #
    # Scope is deliberately the RAW MID only. `modeled_slippage` already applies a worst-case
    # haircut, and the 300s window above is a real policy — an exact quote older than that is not
    # trusted, and this must not quietly overrule it. The raw mid is the one case with no haircut
    # and no defensible claim to be a sell price, so a real quote we actually got beats it.
    # Monotonically conservative: it can only make the displayed position look worse, never
    # better, so it cannot manufacture a reason to hold.
    if exact_price and float(exact_price) > 0 and fallback_basis == "mid":
        stale_exact = float(exact_price)
        if fallback is None or stale_exact <= float(fallback):
            age = round(exact_age_s, 1) if exact_age_s is not None else None
            return stale_exact, "measured_route_stale", exact_observed_at, age
    return fallback, fallback_basis, mid_observed_at, mark_age_s


def _frozen_arm_name(frozen_policy):
    """Arm name from the frozen contract policy id, e.g. `copy-arm-combo_f-v1` -> `combo_f`.

    `_position_contract_view` yields a plain dict, not the dataclass -- reading it with getattr
    silently produced None on every row, which is exactly the "looks fine, shows nothing" failure
    this field exists to remove.
    """
    policy_id = (frozen_policy or {}).get("policy_id") if isinstance(frozen_policy, dict) \
        else getattr(frozen_policy, "policy_id", None)
    if not policy_id:
        return None
    name = str(policy_id)
    if name.startswith("copy-arm-"):
        name = name[len("copy-arm-"):]
    return name.rsplit("-v", 1)[0] or None


def _rails_blind_view(conn, position_id):
    """Latest `rails_blind` audit row for an open position, or None.

    Read-only and derived: the audit row is already the source of truth, so this needs no column
    and no migration. `None` means the rails are reading the exact executable price, which is the
    normal case; a dict means the loss rails are running on the nominal mid and cannot see what
    the bag would actually fetch.
    """
    try:
        row = conn.execute(
            "SELECT display_price, preview_usdc, detail, checked_at "
            "FROM memecoin_exit_trigger_audit WHERE position_id=? AND reason='rails_blind' "
            "ORDER BY checked_at DESC LIMIT 1",
            (position_id,),
        ).fetchone()
    except Exception:
        logger.exception("rails-blind lookup failed pos=%s", position_id)
        return None
    if row is None:
        return None
    try:
        age_s = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(str(row["checked_at"]).replace("Z", "+00:00"))
                 ).total_seconds()
    except (TypeError, ValueError):
        return None
    if not (0 <= age_s <= _RAILS_BLIND_MAX_AGE_S):
        return None
    try:
        detail = json.loads(row["detail"] or "{}")
    except (TypeError, ValueError):
        detail = {}
    return {
        "rail_basis": "nominal_mid",
        "rail_price": row["display_price"],
        "checked_at": row["checked_at"],
        "age_s": round(age_s, 1),
        "code": detail.get("code"),
    }


def _now_iso():
    from sniper.util.clock import real_utc_now_iso
    return real_utc_now_iso()   # corrected UTC (host clock runs +7h)


# How far back the ticker may carry a mark field the freshest row does not carry. The mark tape
# runs about 1 s, so 300 s is many hundreds of samples: past it, the value is not "current".
_MARK_CARRY_S = float(os.getenv("MEMECOIN_TICKER_MARK_CARRY_S", "300"))


def _measured_mark_floor(now_iso):
    """Oldest `sweep_ts` an exact-route mark may have and still be usable for display."""
    from datetime import datetime, timedelta
    try:
        moved = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                 - timedelta(seconds=_MEASURED_MARK_MAX_AGE_S))
    except (TypeError, ValueError):
        return None
    return moved.isoformat().replace("+00:00", "Z")


def _mark_carry_floor(now_iso):
    """Oldest `sweep_ts` the ticker will accept for a carried-forward mcap or liquidity."""
    from datetime import datetime, timedelta
    try:
        moved = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                 - timedelta(seconds=_MARK_CARRY_S))
    except (TypeError, ValueError):
        return None
    return moved.isoformat().replace("+00:00", "Z")


def _setup_exclusion_keys(conn, now_iso, *, cooldown_h=24.0):
    """Coins to HIDE from armed-consensus entry setups so the operator never manual-buys a coin we're
    already committed to: (1) open real positions, (2) in-flight reserved entries (automated buy mid-
    flight, position not yet is_open), (3) coins closed within the re-entry cooldown (one-position-per-
    coin — the re-entry edge is refuted). Raw (net,mint) tuples; consensus_setups normalizes them."""
    keys = set()
    try:
        for p in conn.execute("SELECT network_id, mint FROM memecoin_positions "
                              "WHERE execution_mode='real' AND is_open=1"):
            keys.add((p["network_id"], p["mint"]))
    except Exception:
        pass
    try:
        for p in conn.execute("SELECT network_id, mint FROM memecoin_pending_entries "
                              "WHERE status='reserved'"):
            keys.add((p["network_id"], p["mint"]))
    except Exception:
        pass
    try:
        cutoff = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                  - timedelta(hours=float(cooldown_h))).isoformat()
        for p in conn.execute("SELECT network_id, mint FROM memecoin_positions "
                              "WHERE execution_mode='real' AND is_open=0 AND closed_at >= ?", (cutoff,)):
            keys.add((p["network_id"], p["mint"]))
    except Exception:
        pass
    return keys


def _bounded_query_number(args, name, *, cast, default=None, minimum=None, maximum=None):
    raw = args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _position_contract_view(position, override):
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_chains import canonical_chain_id
    view = _stored_position_contract_view(position, override)
    size_mirror = canonical_chain_id(position.get("network_id") or "solana") in cfg.MEMECOIN_REAL_MIRROR_SIZE_CHAINS
    policy = view.get("effective_exit_policy") or {}
    hold = bool(position.get("mirror_disabled"))
    view["effective_mirror"] = {
        "source": "size_aware" if size_mirror else "funded_contract" if policy else "legacy",
        "enabled": not hold and (size_mirror or policy.get("mirror_on_sell") is not False),
        "operator_hold": hold,
    }
    return view


def _stored_position_contract_view(position, override):
    """Render stored contract truth without rebuilding it from current settings."""
    fields = (
        position.get("funded_contract_version"),
        position.get("funded_contract_hash"),
        position.get("funded_contract_json"),
    )
    if all(value is None for value in fields):
        return {
            "entry_contract": None,
            "effective_exit_policy": None,
            "policy_source": "legacy",
            "operator_override_active": bool(
                override and override.get("source") == "operator"),
            "frozen_policy": None,
        }
    version, digest, payload_json = fields
    base = {"version": version, "hash": digest}
    try:
        contract = json.loads(payload_json)
        if not isinstance(contract, dict):
            raise ValueError("contract root is not an object")
        from sniper.services.memecoin_funded_policy import effective_funded_policy
        _original, effective = effective_funded_policy(position, override=override)
        view = {
            **base,
            "classification": contract.get("classification"),
            "sizing": contract.get("sizing"),
            "policy": contract.get("policy"),
            "expectation": contract.get("expectation"),
            "invalidations": contract.get("invalidations"),
            "source": contract.get("source"),
        }
        return {
            "entry_contract": view,
            "effective_exit_policy": effective,
            "policy_source": "funded_contract",
            "operator_override_active": bool(
                override and override.get("source") == "operator"),
            "frozen_policy": contract.get("policy"),
        }
    except Exception as exc:
        return {
            "entry_contract": {**base, "parse_error": str(exc) or type(exc).__name__},
            "effective_exit_policy": None,
            "policy_source": "funded_contract",
            "operator_override_active": bool(
                override and override.get("source") == "operator"),
            "frozen_policy": None,
        }


_MID_FETCH_CACHE = (None, {})

# LIVE DISPLAY MARKS (S27-05, 2026-09-27): the runner's tmpfs snapshot of held Solana coins' swap and
# curve marks (sniper/services/memecoin_live_marks.py). Display only. Re-parsed only when the file
# changes; a missing, torn, stale or unhealthy snapshot gives {} and the ticker shows the route
# price or unknown (never the tape: DR-04, see _real_ticker_snapshot).
from sniper.services import memecoin_live_marks as _live_marks_mod  # noqa: E402

LIVE_MARKS_PATH = _live_marks_mod.default_path()
# ponytail: the text-ordered candidates the ticker's newest-mark read parses (DR-03a). 16 covers a
# second of every writer in mixed spellings; a non-UTC offset hours away is not covered (DR-03b, no
# such row in production). Canonical UTC writes are the upgrade.
_LATEST_CANDIDATES = 16
_NO_INSTANT = datetime.min.replace(tzinfo=timezone.utc)
# The provider behind a mark-tape row's liquidity, by the row's source.
_LIQUIDITY_SOURCES = {"fastlane_mid": "dexscreener", "dexscreener": "dexscreener",
                      "paper_dexscreener": "dexscreener"}


def _own_sources_sql():
    """SQL test for OUR OWN mark sources (memecoin_candles.OWN_MARK_SOURCES), for a funded read
    (operator via Alpha, 2026-10-08): a provider or paper-sweep row never reaches a funded card."""
    from sniper.services.memecoin_candles import OWN_MARK_SOURCES
    return "COALESCE(source,'') IN (%s)" % ",".join("'%s'" % s for s in OWN_MARK_SOURCES)
_LIVE_MARKS_CACHE = (None, {})


def _live_marks(now_real):
    global _LIVE_MARKS_CACHE
    try:
        st = os.stat(LIVE_MARKS_PATH)
    except OSError:
        return {}
    key = (str(LIVE_MARKS_PATH), st.st_mtime_ns, st.st_size)
    if _LIVE_MARKS_CACHE[0] != key:
        _LIVE_MARKS_CACHE = (key, _live_marks_mod.read_snapshot(LIVE_MARKS_PATH))  # one atomic rebind
    return _live_marks_mod.fresh_marks(_LIVE_MARKS_CACHE[1], now_real)


def _utc_instant(iso):
    """An aware UTC datetime (a naive stamp is the ledger's UTC), or None."""
    dt = _live_marks_mod._parse(iso)
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso_shift(iso, seconds):
    """`iso` moved by `seconds`, spelled with a 'T' and Z; the input unchanged when unparseable."""
    dt = _utc_instant(iso)
    if dt is None:
        return iso
    return (dt + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _mid_fetch_observations():
    """The runner's fastlane.mid_observations ({'chain:mint': {changed_at, fetched_at}}) from its
    heartbeat, re-read only when the file changes: the SSE loop builds this payload four times a
    second per open tab. A missing or unreadable heartbeat is {} (checked time unknown, never now)."""
    global _MID_FETCH_CACHE
    try:
        st = os.stat(HEARTBEAT_PATH)
    except OSError:
        return {}
    key = (str(HEARTBEAT_PATH), st.st_mtime_ns, st.st_size)
    if _MID_FETCH_CACHE[0] != key:
        hb = read_heartbeat(HEARTBEAT_PATH)
        obs = (hb.get("fastlane") or {}).get("mid_observations") if isinstance(hb, dict) else None
        _MID_FETCH_CACHE = (key, obs if isinstance(obs, dict) else {})   # one atomic rebind
    return _MID_FETCH_CACHE[1]


def _exec_extremes_fields(conn, p, entry):
    """exec_high_pct / exec_low_pct / exec_high_at / exec_low_at / exec_samples for one funded
    position, from the live route-series aggregate. Every value None when no quote is recorded or
    the read fails; a failed read is logged and never breaks the payload."""
    live = None
    try:
        live = m.exec_extremes_live(conn, p["id"])
    except Exception:
        logger.exception("exec extremes read failed pos=%s", p.get("id"))
    pct = m.exec_extremes_pct(live, entry)
    return {"exec_high_pct": pct["high_pct"], "exec_low_pct": pct["low_pct"],
            "exec_high_at": (live or {}).get("high_at"), "exec_low_at": (live or {}).get("low_at"),
            "exec_samples": (live or {}).get("samples")}


def _last_known_price(c, p, chain_id, *, priced):
    """A row with no current price says what it last knew, and when (Alpha (d), 2026-10-02: DAODUN
    68544 rendered blank). The newest mark of any age, else the entry route quote, else the fill.
    `no_exit_route`: the runner flagged the bag stuck (its sell ladder found no route)."""
    out = {"last_known_price": None, "last_known_at": None, "last_known_source": None,
           "no_exit_route": bool(p.get("stuck_bag"))}
    if priced:
        return out
    row = c.execute(
        "SELECT mid, sweep_ts, source FROM memecoin_position_marks WHERE chain_id=? AND mint=? "
        "AND mid>0 AND COALESCE(quality,'')!='quarantined_spike' AND " + _own_sources_sql() +
        " ORDER BY sweep_ts DESC LIMIT 1", (chain_id, p["mint"])).fetchone()
    if row:
        found = (row["mid"], row["sweep_ts"], row["source"] or "mark")
    elif (p.get("quote_price") or 0) > 0:
        found = (p["quote_price"], p.get("quote_observed_at"), "entry quote")
    elif (p.get("entry_price") or 0) > 0:
        found = (p["entry_price"], p.get("opened_at"), "entry fill")
    else:
        return out
    out.update(last_known_price=found[0], last_known_at=found[1], last_known_source=found[2])
    return out


def _real_ticker_payload(c, now_real, *, positions=None):
    live = _live_marks(now_real)              # a file read: outside the DB read snapshot
    with m.operator_read_snapshot(c):
        return _real_ticker_snapshot(c, now_real, positions=positions, live_marks=live)


def _unconfirmed_peak(p, entry):
    """{peak_unconfirmed, peak_unconfirmed_pct}: a lone display tick above the confirmed peak
    (2026-10-17, ANIMA 73930), else False / None. Display only."""
    high, peak = p.get("peak_unconfirmed_price"), p.get("peak_price")
    shown = bool(high and (peak is None or high > peak))
    return {"peak_unconfirmed": shown,
            "peak_unconfirmed_pct": (((high / entry) - 1.0) * 100.0) if (shown and entry) else None}


def _real_ticker_snapshot(c, now_real, *, positions=None, live_marks=None):
    """The /real/ticker payload, buildable on ANY connection — the request path uses the
    blueprint's shared connection, the SSE stream its own long-lived read-only one."""
    from sniper.services.memecoin_links import operator_links
    positions = m.list_open_positions(c, mode="real") if positions is None else positions
    from sniper.services.memecoin_bsc_v2_live import read_marks as _bsc_live_marks
    from sniper.services.memecoin_evm_tape_marks import tape_marks as _evm_tape_marks
    _now_ts = datetime.fromisoformat(str(now_real).replace("Z", "+00:00")).timestamp()
    _bsc_marks = _bsc_live_marks(positions, _now_ts)
    # 4663 LIVE DISPLAY MARK (operator 2026-09-28): our own swap tape, anchored on our entry fill,
    # replaces the ~15 s DexScreener mark as the market price. Display only; absent keeps the old.
    _tape_marks = _evm_tape_marks(c, positions, _now_ts)
    out = []
    for p in positions:
        if p.get("tracking_paused_at"):
            continue                      # operator paused it: hidden here, still open and ours
        chain_id = p.get("network_id") or "solana"
        # Resolve each field from the freshest mark that actually CARRIES it, not from
        # whichever row is newest. The tape interleaves sources: `fastlane_rpc` writes an
        # exact_route mid every ~1 s with mcap and liquidity both NULL, while the `web`
        # source writes all three a few seconds apart. Taking one LIMIT 1 row therefore
        # threw away a measured mcap that was seconds old and fell through to the
        # entry-anchored estimate below. Live example 2026-08-12T16:53Z, position 36386
        # (BACKED): the `web` mark measured 322,010 and the ticker showed 609,720 from
        # `entry_mcap` 638,160 -- a 1.9x error that persists for the life of the position.
        # `liquidity` had the identical shape and it is worse, because it feeds
        # `modeled_fill` and so moves the PRICE the operator reads.
        #
        # A carried-forward value is bounded by `_MARK_CARRY_S`: the tape is dense, so a
        # value older than that is not "the current mcap", and the old estimate is honest
        # about being an estimate.
        def _latest(column, since=None, quality=None, source=None):
            sql = (f"SELECT {column} AS v, mid, sweep_ts, quality, source FROM memecoin_position_marks "
                   "WHERE chain_id=? AND mint=? AND mid>0 "
                   "AND COALESCE(quality,'') NOT IN ('quarantined_spike') "
                   "AND COALESCE(source,'')!='fastlane_sol_state'")
            args = [chain_id, p["mint"]]
            if quality == "exact_route":
                sql += " AND quality='exact_route'"
            elif quality == "non_exact":
                sql += " AND COALESCE(quality,'')!='exact_route'"
            if source is not None:
                sql += " AND source=?"
                args.append(source)
            # INSTANTS, NOT TEXT (S27-05 part 2, 2026-09-27). ISO text sorts by spelling: a row
            # "...:00Z" read as later than "...:00.400000Z" and was skipped as a future mark. Each
            # bound is compared with julianday(). The lower bound also keeps a text range, widened
            # by one second, so the (chain_id, mint, sweep_ts) index still seeks into the window;
            # every stored spelling ('T' separator, Z or +00:00, with or without a fraction) of an
            # instant inside the window sorts above that widened bound. No space-separated row
            # exists on any chain (checked 2026-09-27).
            if since is not None:
                sql += (f" AND {column} IS NOT NULL AND sweep_ts>=?"
                        " AND julianday(sweep_ts)>=julianday(?)")
                args += [_iso_shift(since, -1.0), since]
            # Source timestamps in the future are not observations yet.
            sql += " AND julianday(sweep_ts)<=julianday(?)"
            args.append(now_real)
            # NEWEST INSTANT, NOT NEWEST SPELLING (DR-03a, docs/audits/2026-09-27-post-review-delta.md).
            # Text order puts "...:00Z" above "...:00.400000Z", 400 ms newer. The index still picks
            # the candidates (ORDER BY sweep_ts, no julianday scan over the history); the newest
            # parsed instant among them wins, the text breaking an exact tie.
            rows = c.execute(sql + " ORDER BY sweep_ts DESC LIMIT ?",
                             args + [_LATEST_CANDIDATES]).fetchall()
            return max(rows, default=None,
                       key=lambda r: (_utc_instant(r["sweep_ts"]) or _NO_INSTANT, r["sweep_ts"]))

        # Bound the exact_route lookup to the SAME 300s window both consumers already enforce
        # (`exact_is_fresh` below and `_select_display_mark`). Unbounded, `quality='exact_route'`
        # is not in the index, so a position whose recent tape carries no exact mark walked every
        # row for that mint backwards -- 46ms on a 137k-mark coin, four times a second per open
        # SSE tab. With the floor the same index seeks straight into the window: 0.002ms.
        exact_mark = _latest("mid", _measured_mark_floor(now_real), quality="exact_route")
        stored_exact_at = p.get("last_executable_at")
        stored_exact = p.get("last_executable_price")
        if stored_exact is not None and (not exact_mark or (
                _mark_age_seconds(now_real, stored_exact_at) is not None
                and _mark_age_seconds(now_real, stored_exact_at)
                < (_mark_age_seconds(now_real, exact_mark["sweep_ts"]) or 0))):
            exact_mark = {"v": stored_exact, "sweep_ts": stored_exact_at}
        # NO PROVIDER MID ON A FUNDED EVM CARD (operator via Alpha, 2026-10-08, Douyin 72229). The
        # newest non-exact tape row was a DexScreener mid, and a paper twin's sweep writes those on
        # the same (chain, mint). The EVM market price is our own mark below (56 V2 reserves, the 4663
        # swap tape) or the exact route price labelled "route"; with neither it is unknown.
        mid_mark = None
        onchain_liq = None
        # The 4663 tape mark is a SPOT price, about 6 points above the whole-balance sell quote on
        # 66384. It is the MARKET PRICE only (market_mid_* and the market caps below): the sell
        # value, PnL and range bar keep reading the exact route and the provider mid, unchanged.
        tape_mark = _tape_marks.get(p["id"]) if chain_id == "4663" else None
        if chain_id == "56" and p["id"] in _bsc_marks:
            live = _bsc_marks[p["id"]]
            mid_mark = {"v": live["price"], "sweep_ts":
                        datetime.fromtimestamp(live["observed_at"], timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                        "source": live["source"], "quality": "pool_reserves"}
        if chain_id == "solana":
            # NO DEXSCREENER ON THE SOLANA SCREEN (operator, 2026-09-27: "I don't want to see
            # nothing from dex ... we need everything to be subsecond"). The market price is the
            # LIVE DISPLAY MARK only (S27-05): the runner's in-memory swap or curve mark, published
            # without waiting for the tape or the exit sweep, valued with Jupiter's SOL/USD or the
            # exotic-quote book. With none, the ticker shows the exact route price, labelled
            # "route", or unknown.
            # NOT THE TAPE (DR-04, docs/audits/2026-09-27-post-review-delta.md): the fastlane values
            # its fastlane_sol_swap/_curve rows with DexScreener's quote-asset USD, so an on-chain
            # source name does not mean an on-chain price. The live marks cover every mark the tape
            # held for display (swap marks up to 5 s, curves); state marks never reach the screen.
            # Display only: exits, the tape and the rail wake are unchanged.
            # KEYED BY (chain, mint), NOT BY POOL: the runner's cache holds one binding per held mint
            # and refuses rows from any other pool (SolanaSwapMarkCache.record); the position row
            # has no authoritative binding (executed_pool is the route we bought through, and a
            # coin can graduate mid-hold); the mark carries its own pool and source.
            # `checked_at` is the publish time, never the change time. A mark dated more than 1 s
            # after now (the same tolerance fresh_marks gives the publish time) is not an
            # observation yet.
            live = (live_marks or {}).get("solana:%s" % p["mint"])
            now_at = _utc_instant(now_real)
            live_at = _utc_instant(live.get("observed_at")) if live else None
            if now_at is not None and live_at is not None and (live_at - now_at).total_seconds() <= 1.0:
                mid_mark = {"v": live["price_usd"], "sweep_ts": live["observed_at"],
                            "source": live["source"], "quality": "live",
                            "checked_at": live.get("checked_at"),
                            "carried": bool(live.get("carried"))}
                if live.get("liquidity_usd") is not None:
                    onchain_liq = (float(live["liquidity_usd"]), live.get("liquidity_at"))
        exact_age_s = _mark_age_seconds(
            now_real, exact_mark["sweep_ts"] if exact_mark else None)
        exact_is_fresh = bool(
            exact_mark and exact_age_s is not None
            and 0 <= exact_age_s <= _MEASURED_MARK_MAX_AGE_S)
        price_mark = exact_mark if exact_is_fresh else mid_mark
        market_mid = float(mid_mark["v"]) if mid_mark and mid_mark["v"] else None
        # On EVM no fallback to the runner's stored nominal quote: on a chain whose exit sweep still
        # reads DexScreener it can be that mid. The selection then takes the exact price, a stale
        # exact price labelled as such, or nothing.
        mid = (market_mid if market_mid is not None or chain_id != "solana"
               else p.get("last_quote_price"))
        mid_observed_at = mid_mark["sweep_ts"] if mid_mark else None
        # Measure the reach-back from the PRICE mark, not from wall-clock now. The question is
        # how far behind the current price a companion field may sit, and an old-but-internally
        # consistent tape must still render.
        carry_since = _mark_carry_floor(price_mark["sweep_ts"] if price_mark else now_real)
        liq_row = _latest("liquidity", carry_since)
        # A Solana market cap never comes from a DexScreener row: its supply is the runner's pinned
        # on-chain total supply (fdv_supply) or the feed-implied supply below.
        mcap_row = _latest("mcap", carry_since) if chain_id != "solana" else None
        # LIQUIDITY AND ITS SOURCE (operator 2026-09-27: "if it can be replaced with something better
        # than replace it"). On Solana the live mark's on-chain depth wins: 2 x the real quote-side
        # deposit of the bound pool (memecoin_live_marks.CONSTANT_PRODUCT_PROGRAMS, or a curve's
        # real SOL). Otherwise the stored provider value, named: the tape row's own source, or
        # "stored" for memecoin_token_liquidity (written by more than one provider). modeled_fill
        # reads this number, but only when the exact route is older than 300 s.
        liq = float(liq_row["v"]) if liq_row else None
        liq_source = (_LIQUIDITY_SOURCES.get(liq_row["source"], liq_row["source"])
                      if liq_row else None)
        liq_at = liq_row["sweep_ts"] if liq_row else None
        if liq is None:
            liq = m.get_token_liquidity(c, chain_id, p["mint"], now_iso=now_real)
            liq_source = "stored" if liq is not None else None
        if onchain_liq is not None:
            liq, liq_at = onchain_liq
            liq_source = "onchain"
        qty = m.open_qty(c, p["id"])
        # `modeled_fill` estimates what the bag would fetch on the way out. A mark whose
        # quality is `exact_route` ALREADY answers that: the fast lane divides the position's
        # own `preview_exit_fn` -- a full-balance route quote -- by the open quantity, so its
        # impact is measured, not modeled. Applying the model on top charged the exit twice.
        # Position 36325 on 2026-08-12 read liquidity $9.49, which caps the model at its 50%
        # ceiling and halved an exact price of 4.31e-05 to 2.16e-05: the operator saw -67% on
        # a position the route said was -34%.
        last, basis, mark_observed_at, mark_age_s = _select_display_mark(
            mid=mid,
            mid_observed_at=mid_observed_at,
            liquidity=liq,
            qty=qty,
            now_iso=now_real,
            exact_price=(exact_mark["v"] if exact_mark else None),
            exact_observed_at=(exact_mark["sweep_ts"] if exact_mark else None),
        )
        entry = p.get("entry_price") or 0.0
        # mcap is price x supply, so a measured mcap belongs to the price of ITS OWN mark.
        # Carrying the number forward would report an old price; carry the SUPPLY it implies
        # and value it at the current mid. `entry_mcap / entry` is the same ratio measured
        # once at entry and never refreshed, so it is the fallback, not the first choice --
        # on position 36386 it implied 1.97e9 supply against the 9.8e8 the live marks imply,
        # which is the whole 1.9x error.
        supply = None
        if chain_id == "solana" and (p.get("fdv_supply") or 0) > 0:
            supply = float(p["fdv_supply"])
        elif mcap_row and mcap_row["v"] and mcap_row["mid"]:
            supply = float(mcap_row["v"]) / float(mcap_row["mid"])
        elif entry > 0 and (p.get("entry_mcap") or 0) > 0:
            supply = p["entry_mcap"] / entry
        # No entry ratio: the supply the feed's priced events imply, the same fallback
        # /real/positions uses (entry_mcap is null on most 4663 and 56 positions).
        if supply is None:
            supply = m.resolve_mint_supply(c, p["mint"], network_id=chain_id)
        # EXACT-ONLY TAPES (Alpha 2026-09-17). On 5042 and 56 every sample, and on 4663 60% of
        # them, the tape held only exact_route marks, so Market price and Market cap read "—"
        # beside a fresh route price. Show that price, and name it: `market_mid_basis` is
        # "route", never a market mid without the label. A stale route price stays unknown.
        shown_mid, shown_mid_at, shown_mid_basis = (
            market_mid, mid_observed_at, "market" if market_mid is not None else None)
        if shown_mid is None and exact_is_fresh and exact_mark["v"]:
            shown_mid, shown_mid_at, shown_mid_basis = (
                float(exact_mark["v"]), exact_mark["sweep_ts"], "route")
        shown_mid_source = (mid_mark["source"] if shown_mid_basis == "market" and mid_mark
                            else None)
        shown_mark = mid_mark
        if tape_mark is not None:
            shown_mid, shown_mid_at, shown_mid_basis = (
                tape_mark["price"], tape_mark["observed_at"], "market")
            shown_mid_source = tape_mark["source"]
            shown_mark = tape_mark
        # Preserve the existing supply used to convert operator targets. Display supply below
        # must never move a funded rail when a background total-supply read completes.
        fdv_supply = p.get("fdv_supply") if (p.get("fdv_supply") or 0) > 0 else None
        target_supply = fdv_supply or supply
        # GMGN'S BASIS ON THE CARD (operator order "a", 2026-10-02): market cap is price x TOTAL
        # supply wherever it is known (fdv_supply, else the runner's display-only on-chain total).
        # Display only: target_supply above stays the supply the runner's targets read.
        cap_supply, cap_basis = m.position_cap_supply(p)
        # Source mcap / OUR fill price is not a supply: the fill includes entry impact/premium.
        # Keep that legacy ratio for frozen target conversions only. Cards and charts require
        # a pinned supply or cap/price from the same market observation; otherwise show price.
        display_supply = cap_supply
        if display_supply is None and mcap_row and mcap_row["v"] and mcap_row["mid"]:
            display_supply = float(mcap_row["v"]) / float(mcap_row["mid"])
        mcap = shown_mid * display_supply if (shown_mid and display_supply) else None
        # THE PRICE THE OPERATOR READS IS THE PRICE THE RUNNER DECIDED ON (operator, 2026-08-28).
        # `last` is derived here from the mark tape and then put through `modeled_fill`, so the UI
        # showed a number the runner never held: live position 44794 was stored at 0.000195614571079
        # and displayed as 0.000188049267086. Two different quantities under one label is the
        # failure this whole codebase keeps re-learning, and here it was on the price the operator
        # watches a funded position with.
        #
        # `memecoin_positions.last_quote_price` is what the funded sweep committed at
        # memecoin_real_runner.py update_position_last_quote — its decision mid. That is primary.
        # The tape-derived, slippage-modelled figure stays, named for what it is, because it
        # answers a different and also real question: what the bag would fetch on the way out.
        # EVERY PRICE CARRIES ITS OWN CLOCK AND BASIS (operator, 2026-08-28). The first pass moved
        # the NUMBER to the runner's decision mid and left the metadata describing the old one, so
        # the runner price rendered under the mark tape's timestamp, age and basis — the same
        # label-versus-quantity fault one level in. Four independent quantities, four labelled
        # pairs, and no field means "the price" without saying which:
        #
        #   runner_decision_*  what the funded sweep committed; its rails compare against this
        #   selected_exit_*    what _select_display_mark chose, WITH the basis it chose it on
        #   last_executable_*  the last resolved exact whole-balance route quote
        #   modeled_exit_price ONLY when the selection was actually modelled
        #
        # `modeled_exit_price` was wrong before: _select_display_mark returns measured_route,
        # measured_route_stale, mid or modeled_slippage, and all four were published as "modeled".
        # A stale exact route at 0.8 was labelled modelled while PnL ran on a fresh 0.999.
        runner_price = p.get("last_quote_price")
        has_runner = bool(runner_price and runner_price > 0)
        runner_observed_at = p.get("last_quote_ts")
        # The sweep can reuse the last exact quote as its conservative decision price while a
        # current route quote fails. Its sweep time is not the observation time of that price.
        if (has_runner and p.get("last_executable_at")
                and p.get("last_executable_price") == runner_price
                and p.get("last_executable_at") != runner_observed_at):
            runner_observed_at = p.get("last_executable_at")
        # Keep old quotes as evidence, but do not report their PnL as a current value.
        value_age_s = _mark_age_seconds(now_real, mark_observed_at)
        value_price = last if (_positive(last) and value_age_s is not None
                               and 0 <= value_age_s <= _MEASURED_MARK_MAX_AGE_S) else None
        out.append({
            "id": p["id"],
            "network_id": chain_id, "mint": p["mint"],
            "links": operator_links(chain_id, p["mint"], handle=p.get("source_handle"),
                                    position_id=p["id"], origin_event_id=p.get("origin_event_id")),
            # Kept as the primary shown price for every existing reader. It is the runner's
            # decision when there is one, and price_source says which it is.
            "last_quote_price": runner_price if has_runner else last,
            "price_source": "runner" if has_runner else "selected_exit",
            "runner_decision_price": runner_price,
            "runner_decision_at": runner_observed_at,
            "market_mid_price": shown_mid,
            "market_mid_at": shown_mid_at,
            "market_mid_basis": shown_mid_basis,
            "market_mid_source": shown_mid_source,
            # CHANGED vs CHECKED (docs/audits/2026-09-26-price-freshness.md): market_mid_at is when
            # the value last changed; this is when the runner last read the provider for it. Only
            # for the DexScreener mid, the one mark whose stamp holds still while it is re-read.
            "market_mid_fetched_at": (
                (_mid_fetch_observations().get(f"{chain_id}:{p['mint']}") or {}).get("fetched_at")
                if shown_mid_basis == "market" and shown_mid_source == "fastlane_mid"
                else shown_mark.get("checked_at")
                if shown_mid_basis == "market" and isinstance(shown_mark, dict) else None),
            # The provider's own observation time. DexScreener publishes none: unknown, not now.
            "market_mid_source_at": None,
            # HELD, NOT LIVE (DR-06, Alpha 2026-09-27): a quiet pool's last trade price,
            # kept while its reserves still match that trade. market_mid_at is still the trade's time.
            "market_mid_carried": bool(shown_mid_basis == "market" and isinstance(shown_mark, dict)
                                       and shown_mark.get("carried")),
            "selected_exit_price": last,
            "selected_exit_basis": basis,
            "selected_exit_at": mark_observed_at,
            "last_executable_price": p.get("last_executable_price"),
            "last_executable_at": p.get("last_executable_at"),
            "modeled_exit_price": last if basis == "modeled_slippage" else None,
            # PnL is computed on the SELECTED EXIT, not on the runner price. Naming it stops the
            # next reader assuming the headline number and the percentage share a source.
            "pnl_price_basis": basis,
            # THE LEGACY PAIR TRAVELS TOGETHER. `last_quote_price` is the runner's decision when
            # there is one, so `last_quote_ts` must be the runner's stamp — not the tape's. Pinning
            # 0.6 against the tape's 00:00:02 was the last place a price still wore another
            # quantity's clock. Readers that only know the legacy pair now get a consistent one.
            "last_quote_ts": (runner_observed_at if has_runner
                              else (mark_observed_at or p.get("last_quote_ts"))),
            "mark_basis": basis,
            "mark_observed_at": mark_observed_at,
            "mark_age_s": mark_age_s,
            "liquidity": liq,
            "liquidity_source": liq_source,
            "liquidity_at": liq_at,
            "market_cap": mcap,
            "market_cap_basis": ("fdv" if cap_supply else "circulating") if mcap else None,
            "display_supply": display_supply,
            "market_cap_fdv": (shown_mid * fdv_supply if (shown_mid and fdv_supply) else None),
            "target_supply": target_supply,
            "mcap_basis": "fdv" if fdv_supply else "circulating",
            # The supply every cap on the card uses, and whether the runner's targets read it
            # ('fdv') or not yet ('total': re-save a target to switch it). None: today's basis.
            "cap_supply": cap_supply,
            "cap_basis": cap_basis,
            # The DISPLAY peak, from the tracked high-water mark only. No fallback to the stored
            # column: it is NULL on every funded row. No executable aggregate here: this is the
            # 1 s endpoint; /real/positions carries the exec_* fields.
            # EVM: the EXECUTABLE peak (exact whole-balance previews) or nothing; the nominal peak can
            # be a provider mid (operator via Alpha, 2026-10-08). Solana keeps its own-mark display peak.
            # Every chain since 2026-10-17 (ANIMA 73930: one bad state tick showed +70% on Solana):
            # the funded card's max gain is the EXECUTABLE peak's, or nothing.
            "max_gain_pct": (((p.get("executable_peak_price") / entry) - 1.0) * 100.0
                             if (p.get("executable_peak_price") and entry) else None),
            "peak_basis": "exact",
            **_unconfirmed_peak(p, entry),
            # Position return, not price return: after a partial de-risk the banked
            # USDC is part of the result and the bag is smaller. See position_return_pct.
            "pnl_pct": m.position_return_pct(
                c, p["id"], entry_notional=p.get("entry_notional"),
                last_price=value_price, entry_price=entry, open_quantity=qty),
            **m.position_value_view(c, p["id"], last_price=value_price,
                                    entry_notional=p.get("entry_notional"), open_quantity=qty),
            **_position_chart_levels(c, p, target_supply),
        })
        out[-1]["value_basis"] = basis
        out[-1].update(_last_known_price(c, p, chain_id, priced=bool(
            out[-1]["last_quote_price"] or out[-1]["selected_exit_price"] or shown_mid)))
    return {"positions": out, "server_now": now_real, "as_of": now_real,
            "revision": hashlib.sha256(_ticker_change_blob({"positions": out}).encode()).hexdigest()[:24]}


# SOL the swap tool leaves for gas: scripts/memecoin_swap_sol_usdc.py `--keep` default. A test pins
# the two together; the script is not imported here because it loads env files on import paths.
_SOL_GAS_KEEP = 0.14


def _position_chart_levels(conn, position, supply):
    """Read the effective policy without changing the position."""
    from sniper.services.memecoin_real_runner import (
        effective_real_thresholds, effective_trail, _effective_catastrophe_pct)
    levels = {"effective_stop_price": None, "effective_stop_mcap": None,
              "effective_trail_price": None, "effective_catastrophe_price": None,
              "effective_trail_armed": None}
    try:
        override = m.effective_exit_override(conn, position["id"])
        _tp, _target, stop, stop_mcap = effective_real_thresholds(position, override)
        trail, armed = effective_trail(position, override)
        catastrophe = _effective_catastrophe_pct(position, override)
        entry = position.get("entry_price")
        peak = position.get("executable_peak_price")
        levels["effective_stop_mcap"] = stop_mcap
        if stop_mcap:
            levels["effective_stop_price"] = stop_mcap / supply if supply else None
        elif stop and entry:
            levels["effective_stop_price"] = entry * (1 - stop / 100)
        if armed and trail and peak:
            levels["effective_trail_price"] = peak * (1 - trail / 100)
        if catastrophe and entry:
            levels["effective_catastrophe_price"] = entry * (1 - catastrophe / 100)
        levels["effective_trail_armed"] = bool(armed and trail)
    except (ValueError, TypeError, KeyError):
        levels["policy_error"] = "The effective policy is unavailable."
    return levels


def _fill_events(sc, prev_pos, pos_sig):
    """Typed entry/exit dicts for the stream's 'fill' SSE event, detected from two consecutive
    change-signature pos tuples (MAX(id) at [1], MAX(closed_recorded_at) at [3]) — so detection
    itself costs no extra scan and the delta queries run only when something changed."""
    out = []
    if pos_sig[1] > prev_pos[1]:
        for r in sc.execute(
                "SELECT id, ticker, mint, entry_notional, source_handle, network_id, attempt_id, "
                "origin_event_id FROM memecoin_positions"
                " WHERE execution_mode='real' AND id > ? ORDER BY id", (prev_pos[1],)):
            out.append({"kind": "entry", "id": r[0], "ticker": r[1], "mint": r[2],
                        "notional": r[3], "handle": r[4], "position_id": r[0],
                        "network_id": r[5], "attempt_id": r[6], "origin_event_id": r[7]})
            from sniper.services.memecoin_entry_view import entry_view
            projected = entry_view(sc, attempt_id=r[6], origin_event_id=r[7])
            out[-1].update({key: projected.get(key) for key in (
                "status", "label", "tx_sig", "fill_id", "links")} if projected else {
                    "status": "needs_review", "label": "Needs review"})
    if pos_sig[3] > prev_pos[3]:
        for r in sc.execute(
                "SELECT id, ticker, mint, realized_pnl, exit_reason, network_id, attempt_id, "
                "origin_event_id FROM memecoin_positions"
                " WHERE execution_mode='real' AND is_open=0 AND closed_recorded_at > ?"
                " ORDER BY closed_recorded_at", (prev_pos[3],)):
            out.append({"kind": "exit", "id": r[0], "ticker": r[1], "mint": r[2],
                        "realized": r[3], "exit_reason": r[4], "position_id": r[0],
                        "network_id": r[5], "attempt_id": r[6], "origin_event_id": r[7]})
    return out


# Fields recomputed from wall-clock on every tick. They are shown, never compared: `mark_age_s`
# is `now - mark_observed_at`, so at a 250ms tick it changes four times a second and made the
# payload differ from itself. Every open position therefore re-rendered 4x/s — the client replaces
# ticker HTML wholesale — which is a phone-melting amount of work to display an unchanged price.
# The underlying `mark_observed_at` IS compared, so a genuinely new mark still pushes instantly.
_TICKER_VOLATILE_FIELDS = ("mark_age_s",)


def _ticker_change_blob(tick):
    """Serialise the ticker for CHANGE DETECTION only, minus the wall-clock-derived fields."""
    return json.dumps(
        [{k: v for k, v in row.items() if k not in _TICKER_VOLATILE_FIELDS}
         for row in tick.get("positions") or []],
        sort_keys=True, separators=(",", ":"))


def _cockpit_change_signature(c):
    """A cheap tuple that changes exactly when the cockpit's DATA changed — open/closed positions,
    reservations, stuck flags, de-risks, kill switch, and today's realized. Deliberately NOT
    PRAGMA data_version: the mark tape commits every second, which would turn "changed" into
    "always" and put the client right back on a 4s blind poll."""
    from sniper.services.memecoin_arming import is_kill_tripped
    pos = c.execute(
        "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(stuck_bag),0), "
        "COALESCE(MAX(closed_recorded_at),''), COALESCE(MAX(derisked_at),'') "
        "FROM memecoin_positions WHERE execution_mode='real'").fetchone()
    resv = c.execute(
        "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(status='confirmed'),0), "
        "COALESCE(SUM(status='failed'),0) FROM memecoin_pending_entries").fetchone()
    day = c.execute(
        "SELECT ROUND(COALESCE(SUM(realized_pnl),0),4) FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=0 AND date(closed_at)=date('now')").fetchone()
    return (tuple(pos), tuple(resv), tuple(day), bool(is_kill_tripped()), m.entry_view_revision(c))


# Funded-cockpit SSE tick. 250ms so a position's price updates as fast as a new pool-state mark
# lands (~400ms) rather than on a 1s clock; the ticker only PUSHES on a changed payload, so a quiet
# book still costs one keepalive per 15s. MEMECOIN_STREAM_TICK_S overrides.
_STREAM_TICK_S = max(0.1, float(os.getenv("MEMECOIN_STREAM_TICK_S", "0.25")))
# The browser declares the stream dead after STREAM_SILENT_MS in static/app.js (12 s); keep this
# well under it.
_STREAM_HEARTBEAT_S = 5.0



class ExitOverrideError(ValueError):
    """A rejected operator exit-override payload; the message is what the caller returns as 400."""


def parse_exit_override(d, *, require_one=True):
    """Validate an operator TP/SL/trail/derisk payload. ONE copy, two callers.

    Extracted 2026-08-27 so the manual BUY form and the per-position EDIT cannot drift apart. Every
    rule here was paid for: the MILLIONS units guard came from SQUIRE's target stored as $4.11 (four
    dollars), and the mcap-only TP semantics from CHANCE/DREGG reporting "my custom TP doesn't work"
    when the cfg percentage rail fired before their target. Raises ExitOverrideError.

    require_one=False lets the buy form post an all-blank payload, which means "runner default".
    """
    def _num(k):
        if k not in d or d[k] is None or d[k] == "":
            return None
        v = d[k]
        if isinstance(v, bool):
            raise ExitOverrideError(f"{k} must be a finite number")
        try:
            value = float(v)
        except (TypeError, ValueError):
            raise ExitOverrideError(f"{k} must be a finite number") from None
        if not math.isfinite(value):
            raise ExitOverrideError(f"{k} must be a finite number")
        return value

    tp_pct, tp_mcap = _num("tp_pct"), _num("tp_target_mcap")
    sl_pct, sl_mcap = _num("sl_pct"), _num("sl_target_mcap")
    trail = _num("trail_pct")
    derisk = _num("derisk_pct")
    if require_one and all(x is None for x in (tp_pct, tp_mcap, sl_pct, sl_mcap, trail, derisk)):
        raise ExitOverrideError("provide at least one exit threshold")
    if tp_pct is not None and tp_mcap is not None:
        raise ExitOverrideError("choose tp_pct or tp_target_mcap, not both")
    if sl_pct is not None and sl_mcap is not None:
        raise ExitOverrideError("choose sl_pct or sl_target_mcap, not both")
    if tp_pct is not None and tp_pct < 0:
        raise ExitOverrideError("tp_pct must be >= 0 (0 disables TP)")
    if tp_mcap is not None and tp_mcap <= 0:
        raise ExitOverrideError("tp_target_mcap must be > 0")
    if sl_mcap is not None and sl_mcap <= 0:
        raise ExitOverrideError("sl_target_mcap must be > 0")
    # UNITS guard (2026-07-17): SQUIRE's target was stored as 4.11 — four DOLLARS (operator meant
    # 4.11M) -> an always-true target. Absolute USD only; no real mcap target is < $1k.
    for label, v in (("tp_target_mcap", tp_mcap), ("sl_target_mcap", sl_mcap)):
        if v is not None and v < 1000:
            raise ExitOverrideError(f"{label}={v:g} looks like MILLIONS — send absolute USD "
                                    "(e.g. 3200000 for $3.2M)")
    # MCAP-ONLY TP semantics (2026-07-17): with tp_pct unset, effective_real_thresholds falls back
    # to the cfg TP, which fires long before the operator's mcap target — the exact "my custom TP
    # doesn't work" report. An mcap target IS the take-profit: disable the % rail explicitly.
    # (SL keeps its chain-stop fallback — protective.)
    if tp_mcap is not None and tp_pct is None:
        tp_pct = 0.0
    if trail is not None and not (0 < trail < 100):
        raise ExitOverrideError("trail_pct must be in (0, 100)")
    if trail is not None:
        # An operator's bare trail must stay bare across config changes. Persist zeros rather than
        # silently inheriting a later process-wide TP or derisk threshold.
        if tp_pct is None and tp_mcap is None:
            tp_pct = 0.0
        if derisk is None:
            derisk = 0.0
    if derisk is not None and derisk < 0:
        raise ExitOverrideError("derisk_pct must be >= 0 (0 disables de-risk)")
    if sl_pct is not None and sl_pct > 0:
        sl_pct = -sl_pct               # a stop is always a drawdown; accept 40 or -40 as -40%
    if sl_pct is not None and abs(sl_pct) > 100:
        raise ExitOverrideError("sl_pct magnitude must be <= 100 (0 disables stop)")
    return {"tp_pct": tp_pct, "tp_target_mcap": tp_mcap, "sl_pct": sl_pct,
            "sl_target_mcap": sl_mcap, "trail_pct": trail, "derisk_pct": derisk}



# Resolved ONCE, at import. The web is launched through `.runtime/web/current`, which is a SYMLINK,
# and `os.path.abspath` does not follow it -- so the release SHA was never in the path and the guard
# reported "unpinned checkout" on a properly pinned deploy (live, 2026-09-03).
#
# `realpath` is correct here ONLY because it runs at import time, when this process is genuinely
# executing the release `current` points at. Resolving it per-request would follow the symlink to
# whatever a LATER deploy moved it to, report the new SHA while this process still served the old
# code, and go green in precisely the case the guard exists to catch.
def _resolve_web_release_sha():
    import re as _re
    for part in os.path.realpath(__file__).split(os.sep):
        if _re.fullmatch(r"[0-9a-f]{40}", part):
            return part
    return None


_WEB_RELEASE_SHA = _resolve_web_release_sha()
from sniper.util.web_release_relation import read as _read_web_release_relation
_WEB_RELEASE_RELATION = _read_web_release_relation(Path(__file__).resolve().parents[2] / '.web_release_relation.json')


def _release_drift(heartbeat):
    """Is the WEB serving the same release the runner is executing?

    The web has its own release root (`.runtime/web/current`) that memecoin deploys never touch, so
    it drifts behind the engine it reports on with no signal that it has. On 2026-09-03 it sat 127
    commits behind and rendered "funded contract · unreadable" over four valid `manual-unmanaged-v1`
    contracts, using a validation rule the runner had already retired. The operator closed a funded
    position two minutes after opening it. A stale UI that merely LOOKED stale would be tolerable;
    this one invented a specific, alarming, false claim about funded money.

    Both sides SELF-REPORT. The web reads the release directory it is executing from, not
    `.runtime/web/current` -- the symlink moves on deploy while this process keeps serving the old
    code, which is the exact "a restart is not a deploy" failure. The runner's SHA comes from its
    own heartbeat for the same reason.

    Returns None when neither side is release-pinned (a dev checkout), so local runs stay quiet.
    """
    web = _WEB_RELEASE_SHA
    runner = None
    if isinstance(heartbeat, dict):
        lifi = heartbeat.get("lifi")
        if isinstance(lifi, dict):
            runner = lifi.get("release_sha") or None
    if web is None and runner is None:
        return None
    result = {"web": web, "runner": runner,
              "match": bool(web and runner and web == runner)}
    if not result['match']:
        metadata = _WEB_RELEASE_RELATION
        result['relationship'] = ({k: metadata[k] for k in
            ('state','runner_is_ancestor','web_is_ancestor','funded_paths_changed')}
            if metadata.get('state') == 'known' and metadata.get('web_sha') == web
            and metadata.get('runner_sha') == runner else {'state':'unknown'})
    return result


# The ONLY mutating endpoint without the CSRF header. Its body carries a SIGNED, TTL'd, single-use
# token minted at alert time (memecoin_one_tap.verify_token runs before anything else), which a
# cross-origin page cannot forge; the Telegram confirm page that posts it sends no header.
CSRF_EXEMPT = frozenset({"api_memecoin.real_buy_token"})


def _mark_kill_deploy_restarts(rows, now_iso):
    """Dashboard rows say what the Telegram card says for a refusal inside a deploy's own kill-switch
    trip (memecoin_telegram._kill_deploy_words): kill_deploy_restart from the switch's audit log,
    read once per request. Words only; a failure leaves the rows as they were."""
    kills = [r for r in rows if isinstance(r, dict) and r.get("reason") == "kill_switch_tripped"]
    if not kills:
        return
    try:
        from sniper.services import memecoin_arming as arming
        tail = arming.read_kill_audit_tail() or ""
        for row in kills:
            row["kill_deploy_restart"] = arming.kill_deploy_restart_at(
                row.get("decision_started_at"), now_iso=now_iso, tail=tail)
    except Exception:
        logger.warning("entries: kill-switch hold read failed", exc_info=True)


def create_api_memecoin_blueprint(ctx):
    conn = ctx["conn"]
    m.ensure_memecoin_tables(conn)  # idempotent — guarantee tables on the live DB
    bp = Blueprint("api_memecoin", __name__)
    v6_snapshot_lock = threading.Lock()
    v6_snapshot_cache = {}
    v6_snapshot_ttl_s = 5.0
    startup_backlog_cache = {}
    screen_process_lock = threading.Lock()
    screen_process = None
    # Last launch, so the board can say "screening N traders, started HH:MM" instead of a
    # fire-and-forget toast that leaves the operator unable to tell a run from a no-op.
    screen_run = {}

    def _v6_cached(key, build):
        now = _time.monotonic()
        cached = v6_snapshot_cache.get(key)
        if cached and now - cached[0] < v6_snapshot_ttl_s:
            return cached[1]
        with v6_snapshot_lock:
            now = _time.monotonic()
            cached = v6_snapshot_cache.get(key)
            if cached and now - cached[0] < v6_snapshot_ttl_s:
                return cached[1]
            value = build()
            v6_snapshot_cache[key] = (_time.monotonic(), value)
            return value

    def _invalidate_v6_snapshots():
        with v6_snapshot_lock:
            v6_snapshot_cache.clear()
    fomo_get_fn = ctx.get("fomo_get", _fomo_get)  # injectable for tests
    _auth = {"a": None}
    _csrf_token, _csrf_ok = csrf_token, csrf_ok
    # EVERY mutating route on this blueprint needs the token (2026-09-27): 21 of 31 had none,
    # including arm/disarm, the kill switch and manual close. See memecoin_csrf.
    guard_mutations(bp, exempt=CSRF_EXEMPT, enforce=ctx.get("csrf_enforce_all"))

    @bp.get("/api/memecoin/csrf-token")
    def csrf_token_route():
        """The token alone, for the client's refresh after a 403: a kill click must not wait on
        /holdings. Same-origin readable only (no CORS on this app)."""
        resp = jsonify({"csrf_token": _csrf_token()})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _refusal_db():
        from sniper.persistence import db_memecoin_captures as refusal_store
        path = ctx.get("refusal_db_path")
        if not path:
            from sniper.util.state_paths import state_path
            path = state_path("data", "memecoin_refusal_captures.db")
        return refusal_store, refusal_store.connect(path)

    def _refusal_attempt(attempt_id):
        from sniper.services.memecoin_refusal_capture import classify_gate
        row = conn.execute(
            "SELECT attempt_id,origin_event_id,network_id,mint,ticker,source_user_id,"
            "source_handle,source_price,source_market_cap,chase_source_size_usd,"
            "decision_started_at,completed_at,terminal_reason,trace_json "
            "FROM memecoin_entry_attempts WHERE attempt_id=? AND terminal_status IS NOT NULL",
            (str(attempt_id),),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        family = classify_gate(item.get("terminal_reason"))
        if family not in m.EXEMPTIBLE_GATE_FAMILIES:
            return None
        item["gate_family"] = family
        item["refused_at"] = item.get("completed_at") or item["decision_started_at"]
        return item

    def _recent_refusal_attempts(ttl_s=120):
        corrected_now = datetime.fromisoformat(_now_iso().replace("Z", "+00:00"))
        # A slot can first appear near the refusal-window limit. Keep it available for one
        # complete slot TTL after that first display.
        cutoff = (corrected_now - timedelta(seconds=ttl_s * 2)).isoformat()
        rows = conn.execute(
            "SELECT attempt_id FROM memecoin_entry_attempts "
            "WHERE terminal_status IS NOT NULL AND terminal_status<>'filled' "
            "AND COALESCE(completed_at,decision_started_at)>=? "
            "ORDER BY COALESCE(completed_at,decision_started_at) DESC,attempt_id",
            (cutoff,),
        ).fetchall()
        return [item for row in rows if (item := _refusal_attempt(row[0])) is not None]

    def _refusal_slot_facts(attempt, refusal_price):
        from sniper.services.memecoin_entry_view import _gate_evidence
        source_floor = next((row.get("effective_floor_usd") for row in _gate_evidence(attempt)
                             if row.get("name") == "source_buy_floor"), None)
        if (isinstance(source_floor, bool) or not isinstance(source_floor, (int, float))
                or not math.isfinite(source_floor) or source_floor < 0):
            source_floor = None
        market_cap, market_cap_basis = attempt.get("source_market_cap"), "source"
        try:
            known = float(market_cap) > 0
        except (TypeError, ValueError):
            known = False
        if not known:
            from sniper.services.memecoin_chains import resolve_chain, network_ids_for
            chain_key = resolve_chain(attempt.get("network_id"))
            chain_ids = network_ids_for(chain_key) if chain_key else ()
            events = conn.execute(
                "SELECT their_price, their_market_cap FROM memecoin_events "
                f"WHERE mint=? AND LOWER(network_id) IN ({','.join('?' for _ in chain_ids)}) "
                "AND their_price>0 AND their_market_cap>0 "
                "ORDER BY ABS(julianday(created_at) - julianday(?)) LIMIT 1",
                (attempt.get("mint"), *chain_ids, attempt.get("refused_at"))).fetchall() if chain_ids else []
            market_cap = _derived_market_cap(events, attempt.get("source_price"))
            market_cap_basis = "derived_supply" if market_cap else None
        # TRADER SIZE (Alpha 2026-09-17). Only the chase path writes chase_source_size_usd, so 90 of
        # 93 cards in 24 h showed no size. The copied trade's own event row carries it.
        size, size_basis = attempt.get("chase_source_size_usd"), "attempt"
        if not _positive(size):
            row = conn.execute(
                "SELECT their_size_usd FROM memecoin_events WHERE fomo_event_id=?",
                (attempt.get("origin_event_id"),)).fetchone() if attempt.get("origin_event_id") else None
            size, size_basis = (row[0], "event") if row and _positive(row[0]) else (None, None)
        return {
            "attempt_id": attempt["attempt_id"],
            "origin_event_id": attempt.get("origin_event_id"),
            "network_id": attempt["network_id"],
            "mint": attempt["mint"],
            "ticker": attempt.get("ticker"),
            "source_user_id": attempt.get("source_user_id"),
            "source_handle": attempt.get("source_handle"),
            "source_price": attempt.get("source_price"),
            "source_market_cap": market_cap,
            "source_market_cap_basis": market_cap_basis,
            "source_size_usd": size,
            "source_size_basis": size_basis,
            "source_buy_effective_floor_usd": source_floor,
            "gate_family": attempt["gate_family"],
            "refusal_reason": attempt["terminal_reason"],
            "refused_at": attempt["refused_at"],
            "refusal_price": refusal_price,
        }

    def _refusal_prices(attempt, refusal_conn, now_iso):
        """Return (refusal price, current price, basis, current price observed_at).

        The exact-route capture owns Solana (`route`). The EVM capture lane's DexScreener marks
        come next (`display`, chart space, never a route), then the capture-v2 tape (`market`).
        A display or market price is used only within 120 s of the refusal or of now. This
        function makes no provider call from the web request.
        """
        origin = str(attempt.get("origin_event_id") or "")
        route_rows = (refusal_conn.execute(
            "SELECT quote_price, observed_at FROM memecoin_refusal_captures "
            "WHERE origin_event_id=? AND observed_at IS NOT NULL AND quote_price>0 "
            "AND COALESCE(quote_price_basis, 'exact_route')='exact_route' "
            "ORDER BY age_s,id", (origin,)).fetchall() if origin else [])
        if route_rows:
            return (float(route_rows[0][0]), float(route_rows[-1][0]), "route",
                    route_rows[-1][1])
        now_dt = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))

        def fresh(observed_at):
            return abs((now_dt - datetime.fromisoformat(
                str(observed_at).replace("Z", "+00:00"))).total_seconds()) <= 120

        # DISPLAY (Alpha 2026-09-17): 8 of 12 unpriced 4663 cards held these rows.
        display_rows = (refusal_conn.execute(
            "SELECT quote_price, observed_at, age_s FROM memecoin_refusal_captures "
            "WHERE origin_event_id=? AND observed_at IS NOT NULL AND quote_price>0 "
            "AND quote_price_basis='display' ORDER BY age_s,id", (origin,)).fetchall()
            if origin else [])
        if display_rows:
            first, last = display_rows[0], display_rows[-1]
            return ((float(first[0]) if first[2] is not None and float(first[2]) <= 120 else None),
                    (float(last[0]) if fresh(last[1]) else None), "display",
                    (last[1] if fresh(last[1]) else None))

        network = str(attempt.get("network_id") or "")
        mint = str(attempt.get("mint") or "")
        if mint.startswith("0x"):
            mint = mint.lower()
        refused_at = str(attempt.get("refused_at") or "")
        if not network or not mint or not refused_at:
            return None, None, None, None

        # INDEXED BY: without it the planner takes (quality, observed_at) and walks every 'ok'
        # row until the mint appears -- 1.58 s on the 12.8 M-row live tape for an unseen mint.
        before = conn.execute(
            "SELECT price,observed_at FROM memecoin_quote_observations_v2 "
            "INDEXED BY idx_mc_quotes_v2_coin_time "
            "WHERE network_id=? AND mint=? AND quality='ok' AND price>0 AND observed_at<=? "
            "ORDER BY observed_at DESC LIMIT 1", (network, mint, refused_at)).fetchone()
        after = conn.execute(
            "SELECT price,observed_at FROM memecoin_quote_observations_v2 "
            "INDEXED BY idx_mc_quotes_v2_coin_time "
            "WHERE network_id=? AND mint=? AND quality='ok' AND price>0 AND observed_at>=? "
            "ORDER BY observed_at LIMIT 1", (network, mint, refused_at)).fetchone()

        refused_dt = datetime.fromisoformat(refused_at.replace("Z", "+00:00"))
        nearby = [row for row in (before, after) if row]
        nearest = min(
            nearby,
            key=lambda row: abs((datetime.fromisoformat(
                row["observed_at"].replace("Z", "+00:00")) - refused_dt).total_seconds()),
            default=None,
        )
        if nearest:
            distance = abs((datetime.fromisoformat(
                nearest["observed_at"].replace("Z", "+00:00")) - refused_dt).total_seconds())
            if distance > 120:
                nearest = None

        current = conn.execute(
            "SELECT price,observed_at FROM memecoin_quote_observations_v2 "
            "WHERE network_id=? AND mint=? AND quality='ok' AND price>0 "
            "ORDER BY observed_at DESC LIMIT 1", (network, mint)).fetchone()
        if current and not fresh(current["observed_at"]):
            current = None
        return ((float(nearest["price"]) if nearest else None),
                (float(current["price"]) if current else None), "market",
                (current["observed_at"] if current else None))

    def _get_auth():
        if _auth["a"] is None:
            _auth["a"] = FomoAuth()
        return _auth["a"]

    @bp.get("/api/memecoin/resolve/<handle>")
    def resolve(handle):
        # fomo handle -> internal userId (curated list must key on userId, not handle)
        try:
            _s, j, _d = fomo_get_fn(_get_auth(), "/v2/users/userHandle/" + handle)
            ro = j.get("responseObject") or j
            user = ro.get("user") if isinstance(ro.get("user"), dict) else {}
            uid = ro.get("id") or ro.get("userId") or user.get("id")
            disp = (ro.get("displayName") or ro.get("username")
                    or user.get("displayName") or handle)
            if uid:
                return jsonify({"found": True, "source_user_id": uid,
                                "display_name": disp, "handle": handle})
            return jsonify({"found": False, "handle": handle})
        except Exception as e:
            return jsonify({"found": False, "handle": handle, "error": str(e)[:120]})

    @bp.get("/api/memecoin/axiom")
    def axiom_research():
        from sniper.persistence.db_axiom_research import research_snapshot
        with m.operator_read_connection(conn) as observer:
            return jsonify(research_snapshot(observer))

    @bp.post("/api/memecoin/axiom/accounts")
    @bp.post("/api/memecoin/axiom/accounts/swap")
    def axiom_research_action():
        from sniper.persistence.db_axiom_research import (
            ensure_research_tables, set_research_account, swap_research_account,
        )
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "object_required"}), 400
        try:
            with conn:
                ensure_research_tables(conn)
                if request.path.endswith("/swap"):
                    swap_research_account(conn, old_chain=data.get("old_chain"),
                        old_address=data.get("old_address"), new_chain=data.get("new_chain"),
                        new_address=data.get("new_address"), now_iso=_now_iso())
                else:
                    set_research_account(conn, chain=data.get("chain"), address=data.get("address"),
                        active=data.get("active", True),
                        requested_source_user_id=data.get("requested_source_user_id"), now_iso=_now_iso())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except sqlite3.OperationalError:
            return jsonify({"error": "axiom_capture_unavailable"}), 503
        return jsonify({"ok": True, "authority": "research_only"})

    @bp.get("/api/memecoin/wallets")
    def wallets():
        return jsonify({"wallets": m.list_wallets(conn)})

    @bp.get("/api/memecoin/engine-cockpit")
    def engine_cockpit_route():
        """WO-12 — the operator's canary cockpit: what the engine sees/decides per open real
        position. Read-only from the worker-owned exit-state DB (absent pre-arm -> empty)."""
        from sniper.services.memecoin_engine_cockpit import engine_cockpit
        exit_conn = None
        try:
            from sniper.persistence import db_exit_state as xs
            import os
            if os.path.exists(xs.exit_state_db_path()):
                exit_conn = xs.connect_exit_state_existing()   # worker-owned; read use only
        except Exception:
            exit_conn = None
        try:
            return jsonify({"positions": engine_cockpit(exit_conn, conn)})
        finally:
            if exit_conn is not None:
                try:
                    exit_conn.close()
                except Exception:
                    pass

    @bp.post("/api/memecoin/wallets")
    def add():
        d = request.get_json(force=True, silent=True) or {}
        uid = str(d.get("source_user_id") or "").strip()
        if not uid:
            return jsonify({"ok": False, "error": "source_user_id_required"}), 400
        existing = conn.execute(
            "SELECT COALESCE(excluded,0) FROM memecoin_wallets WHERE source_user_id=?", (uid,)
        ).fetchone()
        if existing and existing[0]:
            return jsonify({"ok": False, "error": "excluded_requires_reobserve"}), 409
        m.add_wallet(conn, uid, d.get("handle", ""), d.get("label", ""))
        return jsonify({"ok": True})

    @bp.post("/api/memecoin/wallets/<uid>/pause")
    def pause(uid):
        paused = bool(request.get_json(force=True).get("paused"))
        current = conn.execute(
            "SELECT COALESCE(excluded,0) FROM memecoin_wallets WHERE source_user_id=?", (uid,)
        ).fetchone()
        if not current:
            return jsonify({"ok": False, "error": "wallet_not_found"}), 404
        if not paused and current[0]:
            m.reobserve_wallet(conn, uid)
        else:
            m.set_wallet_paused(conn, uid, paused)
        return jsonify({"ok": True})

    @bp.post("/api/memecoin/wallets/<uid>/exemption")
    def set_wallet_exemption(uid):
        """Grant or revoke ONE selection-gate exemption for a wallet. Body: {gate_family, active}.

        The web writes wallet CONFIG only; entry admission stays the runner's. Safety families are
        refused by the persistence layer, not here — one place decides what is exemptible, so this
        route cannot drift from the gate.
        """
        # CSRF (2026-09-27): a waiver changes which funded entries are admitted, and this route
        # parses with force=True, so a forged cross-origin text/plain POST could grant one.
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        d = request.get_json(force=True, silent=True) or {}
        family = (d.get("gate_family") or "").strip()
        active = bool(d.get("active"))
        actor = (d.get("actor") or "ui").strip()
        reason = (d.get("reason") or "").strip()
        if not family:
            return jsonify({"ok": False, "error": "gate_family required"}), 400
        # The wallet must EXIST. The exemptions table has no foreign key, so a waiver written
        # against an unknown id is an orphan that sits there until some future wallet is created
        # with that id and silently inherits a gate exemption it never earned. The pause route
        # above already checks this; this one did not. Same check, same 404.
        if not conn.execute(
                "SELECT 1 FROM memecoin_wallets WHERE source_user_id=?", (uid,)).fetchone():
            return jsonify({"ok": False, "error": "wallet_not_found"}), 404
        try:
            m.set_wallet_gate_exemption(conn, uid, family, active=active, actor=actor,
                                        reason=reason, now_iso=_now_iso())
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        _notify_operator(
            "exemption %s" % ("granted" if active else "revoked"),
            "%s %s %s" % (uid, family, ("granted" if active else "revoked")))
        return jsonify({"ok": True, "gate_exemptions": sorted(m.wallet_gate_exemptions(conn, uid))})

    # PER-WALLET EXIT RAILS (operator order 2026-09-27: "set different tp strategies for different
    # users through the UI"). The web writes wallet CONFIG only: the runner reads it once when it
    # freezes a NEW funded contract, so open positions keep their rails. The default shown is what
    # THIS web process resolves for the nano band (follow-30-50 is band-independent today).
    def _exit_rails_view(uid, chain, *, with_audit=True):
        from sniper.services.memecoin_chains import canonical_chain_id
        from sniper.services.memecoin_risk import (
            apply_wallet_exit_override, funded_policy_for_origin)
        network = canonical_chain_id(chain) or str(chain)
        base, _copy_arm = funded_policy_for_origin(
            conn, coin_band="nano", network_id=network, source_user_id=uid,
            apply_wallet_override=False)
        effective, provenance = apply_wallet_exit_override(
            conn, base, source_user_id=uid, network_id=network)
        return {
            "source_user_id": uid, "chain": chain, "network_id": network, "band": "nano",
            "default": {"policy_id": base.policy_id, "tp_pct": base.tp_pct,
                        "stop_pct": base.stop_pct},
            "override": m.wallet_exit_override(conn, uid, network_id=network),
            "effective": {"tp_pct": effective.tp_pct, "stop_pct": effective.stop_pct},
            "source": "override" if provenance else "default",
            "bounds": {"tp_pct": list(m.WALLET_EXIT_OVERRIDE_TP_RANGE),
                       "stop_pct": list(m.WALLET_EXIT_OVERRIDE_STOP_RANGE)},
            "audit": m.wallet_exit_override_audit(conn, uid, limit=10) if with_audit else None,
            "applies_to": "new funded entries only; open positions keep their frozen rails",
        }

    def _exit_rails_wallet(uid):
        return conn.execute(
            "SELECT 1 FROM memecoin_wallets WHERE source_user_id=?", (uid,)).fetchone()

    @bp.get("/api/memecoin/wallets/<uid>/exit-rails")
    def wallet_exit_rails(uid):
        chain = (request.args.get("chain") or "").strip()
        if not chain:
            return jsonify({"ok": False, "error": "chain required"}), 400
        if not _exit_rails_wallet(uid):
            return jsonify({"ok": False, "error": "wallet_not_found"}), 404
        try:
            view = _exit_rails_view(uid, chain)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({**view, "csrf_token": _csrf_token()})

    @bp.post("/api/memecoin/wallets/<uid>/exit-rails")
    def set_wallet_exit_rails(uid):
        """Body: {chain, tp_pct, stop_pct, reason}. A missing or null rail keeps the default."""
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        d = request.get_json(force=True, silent=True) or {}
        chain = str(d.get("chain") or "").strip()
        if not chain:
            return jsonify({"ok": False, "error": "chain required"}), 400
        if not _exit_rails_wallet(uid):
            return jsonify({"ok": False, "error": "wallet_not_found"}), 404
        tp, stop = d.get("tp_pct"), d.get("stop_pct")
        try:
            m.set_wallet_exit_override(
                conn, uid, network_id=chain, tp_pct=None if tp in (None, "") else tp,
                stop_pct=None if stop in (None, "") else stop,
                actor=(d.get("actor") or "ui").strip(), reason=(d.get("reason") or "").strip(),
                now_iso=_now_iso())
            view = _exit_rails_view(uid, chain)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        _notify_operator(
            "EXIT RAILS %s" % _handle_of(conn, uid),
            "chain=%s tp=%s stop=%s (new funded entries only)"
            % (chain, view["effective"]["tp_pct"], view["effective"]["stop_pct"]))
        return jsonify({"ok": True, **view})

    @bp.post("/api/memecoin/wallets/<uid>/exit-rails/reset")
    def reset_wallet_exit_rails(uid):
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        d = request.get_json(force=True, silent=True) or {}
        chain = str(d.get("chain") or "").strip()
        if not chain:
            return jsonify({"ok": False, "error": "chain required"}), 400
        if not _exit_rails_wallet(uid):
            return jsonify({"ok": False, "error": "wallet_not_found"}), 404
        cleared = m.reset_wallet_exit_override(
            conn, uid, network_id=chain, actor=(d.get("actor") or "ui").strip(),
            reason=(d.get("reason") or "").strip(), now_iso=_now_iso())
        if cleared:
            _notify_operator("EXIT RAILS %s reset" % _handle_of(conn, uid),
                             "chain=%s back to the default policy" % chain)
        try:
            view = _exit_rails_view(uid, chain)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "cleared": bool(cleared), **view})

    @bp.get("/api/memecoin/entry-knobs")
    def entry_knobs():
        """Every ENTRY control the runner reads: its `.env` default, its effective value, whether
        an override is moving it, and whether it may be moved at all.

        Locked knobs are LISTED, never hidden. An operator who cannot see a control does not know
        it is there; a write naming one is refused by the persistence layer, so this route cannot
        drift from what is actually enforceable.
        """
        from sniper.settings import memecoin as cfg
        from sniper.services.memecoin_entry_knobs import KNOBS, effective, read_overrides
        raw = read_overrides(conn)
        values = effective(conn, cfg, overrides=raw)
        rows = []
        for name, knob in KNOBS.items():
            value, source = values[name]
            rows.append({**knob.as_dict(), "value": value, "source": source,
                         "default": knob_default(cfg, name),
                         "overridden": source == "override"})
        # The runner applies the overlay between decisions, so a write can be stored and not yet
        # live. Say so rather than rendering it as in force — the web process cannot observe the
        # runner's own `pending`, so this reports the WEB process state and the panel labels it.
        from sniper.services.memecoin_entry_knobs import pending_state, effective_control_view
        return jsonify({"knobs": rows,
                        "effective_controls": effective_control_view(
                            conn, cfg, values, source_user_id=request.args.get("source_user_id")),
                        "groups": list(dict.fromkeys(k["group"] for k in rows)),
                        "audit": m.entry_knob_audit(conn, limit=25),
                        "apply_model": {
                            "writer": "funded runner main cycle",
                            "deferred_while_entry_in_flight": True,
                            "note": "a change lands between funded entry decisions, never inside "
                                    "one; it is live on the runner's next cycle"},
                        "pending": pending_state(),
                        "csrf_token": _csrf_token()})

    def knob_default(cfg, name):
        from sniper.services.memecoin_entry_knobs import default_value
        return default_value(cfg, name)

    @bp.post("/api/memecoin/entry-knobs/<name>")
    def set_entry_knob(name):
        """Move ONE entry knob, or clear its override. Body: {value, active, reason, actor}.

        The web writes CONFIG only. Entry admission stays the runner's: it re-reads this overlay on
        its next entry decision, so a change here is live within one event and needs no restart.
        """
        # CSRF, like every other mutating route here (audit finding B1, 2026-09-18). This one
        # was unguarded while it moved selection thresholds; 92e40824 put the drawdown pause
        # and the daily loss breaker behind it, so a forged cross-origin POST could disable a
        # capital control. `force=True` also accepts text/plain, which is exactly the body a
        # simple cross-origin form can send without a preflight.
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        d = request.get_json(force=True, silent=True) or {}
        active = bool(d.get("active", True))
        actor = (d.get("actor") or "ui").strip()
        reason = (d.get("reason") or "").strip()
        try:
            res = m.set_entry_knob_override(conn, name, d.get("value"), active=active,
                                            actor=actor, reason=reason, now_iso=_now_iso())
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        _notify_operator("entry knob %s" % res["action"],
                         "%s %s" % (name, res["value"] if res["value"] is not None else "-> default"))
        from sniper.settings import memecoin as cfg
        from sniper.services.memecoin_entry_knobs import effective
        value, source = effective(conn, cfg)[name]
        return jsonify({"ok": True, "name": name, "value": value, "source": source,
                        "action": res["action"]})

    @bp.get("/api/memecoin/exemptible-gates")
    def exemptible_gates():
        """The families a wallet may be excused from — served from the SAME set the gate reads, so
        the panel can never offer an exemption the runner would ignore."""
        return jsonify({"families": sorted(m.EXEMPTIBLE_GATE_FAMILIES)})

    @bp.post("/api/memecoin/wallets/<uid>/exclude")
    def exclude(uid):
        # operator cut for a FOLLOWED wallet — sticks (sync won't re-add/un-pause). Reversible.
        m.set_wallet_excluded(conn, uid, True)
        return jsonify({"ok": True})

    @bp.delete("/api/memecoin/wallets/<uid>")
    def remove(uid):
        if not m.remove_wallet(conn, uid):
            return jsonify({"ok": False, "error": "wallet_not_found"}), 404
        return jsonify({"ok": True})

    @bp.post("/api/memecoin/wallets/<uid>/reobserve")
    def reobserve(uid):
        if not m.reobserve_wallet(conn, uid):
            return jsonify({"ok": False, "error": "wallet_not_found"}), 404
        return jsonify({"ok": True, "real_approved": False})

    @bp.post("/api/memecoin/wallets/<uid>/trial")
    def wallet_trial(uid):
        """Start or end a trial from the Wallet Map (operator 2026-10-02). The SAME implementation as
        scripts/memecoin_trial_arm.py (sniper/services/memecoin_trial_ops): the same on-chain
        evidence gate, the same refusals, a dry run unless `confirm`. A refusal is a 200 with
        ok=False and the reason, so the page shows it. `add` (a candidate) adds the wallet to the
        roster only after every check passed. DB authority only: no funded seam, no network; the
        runner picks a new arm up at its roster refresh. JSON body only, CSRF like every money route."""
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        body = request.get_json(silent=True)              # never force=True: no text/plain forms
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "json_body_required"}), 400
        chain = str(body.get("chain") or "").strip()
        action = body.get("action")
        if not chain:
            return jsonify({"ok": False, "error": "chain_required"}), 400
        from sniper.services import memecoin_trial_ops as trial_ops
        from sniper.util.clock import real_utc_now_iso
        confirm = bool(body.get("confirm"))
        if action not in ("start", "end"):
            return jsonify({"ok": False, "error": "action_must_be_start_or_end"}), 400
        try:
            if action == "start":
                out = trial_ops.start(conn, chain, uid=uid, handle=str(body.get("handle") or "") or None,
                                      confirm=confirm, now_iso=real_utc_now_iso(),
                                      add_if_missing=bool(body.get("add")), actor="operator:dashboard")
            else:
                out = trial_ops.end(conn, chain, uid=uid, keep_armed=bool(body.get("keep_armed")),
                                    disarm=bool(body.get("disarm")), confirm=confirm,
                                    now_iso=real_utc_now_iso(), actor="operator:dashboard",
                                    ack_stop=str(body.get("ack_stop") or "") or None)
        finally:
            # This thread's connection is on the funded ledger: a write that raised after its
            # implicit BEGIN would keep the write lock and stall the runner (Gamma, 2026-10-02).
            if conn.in_transaction:
                conn.rollback()
        if out.get("written") or out.get("added"):
            _notify_operator("TRIAL %s %s" % (action.upper(), _handle_of(conn, uid)),
                             "chain=%s clip=%s ok=%s reason=%s (Wallet Map)" % (
                                 out.get("chain"), out.get("clip_usd"), out.get("ok"), out.get("reason")))
        return jsonify(out), 200

    @bp.post("/api/memecoin/wallets/<uid>/real")
    def wallet_real(uid):
        """Per-wallet real allowlist toggle (spec §1): approved wallets' BUYS open real positions;
        everything else stays paper + Telegram watch-only. Sells are never gated by this."""
        body = request.get_json(force=True, silent=True) or {}
        approved = bool(body.get("approved"))
        reason = str(body.get("reason") or "")
        force = bool(body.get("force"))
        # Arming is PER CHAIN (2026-07-30). Preferred form: {"chain": "<id>", "armed": bool}
        # — one transactional add/remove that leaves every other armed chain untouched. The
        # old {"approved", "chains":[...]} form REPLACED the whole list, so arming a second
        # chain silently disarmed the first (audit 2026-07-30).
        single_chain = body.get("chain")
        if single_chain is not None:
            arming = bool(body.get("armed"))
            follow_result = None
            follow_failure = None
            # The Fomo follow must exist BEFORE authority is written (spec 2026-07-16). This guard
            # used to live only on the legacy whole-list path below, so the per-chain form — the
            # one the UI actually sends — armed wallets while following nobody (audit 2026-08-05).
            # Order matters three ways: the chain-scoped GATE runs first so a refused arm never
            # makes an external request; the follow runs second; `set_wallet_chain_armed` (which
            # re-runs the same gate in its own transaction and stays the sole authority writer)
            # runs last. Disarm is local and never calls Fomo — this service never unfollows.
            # A new grant or a resume needs the checks. An active re-arm stays a no-op.
            from sniper.services.memecoin_chains import canonical_chain_id as _canon_chain
            already_armed = _canon_chain(single_chain) in set(m.wallet_armed_chains(conn, uid))
            wallet_state = conn.execute(
                "SELECT paused FROM memecoin_wallets WHERE source_user_id=?", (uid,)
            ).fetchone()
            resumes_wallet = bool(wallet_state and wallet_state["paused"])
            requires_activation = arming and (not already_armed or resumes_wallet)
            if requires_activation:
                preview = m.real_approval_preview(
                    conn, uid, reason=reason, force=force, chain=single_chain)
                # UNDER J7 THE FOLLOW IS NOT ATTEMPTED AT ALL (2026-09-12, operator: "you can turn
                # this off"). Every attempt 403s on the banned account and trips the ban guard,
                # which pages FOMO-BAN-GUARD. Signals arrive over the J7 relay; the J7 list is what
                # must hold the wallet.
                from sniper.settings import memecoin as _mc_cfg
                if preview["ok"] and _mc_cfg.MEMECOIN_SIGNAL_SOURCE == "j7":
                    follow_failure = None
                elif preview["ok"]:
                    from sniper.services.memecoin_follow_sync import ensure_target_followed
                    try:
                        follow_result = ensure_target_followed(
                            _get_auth(), uid,
                            user_fn=ctx.get("follow_user_fn"),
                            following_ids_fn=ctx.get("follow_following_ids_fn"),
                            follow_fn=ctx.get("follow_target_fn"),
                        )
                    except Exception as exc:
                        # BEST-EFFORT SINCE 2026-09-12 (operator: "fix"). The follow used to be a hard
                        # precondition (spec 2026-07-16), from when Fomo follows were how signals
                        # arrived. Fomo banned the account on 2026-08-03 and signals now arrive over
                        # the J7 relay, so the follow cannot succeed and changes nothing: it refused
                        # every new arm, including the operator's own clicks on 2026-09-12. The
                        # failure is kept in the approval reason and in the response instead.
                        follow_failure = "%s: %s" % (type(exc).__name__, str(exc)[:200])
                        logger.warning("arm %s on %s: fomo follow unavailable (%s); arming anyway",
                                       uid, single_chain, follow_failure)
            # The Wallet Map's operator override names itself (operator 2026-10-02); any other
            # value, or none, stays "ui": the page cannot write an arbitrary actor.
            actor = "operator:dashboard" if body.get("actor") == "operator:dashboard" else "ui"
            res = m.set_wallet_chain_armed(
                conn, uid, single_chain, arming,
                actor=actor, force=force,
                reason=(reason + (" [fomo follow unavailable: %s]" % follow_failure
                                  if follow_failure else "")).strip())
            status = 200 if res["ok"] or res["action"] == "approve_refused" else 400
            payload = {**res, "source_user_id": uid,
                       "armed_chains": m.wallet_armed_chains(conn, uid)}
            if follow_failure:
                payload["fomo_follow_failed"] = follow_failure
            _notify_operator(
                ("ARM" if arming else "DISARM") + f" {_handle_of(conn, uid)}",
                f"chain={single_chain} ok={res['ok']} action={res['action']} "
                f"armed_now={payload['armed_chains']} forced={force}")
            if follow_result is not None:
                payload["fomo_follow_created"] = bool(follow_result.get("created"))
            return jsonify(payload), status
        chains = body.get("chains")
        if approved and not chains:
            return jsonify({"ok": False, "error": "chains_required",
                            "detail": "arming is per chain; name at least one"}), 400
        current = conn.execute(
            "SELECT COALESCE(real_approved,0) FROM memecoin_wallets WHERE source_user_id=?",
            (uid,),
        ).fetchone()
        newly_approved = approved and not bool(current and current[0])

        follow_result = None
        follow_failure = None
        from sniper.settings import memecoin as _mc_cfg
        if newly_approved:
            preview = m.real_approval_preview(conn, uid, reason=reason, force=force)
            if not preview["ok"]:
                # Preserve the existing refusal writer and response contract. The gate must run
                # before any external request.
                res = m.set_wallet_real_approved(
                    conn, uid, True, chains=chains, actor="ui", reason=reason, force=force,
                )
            elif _mc_cfg.MEMECOIN_SIGNAL_SOURCE == "j7":
                # See the per-chain path: under J7 the follow is never attempted.
                res = m.set_wallet_real_approved(
                    conn, uid, True, chains=chains, actor="ui", reason=reason, force=force,
                )
            else:
                from sniper.services.memecoin_follow_sync import ensure_target_followed
                try:
                    follow_result = ensure_target_followed(
                        _get_auth(),
                        uid,
                        user_fn=ctx.get("follow_user_fn"),
                        following_ids_fn=ctx.get("follow_following_ids_fn"),
                        follow_fn=ctx.get("follow_target_fn"),
                    )
                except Exception as exc:
                    # BEST-EFFORT SINCE 2026-09-12: see the per-chain path above.
                    follow_failure = "%s: %s" % (type(exc).__name__, str(exc)[:200])
                    logger.warning("approve %s: fomo follow unavailable (%s); approving anyway",
                                   uid, follow_failure)
                res = m.set_wallet_real_approved(
                    conn, uid, True, chains=chains, actor="ui", force=force,
                    reason=(reason + (" [fomo follow unavailable: %s]" % follow_failure
                                      if follow_failure else "")).strip(),
                )
        else:
            # Revoke and already-approved idempotent calls stay local. This service never
            # unfollows, and a later Fomo outage cannot revoke an existing approval.
            res = m.set_wallet_real_approved(
                conn, uid, approved, chains=chains, actor="ui", reason=reason, force=force,
            )
        # gate refusal is a 200 with ok=False + the evidence, so the UI can show WHY and offer
        # an explicit force (which is audited as forced)
        payload = {"ok": res["ok"], "source_user_id": uid,
                   "armed_chains": m.wallet_armed_chains(conn, uid),
                   "action": res["action"], "reason": res["reason"], "stats": res["stats"]}
        _notify_operator(
            ("APPROVE" if approved else "REVOKE") + f" {_handle_of(conn, uid)}",
            f"ok={res['ok']} action={res['action']} armed_now={payload['armed_chains']} "
            f"forced={force}")
        if follow_result is not None:
            payload["fomo_follow_created"] = bool(follow_result["created"])
        if follow_failure:
            payload["fomo_follow_failed"] = follow_failure
        return jsonify(payload)

    @bp.get("/api/memecoin/positions/open")
    def open_positions():
        # DB-only operator read. The runner owns quote collection; the web request never competes
        # for provider capacity or stalls on an outbound call. Scoped to ONE chain — chains are
        # never pooled (2026-07-30); omit `chain` only for the legacy cross-chain view.
        from sniper.services.memecoin_chains import resolve_chain
        raw_chain = request.args.get("chain")
        chain = resolve_chain(raw_chain) if raw_chain else None
        if raw_chain and chain is None:
            return jsonify({"error": "unknown_chain", "detail": "chains are never pooled"}), 400
        positions = m.list_open_positions(conn, chain=chain)
        return jsonify({
            "positions": positions,
            "chain": chain,
            "marks": stored_position_marks(conn, positions, _now_iso()),
        })

    @bp.get("/api/memecoin/paper-report")
    def paper_report():
        from sniper.services.memecoin_paper_report import CHAIN_IDS, build_paper_report
        from sniper.settings.memecoin import MEMECOIN_ARMS

        chain = (request.args.get("chain") or "all").strip().lower()
        if chain not in CHAIN_IDS:
            return jsonify({"error": "invalid_chain", "allowed": sorted(CHAIN_IDS)}), 400
        try:
            return jsonify(build_paper_report(conn, MEMECOIN_ARMS, chain=chain))
        except Exception as exc:
            logger.exception("paper report failed")
            return jsonify({"error": "paper_report_unavailable", "detail": str(exc)[:160]}), 500

    @bp.get("/api/memecoin/promotion")
    def promotion_lane():
        from sniper.services.memecoin_chains import resolve_chain
        chain = resolve_chain(request.args.get("chain"))
        if chain is None:
            return jsonify({"error": "chain_required", "detail": "name one chain"}), 400
        try:
            days = _bounded_query_number(request.args, "days", cast=float, default=7.0,
                                         minimum=0.01, maximum=365.0)
        except ValueError as exc:
            return jsonify({"error": "invalid_window", "detail": str(exc)}), 400
        # Corrected UTC, not the host clock (the deployed host's clock is known to drift).
        since = (datetime.fromisoformat(_now_iso()) - timedelta(days=days)).isoformat()
        try:
            # One short read-only snapshot for both observation-only reads, never the
            # blueprint's long-lived writable connection.
            with m.operator_read_connection(conn) as observer:
                out = m.promotion_lane_rollup(observer, chain=chain, since=since)
                real = m.real_stats_by_wallet(observer, chain)
        except Exception:
            logger.exception("promotion readout failed for chain=%s", chain)
            return jsonify({"error": "promotion_unavailable"}), 500
        out["funded"] = {
            w["source_user_id"]: {
                "closed": real[w["source_user_id"]]["closed_count"],
                "pnl": real[w["source_user_id"]]["realized_pnl"],
            }
            for w in out["wallets"]
            if w.get("source_user_id") in real
        }
        out["funded_window"] = "all_time"
        return jsonify(out)

    @bp.get("/api/memecoin/promotion/signals/<source_user_id>")
    def promotion_lane_signals(source_user_id):
        from sniper.services.memecoin_chains import resolve_chain
        chain = resolve_chain(request.args.get("chain"))
        if chain is None:
            return jsonify({"error": "chain_required", "detail": "name one chain"}), 400
        try:
            days = _bounded_query_number(request.args, "days", cast=float, default=7.0,
                                         minimum=0.01, maximum=365.0)
            limit = _bounded_query_number(request.args, "limit", cast=int, default=100,
                                          minimum=1, maximum=100)
        except ValueError as exc:
            return jsonify({"error": "invalid_window", "detail": str(exc)}), 400
        since = (datetime.fromisoformat(_now_iso()) - timedelta(days=days)).isoformat()
        try:
            with m.operator_read_connection(conn) as observer:
                rows = m.promotion_lane_signals(observer, source_user_id, chain=chain,
                                                since=since, limit=limit)
        except Exception:
            logger.exception("promotion signals readout failed for chain=%s", chain)
            return jsonify({"error": "promotion_unavailable"}), 500
        return jsonify({"signals": rows, "chain": chain, "source_user_id": source_user_id,
                        "since": since, "limit": limit})

    @bp.get("/api/memecoin/positions/closed")
    def closed_positions():
        # stats still span the WHOLE closed ledger; only the display page is bounded. Fetching
        # all 22.5k rows to slice 200 off the front cost ~980ms — the aggregates read three
        # columns instead of eighty-five.
        from sniper.services.memecoin_chains import resolve_chain
        raw_chain = request.args.get("chain")
        chain = resolve_chain(raw_chain) if raw_chain else None
        if raw_chain and chain is None:
            return jsonify({"error": "unknown_chain", "detail": "chains are never pooled"}), 400
        return jsonify({
            "positions": m.list_closed_positions(conn, limit=200, chain=chain),
            "stats": m.closed_position_totals(conn, chain=chain),
            "chain": chain,
        })

    @bp.get("/api/memecoin/wallets/stats")
    def wallets_stats():
        return jsonify({"stats": m.wallet_stats(conn)})

    @bp.get("/api/memecoin/wallets/recommendations")
    def wallet_recommendations_route():
        """Exact-event paired paper evidence. Never mutates wallet approval."""
        try:
            return jsonify(_wallet_recommender.wallet_recommendations(
                conn,
                min_paired=int(request.args.get("min_paired", 5)),
                window_days=float(request.args.get("days", 30)),
                chain=request.args.get("chain") or None,
                include_inactive=request.args.get("include_inactive") in ("1", "true", "yes"),
            ))
        except Exception as e:
            logger.exception("wallet_recommendations failed")
            return jsonify({"error": str(e), "recommendations": []}), 500

    @bp.get("/api/memecoin/events/recent")
    def events_recent():
        try:
            limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        except (TypeError, ValueError):
            limit = 50
        return jsonify({"events": m.list_recent_events(conn, limit)})

    @bp.get("/api/memecoin/entries")
    def entries():
        from sniper.services.memecoin_entry_view import recent_entry_views
        try:
            limit = _bounded_query_number(request.args, "limit", cast=int, default=100,
                                          minimum=1, maximum=200)
            as_of = _now_iso()
            with m.operator_read_connection(conn) as observer:
                rows = recent_entry_views(observer, limit=limit, chain=request.args.get("chain") or None,
                                          origin_event_id=request.args.get("origin_event_id") or None)
                revision = m.entry_view_revision(observer)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _mark_kill_deploy_restarts(rows, as_of)
        return jsonify({"entries": rows, "as_of": as_of, "revision": revision})

    @bp.get("/api/memecoin/radar")
    def radar_list():
        try:
            limit = min(max(int(request.args.get("limit", 200)), 1), 500)
            rows = _radar.radar_rows(
                conn, window_hours=float(request.args.get("hours", 24)),
                chain=request.args.get("chain") or None,
                source=request.args.get("source") or None,
                rating=request.args.get("rating") or None,
                fast=request.args.get("fast") in ("1", "true", "yes"),
                phase=request.args.get("phase") or None, limit=limit)
            return jsonify({"episodes": rows})
        except Exception as e:
            logger.exception("radar_list failed")
            return jsonify({"error": str(e), "episodes": []}), 500

    @bp.get("/api/memecoin/radar/summary")
    def radar_summary_route():
        try:
            return jsonify(_radar.radar_summary(
                conn, window_hours=float(request.args.get("hours", 24)),
                chain=request.args.get("chain") or None,
                source=request.args.get("source") or None))
        except Exception as e:
            logger.exception("radar_summary failed")
            return jsonify({"error": str(e)}), 500

    @bp.get("/api/memecoin/missed-entry-report")
    def missed_entry_report_route():
        """Episode-deduped, exact-event forward grading. SELECT-only."""
        try:
            return jsonify(_missed_entries.grade_missed_entries(
                conn,
                window_hours=float(request.args.get("hours", 24)),
                chain=request.args.get("chain") or None,
                source=request.args.get("source") or None,
            ))
        except Exception as e:
            logger.exception("missed_entry_report failed")
            return jsonify({"error": str(e)}), 500

    @bp.get("/api/memecoin/latency-report")
    def latency_report_route():
        """Monotonic entry-stage/substage distributions. SELECT-only."""
        try:
            return jsonify(_entry_latency.latency_report(
                conn,
                window_hours=float(request.args.get("hours", 168)),
                chain=request.args.get("chain") or None,
            ))
        except Exception as e:
            logger.exception("latency_report failed")
            return jsonify({"error": str(e)}), 500

    @bp.get("/api/memecoin/shadow-signals")
    def shadow_signals_route():
        """Immutable post-capture self-signals. SELECT-only."""
        try:
            return jsonify({"signals": _shadow_signals.list_shadow_signals(
                conn,
                chain=request.args.get("chain") or None,
                source=request.args.get("source") or None,
                rating=request.args.get("rating") or None,
                limit=min(int(request.args.get("limit", 100)), 500),
            )})
        except Exception as e:
            logger.exception("shadow_signals failed")
            return jsonify({"error": str(e), "signals": []}), 500

    @bp.get("/api/memecoin/exit-policy-tournament")
    def exit_policy_tournament_route():
        """Exact-origin paired paper policy evidence. SELECT-only."""
        try:
            return jsonify(_exit_tournament.exit_policy_tournament(
                conn,
                window_days=float(request.args.get("days", 30)),
                chain=request.args.get("chain") or None,
                min_paired=int(request.args.get("min_paired", 30)),
            ))
        except Exception as e:
            logger.exception("exit_policy_tournament failed")
            return jsonify({"error": str(e), "segments": []}), 500

    @bp.get("/api/memecoin/radar/<network_id>/<path:mint>")
    def radar_timeline(network_id, mint):
        try:
            return jsonify(_radar.coin_timeline(
                conn, network_id, mint,
                episode_id=request.args.get("episode_id") or None))
        except Exception as e:
            logger.exception("radar_timeline failed")
            return jsonify({"error": str(e)}), 500

    @bp.get("/api/memecoin/candles")
    def candles_route():
        """OHLC over the position-mark tape, plus the fills and rail levels that explain an exit.

        Read-only, no live fetch — it serves what the sweep already recorded, so an open chart
        never competes with the funded runner's rate budget. `position_id` adds that position's
        fills, its frozen contract's rails, and BOTH peaks: the nominal tape peak and the
        executable peak the rails actually judged. On 11984 those differ by 2.2 points, which is
        the entire reason it stopped out instead of de-risking.
        """
        from sniper.services.memecoin_candles import candles_payload
        a = request.args
        chain_id = (a.get("chain_id") or "").strip()
        mint = (a.get("mint") or "").strip()
        if not chain_id or not mint:
            return jsonify({"error": "chain_id and mint are required"}), 400
        try:
            interval = _bounded_query_number(a, "interval", cast=int, default=60,
                                             minimum=1, maximum=86400)
            max_rows = _bounded_query_number(a, "max_rows", cast=int, default=2000,
                                             minimum=1, maximum=5000)
            position_id = _bounded_query_number(a, "position_id", cast=int, minimum=1)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            return jsonify(candles_payload(
                conn, chain_id=chain_id, mint=mint, interval_s=interval,
                since=(a.get("since") or "").strip() or None,
                position_id=position_id, max_rows=max_rows))
        except ValueError as exc:
            # position_id / (chain_id, mint) disagree — a caller error, not a server fault.
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("candles route failed chain=%s mint=%s", chain_id, mint)
            return jsonify({"error": "candles unavailable"}), 500

    @bp.get("/api/memecoin/universe")
    def universe_route():
        """Coin Radar watchboard: one row per coin, sortable by mcap/liq/vol/buyers/armed/age, with the
        known-BUYER overlap (which tracked/armed wallets bought it). Quotes are served from the
        background daemon quote cache (radar_quote_map) — no per-request live fetch, so the board stays
        instant and never competes with the funded runner's rate budget. Supports min_liq/min_vol/
        min_armed quality filters."""
        from sniper.services import memecoin_universe as U
        a = request.args
        try:
            window = _bounded_query_number(
                a, "window_hours", cast=float, default=72.0, minimum=1.0, maximum=720.0)
            min_liq = _bounded_query_number(
                a, "min_liq", cast=float, minimum=0.0)
            min_vol = _bounded_query_number(
                a, "min_vol", cast=float, minimum=0.0)
            min_armed = _bounded_query_number(
                a, "min_armed", cast=int, minimum=0, maximum=1000)
            limit = _bounded_query_number(
                a, "limit", cast=int, default=100, minimum=1, maximum=300)
        except ValueError as exc:
            return jsonify({"error": "invalid_query", "detail": str(exc)}), 400
        try:
            extra = m.external_discovery_rows(
                conn, window_hours=window, chain=a.get("chain") or None,
                now_iso=_now_iso(), sort=a.get("sort", "armed"),
                desc=(a.get("dir", "desc") != "asc"),
            )
        except Exception:
            extra = []
        try:
            qcache = m.radar_quote_map(conn)     # daemon-refreshed; instant, no live fetch here
        except Exception:
            qcache = {}
        rows = U.universe_rows(
            conn, sort=a.get("sort", "armed"), desc=(a.get("dir", "desc") != "asc"),
            chain=a.get("chain") or None, source=a.get("source") or None,
            min_liq=min_liq, min_vol=min_vol, min_armed=min_armed,
            q=a.get("q") or None, window_hours=window, extra_sources=extra,
            quote_cache=qcache, limit=limit, now_iso=_now_iso())
        # `qcache` above is the radar daemon's cache and that daemon has been stopped since
        # 2026-08-21, so every row arrived with liquidity/mcap/volume None and the page read as a
        # universe of worthless coins. Enrich the DISPLAYED page from one live batch, exactly as
        # /radar/triage and /coin-search already do -- bounded to `limit` rows, never the universe.
        try:
            U.enrich_live_quotes(conn, rows, dex_batch_fn=_cached_dex_batch,
                                 gt_batch_fn=lambda ks: gt_stats_batch_keyed(ks, ttl_s=15),
                                 quote_for=quote_for)
        except Exception:
            logger.exception("universe: live quote enrich failed (rows stay unpriced)")
        try:
            tmap = m.radar_triage_map(conn, now_iso=_now_iso())
            smap = m.token_social_map(conn, now_iso=_now_iso())
            for r in rows:
                mk = m.accounting_mark_key(r["network_id"], r["mint"])
                r["triage"] = tmap.get(mk)
                r["social"] = smap.get(mk)
        except Exception:
            pass
        # "Unpriced" means the row has NOTHING to show -- no price and no market cap. It is not
        # keyed on liquidity: GeckoTerminal is the only source for the DexScreener-blind 4663 chain
        # and it never returns liquidity, so a liquidity test would mark every 4663 coin unpriced
        # forever and the label would carry no information.
        unpriced = [r["mint"] for r in rows
                    if r.get("price") is None and r.get("market_cap") is None]
        return jsonify({"rows": rows, "unpriced": unpriced, "server_now": _now_iso()})

    @bp.post("/api/memecoin/radar/triage")
    def radar_triage_route():
        """Run the LLM triage over the top-N radar rows -> promising/watch/skip drafts (persisted). No
        auto-trade. No-ops with llm='unconfigured' until an XAI_API_KEY / OPENAI_API_KEY is set."""
        from sniper.services import memecoin_universe as U
        from sniper.services import memecoin_radar_triage as T
        try:
            top_n = _bounded_query_number(
                request.args, "top_n", cast=int, default=15, minimum=1, maximum=50)
        except ValueError as exc:
            return jsonify({"error": "invalid_query", "detail": str(exc)}), 400
        try:
            extra = m.external_discovery_rows(conn, window_hours=72.0, now_iso=_now_iso())
        except Exception:
            extra = []
        rows = U.universe_rows(conn, sort=request.args.get("sort", "armed"), desc=True,
                               window_hours=72.0, extra_sources=extra, limit=top_n, now_iso=_now_iso())
        try:
            U.enrich_live_quotes(conn, rows, dex_batch_fn=_cached_dex_batch,
                                 gt_batch_fn=lambda ks: gt_stats_batch_keyed(ks, ttl_s=15),
                                 quote_for=quote_for)
        except Exception:
            pass
        return jsonify(T.triage(conn, rows, top_n=top_n, now_iso=_now_iso()))

    @bp.get("/api/memecoin/coin-search")
    def coin_search_route():
        """Search any coin by name/ticker/mint across our whole history, with a live DexScreener fallback
        for coins we've never seen — the in-app Fomo-lookup replacement."""
        from sniper.services import memecoin_universe as U
        from sniper.execution.solana_quote import dexscreener_search
        try:
            limit = _bounded_query_number(
                request.args, "limit", cast=int, default=25, minimum=1, maximum=100)
        except ValueError as exc:
            return jsonify({"error": "invalid_query", "detail": str(exc)}), 400
        rows = U.search_coins(conn, request.args.get("q", ""), live_fn=dexscreener_search,
                              chain=request.args.get("chain") or None,
                              limit=limit, now_iso=_now_iso())
        try:
            U.enrich_live_quotes(conn, rows, dex_batch_fn=_cached_dex_batch,
                                 gt_batch_fn=lambda ks: gt_stats_batch_keyed(ks, ttl_s=15),
                                 quote_for=quote_for)
        except Exception:
            logger.exception("coin-search live-enrich failed")
        return jsonify({"rows": rows, "q": request.args.get("q", "")})

    @bp.get("/api/memecoin/entry-signals")
    def entry_signals_route():
        """Fresh armed-consensus entry SETUPS (>=2 armed wallets buying the same coin in-window), ranked
        for a human decision. Surfacing only — never auto-buys (naive consensus-wait entry is refuted)."""
        from sniper.services import memecoin_entry_signals as ES
        now = _now_iso()
        held = _setup_exclusion_keys(conn, now)     # open + in-flight reserved + re-entry cooldown
        try:
            qmap = m.radar_quote_map(conn)
        except Exception:
            qmap = {}
        # `hot` needs liquidity and radar_quote_map has been empty since the quote daemon stopped on
        # 2026-08-21, so every candidate scored hot=False. Price the surviving candidates live.
        setups = ES.consensus_setups(
            conn, window_min=float(request.args.get("window_min", 90)),
            min_armed=int(request.args.get("min_armed", 2)), held_keys=held, quote_map=qmap,
            limit=int(request.args.get("limit", 15)), now_iso=now,
            quote_fill_fn=_live_quote_map)
        # attach "is anyone talking about it" to the highest-intent surface. 4663 is DexScreener-blind
        # (no social source), so mark it n/a rather than showing a misleading blank == "no socials".
        try:
            smap = m.token_social_map(conn, now_iso=now)
            for s in setups:
                s["social"] = smap.get(m.accounting_mark_key(s["network_id"], s["mint"]))
                s["dex_blind"] = str(s["network_id"]) == "4663"
        except Exception:
            pass
        # A setup with UNKNOWN liquidity can never be hot, so an all-cold list means either
        # "nothing is running" or "we could not read the depth". Name which. Liquidity, not price:
        # liquidity is the input the hot gate actually reads.
        no_liquidity = [s["mint"] for s in setups if not s.get("liquidity")]
        return jsonify({"setups": setups, "no_liquidity": no_liquidity, "server_now": now})

    @bp.get("/api/memecoin/stuck-bags")
    def stuck_bags_route():
        """Stuck (untradeable/DEAD) bags + a redeploy recommendation ONLY when all slots are full and a
        hot consensus setup is waiting. Detection is automatic; the cut stays a one-click operator action."""
        from sniper.services import memecoin_stuck_bags as SB
        from sniper.services import memecoin_entry_signals as ES
        from sniper.settings import memecoin as cfg
        now = _now_iso()
        try:
            qmap = m.radar_quote_map(conn)
        except Exception:
            qmap = {}
        # The radar quote daemon has been STOPPED since 2026-08-21 (crontab, "radar resource
        # isolation"), so radar_quote_map returns {} and every position's liquidity reads unknown.
        # position_verdict only calls DEAD on a KNOWN positive-but-tiny liquidity, so the detector
        # went blind while still answering 200 with an empty list. The funded runner already writes
        # liquidity for each open real position into memecoin_position_marks (76,236 of 76,739 rows
        # in a 24h sample carry a positive value) -- read that tape instead of depending on a
        # research daemon. Bounded by sweep_ts so a stale mark can never justify a write-off, and so
        # the read stays index-served rather than walking the mint's whole history.
        try:
            mark_cut = (datetime.fromisoformat(now)
                        - timedelta(seconds=float(getattr(cfg, "MEMECOIN_STUCK_MARK_MAX_AGE_S", 600.0)))
                        ).isoformat()
            for p in conn.execute("SELECT network_id, mint FROM memecoin_positions "
                                  "WHERE execution_mode='real' AND is_open=1"):
                key = m.accounting_mark_key(p["network_id"], p["mint"])
                if (qmap.get(key) or {}).get("liquidity"):
                    continue
                row = conn.execute(
                    "SELECT mid, liquidity, mcap FROM memecoin_position_marks "
                    "WHERE chain_id=? AND mint=? AND sweep_ts >= ? AND liquidity > 0 "
                    "ORDER BY sweep_ts DESC LIMIT 1", (key[0], key[1], mark_cut)).fetchone()
                q = dict(qmap.get(key) or {})
                if row:
                    q["liquidity"] = row["liquidity"]
                    q.setdefault("price", row["mid"])
                    if row["mcap"] and not q.get("market_cap"):
                        q["market_cap"] = row["mcap"]
                else:
                    # EVM fast-lane marks never carry liquidity, and a coin with no provider pair
                    # (29 of 49 funded 4663 positions in the week to 2026-09-23) has no other mark.
                    # The impact-gate depth cache is our own pool read of the same coin, bounded by
                    # the age the funded entry gate itself accepts.
                    depth = m.pool_depth_usd(
                        conn, key[0], key[1], now_iso=now,
                        max_age_s=getattr(cfg, "MEMECOIN_POOL_DEPTH_MAX_AGE_S", 3600.0))
                    if depth:
                        q["liquidity"] = depth
                    else:
                        # A fresh whole-balance route quote proves the bag sells, depth unknown
                        # (68538 on 56, 2026-10-02: quoted every 2 s, flagged "no live liquidity").
                        # Tradeable, not priced: DEAD still needs a liquidity number.
                        route = conn.execute(
                            "SELECT sweep_ts FROM memecoin_position_marks "
                            "WHERE chain_id=? AND mint=? AND sweep_ts >= ? "
                            "AND quality='exact_route' AND mid > 0 "
                            "ORDER BY sweep_ts DESC LIMIT 1", (key[0], key[1], mark_cut)).fetchone()
                        if not route:
                            continue
                        q["exact_route_at"] = route["sweep_ts"]
                qmap[key] = q
        except Exception:
            logger.exception("stuck bags: mark-tape liquidity fallback failed (verdicts stay unknown)")
        slots = int(getattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7))
        rep = SB.stuck_bag_report(conn, quote_map=qmap, max_slots=slots, hot_setups=None, now_iso=now)
        # The report reads `hot_setups` ONLY to name the setup a full book is blocking, so a free slot
        # or an empty stuck list discards it unread. consensus_setups grades every armed wallet by
        # all-time copy-PnL -- a full scan of memecoin_fills, ~34ms -- and this endpoint is one of the
        # five parallel reads the funded cockpit's first paint waits on. It was the slowest of the
        # five, for a value the report threw away. Compute it only when it can change the answer.
        if rep["slots_free"] == 0 and rep["stuck"]:
            held = _setup_exclusion_keys(conn, now)  # same exclusion as /entry-signals (open+reserved+cooldown)
            hot = ES.consensus_setups(conn, window_min=90, min_armed=2, held_keys=held,
                                      quote_map=qmap, now_iso=now,
                                      quote_fill_fn=_live_quote_map)
            rep = SB.stuck_bag_report(conn, quote_map=qmap, max_slots=slots,
                                      hot_setups=hot, now_iso=now)
        return jsonify(rep)

    @bp.get("/api/memecoin/health")
    def health():
        # "Is the system actually entering/copying?" — runner liveness + feed activity.
        import time as _time
        from sniper.util.clock import real_utc_now
        hb = read_heartbeat(ctx.get("heartbeat_path", HEARTBEAT_PATH))
        # memecoin_events.received_at is stamped in CORRECTED real-UTC (real_utc_now_iso); the host clock
        # runs +7h, so event age/window math must use the corrected clock too — otherwise a live feed
        # reads as ~7h stale with 0 events/hr next to a green 'runner live' dot. (runner_age below stays
        # on host time.time(): the heartbeat ts is a host epoch, so both sides of that diff are host time.)
        now = datetime.fromtimestamp(real_utc_now(), tz=timezone.utc)
        cutoff = (now - timedelta(hours=1)).isoformat()
        recent_cutoff = (now - timedelta(minutes=5)).isoformat()

        runner_age = None
        if hb and hb.get("ts") is not None:
            runner_age = max(0.0, _time.time() - float(hb["ts"]))
        runner_alive = runner_age is not None and runner_age < RUNNER_STALE_S

        last_row = conn.execute(
            "SELECT received_at FROM memecoin_events ORDER BY id DESC LIMIT 1").fetchone()
        last_event_at = last_row[0] if last_row else None
        last_event_age_s = None
        if last_event_at:
            try:
                last_event_age_s = max(0.0, (now - datetime.fromisoformat(last_event_at)).total_seconds())
            except (ValueError, TypeError):
                pass

        event_rows = conn.execute(
            "SELECT lag_s, delivery_lane, created_at FROM memecoin_events WHERE received_at > ?",
            (cutoff,)).fetchall()
        events_last_hour = len(event_rows)
        # A LAG NEEDS TWO STAMPS (inherited review finding O2). The normalisers write lag_s=-1.0
        # when the source carried no timestamp (sniper/feeds/fomo_feed.py, follow_activity), and
        # that sentinel is not a measurement: it is neither a future-dated clock nor a member of
        # the lag population. Only rows with a source stamp enter either count; the rest are
        # counted apart so a reader can see how much of the hour had no source time at all.
        timed_rows = [r for r in event_rows if r[2]]
        no_source_ts_count = len(event_rows) - len(timed_rows)
        lags = [float(r[0]) for r in timed_rows
                if r[0] is not None and math.isfinite(float(r[0]))]
        future_lags = sum(lag < 0 for lag in lags)
        # WHOSE CLOCK, AND HOW MANY OF WHAT (2026-09-19). The card said "205 clocks ~13s ahead".
        # 205 was a count of EVENTS, from 37 distinct wallets, all arriving on ONE relay lane — so
        # it read as 205 broken clocks when it is one relay stamping everything it forwards. The
        # lane is the unit that can actually be wrong here; the wallets behind it are passengers.
        _lead_lanes = collections.Counter(
            str(r[1] or "unknown") for r in timed_rows
            if r[0] is not None and math.isfinite(float(r[0])) and float(r[0]) < 0)
        lead_lane = _lead_lanes.most_common(1)[0][0] if _lead_lanes else None
        recent_rows = conn.execute(
            "SELECT lag_s, source_ts FROM memecoin_feed_observations_v2 WHERE observed_at > ?",
            (recent_cutoff,),
        ).fetchall()
        events_last_5m = len(recent_rows)
        recent_timed = [r for r in recent_rows if r[1]]         # same rule as the hourly set
        recent_no_source_ts_count = len(recent_rows) - len(recent_timed)
        recent_lags = [float(r[0]) for r in recent_timed
                       if r[0] is not None and math.isfinite(float(r[0]))]
        recent_future_lags = sum(lag < 0 for lag in recent_lags)
        # ONE BAD CLOCK IS NOT AN UNTRUSTED FEED (2026-09-17). Any single future-dated sample used
        # to void the whole reading: the cockpit said "source clock untrusted" while the median lag
        # was 0.30 s over 2,771 samples, 10 of them (0.36%) future-dated. The median is now taken
        # over the samples whose clock is sane, the future-dated ones are counted and reported, and
        # only a share above MEMECOIN_FEED_LAG_FUTURE_SHARE_MAX suppresses the number again.
        from sniper.settings import memecoin as _feed_cfg
        future_share_max = float(getattr(
            _feed_cfg, "MEMECOIN_FEED_LAG_FUTURE_SHARE_MAX", 0.05) or 0.0)
        future_min_count = int(getattr(_feed_cfg, "MEMECOIN_FEED_LAG_FUTURE_MIN_COUNT", 5) or 0)
        recent_future_share = (recent_future_lags / len(recent_lags)) if recent_lags else 0.0
        hour_future_share = (future_lags / len(lags)) if lags else 0.0

        def _clock_untrusted(future_count, share):
            """A share alone cannot condemn the feed: a quiet 5-minute window holds ~16 events, so
            two bad clocks read as 12.5% while the base rate over 2,790 samples is 0.43%."""
            return future_count >= future_min_count and share > future_share_max

        feed_lag_status = ("missing" if not recent_lags else
                           "untrusted_source_clock"
                           if _clock_untrusted(recent_future_lags, recent_future_share) else "ok")

        def _clock_lead_s(sample):
            """How far ahead the LEADING clocks run, estimated from those samples alone.

            CORRECTED 2026-09-18, hours after the first attempt. That version assumed ONE global
            offset shared by every source, took the 5th percentile of all samples as the offset and
            subtracted it from the median. The feed is a MIXTURE, not a shifted distribution: of
            1,489 samples in one hour, 1,279 were sane with a median of +0.19 s and 210 were
            future-dated with a median of -12.4 s. Subtracting a tail estimate from a median that
            was never offset produced "source clock 19.0s ahead, lag ~19.23s" on the operator's
            screen — two numbers that cannot both be true, which is how the error announced itself.

            A future-dated sample is the only one that PROVES a bad clock: a delivery delay cannot
            be negative, so the event cannot have been stamped after we received it. Their median is
            the lead of that subgroup. It says nothing about the sources whose clocks work.
            """
            leading = [lag for lag in sample if lag < 0]
            return abs(statistics.median(leading)) if len(leading) >= 5 else None

        clock_lead_s = (_clock_lead_s(lags)
                        if feed_lag_status == "untrusted_source_clock" else None)
        sane_recent = [lag for lag in recent_lags if lag >= 0]
        sane_hour = [lag for lag in lags if lag >= 0]
        startup_backlog_count = None
        startup_backlog_complete = None
        started_at = hb.get("started_at") if hb else None
        if started_at:
            try:
                startup_start = datetime.fromisoformat(started_at)
                if startup_start.tzinfo is None:
                    raise ValueError("runner started_at must include a timezone")
                startup_end_dt = startup_start + timedelta(minutes=5)
                startup_backlog_complete = now >= startup_end_dt
                if startup_backlog_complete:
                    startup_end = startup_end_dt.isoformat()
                    if started_at not in startup_backlog_cache:
                        startup_backlog_cache[started_at] = conn.execute(
                            "SELECT COUNT(*) FROM memecoin_feed_observations_v2 "
                            "WHERE source_ts < ? AND observed_at >= ? AND observed_at < ?",
                            (started_at, started_at, startup_end),
                        ).fetchone()[0]
                    startup_backlog_count = startup_backlog_cache[started_at]
            except (TypeError, ValueError):
                pass
        open_count = conn.execute(   # paper-scoped — the strip labels this 'paper'; real is in /real/status
            "SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1 AND execution_mode='paper'").fetchone()[0]

        return jsonify({
            "runner_alive": runner_alive,
            "runner_age_s": runner_age,
            "last_event_at": last_event_at,
            "last_event_age_s": last_event_age_s,
            "events_last_hour": events_last_hour,
            "events_last_5m": events_last_5m,
            "open_count": open_count,
            "curated_count": len(m.curated_user_ids(conn)),
            "feed_lag_status": feed_lag_status,
            "feed_lag_sample_count": len(lags),
            "feed_lag_future_count": future_lags,
            "feed_lag_no_source_ts_count": no_source_ts_count,
            "feed_lag_future_share": hour_future_share,
            # KEPT EVEN WHEN THE VERDICT IS UNTRUSTED (2026-09-18). Suppressing it left the
            # operator with no number at all while 86% of the samples were perfectly good. It is
            # the median over the SANE samples and the future-dated count rides beside it.
            "feed_lag_median_s": (statistics.median(sane_hour) if sane_hour else None),
            "feed_lag_5m_sample_count": len(recent_lags),
            "feed_lag_5m_future_count": recent_future_lags,
            "feed_lag_5m_no_source_ts_count": recent_no_source_ts_count,
            "feed_lag_5m_future_share": recent_future_share,
            "feed_lag_5m_median_s": (statistics.median(sane_recent)
                                     if sane_recent and feed_lag_status == "ok" else None),
            # How far the LEADING clocks run ahead, and how many of them there are. The lag
            # itself is `feed_lag_median_s` over the samples whose clock is sane — a leading clock
            # in one source says nothing about the delivery time of the others, so there is no
            # "corrected" median to report.
            "feed_lag_clock_lead_s": clock_lead_s,
            # The lane carrying the future-dated stamps, and the sample sizes the card needs to say
            # "N of M events" rather than implying N independent broken clocks.
            "feed_lag_lead_lane": lead_lane if clock_lead_s is not None else None,
            "startup_backlog_count": startup_backlog_count,
            "startup_backlog_complete": startup_backlog_complete,
            "runner_started_at": started_at,
            "auth_ok": bool(hb.get("auth_ok")) if hb else None,
            "source_health": _source_health(hb),
            "fastlane": hb.get("fastlane") if hb else None,
            "server_now": _now_iso(),   # corrected real-UTC — client diffs ages against THIS,
                                        # not its own (host-skewed +7h) Date.now().
        })

    def _wallet_address(load_fn, addr_fn, path):
        # cache by (path, mtime) — the address never changes for a given keypair, and /real/status is
        # polled ~every 4s; without this it would load the keypair (disk + solders/eth_account) each call.
        import os as _os
        if not (path and _os.path.exists(path)):
            return None, False
        try:
            key = (path, _os.path.getmtime(path))
            if key not in _ADDR_CACHE:
                _ADDR_CACHE[key] = addr_fn(load_fn(path))
            return _ADDR_CACHE[key], True
        except Exception:
            return None, True   # present but unreadable -> flag it

    @bp.get("/api/memecoin/real/status")
    def real_status():
        """The real-money cockpit state — cheap DB + config reads (no live RPC on the poll). Arm state,
        kill switch, daily-loss breaker, envelope, reservations, real exposure, route drops, and
        per-venue readiness (config + wallet address). Live balances only when ?balances=1 (on demand)."""
        from sniper.settings import memecoin as cfg
        from sniper.services.memecoin_arming import kill_switch_status
        now_real = _now_iso()
        day = now_real[:10]
        snap = m.real_open_snapshot(conn)

        venues = {}
        from sniper.execution.solana_wallet import load_keypair as _sol_load, pubkey_str as _sol_addr
        s_addr, s_present = _wallet_address(_sol_load, _sol_addr, cfg.MEMECOIN_SOLANA_KEYPAIR)
        venues["solana"] = {"rpc_set": bool(cfg.MEMECOIN_SOLANA_RPC_URL), "keypair_present": s_present,
                            "address": s_addr, "balances": None,
                            "configured": bool(cfg.MEMECOIN_SOLANA_RPC_URL) and s_present}
        from sniper.execution.evm_wallet import load_keypair as _evm_load, address_str as _evm_addr
        for chain in sorted(cfg.MEMECOIN_EVM_ALLOWLIST):
            rpc = cfg.evm_rpc_for_chain(chain)
            e_addr, e_present = _wallet_address(_evm_load, _evm_addr, cfg.MEMECOIN_EVM_KEYPAIR)
            execution_venue = cfg.memecoin_evm_exec_venue(chain)
            direct_configured = (execution_venue == "direct"
                                 and bool(cfg.memecoin_evm_v3_addresses(chain)))
            venues[chain] = {"rpc_set": bool(rpc), "keypair_present": e_present, "address": e_addr,
                            "balances": None, "execution_venue": execution_venue,
                            "direct_configured": direct_configured,
                            "configured": bool(rpc) and e_present and direct_configured}

        if request.args.get("balances") == "1":   # ON DEMAND only — never on the 4s poll
            _fill_live_balances(venues, cfg)

        # runner liveness in the SAME payload (was only on /health — the UI's two polls raced
        # and the arm pill could show LIVE against a dead runner for one cycle).
        _hb = read_heartbeat(ctx.get("heartbeat_path", HEARTBEAT_PATH))
        _hb_age = (max(0.0, __import__("time").time() - float(_hb["ts"]))
                   if _hb and _hb.get("ts") is not None else None)
        _lifi = dict(_hb.get("lifi")) if _hb and isinstance(_hb.get("lifi"), dict) else None
        if _lifi is not None:
            try:
                _lifi_at = datetime.fromisoformat(
                    str(_lifi.get("corrected_at") or "").replace("Z", "+00:00"))
                _lifi["stale"] = max(
                    0.0, (datetime.fromisoformat(now_real) - _lifi_at).total_seconds()
                ) >= RUNNER_STALE_S
            except (TypeError, ValueError):
                _lifi["stale"] = True
        # The strip must show what the runner ENFORCES. The runner applies entry-knob overrides
        # from the ledger; raw settings here showed "follow <=86,400s" beside an effective 60 s.
        try:
            from sniper.services.memecoin_entry_knobs import effective as _knob_effective
            # Only an OVERRIDE replaces the live setting; effective()'s own default can differ
            # from the process value (tests/test_memecoin_real_knobs.py caught 30M vs 12.3M).
            _knob_values = {n: v for n, (v, _src) in _knob_effective(conn, cfg).items()
                            if _src == "override"}
        except Exception:
            _knob_values = {}

        def _eff_knob(name, fallback):
            return _knob_values.get(name, fallback)

        daily_limit = _eff_knob("MEMECOIN_DAILY_LOSS_LIMIT_USD", cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD)
        pause_until = m.drawdown_pause_until(
            conn, day,
            _eff_knob("MEMECOIN_DRAWDOWN_PAUSE_USD", cfg.MEMECOIN_DRAWDOWN_PAUSE_USD),
            _eff_knob("MEMECOIN_DRAWDOWN_PAUSE_S", cfg.MEMECOIN_DRAWDOWN_PAUSE_S), now_real)
        return jsonify({
            "armed": bool(cfg.MEMECOIN_REAL_ENABLED),
            "runner_alive": _hb_age is not None and _hb_age < RUNNER_STALE_S,
            "release_drift": _release_drift(_hb),
            "fastlane": _hb.get("fastlane") if _hb else None,
            "lifi": _lifi,
            **kill_switch_status(),
            # THE BUILD THE SERVER SERVES (2026-09-17). A page left open since before a deploy keeps
            # its old JS: such a page sent no kill-switch token, and five Clear clicks were rejected
            # with nothing on screen saying why. The page compares this with its own build.
            "ui_build": _ui_build(),
            # A LATCH IS A HISTORICAL FACT; "tripped" IS THE EFFECTIVE STATE. A row in
            # memecoin_daily_loss_trips can outlive the operator switching the breaker off, and
            # "tripped" above correctly stops reading it as live once the limit is 0 -- but that
            # also made the latch itself invisible, so a stale trip from earlier today disappeared
            # from the cockpit entirely instead of showing as correctly un-flagged (Alpha, B2).
            "daily": {"tripped": daily_limit > 0 and m.is_daily_loss_tripped(conn, day),
                      "latched_today": m.is_daily_loss_tripped(conn, day),
                      "realized_today": m.daily_real_realized_pnl(conn, day),
                      "risk_realized_since_reset": m.daily_risk_realized_pnl(conn, day),
                      "risk_since": m.risk_day_since(day),
                      "drawdown_pause_until": pause_until,
                      "pause_reason": "drawdown" if pause_until else None,
                      "limit": daily_limit},
            "envelope": {"trade_size": cfg.MEMECOIN_TRADE_SIZE_USD,
                         "max_concurrent": cfg.MEMECOIN_MAX_CONCURRENT_TOTAL,
                         "max_concurrent_by_chain": {
                             network: cfg.max_concurrent_for(network)
                             for network in ["solana"] + sorted(cfg.MEMECOIN_EVM_ALLOWLIST)},
                         "max_exposure": cfg.MEMECOIN_MAX_TOTAL_EXPOSURE_USD,
                         "policy": cfg.MEMECOIN_REAL_POLICY,
                         "slot_scope": "global_cross_venue",
                         "exposure_scope": "global_cross_venue",
                         "balance_scope": "chain_local",
                         "gas_scope": "chain_local"},
            # WS4: the live risk knobs the runner enforces, read from settings AT REQUEST TIME so a
            # .env change (+ runner restart) is legible on the cockpit instead of hard-coded in the UI.
            "knobs": {
                "mcap_ceiling_usd": _eff_knob("MEMECOIN_REAL_MAX_MCAP_USD", cfg.MEMECOIN_REAL_MAX_MCAP_USD),
                # AUTO_TEMPLATES on => coins >= scalp_mcap_usd are NO LONGER refused by the ceiling;
                # they OPEN as scalps (tp/stop below). off => the ceiling refuses them.
                "auto_templates": cfg.MEMECOIN_AUTO_TEMPLATES,
                "scalp_mcap_usd": cfg.MEMECOIN_SCALP_MCAP_USD,
                "scalp_tp_pct": cfg.MEMECOIN_SCALP_TP_PCT,
                "scalp_stop_pct": cfg.MEMECOIN_SCALP_STOP_PCT,
                "impact_cap_base_pct": _eff_knob("MEMECOIN_ENTRY_IMPACT_CAP_PCT", cfg.MEMECOIN_ENTRY_IMPACT_CAP_PCT),
                "impact_cap_tiers": cfg.MEMECOIN_ENTRY_IMPACT_CAP_TIERS,
                # The pre-flight impact cap is Solana-only; direct EVM quotes enforce their configured
                # amountOutMinimum instead. The UI must not imply the cap protects every chain.
                "impact_cap_scope": "solana_only",
                "chase_max_pct": _eff_knob("MEMECOIN_REAL_CHASE_MAX_PCT", cfg.MEMECOIN_REAL_CHASE_MAX_PCT),
                "entry_max_age_s": _eff_knob("MEMECOIN_REAL_ENTRY_MAX_AGE_S", cfg.MEMECOIN_REAL_ENTRY_MAX_AGE_S),
                "follow_max_age_s": _eff_knob("MEMECOIN_REAL_FOLLOW_MAX_AGE_S", cfg.MEMECOIN_REAL_FOLLOW_MAX_AGE_S),
                "stop_confirm_sweeps": cfg.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS,
                "demote_loss_usd": cfg.MEMECOIN_REAL_DEMOTE_LOSS_USD,
                "demote_consec": cfg.MEMECOIN_REAL_DEMOTE_CONSEC,
                "unquotable_exit_s": cfg.MEMECOIN_REAL_UNQUOTABLE_EXIT_S,
                "sizing": cfg.MEMECOIN_REAL_SIZING,
                "bankroll_fraction": cfg.MEMECOIN_BANKROLL_FRACTION,
                "min_trade_usd": cfg.MEMECOIN_MIN_TRADE_USD,
                "tp_pct": cfg.MEMECOIN_REAL_TP_PCT,
                "derisk_pct": cfg.MEMECOIN_REAL_DERISK_PCT,
                "stop_pct": cfg.MEMECOIN_REAL_STOP_PCT,
                "max_hold_s": cfg.MEMECOIN_REAL_MAX_HOLD_S,
                "policy": cfg.MEMECOIN_REAL_POLICY,
                "gas_reserve": {
                    "formula": "GAS_ENTRY_SOL + (open+1)·GAS_EXIT_SOL·GAS_RETRY_FACTOR (per-venue, Solana)",
                    "entry_sol": cfg.MEMECOIN_GAS_ENTRY_SOL,
                    "exit_sol": cfg.MEMECOIN_GAS_EXIT_SOL,
                    "retry_factor": cfg.MEMECOIN_GAS_RETRY_FACTOR,
                },
            },
            "reservations": m.pending_summary(conn),
            "real_open": {"count": snap["count"], "notional": snap["notional"],
                          "by_network": {net: {"count": s["count"], "notional": s["notional"]}
                                         for net, s in ((n, m.real_open_snapshot(conn, network=n))
                                                        for n in (["solana"] + sorted(cfg.MEMECOIN_EVM_ALLOWLIST)))}},
            # network_drop_counts is keyed by (network, reason) tuples -> stringify for JSON/UI.
            "route_drops": {f"{net}:{reason}": n
                            for (net, reason), n in m.network_drop_counts(conn).items()},
            "venues": venues,
            # Sanitized free-direct submission status: per-provider health + attempt p50/p95/counts +
            # sidecar leader/restart. DB + last-snapshot reads only — NO live RPC on the poll.
            "submission": _submission_status_safe(),
            "csrf_token": _csrf_token(),
            "server_now": now_real,
        })

    def _submission_status_safe():
        try:
            from sniper.services.memecoin_submission_status import submission_status
            from sniper.util.state_paths import state_root
            return submission_status(conn, str(state_root()))
        except Exception:
            return {"providers": {}, "sidecars": []}

    def _fill_live_balances(venues, cfg):
        try:
            from sniper.execution.solana_wallet import sol_balance, usdc_balance
            v = venues.get("solana")
            if v and v.get("configured") and v.get("address"):
                v["balances"] = {"native": sol_balance(v["address"], cfg.MEMECOIN_SOLANA_RPC_URL),
                                 "cash_usd": usdc_balance(v["address"], cfg.MEMECOIN_SOLANA_RPC_URL),
                                 "quote_symbol": "USDC"}
        except Exception:
            pass
        try:
            from sniper.execution import evm_wallet as W
            for chain, v in venues.items():
                if chain == "solana" or not (v.get("configured") and v.get("address")):
                    continue
                rpc = cfg.evm_rpc_for_chain(chain)
                # the chain's QUOTE token, not USDC: 4663 holds its bankroll in WETH and has zero
                # USDC, so a usdc_balance() read there reports a funded wallet as $0 (2026-07-14).
                v["balances"] = {"native": W.native_balance(v["address"], rpc),
                                 "cash_usd": W.quote_balance_usd_or_none(v["address"], chain, rpc),
                                 "quote_symbol": W.quote_symbol(chain)}
        except Exception:
            pass

    @bp.get("/api/memecoin/marks/<chain>/<mint>")
    def token_marks(chain, mint):
        """Local price-mark series from ``memecoin_position_marks``.

        This is the chart for DexScreener-blind chains such as Robinhood 4663. These are provider
        price marks, not candles or traded volume; each point keeps its persisted source. Empty is
        an honest 'collecting marks' state, never an error. Quarantined spikes are excluded."""
        MAX_POINTS = 600
        # A coin we HOLD charts only our own marks (operator via Alpha, 2026-10-08): the tape is keyed
        # (chain, mint), so a paper twin's provider rows would otherwise draw on the funded card.
        held_real = conn.execute(
            "SELECT 1 FROM memecoin_positions WHERE network_id=? AND mint=? "
            "AND execution_mode='real' AND is_open=1 LIMIT 1", (str(chain), str(mint))).fetchone()
        rows = conn.execute(
            "SELECT sweep_ts, mid, mcap, source, quality FROM memecoin_position_marks "
            "WHERE chain_id=? AND mint=? AND mid>0 "
            "AND COALESCE(quality,'') NOT IN ('quarantined_spike') "
            + ("AND " + _own_sources_sql() + " " if held_real else "") +
            "ORDER BY sweep_ts DESC LIMIT ?",
            (str(chain), str(mint), MAX_POINTS)).fetchall()
        rows = list(reversed(rows))   # oldest -> newest, capped to the most recent MAX_POINTS
        pts = [{"t": r["sweep_ts"], "mid": r["mid"], "mcap": r["mcap"],
                "source": r["source"], "quality": r["quality"]} for r in rows]
        # A HELD 4663 COIN charts our own swap tape (operator 2026-09-28) in place of the ~15 s
        # provider mids; the exact route quotes stay as their own line. No tape: as before.
        if str(chain) == "4663":
            from sniper.services.memecoin_evm_tape_marks import tape_series
            held = conn.execute(
                "SELECT id FROM memecoin_positions WHERE network_id=? AND mint=? "
                "AND execution_mode='real' AND is_open=1 ORDER BY id DESC LIMIT 1",
                (str(chain), str(mint))).fetchone()
            tape = tape_series(conn, held["id"], max_points=MAX_POINTS) if held else []
            if len(tape) >= 2:
                # These points come from the verified swap-tape producer, not a source-name guess.
                pts = [pt for pt, r in zip(pts, rows) if r["quality"] == "exact_route"] + [
                    {**pt, "quality": "live"} for pt in tape]
        last = pts[-1]["mid"] if pts else None
        first = pts[0]["mid"] if pts else None
        change = ((last / first - 1.0) * 100.0) if (last and first and first > 0) else None
        return jsonify({
            "mint": mint, "chain": str(chain),
            "source": (pts[-1]["source"] if pts else None),
            "points": pts, "n": len(pts), "last": last,
            "mcap": (pts[-1]["mcap"] if pts else None),
            "lo": (min(p["mid"] for p in pts) if pts else None),
            "hi": (max(p["mid"] for p in pts) if pts else None),
            "change_pct": change,
        })

    @bp.get("/api/memecoin/real/ticker")
    def real_ticker():
        """Compact 1s operator ticker from the runner's local mark tape only."""
        with m.operator_read_connection(conn) as observer:
            payload = _real_ticker_payload(observer, _now_iso())
        return jsonify(payload)

    @bp.get("/api/memecoin/real/stream")
    def real_stream():
        """SSE push for the funded cockpit — replaces the browser's 1s ticker poll and turns the 4s
        cockpit poll change-driven. One idle tab held ~100 requests/min against the dev server;
        this holds one connection and pushes only when something changed.

        Three event types:
          ticker   — the full /real/ticker payload, at most 1/s, only when it changed.
          cockpit  — an empty change signal; the client refetches through its EXISTING code path,
                     so payload shapes and render behavior cannot drift from the poll path.
          activity — an empty change signal, pushed the moment the runner's feed snapshot file
                     changes. The Activity table refetches through its EXISTING code path, so the
                     payload shape cannot drift from the poll path.

        The generator runs on its own WAL-safe read-only connection (never the blueprint's shared
        one — this loop outlives any request). The client falls back to plain polling the moment
        the stream errors, so a dead stream degrades to exactly today's behavior."""
        from sniper.persistence.db_core import get_readonly_connection

        from sniper.services.memecoin_runner import FEED_SNAPSHOT_PATH

        @stream_with_context
        def generate():
            sc = None
            last_tick = last_rev = None
            last_feed_mtime = None
            cockpit_accum = 0.0
            last_keepalive = _time.monotonic()
            last_heartbeat = None
            try:
                sc = get_readonly_connection()
                while True:
                    now_real = _now_iso()
                    try:
                        # TICKER at the FAST interval: the price a position shows updates as fast as
                        # a new pool-state mark lands (~400ms), not on a 1s clock. Only pushed when
                        # the payload changed, so a quiet pool costs one keepalive, not a frame/tick.
                        tick = _real_ticker_payload(sc, now_real)
                        blob = _ticker_change_blob(tick)
                        if blob != last_tick:
                            yield ("event: ticker\ndata: "
                                   + json.dumps(tick, separators=(",", ":")) + "\n\n")
                            last_tick = blob
                            last_keepalive = _time.monotonic()
                        # HEARTBEAT, only after a tick that SUCCEEDED (2026-09-26). An OPEN socket
                        # is not a live stream: a caught SQLite error keeps it open with no data, and
                        # the ': keepalive' comment never reaches JavaScript. The browser treats a
                        # stream silent past its deadline as dead and resumes polling.
                        if (last_heartbeat is None
                                or _time.monotonic() - last_heartbeat >= _STREAM_HEARTBEAT_S):
                            yield ("event: heartbeat\ndata: "
                                   + json.dumps({"server_time": now_real}) + "\n\n")
                            last_heartbeat = last_keepalive = _time.monotonic()
                        # COCKPIT signature is heavier (counts, kill switch) and its drivers change
                        # on human timescales — check it about once a second, not every fast tick.
                        # ACTIVITY at the FAST interval: an os.stat is ~microseconds, and the
                        # whole point of the tape is beating fomo's 15s client poll — a 1s gate
                        # here would give back most of the win. Read + push only on mtime change.
                        try:
                            feed_mtime = os.stat(FEED_SNAPSHOT_PATH).st_mtime_ns
                        except OSError:
                            feed_mtime = None
                        if feed_mtime is not None and feed_mtime != last_feed_mtime:
                            # Change SIGNAL only, like `cockpit`. The client refetches the events
                            # table through its own code path and never read this payload, so
                            # shipping the whole snapshot re-read and re-serialized the feed every
                            # 1-2s for every open tab to be discarded on arrival.
                            yield "event: activity\ndata: {}\n\n"
                            last_keepalive = _time.monotonic()
                            last_feed_mtime = feed_mtime
                        cockpit_accum += _STREAM_TICK_S
                        if cockpit_accum >= 1.0:
                            cockpit_accum = 0.0
                            rev = _cockpit_change_signature(sc)
                            # FILL events for the operator's entry/exit notifications. First pass
                            # is baseline — history is never replayed to a fresh connection.
                            if last_rev is not None:
                                for fill in _fill_events(sc, last_rev[0], rev[0]):
                                    yield ("event: fill\ndata: "
                                           + json.dumps(fill, separators=(",", ":")) + "\n\n")
                                    last_keepalive = _time.monotonic()
                            if rev != last_rev:
                                if last_rev is not None:  # first pass is baseline, not a change
                                    yield "event: cockpit\ndata: {}\n\n"
                                    last_keepalive = _time.monotonic()
                                last_rev = rev
                    except sqlite3.Error:
                        logger.exception("real stream tick failed; stream stays up")
                    _time.sleep(_STREAM_TICK_S)
                    if _time.monotonic() - last_keepalive >= 15.0:
                        yield ": keepalive\n\n"
                        last_keepalive = _time.monotonic()
            except GeneratorExit:
                return
            finally:
                if sc is not None:
                    sc.close()

        return Response(generate(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @bp.get("/api/memecoin/real/positions")
    def real_positions():
        now_real = _now_iso()
        with m.operator_read_connection(conn) as observer:
            with m.operator_read_snapshot(observer):
                return _real_positions_read(observer, now_real)

    def _real_positions_read(conn, now_real):
        """Real-money open positions (mode-scoped — never mixed with paper). Venue, entry, MTM from the
        last live quote, hold vs the 12h timecap, and the stuck-bag flag for the operator's exit board."""
        from sniper.settings import memecoin as cfg
        out = []
        positions = m.list_open_positions(conn, mode="real")
        active_positions = [p for p in positions if not p.get("tracking_paused_at")]
        shared_prices = _real_ticker_payload(conn, now_real)
        price_by_id = {row["id"]: row for row in shared_prices["positions"]}
        # NO EXTERNAL PRICE CALLS ON THIS REQUEST (operator, 2026-08-28). This endpoint used to hit
        # DexScreener, and GeckoTerminal for 4663, on every poll — for a PRICE the runner has
        # already decided and stored. Those calls added provider latency and a single-flight
        # contention with the funded runner to a read that had a fresher, more authoritative number
        # in the ledger the whole time. They were added when the stored quote came from a 15s sweep
        # and the operator complained the price lagged; the mark tape now lands inside a second, so
        # the reason has expired.
        #
        # Market cap and liquidity ENRICHMENT still wants live data, but it is not the price and it
        # must not be on this path — it moves to the slow enrichment lane below, served from the
        # mark tape here and refreshed out of band.
        live = {}
        # DexScreener indexes NO pair for chain 4663 — so `live` is empty for every 4663 row. Price
        # those per-request from LIVE GeckoTerminal (batched + TTL-cached, so a 4s poll shares one
        # fetch and never rate-limits), and keep the ~63s mark tape only as the fallback when GT
        # misses. Before this the 4663 "current price" lagged a full minute (mark cadence). GT gives
        # no liquidity, so the slippage haircut still uses the last mark's liquidity. (2026-07-18)
        unpriced = [(p.get("network_id") or "solana", p["mint"]) for p in active_positions
                    if not (quote_for(live, p["mint"], p.get("network_id")) or {}).get("price")]
        # GeckoTerminal removed from this path for the same reason. Its own comment already said
        # the real fix was the runner keeping the 4663 mark tape fresh rather than the display
        # fetching live — and it noted the fetch contends with the funded runner on a single-flight
        # gate for ~0.5s per poll. The mark tape and the runner's stored quote now serve it.
        gt_live = {}
        marks = {}
        try:
            for chain_id, mint in set(unpriced):
                row = conn.execute(
                    "SELECT mid, liquidity, mcap, sweep_ts FROM memecoin_position_marks "
                    "WHERE chain_id=? AND mint=? "
                    "AND COALESCE(source,'')!='fastlane_sol_state' AND " + _own_sources_sql() +
                    " ORDER BY sweep_ts DESC LIMIT 1",
                    (chain_id, mint)).fetchone()
                if row and row["mid"]:
                    marks[(chain_id, mint)] = row
        except Exception:
            logger.exception("real positions: mark fallback failed (live quotes still served)")

        mark_writes = {}     # accrue a chart-mark for each priced position (written after the loop)
        open_names = _coin_names(conn, _unnamed_keys(positions))
        try:
            _smap = m.token_social_map(conn, now_iso=_now_iso())
        except Exception:
            _smap = {}
        try:
            source_sells = m.latest_mirror_decisions(conn, [p["id"] for p in active_positions])
        except Exception:
            logger.exception("real positions: source-sell read failed (cards still served)")
            source_sells = {}
        # Anchor-checked EVM exit (spec 2026-10-01 §3.2): a held sell shows on its row with the
        # "sell at any price" button. One read for the board; a hold older than an episode is over.
        # Fill timing (2026-10-02): "Landed S+n · why" per open position, one batch (the timing and
        # lane files are opened once per poll, read-only; never raises).
        from sniper.services.memecoin_fill_timing import view_lines as _fill_view_lines
        _fill_ids = [p.get("attempt_id") for p in positions if p.get("attempt_id")]
        _fill_lines = _fill_view_lines(conn, [dict(r) for r in conn.execute(
            "SELECT * FROM memecoin_entry_attempts WHERE attempt_id IN (%s)" % ",".join("?" * len(_fill_ids)),
            _fill_ids)]) if _fill_ids else {}
        from sniper.services.memecoin_exit_anchor import EPISODE_GAP_S
        _anchor_holds = m.open_exit_anchor_holds(conn, since_iso=(
            datetime.fromisoformat(str(now_real).replace("Z", "+00:00"))
            - timedelta(seconds=EPISODE_GAP_S)).isoformat())
        for p in positions:
            if p.get("tracking_paused_at"):
                # Long Hold is still funded inventory. Keep it visible without reusing an old
                # quote as a live price or running per-position price enrichment on each poll.
                out.append({"id": p["id"], "mint": p["mint"],
                            "ticker": p.get("ticker"), "network_id": p.get("network_id") or "solana",
                            "venue": "EVM" if str(p["mint"]).startswith("0x") else "SOL",
                            "entry_notional": p.get("entry_notional"),
                            "tracking_paused_at": p["tracking_paused_at"],
                            "market_mid_price": None, "selected_exit_price": None})
                continue
            entry = p.get("entry_price") or 0.0
            q = quote_for(live, p["mint"], p.get("network_id")) or {}
            # OBSERVATION time of the mid we are about to serve, tracked per source so the client
            # can age the number it actually shows. NEVER `last_quote_ts`: that column is stamped
            # with the exit sweep's own `now_iso` (update_position_last_quote), so it reads ~0s no
            # matter how old the price is — the card asserted a 0.9s age over a 45.7s quote.
            # `now_real` for the two LIVE fetches is honest to their display caches (DexScreener
            # batch <=1s, GeckoTerminal <=12s); the mark tape carries its own sweep_ts.
            mid_observed_at = now_real if q.get("price") else None
            mid_from_live = bool(q.get("price"))
            if not q.get("price"):
                key = (p.get("network_id") or "solana", p["mint"])
                mk = marks.get(key)
                gt = gt_live.get(key)
                if gt and gt.get("price"):
                    # LIVE GT price (fresh) + last mark's liquidity for the slippage haircut. GT also
                    # carries 24h volume for Robinhood — surface it (was hardcoded None).
                    q = {"price": gt["price"], "liquidity": (mk["liquidity"] if mk else None),
                         "market_cap": gt.get("mcap"), "volume_24h": gt.get("volume_24h")}
                    mid_observed_at = now_real
                    mid_from_live = True
                elif mk:
                    q = {"price": mk["mid"], "liquidity": mk["liquidity"], "market_cap": mk["mcap"],
                         "volume_24h": None}   # local mark tape has no 24h volume — leave unknown
                    mid_observed_at = mk["sweep_ts"]
            lv = q.get("price")
            mid = lv or p.get("last_quote_price")
            # mcap fallback: a 4663 bag whose GT/mark mcap is momentarily null still shows a market cap,
            # derived from its FROZEN entry ratio (circulating supply ~ entry_mcap/entry_price is ~const
            # for these tokens — the same derivation the exit sweep uses). Fixes WOLVES-class blank mcap.
            if not q.get("market_cap"):
                e_mcap = p.get("entry_mcap") or 0.0
                if mid and e_mcap > 0 and entry > 0:
                    q["market_cap"] = mid * (e_mcap / entry)
                elif mid and entry > 0:
                    # entry_mcap is NULL on 52% of 4663 real positions (37/71 measured 2026-08-13):
                    # the funded lane enters on a coin's FIRST signal, so when that event carried no
                    # market cap there is no same-mint history for resolve_mint_mcap to fall back
                    # to, and the ratio derivation above cannot fire either. Recover the supply from
                    # the feed instead — any priced event for this mint gives mcap/price, which is
                    # the same constant the frozen ratio would have provided.
                    supply = m.resolve_mint_supply(
                        conn, p["mint"], network_id=p.get("network_id"))
                    if supply:
                        q["market_cap"] = mid * supply
            # GT POOL LIQUIDITY REMOVED FROM THIS PATH (operator, 2026-08-28). It was the last
            # provider call reachable from /real/positions — config-disabled
            # (MEMECOIN_GT_POOL_LIQUIDITY=0), so dead in practice, but a false contract: the
            # endpoint claimed to make no external call while holding a branch that could.
            # My own test missed it by naming only two provider functions, which is a guard
            # written against the fix rather than against the property.
            #
            # Nothing is lost today because the flag is off, and the runner-persisted fallback
            # immediately below is the intended source anyway — the runner captures GT's
            # reserve_in_usd from pool discovery it already performs. If 4663 liquidity needs
            # live refresh again it belongs on the slow enrichment lane, not on a 4s poll.
            # RUNNER-PERSISTED liquidity fallback: the runner captures GT's reserve_in_usd (from the
            # pool discovery it already makes) into memecoin_token_liquidity — the reliable 4663 source
            # the rate-limited web can't fetch itself. None until the runner-write deploys -> honest '—'.
            if q.get("liquidity") is None:
                q["liquidity"] = m.get_token_liquidity(
                    conn, p.get("network_id") or "solana", p["mint"], now_iso=now_real)
            # NET-OF-EXIT-SLIPPAGE mark (matches the accounting panel): haircut the mid by the
            # modeled exit impact where liquidity is known, so Now/Value/PnL reflect what the bag
            # could be SOLD for, not the optimistic mid. Unknown liquidity -> keep the mid.
            liq = q.get("liquidity")
            qty = m.open_qty(conn, p["id"])
            # MEASURED beats MODELED. `modeled_fill` is a worst-case estimate whose slippage
            # saturates its 50% cap the moment notional exceeds liquidity, so on any thin 4663 bag
            # it prints exactly mid*(1-cap) regardless of the real book. On position 29763 that
            # showed -67.6% / $16.19 while the runner's REAL whole-balance route quote for the same
            # bag returned -35.2% / $32.39 -- a 32-point error, and pessimistic, which is the
            # direction that panics an operator into selling. The runner persists every resolved
            # exact quote, so prefer it whenever it is fresh and fall back to the model otherwise.
            last, mark_basis, mark_observed_at, mark_age_s = _select_display_mark(
                mid=mid,
                mid_observed_at=mid_observed_at,
                liquidity=liq,
                qty=qty,
                now_iso=now_real,
                exact_price=p.get("last_executable_price"),
                exact_observed_at=p.get("last_executable_at"),
            )
            # Position return, not price return: after a partial de-risk the banked USDC is
            # part of the result and the bag is smaller. See position_return_pct.
            pnl_pct = m.position_return_pct(
                conn, p["id"], entry_notional=p.get("entry_notional"),
                last_price=last, entry_price=entry, open_quantity=qty)
            # SHORT-WINDOW MOMENTUM from our own mark tape: mid now vs ~1h ago. PnL is entry-relative
            # ("am I up?"); this is "is it moving RIGHT NOW" — the signal for exit timing. None until a
            # position has a >=1h-old mark (fresh entries, or Solana until the funded mark-write deploys).
            chg_1h = None
            try:
                ago = (datetime.fromisoformat(now_real) - timedelta(hours=1)).isoformat()
                r1h = conn.execute(
                    "SELECT mid FROM memecoin_position_marks WHERE chain_id=? AND mint=? AND mid>0 "
                    "AND COALESCE(quality,'')!='quarantined_spike' AND sweep_ts<=? AND "
                    + _own_sources_sql() + " ORDER BY sweep_ts DESC LIMIT 1",
                    (p.get("network_id") or "solana", p["mint"], ago)).fetchone()
                if r1h and r1h["mid"] and mid:
                    chg_1h = (mid / r1h["mid"] - 1.0) * 100.0
            except Exception:
                logger.exception("real positions: 1h change compute failed pos=%s", p["id"])
            held_s = None
            try:
                held_s = max(0.0, (datetime.fromisoformat(now_real)
                                   - datetime.fromisoformat((p.get("opened_at") or "").replace("Z", "+00:00"))
                                   ).total_seconds())
            except (ValueError, TypeError):
                pass
            exit_override = m.effective_exit_override(conn, p["id"])
            contract_view = _position_contract_view(p, exit_override)
            frozen_policy = contract_view.pop("frozen_policy")
            # Token NAME: the stored ticker is blank for ~1/3 of entries. Resolve from the live price
            # stats' symbol (already fetched) or the feed observations, and backfill so it sticks for
            # Telegram + closed history instead of showing '?'.
            disp_ticker = (p.get("ticker") or q.get("symbol")
                           or open_names.get((p.get("network_id") or "solana", p["mint"])))
            # Recovery intel: advisory verdict (RIDE/HOLD/LOCK/WATCH/CUT/DEAD) + score, so the operator
            # sees the hold / cut / lock call at a glance — the by-hand analysis, automated.
            try:
                _lb = _pos_intel.last_armed_buy_minutes(conn, p.get("network_id"), p["mint"], now_iso=now_real)
                intel = _pos_intel.position_verdict(
                    dict(p), {"price": mid, "market_cap": q.get("market_cap"),
                              "liquidity": q.get("liquidity"), "volume_24h": q.get("volume_24h")},
                    last_armed_buy_min=_lb, now_iso=now_real)
            except Exception:
                logger.exception("position intel failed pos=%s", p["id"])
                intel = None
            # derisk display state: how much a landed derisk recovered, and the EFFECTIVE threshold
            # (post-override) so the card can tell "armed @ +N%" from "off" (bare-trail/operator derisk=0).
            _derisk_recovered = m.derisk_proceeds(conn, p["id"])
            # RAIL BASIS. `last` above is haircut by modeled exit slippage, so Now/Value/PnL show
            # what the bag could be SOLD for. The stop and catastrophe rails do NOT read that
            # number -- they read the raw `mid` whenever the exact preview is unresolved. On a thin
            # pool the two diverge hard: position 29763 showed -62.8% here while its -50% stop was
            # measuring -28.4% and never nominated. Publish the rail's own number so the card can
            # say which price each figure is, instead of implying they are the same one.
            nominal_pnl_pct = ((mid / entry - 1.0) * 100.0) if (mid and entry > 0) else None
            stop_blind = False
            try:
                from sniper.services.memecoin_real_runner import effective_real_thresholds
                _tp, _tpm, _stop_mag, _slm = effective_real_thresholds(dict(p), exit_override)
                stop_blind = bool(
                    _stop_mag and _stop_mag > 0
                    and pnl_pct is not None and nominal_pnl_pct is not None
                    and float(pnl_pct) <= -float(_stop_mag)          # sellable price IS through it
                    and float(nominal_pnl_pct) > -float(_stop_mag))  # rail's price is NOT
            except Exception:
                logger.exception("rail-basis compare failed pos=%s", p["id"])
            from sniper.services.memecoin_real_runner import _effective_derisk_pct as _eff_derisk_fn
            try:
                _derisk_eff_pct = _eff_derisk_fn(dict(p), exit_override)
            except Exception:
                _derisk_eff_pct = None
            out.append({
                "intel": intel,
                "social": _smap.get(m.accounting_mark_key(p.get("network_id"), p["mint"])),
                "id": p["id"], "mint": p["mint"], "ticker": disp_ticker,
                "tracking_paused_at": None,
                "network_id": p.get("network_id") or "solana",
                "venue": "EVM" if (p["mint"] or "").startswith("0x") else "SOL",
                "entry_price": entry, "entry_notional": p.get("entry_notional"),
                "last_quote_price": last, "pnl_pct": pnl_pct,
                "market_mid_price": lv if lv and lv > 0 else None,
                "market_mid_at": mid_observed_at if lv and lv > 0 else None,
                # live dexscreener context for the position table (None when the pair is unlisted this sweep)
                "market_cap": q.get("market_cap"), "liquidity": q.get("liquidity"),
                "market_cap_basis": "circulating",
                # Operator mcap targets read price x this pinned TOTAL supply (FDV); None = not
                # pinned, the targets stay on the provider market cap. Same fields as /real/ticker.
                "target_supply": p.get("fdv_supply") if (p.get("fdv_supply") or 0) > 0 else None,
                "mcap_basis": "fdv" if (p.get("fdv_supply") or 0) > 0 else "circulating",
                "volume_24h": q.get("volume_24h"), "change_1h_pct": chg_1h,
                # provenance + execution-quality columns (who we copied, their conviction, our copy latency)
                "source_handle": p.get("source_handle"), "their_size_usd": p.get("their_size_usd"),
                "entry_lag_s": p.get("entry_lag_s"), "entry_liquidity": p.get("entry_liquidity"),
                "fill_timing": _fill_lines.get(p.get("attempt_id")) or None,
                # execution drift: our on-chain fill vs the trader's price we copied (+% = we paid up)
                "their_price": p.get("their_price"), "fill_gap_pct": p.get("fill_gap_pct"),
                # price x this = market cap for display ("$34K", not 0.0000343); None = show the price
                "display_supply": m.position_display_supply(conn, p),
                # The DISPLAY peak, derived live from the tracked high-water mark. No fallback to
                # the stored column: it is NULL on every funded row (2026-09-30).
                "peak_price": p.get("peak_price"),
                # The funded max gain is the EXECUTABLE peak's (2026-10-17, ANIMA 73930); the display
                # peak above stays for the chart.
                "max_gain_pct": (((p.get("executable_peak_price") / entry) - 1.0) * 100.0
                                 if (p.get("executable_peak_price") and entry and entry > 0) else None),
                "peak_basis": "exact",
                **_unconfirmed_peak(p, entry),
                # The EXECUTABLE top and bottom: whole-bag sell quotes of the exit_decision route
                # series against the fill. None until a quote is recorded, never zero.
                **_exec_extremes_fields(conn, p, entry),
                "last_quote_ts": p.get("last_quote_ts"),
                # REAL rails, not the paper alpha_b study knobs (0 = that rail off). These are the
                # DEFAULTS the TP/SL columns fall back to when a position has no operator override.
                "held_s": held_s,
                "timecap_s": (frozen_policy.get("max_hold_s") if frozen_policy
                              else cfg.MEMECOIN_REAL_MAX_HOLD_S),
                "stop_pct": cfg.MEMECOIN_REAL_STOP_PCT,
                # the ACTUAL per-chain default stop (4663 = 0), not the global — the old field lied
                # for 4663 rows. Plus the absolute catastrophe floor that ALWAYS backstops (2026-07-18).
                "chain_stop_pct": (frozen_policy.get("stop_pct") if frozen_policy
                                   else cfg.real_stop_pct_for_chain(p.get("network_id"))),
                "catastrophe_pct": (frozen_policy.get("catastrophe_pct") if frozen_policy
                                    else cfg.MEMECOIN_REAL_CATASTROPHE_PCT),
                "default_tp_pct": (frozen_policy.get("tp_pct") if frozen_policy
                                   else cfg.MEMECOIN_REAL_TP_PCT),
                "derisk_pct": (frozen_policy.get("derisk_pct") if frozen_policy
                               else cfg.MEMECOIN_REAL_DERISK_PCT),
                "derisked": bool(p.get("derisked_at")),
                # honest derisk state (2026-07-17, pos 6605: the flat boolean showed a green
                # "derisked" badge for a partial sell that never landed on-chain):
                #   confirmed -> a kind='derisk' fill is booked (money actually recovered)
                #   unknown   -> one-shot stamped, tx unverified (recovery pass settling it)
                #   unbooked  -> one-shot stamped, no fill, nothing left to verify
                "derisk_state": (
                    None if not p.get("derisked_at")
                    else "unpriced" if _derisk_recovered is None     # landed, receipt not yet proved
                    else "confirmed" if _derisk_recovered > 0
                    else "unknown" if p.get("derisk_sig")
                    else "unbooked"),
                # USDC actually recovered by kind='derisk' partial sells (None = not derisked), and the
                # EFFECTIVE derisk threshold (0 => the stage is disabled, e.g. an operator/bare-trail
                # override). Lets the card show "derisked ✓ $X" vs "armed @ +N%" vs "off".
                "derisk_recovered_usd": round(_derisk_recovered, 2) if _derisk_recovered else None,
                "derisk_effective_pct": _derisk_eff_pct,
                "stuck_bag": bool(p.get("stuck_bag")), "strategy": p.get("strategy"),
                "anchor_hold": _anchor_holds.get(p["id"]),
                # The ARM ACTUALLY RUNNING. `strategy` is the coarse label and reads `alpha_b` on
                # every funded row because that is MEMECOIN_REAL_POLICY -- position 29847 showed
                # "alpha_b" on the card while its frozen contract ran `copy-arm-combo_f-v1` with a
                # different de-risk and a different stop. Per-wallet arm selection is the whole
                # point of the copy arm; the operator has to be able to see which one is live.
                "frozen_arm": _frozen_arm_name(frozen_policy),
                "opened_at": p.get("opened_at"),
                # per-position mirror-exit override: True => this position will NOT auto-sell when its
                # copied trader sells (operator holds past the trader's exit); TP/stop/timecap rails
                # still apply. Default False => the copy-exit (mirror-on-sell) is active.
                "mirror_disabled": bool(p.get("mirror_disabled")),
                # The runner's latest decision on a sell by the copied wallet (None = no sell seen).
                "source_sell": source_sells.get(p["id"]),
                # per-position exit control: the effective override (operator > template) or None;
                # the row UI edits this. None => the default REAL rails above are in force.
                "effective_exit": exit_override,
                **contract_view,
                # operator mcap full-sell band (memecoin_exit_targets). Has PRECEDENCE over the
                # %-TP in the exit sweep, so the TP column must show it when active.
                "exit_target": (lambda t: {"mcap_low": t["mcap_low"], "mcap_high": t["mcap_high"],
                                           "note": t["note"], "actor": t["actor"]} if t else None
                                )(m.active_exit_target(conn, p["id"])),
                # RAILS-BLIND: the card's PnL is the exact whole-balance preview, but the stop and
                # catastrophe rails read the nominal mid whenever the exact price cannot be
                # resolved. Showing a -62.8% PnL beside a -50% stop that is measuring -28.4% reads
                # as "the stop is broken". Publish the rail basis so the card can say which price
                # each number is, instead of implying they are the same number.
                "rails_blind": _rails_blind_view(conn, p["id"]),
                # The sellable price is through the stop but the price the stop reads is not.
                "nominal_pnl_pct": (round(nominal_pnl_pct, 2)
                                    if nominal_pnl_pct is not None else None),
                "stop_blind": stop_blind,
                # Which price Now/Value/PnL are: a real route quote, a capped worst-case model, or
                # the bare mid. The card must never present a model as "what you would get".
                "mark_basis": mark_basis,
                "mark_age_s": mark_age_s,
                # The observation time of the price in `last_quote_price` on THIS row, whichever
                # source produced it. Travels with price+basis so the client never ages one
                # number with another number's clock. None = genuinely unknown; render it as
                # unknown, never as fresh.
                "mark_observed_at": mark_observed_at,
            })
            out[-1].update(price_by_id.get(p["id"], {}))
            # Live volume and the last trade, from the stored tape (operator 2026-09-13). No
            # provider call on this request; the runner's sweep writes these on its marks.
            out[-1]["live_activity"] = m.position_live_activity(
                conn, p.get("network_id") or "solana", p["mint"], now_iso=now_real)
            if mid and mid > 0 and mid_from_live:
                mark_writes[(p.get("network_id") or "solana", p["mint"])] = {
                    "price": mid, "liquidity": q.get("liquidity"),
                    "mcap": q.get("market_cap"), "source": "web", "quality": "live",
                    "observed_at": mid_observed_at}
        # WEB-side chart-mark write: persist a mark for every priced open position so its in-house chart
        # tape fills WITHOUT waiting on a funded deploy — the gap that left open real Solana bags (KITTY)
        # chart-less while the runner only marked blind 4663. Deduped (MEMECOIN_MARK_WRITE_MIN_INTERVAL_S)
        # + log-only; the funded runner writes the SAME tape, so the two coexist harmlessly.
        # THROTTLED: write_marks runs a MAX(sweep_ts) GROUP BY over the whole marks table (~0.6s on the
        # live tape) — doing it on every 4s poll was the bulk of the "UI hyper slow" latency. The chart
        # doesn't need 4s-fresh marks, so persist at most once per _WEB_MARK_WRITE_INTERVAL_S; the other
        # polls return in ~30ms. The funded runner writes the same tape on its own cadence.
        if mark_writes and (_time.monotonic() - _WEB_MARK_WRITE["ts"]) >= _WEB_MARK_WRITE_INTERVAL_S:
            _WEB_MARK_WRITE["ts"] = _time.monotonic()
            try:
                from sniper.services.memecoin_exits import _quarantine_spikes, write_marks
                # Same spike/collapse filter the funded sweep applies at
                # memecoin_real_runner.py:2344. Without it this writer stamped quality='live' on
                # anything the provider returned, so a glitched tick entered the tape as a GOOD
                # mark while the runner's own copy of the same tick sat quarantined beside it.
                # PURR (position 40687, 2026-08-19): the provider flipped from mid 2.35e-05 /
                # liq $19.7k to mid 0.0979 / liq $86.1M mid-hold. The runner quarantined 420
                # ticks; these four went in as live, and the card read +352,997% on a $25 bag.
                # The exit rails were never fooled - they filter quarantined marks - but the
                # operator surface was, and an operator acts on what the card says.
                write_marks(conn, _quarantine_spikes(conn, mark_writes, now_real), now_real)
            except Exception:
                logger.exception("real positions: web mark-write failed (display unaffected)")
        return jsonify({"positions": out, "server_now": now_real, "as_of": now_real,
                        "revision": shared_prices["revision"]})

    @bp.get("/api/memecoin/real/positions/<int:pid>")
    def real_position_detail(pid):
        from sniper.services.memecoin_links import operator_links
        as_of = _now_iso()
        with m.operator_read_connection(conn) as observer:
            position = m.get_position(observer, pid)
            if not position or position.get("execution_mode") != "real":
                return jsonify({"error": "Real position not found."}), 404
            fields = ("id", "network_id", "mint", "ticker", "source_handle", "source_user_id",
                      "origin_event_id", "attempt_id", "is_open", "opened_at", "closed_at",
                      "closed_recorded_at", "entry_price", "entry_notional", "realized_pnl",
                      "exit_reason", "last_quote_price", "last_quote_ts", "tracking_paused_at")
            detail = {key: position.get(key) for key in fields}
            detail.update(_stored_position_contract_view(position,
                m.effective_exit_override(observer, pid) if position.get("is_open") else None))
            fills = m.operator_position_fills(observer, pid)
            detail["fills"] = fills
            if position.get("is_open"):
                prices = _real_ticker_payload(observer, as_of, positions=[position])["positions"]
                if prices:
                    detail.update(prices[0])
            else:
                detail.update(m.position_value_view(observer, pid, last_price=0,
                                                     entry_notional=position.get("entry_notional")))
                detail["value_basis"] = "closed_position"
                detail["remaining_value_usd"] = 0.0
                if position.get("realized_pnl") is not None:
                    detail["total_pnl_usd"] = position["realized_pnl"]
                    notional = position.get("entry_notional")
                    detail["total_pnl_pct"] = position["realized_pnl"] / notional * 100 if notional else None
                # What decided the exit (exact route or on-chain proof) beside the mid the screens
                # showed; display only (2026-09-27).
                detail["exit_decision"] = m.exit_decisions(observer, [pid]).get(pid)
            detail["quote_residue"] = _quote_residue_map(observer, [pid]).get(pid)
            detail["links"] = operator_links(position.get("network_id"), position.get("mint"),
                handle=position.get("source_handle"), origin_event_id=position.get("origin_event_id"),
                position_id=pid, tx_sig=fills[0].get("tx_sig") if fills else None)
        return jsonify({"position": detail, "as_of": as_of})

    @bp.post("/api/memecoin/real/positions/<int:pid>/exit")
    def set_real_position_exit(pid):
        d = request.get_json(force=True, silent=True) or {}
        edited = d.get("edited_leg")
        if "edited_leg" in d and edited not in ("tp", "sl"):
            return jsonify({"ok": False, "error": "edited_leg must be tp or sl"}), 400
        if edited is not None and not any(key in d for key in (edited + "_pct", edited + "_target_mcap")):
            return jsonify({"ok": False, "error": "provide the edited leg's threshold or explicit null"}), 400
        try:
            conn.execute("BEGIN IMMEDIATE")  # Keep close ownership and leg edits in one read/merge/write.
            return _set_real_position_exit(pid, d, edited)
        finally:
            if conn.in_transaction:
                conn.rollback()

    def _set_real_position_exit(pid, d, edited):
        """Operator per-position exit override (spec 2026-07-14). Body: tp_pct | tp_target_mcap,
        sl_pct | sl_target_mcap, trail_pct, derisk_pct, actor, note, or {clear:true}.
        Operator authority is full — a wider SL than default is allowed (the UI confirms a loosening);
        this endpoint only writes the override row, the exit sweep honors it (deploy-gated). 404 if the
        position isn't an OPEN real position."""
        pos = m.get_position(conn, pid) if hasattr(m, "get_position") else None
        if not pos or pos.get("is_open") != 1 or (pos.get("execution_mode") or "paper") != "real":
            return jsonify({"ok": False, "error": "not an open real position"}), 404
        if m.close_submission_state(pos) != "idle":
            return jsonify({"ok": False,
                            "error": "Exit already in progress; these settings cannot change it."}), 409
        actor = (d.get("actor") or "operator").strip()
        if d.get("clear"):
            # Reverting to default rails must also drop any operator mcap band — a band is a take-profit
            # too, and leaving it would keep firing after a "clear to default", re-introducing the exact
            # shadow this unifies away (one TP control, 2026-07-18). One transaction (Codex R1).
            try:
                m.clear_position_exit_override(conn, pid, "operator", commit=False)
                m.cancel_exit_target(conn, pid, _now_iso(), commit=False)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return jsonify({"ok": True, "cleared": True, "effective_exit": m.effective_exit_override(conn, pid)})

        try:
            if edited is not None:
                # Parse only the selected leg; bare-trail normalization must not change the other rails.
                _ov = parse_exit_override({key: d.get(key) for key in
                    (edited + "_pct", edited + "_target_mcap")}, require_one=False)
                current = m.effective_exit_override(conn, pid) or {}
                _ov = {key: value if key.startswith(edited + "_") else current.get(key)
                       for key, value in _ov.items()}
            else:
                _ov = parse_exit_override(d)
        except ExitOverrideError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        tp_pct, tp_mcap = _ov["tp_pct"], _ov["tp_target_mcap"]
        sl_pct, sl_mcap = _ov["sl_pct"], _ov["sl_target_mcap"]
        trail, derisk = _ov["trail_pct"], _ov["derisk_pct"]
        # A TYPED MCAP MEANS WHAT THE CARD SHOWED (Alpha, 2026-10-02). A card on the display-only
        # total supply ('total') reads FDV while the runner reads this position's targets on the
        # provider/entry ratio: $300K typed off a 56 card fired near $377K. Typing an mcap on the
        # EDITED leg of such a card pins fdv_supply from that supply (write-once), the runner's
        # existing FDV target path. The page says which basis it drew and which leg it edited; a
        # basis the server no longer holds is refused (409), never guessed.
        #
        # THE PIN MOVES EVERY MCAP TARGET ON THE POSITION (Gamma MED on cc6cd9352): the page re-sends
        # the other leg verbatim, and a floor typed while the card read circulating would fire ~20%
        # lower in price once read on FDV. The carried leg is converted from its STORED value by
        # total / the rails' supply, so its trigger price does not move. A band set elsewhere
        # (Telegram, scripts) is not converted: the save is refused unless it replaces the band.
        cap_supply, cap_basis = m.position_cap_supply(pos)
        mcap_basis = cap_basis or "circ"
        converted = {}
        band_cancelled = 0
        cancel_first, pin_supply = False, None
        legs = {"tp": tp_mcap, "sl": sl_mcap}

        def _refuse(error):
            return jsonify({"ok": False, "error": error, "mcap_basis": mcap_basis,
                            "mcap_supply": cap_supply}), 409
        if (tp_mcap is not None or sl_mcap is not None) and edited is None and cap_basis == "total":
            return _refuse("this page is older than the market-cap basis switch; reload it")
        if edited is not None and legs[edited] is not None:
            sent = d.get("mcap_basis")
            try:
                sent_supply = float(d.get("mcap_supply") or 0)
            except (TypeError, ValueError):
                sent_supply = 0.0
            if sent != mcap_basis or (cap_supply is not None
                                      and abs(sent_supply / cap_supply - 1.0) > 1e-9):
                return _refuse("the market-cap basis changed since this card was drawn; reload and "
                               "enter the target again")
            if cap_basis == "total":
                other = "sl" if edited == "tp" else "tp"
                if m.active_exit_target(conn, pid) and not (edited == "tp"
                                                            and d.get("supersede_band")):
                    return _refuse("an mcap band set on the circulating basis is active; replace it "
                                   "from the TP editor first")
                if legs[other] is not None:
                    stored = (m.effective_exit_override(conn, pid) or {}).get(
                        other + "_target_mcap")
                    rows = _real_ticker_payload(conn, _now_iso(), positions=[pos])["positions"]
                    rail = rows[0].get("target_supply") if rows else None
                    if not (stored and stored > 0 and rail and rail > 0):
                        return _refuse("the other leg's market cap cannot be carried to FDV; set it "
                                       "again or switch it to a percent")
                    legs[other] = float(stored) * cap_supply / float(rail)
                    converted[other] = {"from": float(stored), "to": legs[other]}
                    tp_mcap, sl_mcap = legs["tp"], legs["sl"]
                cancel_first = edited == "tp" and bool(d.get("supersede_band"))
                pin_supply = cap_supply
                mcap_basis = "fdv"
        # ONE TRANSACTION (Codex R1): the superseded band, the pin, the override and the TP's band
        # cancel commit together or not at all. A sweep on another connection sees the old exit or
        # the new one, never a cancelled band without its replacement (Gamma LOW: never a circulating
        # band read on FDV either). Every refusal above wrote nothing.
        try:
            if cancel_first:
                band_cancelled = m.cancel_exit_target(conn, pid, _now_iso(), commit=False)
            if pin_supply is not None:
                m.pin_position_fdv_supply(conn, pid, pin_supply)
            if edited is not None and all(value is None for value in _ov.values()):
                m.clear_position_exit_override(conn, pid, "operator", commit=False)
            else:
                m.set_position_exit_override(conn, pid, "operator", tp_pct=tp_pct, tp_target_mcap=tp_mcap,
                                             sl_pct=sl_pct, sl_target_mcap=sl_mcap, trail_pct=trail,
                                             derisk_pct=derisk, actor=actor, note=(d.get("note") or ""),
                                             commit=False)
            # ONE take-profit, last-write-wins: when the operator edits the TP leg (client sends
            # supersede_band), cancel any active mcap band so the new TP is what actually fires and
            # shows — not silently outranked by a stale band (2026-07-18). SL edits leave it alone.
            if d.get("supersede_band"):
                band_cancelled += m.cancel_exit_target(conn, pid, _now_iso(), commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        # No Telegram ping for an exit-override EDIT (removed 2026-08-14): the operator making the
        # edit is the one holding the phone, and 51 OPERATOR pings in 20 minutes of active TP/SL
        # management buried the alerts that matter. The action stays fully attributable — the
        # override row records actor/note, and MANUAL CLOSE (a position actually vanishing) still
        # pages. This is a log line now, not a send.
        logger.info("EXIT OVERRIDE pos#%s tp_pct=%s tp_target_mcap=%s sl_pct=%s sl_target_mcap=%s",
                    pid, tp_pct, tp_mcap, sl_pct, sl_mcap)
        return jsonify({"ok": True, "effective_exit": m.effective_exit_override(conn, pid),
                        "band_cancelled": band_cancelled, "mcap_basis": mcap_basis,
                        "converted": converted})

    @bp.post("/api/memecoin/real/positions/<int:pid>/mirror")
    def set_real_position_mirror(pid):
        """Per-position mirror-exit toggle. Body {disabled: bool}. disabled=true suppresses the
        copy-exit (mirror-on-sell) for THIS position so the operator can hold past the trader's sell;
        TP / stop / de-risk / timecap / unquotable rails stay active, so the bag is never left
        unprotected. 404 if the position is not an OPEN real position. Enforcement lives in the funded
        runner: the flag is written live here and is honored on the runner's next sell event once the
        matching runtime is deployed (deploy-gated, same as the exit-override endpoint)."""
        pos = m.get_position(conn, pid) if hasattr(m, "get_position") else None
        if not pos or pos.get("is_open") != 1 or (pos.get("execution_mode") or "paper") != "real":
            return jsonify({"ok": False, "error": "not an open real position"}), 404
        d = request.get_json(force=True, silent=True) or {}
        if "disabled" not in d:
            return jsonify({"ok": False, "error": "body must include 'disabled' (bool)"}), 400
        disabled = bool(d.get("disabled"))
        m.set_position_mirror_disabled(conn, pid, disabled)
        return jsonify({"ok": True, "position_id": pid, "mirror_disabled": disabled})

    def _quote_residue_map(conn, ids):
        """{position_id: {"pending_usd", "credited_usd"}} for the card: quote-asset residue not yet
        sold (record-time price, not money) apart from residue credited into realized_pnl
        (memecoin_quote_residue_credits). Display only; a read failure shows nothing."""
        try:
            return m.quote_residue_by_position(conn, ids)
        except Exception:
            logger.exception("real positions: quote residue read failed (display unaffected)")
            return {}

    @bp.get("/api/memecoin/real/closed")
    def real_closed():
        """Recent real-money closed positions — the operator's row-level history (daily PnL alone
        loses per-trade detail after a close). Mode-scoped; never mixed with the paper closed board."""
        rows = m.list_closed_positions(conn, mode="real", limit=100)
        from sniper.services.memecoin_fill_timing import view_details
        ids = [p["attempt_id"] for p in rows if p.get("attempt_id")]
        timing = view_details(conn, [dict(r) for r in conn.execute(
            "SELECT * FROM memecoin_entry_attempts WHERE attempt_id IN (%s)" % ",".join("?" * len(ids)),
            ids)]) if ids else {}
        decisions = m.exit_decisions(conn, [p["id"] for p in rows])
        names = _coin_names(conn, _unnamed_keys(rows))
        out = [{"id": p["id"], "mint": p["mint"],
                "ticker": (p.get("ticker") or names.get((p.get("network_id") or "solana", p["mint"]))),
                # Who we copied and at what price, beside what we paid (operator, 2026-09-28).
                # their_price is the source's own buy price; None is unknown, never 0.
                "source_handle": p.get("source_handle"), "signal_source": p.get("signal_source"),
                "their_price": p.get("their_price") if (p.get("their_price") or 0) > 0 else None,
                # price x this = market cap, so the screen can say "$34K" instead of 0.0000343
                "display_supply": _closed_supply(conn, p),
                "venue": "EVM" if (p["mint"] or "").startswith("0x") else "SOL",
                "network_id": p.get("network_id") or "solana",
                "entry_price": p.get("entry_price"), "entry_notional": p.get("entry_notional"),
                "realized_pnl": p.get("realized_pnl"), "exit_reason": p.get("exit_reason"),
                # Who decided this row. The client badges manual and defaults its summary to robot,
                # so the operator's own picks neither hide nor distort the system's result.
                "manual": (p.get("signal_source") or "") == m.MANUAL_SIGNAL_SOURCE,
                "exit_decision": decisions.get(p["id"]),
                "fill_timing": timing.get(p.get("attempt_id")),
                "opened_at": p.get("opened_at"), "closed_at": p.get("closed_at")} for p in rows]
        residue = _quote_residue_map(conn, [r["id"] for r in out])
        for r in out:
            r["quote_residue"] = residue.get(r["id"])
        return jsonify({"closed": out, "server_now": _now_iso()})

    @bp.get("/api/memecoin/real/refusal-slots")
    def real_refusal_slots():
        """Return recent selection-gate refusals. This route does not change the ledger."""
        store, refusal_conn = _refusal_db()
        try:
            attempts = _recent_refusal_attempts()
            saved = store.operator_slots(refusal_conn, [row["attempt_id"] for row in attempts])
            slots = []
            now = datetime.fromisoformat(_now_iso().replace("Z", "+00:00"))
            for attempt in attempts:
                first_price, last_price, price_basis, quote_observed_at = _refusal_prices(
                    attempt, refusal_conn, now.isoformat())
                prior = saved.get(attempt["attempt_id"]) or {}
                refused = datetime.fromisoformat(attempt["refused_at"].replace("Z", "+00:00"))
                if not prior and (now - refused).total_seconds() > 120:
                    continue
                refusal_price = prior.get("refusal_price") or first_price
                drift = None
                if refusal_price and last_price:
                    drift = round((last_price / refusal_price - 1.0) * 100.0, 6)
                shown_at = prior.get("slot_shown_at")
                shown = (datetime.fromisoformat(shown_at.replace("Z", "+00:00"))
                         if shown_at else now)
                slots.append({
                    **_refusal_slot_facts(attempt, refusal_price),
                    **prior,
                    "refusal_price": refusal_price,
                    "quote_price": last_price,
                    "price_drift_pct": drift,
                    "price_basis": price_basis,
                    "quote_observed_at": quote_observed_at,
                    "ttl_s": 120,
                    "remaining_s": max(0, 120 - int((now - shown).total_seconds())),
                })
            return jsonify({"slots": slots, "server_now": _now_iso()})
        finally:
            refusal_conn.close()

    @bp.post("/api/memecoin/real/refusal-slots/<attempt_id>/shown")
    def real_refusal_slot_shown(attempt_id):
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        attempt = _refusal_attempt(attempt_id)
        if attempt is None:
            return jsonify({"ok": False, "error": "selection refusal not found"}), 404
        store, refusal_conn = _refusal_db()
        try:
            first_price, _, _, _ = _refusal_prices(attempt, refusal_conn, _now_iso())
            row = store.show_operator_slot(
                refusal_conn, _refusal_slot_facts(attempt, first_price), now_iso=_now_iso())
            if bool((request.get_json(silent=True) or {}).get("visible")):
                row = store.mark_operator_slot_visible(
                    refusal_conn, attempt_id, now_iso=_now_iso())
            return jsonify({"ok": True, "slot": row})
        finally:
            refusal_conn.close()

    @bp.post("/api/memecoin/real/refusal-slots/<attempt_id>/decision")
    def real_refusal_slot_decision(attempt_id):
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        attempt = _refusal_attempt(attempt_id)
        if attempt is None:
            return jsonify({"ok": False, "error": "selection refusal not found"}), 404
        data = request.get_json(force=True, silent=True) or {}
        decision = str(data.get("decision") or "")
        allowed = {"enter", "dismiss", "expired_tab_visible", "expired_tab_hidden"}
        if decision not in allowed:
            return jsonify({"ok": False, "error": "invalid decision"}), 400
        size_pct = data.get("size_pct_equity")
        intent_id = str(data.get("intent_id") or "").strip() or None
        # OPERATOR OVERRIDE (2026-09-22): "Enter anyway" names a dollar amount, never one of the
        # two equity presets, so it cannot satisfy the preset check below. This row is research
        # attribution only (did offering the slot lead to a fill) -- it never gates the real
        # buy, which already happened via /api/memecoin/real/buy by the time this call runs.
        is_override_enter = decision == "enter" and bool(data.get("override"))
        if is_override_enter:
            if not intent_id:
                return jsonify({"ok": False, "error": "enter needs an intent_id"}), 400
            size_pct = None
        elif decision == "enter":
            try:
                size_pct = float(size_pct)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "enter size must be 0.13 or 0.26"}), 400
            if size_pct not in (0.13, 0.26) or not intent_id:
                return jsonify({"ok": False, "error": "enter needs a preset and intent_id"}), 400
        else:
            size_pct = None
            intent_id = None
        store, refusal_conn = _refusal_db()
        try:
            first_price, last_price, _, _ = _refusal_prices(
                attempt, refusal_conn, _now_iso())
            store.show_operator_slot(
                refusal_conn, _refusal_slot_facts(attempt, first_price), now_iso=_now_iso())
            row = store.decide_operator_slot(
                refusal_conn, attempt_id, decision=decision, now_iso=_now_iso(),
                decided_size_pct=size_pct, intent_id=intent_id,
                quote_price_at_decision=last_price)
            return jsonify({"ok": True, "slot": row})
        finally:
            refusal_conn.close()

    @bp.post("/api/memecoin/real/buy")
    def real_buy():
        """Manual REAL entry (spec §6) — the operator's path for watch-only signals he likes.
        Runs the FULL gate chain (kill switch -> risk/reservation at the live clip -> token
        safety -> executor); never a modeled fill. 503 when the real leg isn't bound (disarmed /
        no creds) — fail loud, exactly like the real close button."""
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        (ctx.get("ensure_real_fns") or (lambda: None))()
        buy_fn = ctx.get("real_buy_fn")
        if buy_fn is None:
            return jsonify({"ok": False, "error": "real execution not configured"}), 503
        d = request.get_json(force=True, silent=True) or {}
        mint = (d.get("mint") or "").strip()
        # source_user_id is OPTIONAL (2026-08-23, operator): naming a trader only buys the
        # mirror-close on their later sell. Without one the position runs its own rails. The mint
        # alone is enough, because manual entry never needed the uid for authority.
        uid = (d.get("source_user_id") or "").strip() or None
        if not mint:
            return jsonify({"ok": False, "error": "mint required"}), 400
        size_usd = d.get("size_usd")
        size_pct_equity = d.get("size_pct_equity")
        if size_usd is not None and size_pct_equity is not None:
            return jsonify({"ok": False, "error": "provide one size input"}), 400
        if size_usd is not None:
            if isinstance(size_usd, bool):
                return jsonify({"ok": False, "error": "size_usd must be a number"}), 400
            try:
                size_usd = float(size_usd)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "size_usd must be a number"}), 400
            if not math.isfinite(size_usd) or not (size_usd > 0):
                return jsonify({"ok": False, "error": "size_usd must be positive"}), 400
        if size_pct_equity is not None:
            if isinstance(size_pct_equity, bool):
                return jsonify({"ok": False,
                                "error": "size_pct_equity must be a number"}), 400
            try:
                size_pct_equity = float(size_pct_equity)
            except (TypeError, ValueError):
                return jsonify({"ok": False,
                                "error": "size_pct_equity must be a number"}), 400
            if not math.isfinite(size_pct_equity) or not 0 < size_pct_equity <= 1:
                return jsonify({"ok": False,
                                "error": "size_pct_equity must be in (0,1]"}), 400
        network_id = (d.get("network_id") or "").strip() or None
        if network_id is None and mint.startswith("0x"):
            # NEVER blind-default a 0x mint to Base (B1): the same address exists on 4663 / 8453 /
            # BSC, and defaulting to 8453 routed a Robinhood-4663 token to the wrong (unfunded)
            # wallet. Resolve the real chain from the signal that produced this mint; refuse if
            # genuinely unknown rather than guess a chain for a real-money swap.
            # uid may be absent now; the lookup falls back to any signal for this mint.
            network_id = m.latest_network_for_mint(conn, mint, source_user_id=uid)
            if network_id is None:
                return jsonify({"ok": False,
                                "error": "network_id required for EVM mint (chain unknown)"}), 400
        # OPERATOR EXIT RAILS ON THE BUY (2026-08-27). Only what the operator TYPED crosses; a
        # blank field stays None and the RUNNER supplies the default. Defaulting here from cfg is
        # forbidden and pinned by test_the_web_never_sources_a_rail_default_from_its_own_config —
        # the web's config is the stale snapshot that opened 44706 under replaced rails.
        try:
            rails = parse_exit_override(d, require_one=False)
        except ExitOverrideError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        rails_mode = (d.get("rails_mode") or "").strip().lower() or None
        if rails_mode not in (None, "default", "custom"):
            return jsonify({"ok": False, "error": "rails_mode must be 'default' or 'custom'"}), 400
        if rails_mode == "custom" and all(v is None for v in rails.values()):
            return jsonify({"ok": False,
                            "error": "custom rails need at least one threshold — "
                                     "an all-blank custom buy would arm no exits at all"}), 400
        # Pass size_usd ONLY when the operator named one, so any real_buy_fn that predates the
        # per-buy clip keeps working on its own signature instead of raising TypeError.
        kw = {"ticker": (d.get("ticker") or "").strip() or None, "network_id": network_id}
        if size_usd is not None:
            kw["size_usd"] = size_usd
        if size_pct_equity is not None:
            kw["size_pct_equity"] = size_pct_equity
        for field in ("intent_id", "origin_event_id"):
            value = (d.get(field) or "").strip()
            if value:
                kw[field] = value
        if rails_mode or any(v is not None for v in rails.values()):
            kw["rails"] = rails
            kw["rails_mode"] = rails_mode
        # OPERATOR OVERRIDE (2026-09-22, operator order): "Enter anyway" on a refused/failed card.
        # A plain, explicit boolean from the request -- never inferred, never defaulted to True.
        # The $175 hard cap and the risk-gate bypass live server-side (memecoin_real_runner.py's
        # buy_fn / real_entry_hook), never here; this route only carries the operator's intent.
        override = d.get("override")
        if override is not None and not isinstance(override, bool):
            # bool("false") is True (Codex review 2026-09-23, A2): only a JSON boolean is an
            # answer; any other type is a malformed request, never an authorisation.
            return jsonify({"ok": False, "error": "override must be a JSON boolean"}), 400
        if override is True:
            kw["override"] = True
        res = buy_fn(conn, mint, uid, **kw) or {}
        return jsonify({"ok": res.get("status") == "filled", **res})

    @bp.get("/api/memecoin/real/intent/<intent_id>")
    def real_intent_status(intent_id):
        """Read a manual intent back. The point of the queue: a refusal reaches the person who
        clicked, instead of only a log. Today the operator learned "insufficient bankroll" from a
        Telegram alert rather than from the button."""
        from sniper.services.memecoin_manual_intents import read_status
        row = read_status(conn, intent_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown intent"}), 404
        return jsonify({"ok": True, "intent": row})

    @bp.post("/api/memecoin/real/buy_token")
    def real_buy_token():
        """One-tap entry (Phase 7): a SIGNED, TTL'd, single-use token minted at alert time with
        the position parameters frozen inside it. Verify -> consume (atomic single-use) -> then
        DRY-RUN preview (default) or, only when MEMECOIN_ONE_TAP_ARMED (Checkpoint E), the SAME
        buy path as /real/buy — full kill-switch/risk/safety/executor chain, no bypass."""
        from sniper.services import memecoin_one_tap as ot
        if not ot.one_tap_enabled():
            return jsonify({"ok": False, "error": "one-tap disabled"}), 404
        d = request.get_json(force=True, silent=True) or {}
        now = _now_iso()
        payload, err = ot.verify_token(d.get("token"), now)
        if err is not None:
            return jsonify({"ok": False, "error": err}), 400
        armed = ot.one_tap_armed()
        (ctx.get("ensure_real_fns") or (lambda: None))()
        buy_fn = ctx.get("real_buy_fn")
        if armed and buy_fn is None:
            # runner-dead refusal BEFORE consuming: the operator can retry once it's back
            return jsonify({"ok": False, "error": "real execution not configured"}), 503
        if not ot.consume_token(conn, payload, now):
            return jsonify({"ok": False, "error": "token already used"}), 409
        preview = {"mint": payload["mint"], "network_id": payload.get("network_id") or None,
                   "source_user_id": payload["source_user_id"],
                   "ticker": payload.get("ticker") or None, "issued_at": payload["issued_at"]}
        if not armed:
            return jsonify({"ok": True, "mode": "dry_run", "preview": preview,
                            "note": "one-tap is in dry-run; arming is Checkpoint E"})
        res = buy_fn(conn, payload["mint"], payload["source_user_id"],
                     ticker=payload.get("ticker") or None,
                     network_id=payload.get("network_id") or None) or {}
        return jsonify({"ok": res.get("status") == "filled", "mode": "armed", **res})

    @bp.get("/memecoin/one-tap/<path:token>")
    def one_tap_confirm(token):
        """Idempotent confirm page for the Telegram deep link. Telegram's link-preview crawler
        fetches GETs — this page therefore CONSUMES NOTHING; only the explicit button POST does."""
        from sniper.services import memecoin_one_tap as ot
        if not ot.one_tap_enabled():
            return "one-tap disabled", 404
        payload, err = ot.verify_token(token, _now_iso())
        if err is not None:
            # err is a fixed reason string, but escape defensively.
            return f"<h3>one-tap token refused: {html.escape(str(err))}</h3>", 200
        armed = ot.one_tap_armed()
        import json as _json
        # A memecoin ticker is ATTACKER-CONTROLLED (anyone can name a token `<img onerror=...>`)
        # and rides inside the signed token, so signing does not sanitize it. Escape every
        # reflected field or the confirm page is reflected XSS (2026-07-23 review P1).
        e_ticker = html.escape(str(payload.get("ticker") or payload["mint"][:12]))
        e_network = html.escape(str(payload.get("network_id") or "solana"))
        e_mint = html.escape(str(payload["mint"]))
        return (
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<h3>{'⚡ ARMED buy' if armed else '🧪 DRY-RUN preview'}</h3>"
            f"<p><b>{e_ticker}</b> — chain "
            f"{e_network}<br>mint <code>{e_mint}</code></p>"
            "<button style='font-size:1.4em;padding:0.6em 1.2em' onclick=\"fetch("
            "'/api/memecoin/real/buy_token', {method: 'POST', headers: {'Content-Type': "
            "'application/json'}, body: JSON.stringify({token: " + _json.dumps(token) + "})})"
            ".then(r => r.json()).then(j => document.getElementById('r').textContent = "
            "JSON.stringify(j, null, 1))\">"
            f"{'BUY NOW' if armed else 'PREVIEW'}</button><pre id='r'></pre>"
        ), 200

    def _chain_admission(cfg):
        """{chain_id: {'reason': str|None, 'capabilities': {...}}} for chains with a pinned
        admission gate. Today only Ink. A read error is reported as its reason, never as ready."""
        try:
            manifest = cfg._ink_capability_manifest()
            reason = cfg.ink_capability_reason()
        except Exception as exc:                      # the gate itself failed: say so
            logger.exception("ink capability read failed")
            return {"57073": {"reason": "ink_capability_unreadable: %s" % type(exc).__name__,
                              "capabilities": {}}}
        caps = (manifest or {}).get("required_capabilities") or {}
        return {"57073": {"reason": reason,
                          "capabilities": {str(k): str(v) for k, v in caps.items()},
                          "venues": [{"route_class": v.get("route_class"), "state": v.get("state"),
                                      "unsupported_reason": v.get("unsupported_reason")}
                                     for v in (manifest or {}).get("venues") or ()
                                     if isinstance(v, dict)]}}

    @bp.get("/api/memecoin/real/accounting")
    def real_accounting_route():
        """Operator-truth accounting (spec §7): per venue + combined cash / deployed (remaining
        cost basis + reserved) / unrealized (batched live marks; unquotables COUNTED, never
        zeroed) / realized (cumulative + today) / equity / next clip / slots. Live RPC balance
        reads are best-effort: a dead RPC yields cash=None + equity=None — never a fabricated
        number. Seams (acct_stats_fn / acct_balances_fn) are injectable for tests."""
        from sniper.services.memecoin_chains import (
            canonical_chain_id as _canonical_chain_id,
            funded_chain_keys as _funded_chain_keys)
        from sniper.settings import memecoin as cfg
        from sniper.execution.evm_wallet import native_is_quote
        from sniper.services.memecoin_risk import (
            manual_percent_size, real_trade_size, solana_above_nano_ceiling_usd,
            solana_flat_clip_ceiling_usd)
        now = _now_iso()
        day = now[:10]

        open_pos = m.list_open_positions(conn, mode="real")
        priced_pos = [p for p in open_pos if not p.get("tracking_paused_at")]
        stats_fn = ctx.get("acct_stats_fn")
        if stats_fn is None:
            # Keep the existing Solana-only request contract. EVM executable marks come from the
            # venue mark seam below; widening this fetch would add an unmeasured network request.
            from sniper.execution.solana_quote import dexscreener_stats_batch
            stats_fn = dexscreener_stats_batch
        quote_keys = list(dict.fromkeys(
            m.accounting_mark_key(p["network_id"], p["mint"]) for p in priced_pos
        ))
        stats = (stats_fn([p["mint"] for p in priced_pos]) or {}) if priced_pos else {}
        # NET-OF-EXIT-SLIPPAGE marks (not raw mid): a thin memecoin bag can't be sold at mid — the
        # exit pays impact. Haircut the mid by the SAME modeled slippage the paper honest-exit uses
        # (mid*(1-k*value/liquidity)), but ONLY where liquidity is known; an unknown-liquidity mark
        # (e.g. 4663) is left to real_mark_fn's executable quote below rather than slammed to the
        # conservative fill-cap. So displayed unrealized/equity ~ what the bags could be sold for.
        from sniper.execution.solana_quote import modeled_fill
        price_map = {}
        for p in priced_pos:
            key = m.accounting_mark_key(p["network_id"], p["mint"])
            st = stats.get(key) or {}
            # Test/legacy Solana quote seams may still return bare mint keys. Never apply this
            # compatibility path to EVM, where the same address can exist on several chains.
            if (not st and key[0] == "solana"
                    and sum(candidate[1] == key[1] for candidate in quote_keys) == 1):
                st = stats.get(key[1]) or {}
            mid = st.get("price")
            if not (mid and mid > 0):
                continue
            liq = st.get("liquidity")
            qty = m.open_qty(conn, p["id"])
            price_map[key] = (modeled_fill(mid, qty * mid, liq, "sell")
                              if (liq and liq > 0 and qty > 0) else mid)
        # M1: DexScreener can't price Robinhood-4663 bags (100% missing_pair), so real_accounting
        # would value them at COST BASIS and overstate equity. Mark the unpriced ones at their
        # EXECUTABLE sell value via each position's venue quote (same proceeds/open-qty unit price
        # the exit sweep uses). Best-effort — a mark failure leaves it unquotable, never fake-priced.
        mark_fn = ctx.get("real_mark_fn")
        if mark_fn is not None and priced_pos:
            missing = [
                {**p, "open_qty": m.open_qty(conn, p["id"])} for p in priced_pos
                if not price_map.get(m.accounting_mark_key(p["network_id"], p["mint"]))
            ]
            if missing:
                try:
                    for k, v in (mark_fn(missing) or {}).items():
                        if v and v > 0:
                            price_map[k] = v
                except Exception:
                    pass   # accounting must never 500 on a mark failure
        # The 0x/venue preview quote is dead (key retired 2026-07-16), so mark_fn can't price
        # DexScreener-blind (4663) bags. Fall back to the newest STORED price mark exactly as
        # /real/positions does — memecoin_position_marks now carries fresh GeckoTerminal marks
        # (bracket_pass), so a blind-chain bag is VALUED, not counted unquotable at cost basis.
        for p in priced_pos:
            key = m.accounting_mark_key(p["network_id"], p["mint"])
            if price_map.get(key):
                continue
            try:
                row = conn.execute(
                    "SELECT mid FROM memecoin_position_marks WHERE chain_id=? AND mint=? "
                    "ORDER BY sweep_ts DESC LIMIT 1", (key[0], key[1])).fetchone()
            except Exception:
                row = None
            if row and row["mid"] and float(row["mid"]) > 0:
                price_map[key] = float(row["mid"])   # unknown liquidity on 4663 -> raw mid, no haircut
        acct = m.real_accounting(conn, price_map=price_map, day=day)

        balances_fn = ctx.get("acct_balances_fn", lambda: _live_acct_balances(cfg))
        try:
            balances = balances_fn() or {}
        except Exception:
            balances = {}

        venues = ["solana"] + sorted(cfg.MEMECOIN_EVM_ALLOWLIST)
        by_network = {}
        for net in sorted(set(venues) | set(acct["by_network"])):
            a = acct["by_network"].get(net, {"deployed": 0.0, "unrealized": 0.0,
                                             "open_n": 0, "reserved_n": 0, "unquotable_n": 0})
            bal = balances.get(net) or {}
            cash = bal.get("cash_usd")
            # next clip exactly as the entry gate would size it (snapshot + pending basis)
            vsnap = m.real_open_snapshot(conn, network=net)
            _, vpend = m.pending_totals(conn, network=net)
            # MARK, NOT COST (2026-09-06). This preview must be the number the entry gate would
            # actually use; showing cost basis here is how the operator saw the same inflated clip
            # the runner was sizing with.
            sizing_equity = (max(0.0, cash - vpend) + vsnap["mark_notional"] + vpend
                             if cash is not None else None)
            slots = cfg.max_concurrent_for(net, equity=sizing_equity)
            # SAME CEILING AS THE ENTRY GATE. This is the "next clip" the operator reads, and it
            # showed $215 against a $150 cap because the ceiling lived only in the EVM gate.
            # SAME CEILING AS THE ENTRY GATE, on BOTH venues now (2026-09-08). Solana read 0.0
            # here because it had no ceiling to read; it has one, and a dashboard that ignored it
            # would reproduce the $215-against-a-$150-cap divergence this line exists to prevent.
            _clip_cap = float(getattr(
                cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD" if str(net) == "solana"
                else "MEMECOIN_EVM_MAX_CLIP_USD", 0.0) or 0.0)
            if str(net) != "solana":
                _clip_cap = float(cfg.evm_max_clip_usd_for(net) or 0.0)
            # THRESHOLD 0 (2026-10-08): every Solana entry sizes under the raised ceiling, so it is
            # the base clip's ceiling here too (never lowering a larger one, never enabling 0).
            _flat = solana_flat_clip_ceiling_usd() if str(net) == "solana" else None
            if _flat is not None and _clip_cap > 0:
                _clip_cap = max(_clip_cap, _flat)
            clip = (round(real_trade_size(usdc_balance=max(0.0, cash - vpend),
                                          open_notional_usd=vsnap["mark_notional"] + vpend,
                                          max_concurrent=slots,
                                          max_clip_usd=_clip_cap,
                                          mode=cfg.real_sizing_for(net),
                                          bankroll_fraction=cfg.bankroll_fraction_for(net)), 2)
                    if cash is not None else None)
            # ABOVE NANO (2026-10-08): a Solana coin resolved at $50K or more sizes under the raised
            # ceiling. Shown beside the base clip; None when the raise is off or does not bind.
            clip_above_nano = None
            _raised = (solana_above_nano_ceiling_usd()
                       if str(net) == "solana" and _flat is None else None)
            if _raised is not None and _clip_cap > 0 and cash is not None:
                clip_above_nano = round(real_trade_size(
                    usdc_balance=max(0.0, cash - vpend),
                    open_notional_usd=vsnap["mark_notional"] + vpend, max_concurrent=slots,
                    max_clip_usd=max(_clip_cap, _raised), mode=cfg.real_sizing_for(net),
                    bankroll_fraction=cfg.bankroll_fraction_for(net)), 2)
                if clip_above_nano == clip:
                    clip_above_nano = None
            presets = ({str(pct): manual_percent_size(
                quote_balance=cash,
                open_notional_usd=vsnap["mark_notional"],
                pending_notional_usd=vpend,
                global_exposure_usd=acct["deployed_total"],
                max_total_exposure_usd=cfg.MEMECOIN_MAX_TOTAL_EXPOSURE_USD,
                pct=pct,
            ) for pct in (0.13, 0.26)} if cash is not None else {})
            # equity = liquid cash + open-position value. deployed includes RESERVED pending, but
            # that USDC is still sitting in `cash` (not yet spent on-chain) — adding it via deployed
            # too would double-count it. Subtract reserved so equity = cash + open cost + unrealized.
            # EQUITY = spendable cash + gas-asset value + open-position value. `cash` is the
            # quote asset only (USDC on Solana), because that is what an entry spends and what
            # `clip_next` above must size against. The native gas asset is real value we hold and
            # belongs in equity. Spare EVM native also sizes the automatic clip, while wrapped
            # cash remains its spendable bound; Solana retains its existing funding policy.
            # deployed includes RESERVED pending, but that USDC is still sitting in `cash` (not yet
            # spent on-chain), so reserved is subtracted to avoid double-counting it.
            # ARC exposes one USDC balance through native and ERC20 units.
            native_usd = 0.0 if native_is_quote(net) else bal.get("native_usd")
            equity = (round(cash + (native_usd or 0.0)
                            + (a["deployed"] - a.get("reserved", 0.0)) + a["unrealized"], 2)
                      if cash is not None else None)
            gas_required = gas_headroom = gas_ok = None
            native = bal.get("native")
            if str(net) in cfg.MEMECOIN_EVM_ALLOWLIST:
                try:
                    from sniper.services.memecoin_evm_risk import (
                        build_evm_risk_context, required_native_gas)
                    gas_ctx = build_evm_risk_context(
                        conn, usdc_balance=float(cash) if cash is not None else 0.0,
                        native_balance=native, day=day, network_id=net,
                        native_quote_price_usd=(float(native_usd) / float(native)
                            if native is not None and float(native) > 0 and native_usd is not None
                            else None),
                        include_reentry_policy=False)
                    gas_required = required_native_gas(gas_ctx)
                    if native is not None:
                        gas_headroom = float(native) - gas_required
                        gas_ok = math.isfinite(gas_headroom) and gas_headroom >= 0
                    if cash is not None:
                        clip = round(gas_ctx.trade_size_usd, 2)
                        slots = gas_ctx.chain_max_concurrent
                        sizing_equity = (max(0.0, cash - vpend - gas_ctx.native_quote_gas_reserve_usd)
                                         + vsnap["mark_notional"] + vpend
                                         + gas_ctx.native_sizing_equity_usd)
                except (TypeError, ValueError):
                    pass
            # USDC remains the ordinary/manual sizing basis. The native slot lane can spend its
            # SOL buffer, so display that separately from fees/rent and the refill cushion.
            # `_SOL_GAS_KEEP` is the manual swap tool's more conservative keep, not entry gas.
            native_swappable_usd = None
            sol_funds = {"fast_lane_enabled": False, "fast_lane_funds_usd": None,
                         "native_reserve_usd": None, "fast_lane_headroom_usd": None,
                         "fast_lane_tradable_usd": None}
            if str(net) == "solana":
                from sniper.services.memecoin_slot_lane import lane_enabled, lane_mode
                sol_funds["fast_lane_enabled"] = (
                    cfg.MEMECOIN_REAL_ENABLED and lane_enabled() and lane_mode() == "live"
                    and cfg.MEMECOIN_SOL_EXEC_BUFFER_CAP_USD > 0)
            if str(net) == "solana" and native is not None and native_usd is not None:
                try:
                    _sol = float(native)
                    _px = bal.get("native_price_usd") or (float(native_usd) / _sol if _sol > 0 else None)
                    from sniper.services.memecoin_gas_topup import sol_buffer_allocation
                    _buffer_usd = cfg.MEMECOIN_SOL_EXEC_BUFFER_CAP_USD if sol_funds["fast_lane_enabled"] else 0.0
                    allocation = sol_buffer_allocation(
                        conn, sol_balance=_sol, sol_usd=_px, cap_usd=_buffer_usd)
                    gas_required = allocation["reserve_sol"]
                    gas_headroom = _sol - allocation["pending_sol_lamports"] / 1e9 - gas_required
                    gas_ok = gas_headroom >= 0
                    sol_funds.update(
                        fast_lane_funds_usd=allocation["funds_usd"],
                        native_reserve_usd=allocation["reserve_usd"],
                        fast_lane_headroom_usd=allocation["headroom_usd"],
                        fast_lane_tradable_usd=(sizing_equity + allocation["funds_usd"]
                                               if sizing_equity is not None else None))
                    # The manual swap tool also keeps 0.14 SOL. Never suggest spending an
                    # execution buffer, pending buy, or refill cushion as idle native funds.
                    native_swappable_usd = round(
                        max(0.0, (_sol - max(_SOL_GAS_KEEP, gas_required)
                                  - allocation["pending_sol_lamports"] / 1e9) * _px
                            - _buffer_usd - (cfg.MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD
                                             if _buffer_usd > 0 else 0.0)), 2)
                except Exception:
                    # Display-only best effort; unknown is explicit and never changes admission.
                    logger.warning("SOL accounting allocation unavailable", exc_info=True)
                    native_swappable_usd = None
            by_network[net] = {**a, "cash": cash, "gas": bal.get("native"),
                               "gas_usd": native_usd,
                               "native_keep": _SOL_GAS_KEEP if str(net) == "solana" else None,
                               "native_swappable_usd": native_swappable_usd,
                               "equity": equity, "clip_next": clip,
                               "clip_next_above_nano": clip_above_nano,
                               "sizing_equity": sizing_equity, "max_concurrent": slots,
                               "manual_presets": presets, "gas_required": gas_required,
                               "gas_headroom": gas_headroom, "gas_ok": gas_ok,
                               **(sol_funds if str(net) == "solana" else {})}
        known = [b for b in by_network.values() if b["equity"] is not None]
        totals = {"cash": round(sum(b["cash"] for b in known), 2) if known else None,
                  "gas_usd": (round(sum(b["gas_usd"] or 0.0 for b in known), 2)
                              if known else None),
                  "deployed": acct["deployed_total"],
                  "unrealized": acct["unrealized_total"],
                  "equity": round(sum(b["equity"] for b in known), 2) if known else None,
                  "balances_missing": [n for n, b in by_network.items() if b["equity"] is None]}
        return jsonify({
            "by_network": by_network, "totals": totals,
            # ALL-MONEY pair (reconciles to the wallet, same quantity the breaker reads) plus the
            # ROBOT and MANUAL split. The client leads with robot and shows manual as a secondary
            # line; it must never show one of the three unlabelled.
            "realized_total": acct["realized_total"], "realized_today": acct["realized_today"],
            "robot_realized_total": acct["robot_realized_total"],
            "robot_realized_today": acct["robot_realized_today"],
            "manual_realized_total": acct["manual_realized_total"],
            "manual_realized_today": acct["manual_realized_today"],
            # The gates' own count (automatic only; long holds free after MEMECOIN_SLOT_FREE_AFTER_S).
            "slots": {"used": m.automatic_funded_count(conn),
                      "max": cfg.MEMECOIN_MAX_CONCURRENT_TOTAL,
                      "free_after_s": cfg.MEMECOIN_SLOT_FREE_AFTER_S},
            "sizing": {"mode": cfg.MEMECOIN_REAL_SIZING, "fraction": cfg.MEMECOIN_BANKROLL_FRACTION,
                       "floor": cfg.MEMECOIN_MIN_TRADE_USD},
            # DERIVED, NOT A LITERAL (2026-09-06). This tuple was hand-synced with the funded
            # chain set, so BSC's funded implementation landed and the dashboard still reported
            # three chains — a chain you could open and not see. `funded_chain_keys()` already
            # reads MEMECOIN_EVM_ALLOWLIST, so opening a chain shows it here with no code edit.
            "allowlist_by_chain": {
                _canonical_chain_id(k): len(
                    m.real_approved_user_ids(conn, network_id=_canonical_chain_id(k)))
                for k in _funded_chain_keys()},
            # WHY A CHAIN CANNOT ADMIT (Ink, 2026-09-29). The Ink G0 gate refuses every entry until
            # its pinned coverage manifest certifies each capability; the dashboard shows that
            # reason instead of an Ink chain that silently never trades. Local file read, no RPC.
            "chain_admission": _chain_admission(cfg),
            "server_now": now,
        })

    def _sol_usd_price_or_none(cfg):
        """Live SOL/USD, or None.

        Display twin of the money path's pricing: an operator surface must never render "we could
        not price this" as a zero — the rule evm_wallet.quote_balance_usd_or_none already states.
        Uses the same Jupiter seam the executors quote through."""
        try:
            from sniper.execution.solana_real_seams import _quote_with_fallback
            usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            wsol = "So11111111111111111111111111111111111111112"
            quote = _quote_with_fallback(wsol, usdc, 1_000_000_000, 50, None)   # 1 SOL -> USDC
            out = float((quote or {}).get("outAmount") or 0) / 1e6
            return out if out > 0 else None
        except Exception:
            return None

    def _live_acct_balances(cfg):
        """{venue: {cash_usd, native}} from live RPC — reuses the /real/status balance readers.
        `cash_usd` is the USD value of the chain's QUOTE token (the thing entries actually spend):
        USDC on Solana/Base, WETH x live ETH/USD on 4663. None = unknown, never a fabricated 0."""
        out = {}
        try:
            # USE THE NON-LOSSY READERS. `usdc_balance` and `sol_balance` are compatibility
            # wrappers that collapse EVERY failure to 0.0, so a rate-limited custody RPC reported a
            # confident $0.00 instead of "unknown". Live on 2026-08-25: the custody key was
            # HTTP 429 and the panel showed equity $0.07 against a real $21.69 (0.1399 SOL + $7.98
            # USDC, confirmed on a healthy endpoint). This docstring is already on
            # token_balance_checked -- "Balance that RAISES when the read fails, instead of
            # reporting a confident zero" -- and this caller was not using it.
            # None means UNKNOWN. The endpoint below already treats a None cash as unknown equity
            # and lists the venue in `balances_missing`, so the honest path was fully built.
            from sniper.execution.solana_wallet import (
                load_keypair as _l, pubkey_str as _a, sol_balance_read, token_balance_checked,
                USDC_MINT)
            addr, present = _wallet_address(_l, _a, cfg.MEMECOIN_SOLANA_KEYPAIR)
            if present and cfg.MEMECOIN_SOLANA_RPC_URL:
                # TRY EVERY CONFIGURED ENDPOINT, not just the single custody URL. The custody key
                # was HTTP 429 all of 2026-08-25 while three other pool endpoints answered fine, so
                # a panel bound to one URL reported unknown (previously $0.00) on a healthy wallet.
                try:
                    urls = list(cfg.solana_funded_rpc_urls())
                except Exception:
                    urls = []
                for u in ([cfg.MEMECOIN_SOLANA_RPC_URL] + urls):
                    if not u:
                        continue
                    try:
                        cash = token_balance_checked(addr, USDC_MINT, u)
                    except Exception:
                        continue                     # this endpoint failed; try the next
                    native_read = sol_balance_read(addr, u)
                    native = native_read.balance if native_read.ok else None
                    # CASH IS USDC ONLY for ordinary/manual buys. Native SOL can fund supported
                    # slot-lane buys; accounting allocates it separately without changing the
                    # manual presets or adding it twice to equity. Unknown native pricing stays
                    # unknown rather than reporting a confident zero.
                    sol_usd = _sol_usd_price_or_none(cfg)
                    out["solana"] = {
                        "cash_usd": cash,
                        "native": native,
                        "native_price_usd": sol_usd,
                        "native_usd": (native * sol_usd
                                       if (native is not None and sol_usd) else None)}
                    break
                else:
                    out["solana"] = {"cash_usd": None, "native": None, "native_usd": None}
        except Exception:
            pass
        try:
            from sniper.execution import evm_wallet as W
            for chain in sorted(cfg.MEMECOIN_EVM_ALLOWLIST):
                rpc = cfg.evm_rpc_for_chain(chain)
                addr, present = _wallet_address(W.load_keypair, W.address_str,
                                                cfg.MEMECOIN_EVM_KEYPAIR)
                if present and rpc:
                    native = W.native_balance_or_none(addr, rpc)
                    # One native/USD read per chain, and it is THAT chain's asset -- BNB on 56,
                    # ETH on 4663/Base. Keying this on `== "4663"` both priced BSC's WBNB at the
                    # ether mark (3.3x high) and hid Base/BSC native value entirely.
                    native_usd_px = W.native_usd_price(chain)
                    # Display only: native reads already retry once. A single failed WETH read
                    # must get the same chance before this chain disappears from total equity.
                    for _ in range(2):
                        cash = W.quote_balance_usd_or_none(
                            addr, chain, rpc,
                            price_fn=(lambda px=native_usd_px: px) if native_usd_px else None)
                        if cash is not None:       # a measured zero is a successful read
                            break
                        # A 429 IS NOT ASKED AGAIN AT ONCE (Gamma LOW on 24d2b8a6f): the page
                        # re-polls every 5 s while cash is null, so a second read here doubles each
                        # tab's reads into the funded gate's quota (Infura on chain 1).
                        if W.last_read_failure() == "http_429":
                            break
                    native_usd = (native * native_usd_px
                                  if native is not None and native_usd_px else None)
                    out[chain] = {"cash_usd": cash, "native": native,
                                  "native_usd": native_usd}
        except Exception:
            pass
        return out

    def _native_float_path():
        from sniper.util.state_paths import state_path
        return ctx.get("native_float_json") or state_path("data", "native_float.json")

    @bp.get("/api/memecoin/real/equity-change")
    def real_equity_change():
        """The equity change card (operator 2026-10-08): the runner's recorder snapshot, read from its
        file. READ-ONLY: no RPC, ledger or Binance read from the web. Stale after 3 min
        (memecoin_equity_change.read_snapshot); the page ages it too."""
        from sniper.services.memecoin_equity_change import read_snapshot
        from sniper.util.state_paths import state_path
        return jsonify(read_snapshot(ctx.get("equity_change_json") or state_path("data", "equity_change.json")))

    @bp.get("/api/memecoin/real/native-drift")
    def real_native_drift():
        """The native float drift panel (operator 2026-10-08): the runner sampler's snapshot, read
        from its file. READ-ONLY: no RPC, Jupiter or DexScreener from the web. Stale after 3 min or a
        failed price source, and then no price is shown as live (memecoin_native_float.read_snapshot).
        Windows 1h, 24h and 7d only: the operator removed the baseline reset (2026-10-08)."""
        from sniper.services.memecoin_native_float import read_snapshot
        return jsonify(read_snapshot(_native_float_path()))

    @bp.post("/api/memecoin/real/risk-reset")
    def real_risk_reset():
        """Release TODAY's drawdown pause and daily latch, leaving both rails armed.

        The kill switch's Clear, for the risk rails (operator, 2026-09-18). It starts the day's risk
        count from now, so a pause or a latch that fired earlier is released and the NEXT trigger
        pauses again. It is not a disable: the switch for that is the `Trading breakers` knob group.
        The row is keyed by UTC day, so the release expires on its own at midnight.
        """
        if not _csrf_ok():                      # a capital control; same guard as the kill switch
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        from sniper.settings import memecoin as cfg
        from sniper.services.memecoin_alerts import send_alert
        payload = request.get_json(force=True, silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "expected an object"}), 400
        now = _now_iso()          # corrected UTC; the host clock runs ahead
        day = now[:10]
        # THE WEB PROCESS NEVER APPLIES THE OVERLAY (Beta, 2026-09-18). `apply_overrides` is
        # called only by the runner's own main cycle, so this process's `cfg` is frozen at its
        # start-up `.env` value for the life of the process — not stale, permanently wrong once
        # an override exists. `235bc132` resolved that for /real/status and did not reach here,
        # so this endpoint could report a pause the runner does not enforce.
        from sniper.services.memecoin_entry_knobs import effective as _knob_eff
        try:
            _eff = {n: v for n, (v, _s) in _knob_eff(conn, cfg).items()}
        except Exception:
            logger.exception("breaker overlay unreadable; reporting this process's settings")
            _eff = {}
        _dd_usd = _eff.get("MEMECOIN_DRAWDOWN_PAUSE_USD", cfg.MEMECOIN_DRAWDOWN_PAUSE_USD)
        _dd_s = _eff.get("MEMECOIN_DRAWDOWN_PAUSE_S", cfg.MEMECOIN_DRAWDOWN_PAUSE_S)
        _daily = float(_eff.get("MEMECOIN_DAILY_LOSS_LIMIT_USD",
                                cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD) or 0)
        before = {"paused_until": m.drawdown_pause_until(conn, day, _dd_usd, _dd_s, now),
                  "daily_tripped": _daily > 0 and m.is_daily_loss_tripped(conn, day)}
        res = m.set_risk_reset(conn, day, actor="web:%s" % (request.remote_addr or "?"),
                               reason=(payload.get("reason") or "").strip(), now_iso=now)
        after = {"paused_until": m.drawdown_pause_until(conn, day, _dd_usd, _dd_s, now),
                 "daily_tripped": _daily > 0 and m.is_daily_loss_tripped(conn, day)}
        # Releasing a risk rail re-enables entries, so it is alert-worthy in the same way that
        # clearing the kill switch is. A silent release is the thing nobody notices until it costs.
        try:
            send_alert("risk rails released",
                       "drawdown pause and daily latch released for %s; both rails stay armed" % day)
        except Exception:
            logger.exception("risk reset alert failed (the release itself stands)")
        return jsonify({"ok": True, "day": day, "reset_at": res["reset_at"],
                        "before": before, "after": after})

    @bp.post("/api/memecoin/real/kill")
    def real_kill():
        """Operator kill switch — HALTS real entries (exits keep running). Tripping is fail-safe and
        instant; clearing re-enables entries and is the sensitive direction (UI double-confirms)."""
        from sniper.services.memecoin_arming import (clear_kill_switch, is_kill_tripped, kill_switch_status,
                                                     transfer_hold, trip_kill_switch)
        from sniper.services.memecoin_alerts import send_alert
        payload = request.get_json(force=True, silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "expected an object"}), 400
        action = payload.get("action")
        src = "web:%s" % (request.remote_addr or "?")
        if action == "trip":
            trip_kill_switch(source=src, only_if_clear=True)
        elif action == "clear":
            holder = kill_switch_status()["kill_source"]
            held = transfer_hold(holder)
            if held:
                return jsonify({"ok": False, "error":
                    "A funds transfer holds this pause (%s). It clears itself when the credit "
                    "lands. If the transfer is stuck, run: python3 %s --resume %s"
                    % (holder, held[0], held[1])}), 409
            if not clear_kill_switch(source=src, expected_token=payload.get("expected_token"),
                                     override_deploy=payload.get("override_deploy") is True,
                                     refuse_transfer=True):
                return jsonify({"ok": False, "error":
                    "Pause changed or deploy override was not confirmed. Refresh status and retry."}), 409
            # clearing re-enables real entries — as alert-worthy as tripping (a silent clear by
            # the wrong hand would be worse than a silent trip).
            try:
                send_alert("KILL-CLEARED", "entries RE-ENABLED via dashboard (%s)" % src)
            except Exception:
                pass
        else:
            return jsonify({"ok": False, "error": "action must be 'trip' or 'clear'"}), 400
        return jsonify({"ok": True, "tripped": is_kill_tripped()})

    @bp.post("/api/memecoin/positions/<int:pid>/recover")
    def manual_recover(pid):
        """Operator recovery for a STUCK bag: forget the stored route, or stop tracking it.

        A stuck bag is one whose sell reverts on chain. FROGE 50786 is the shape: both multihop
        legs quoted cleanly from 100% down to 1%, but its only route to native ran through a pool
        carrying a hook that refuses us, and the swap reverted after burning 899k gas. Retrying the
        SAME route at a smaller size cannot fix that -- the route itself is the problem.

        Two actions, both reversible, neither of them a sale:

          retry  -- clear `evm_route_json` so the next exit rediscovers a route from scratch
                    instead of reusing the one that reverts, and clear the stuck flag so the rails
                    re-arm. Does NOT sell: the rails decide, as they always do.
          pause  -- move to Long Hold: stop automatic reads and exits while keeping the position
                    open and counted in exposure and slots. `resume` puts it back on live tracking.
        """
        action = str((request.get_json(silent=True) or {}).get("action") or "").strip().lower()
        if action not in ("retry", "pause", "resume"):
            return jsonify({"ok": False,
                            "error": "action must be 'retry', 'pause' or 'resume'"}), 400
        if action in ("pause", "resume") and not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        pos = m.get_position(conn, pid)
        if not pos or not pos.get("is_open"):
            return jsonify({"ok": False, "error": "not open"}), 404
        if action == "retry":
            conn.execute(
                "UPDATE memecoin_positions SET evm_route_json=NULL, stuck_bag=0 WHERE id=?",
                (pid,))
            conn.commit()
            _notify_operator(f"RECOVER pos#{pid}",
                             f"{pos.get('ticker') or pos.get('mint')} route cleared; "
                             f"the next sweep rediscovers a route")
            return jsonify({"ok": True, "action": "retry", "position_id": pid})
        if not m.set_position_tracking_paused(
                conn, pid, None if action == "resume" else _now_iso()):
            return jsonify({"ok": False, "error": "exit already in progress; retry after it settles"}), 409
        return jsonify({"ok": True, "action": action, "position_id": pid})

    @bp.get("/api/memecoin/real/positions/<int:pid>/refresh-price")
    def long_hold_refresh_price(pid):
        """One operator-requested market mark; never writes the funded mark or arms exit rails."""
        pos = m.get_position(conn, pid)
        if (not pos or not pos.get("is_open") or pos.get("execution_mode") != "real"
                or not pos.get("tracking_paused_at")):
            return jsonify({"ok": False, "error": "not an open Long Hold position"}), 404
        key = (pos.get("network_id") or "solana", pos["mint"])
        try:
            stats = {}
            if key[0] != "4663":
                stats = quote_for(dexscreener_stats_batch_keyed([key]), key[1], key[0]) or {}
            if not stats:
                stats = quote_for(gt_stats_batch_keyed([key], ttl_s=0), key[1], key[0]) or {}
        except Exception:
            logger.exception("long hold price refresh failed pos=%s", pid)
            stats = {}
        price = stats.get("price")
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            return jsonify({"ok": False, "error": "price unavailable"}), 503
        return jsonify({"ok": True, "position_id": pid, "price": price,
                        "source": stats.get("source") or "market", "fetched_at": _now_iso()})

    @bp.get("/api/memecoin/withdraw")
    def withdraw_status():
        """The one configured withdrawal address and the latest withdrawals."""
        from sniper.settings.memecoin import MEMECOIN_WITHDRAW_SOLANA_ADDRESS as to_address
        return jsonify({"ok": True, "configured": bool(to_address), "network_id": "solana",
                        "asset": "USDC", "to_address": to_address or None,
                        "intents": m.recent_withdraws(conn, "solana"),
                        "csrf_token": _csrf_token()})

    @bp.post("/api/memecoin/withdraw")
    def withdraw_request():
        """Queue one USDC withdrawal to the configured address. The runner sends it."""
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        from sniper.settings.memecoin import MEMECOIN_WITHDRAW_SOLANA_ADDRESS as to_address
        from sniper.execution.solana_usdc_transfer import valid_wallet
        if not to_address or not valid_wallet(to_address):
            return jsonify({"ok": False, "reason": "no withdrawal address configured"}), 503
        d = request.get_json(force=True, silent=True) or {}
        amount = d.get("amount_usdc")
        if isinstance(amount, bool):
            amount = None
        try:
            amount = round(float(amount), 6)
        except (TypeError, ValueError):
            amount = None
        if amount is None or not math.isfinite(amount) or amount < 0.01:
            return jsonify({"ok": False, "reason": "enter an amount of at least $0.01"}), 400
        request_id = d.get("request_id")
        if request_id is not None:
            import uuid
            try:
                if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                    raise ValueError
            except (ValueError, AttributeError):
                return jsonify({"ok": False, "reason": "request_id must be a canonical UUID"}), 400
        try:
            intent_id = m.enqueue_withdraw(conn, "solana", to_address, amount,
                                           now_iso=_now_iso(), request_id=request_id)
        except ValueError as exc:
            return jsonify({"ok": False, "reason": str(exc)}), 409
        intent = m.withdraw_intent(conn, intent_id)
        return jsonify({"ok": True, **intent}), 202

    def _holding_sell_networks():
        from sniper.services.memecoin_evm_holdings import supported_chains
        supported = supported_chains()
        hb = read_heartbeat(ctx.get("heartbeat_path", HEARTBEAT_PATH))
        status = hb.get("other_holdings") if isinstance(hb, dict) else None
        status = status if isinstance(status, dict) else {}
        # Older pinned runners consume only Robinhood EVM holdings. New-chain buttons require
        # the runner's executor map, including during a web-first cutover or rollback.
        if "evm_ready_chains" in status:
            ready = status["evm_ready_chains"]
            supported &= {c for c in ready if isinstance(c, str)} if isinstance(ready, list) else set()
        else:
            supported &= {"4663"}
        return {"solana", *supported}

    @bp.get("/api/memecoin/holdings")
    def other_holdings():
        """Tokens outside funded positions, from the runner's asynchronous wallet scans."""
        from sniper.util.state_paths import state_path
        from sniper.execution.evm_wallet import CHAIN_QUOTE, QUOTE_SYMBOL
        supported = _holding_sell_networks()
        chains = m.other_holdings(conn)
        try:
            hidden = json.loads(state_path('data', 'memecoin_hidden_holdings.json').read_text())
            if not isinstance(hidden, dict):
                hidden = {}
        except FileNotFoundError:
            hidden = {}
        except (OSError, ValueError):
            logger.warning('Could not read the hidden holdings list')
            hidden = {}
        for chain, net in chains.items():
            net['coverage'] = 'wallet_enumeration' if chain in ('solana', '4663') else 'known_inventory'
            omitted = hidden.get(chain)
            if isinstance(omitted, list):
                hidden_mints = set()
                now = _utc_instant(_now_iso())
                for entry in omitted:
                    if isinstance(entry, str):
                        hidden_mints.add(entry)
                    elif isinstance(entry, dict) and isinstance(entry.get('mint'), str):
                        until = _utc_instant(entry.get('until'))
                        if until is not None and now is not None and until > now:
                            hidden_mints.add(entry.get('mint'))
                net['rows'] = [r for r in net['rows'] if r['mint'] not in hidden_mints
                               or (r.get('sell') or {}).get('state') in
                               ('queued', 'claimed', 'submitted', 'approval_submitted')]
            # Display only: hide total holding values below $0.60, including pending sells.
            # Unknown values stay visible; balances and sell intents remain untouched.
            def is_dust(row):
                value = row.get('value_usd')
                return (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and 0 <= value < 0.60)
            net['dust_count'] = sum(is_dust(r) for r in net['rows'])
            net['rows'] = [r for r in net['rows'] if not is_dust(r)]
            for row in net["rows"]:
                # A tiny pool's marginal price cannot value a balance larger than its liquidity.
                # Display only: keep the balance and sell authority, never invent a proceeds cap.
                value, liquidity = row.get('value_usd'), row.get('liquidity_usd')
                if (all(isinstance(x, (int, float)) and not isinstance(x, bool)
                        and math.isfinite(x) and x >= 0 for x in (value, liquidity))
                        and value > liquidity):
                    row['price_usd'] = None
                    row['value_usd'] = None
                    row['valuation_note'] = ('Reference value exceeds reported pool liquidity; '
                                             'sale value unknown')
                # A TOKEN NOBODY QUOTES IS NOT A SELL BUTTON (2026-09-22, operator). Every row
                # offered one, so a marketless airdrop read as a red "Failed" and invited another
                # click — LIAM alone was clicked 6 times in a day, and each click spends a LI.FI
                # quote. Measured that day: LIAM has no V2 pair with WETH or either connector, no
                # launchpad curve, no pool in our own store and no DexScreener pair, while its
                # $1.47 is a LI.FI reference price, not a bid.
                #
                # THE MARK IS EVIDENCE, NOT A GUESS, AND IT EXPIRES BY ITSELF: it needs a real
                # refusal from the venues AND a row that still shows no liquidity. The moment a
                # pool appears the snapshot prices it again and the button comes back, so this can
                # never strand a token that becomes sellable later.
                # ⚠️ ONLY A REFUSAL THAT ASKED THE VENUES COUNTS (2026-09-23). "no validated
                # LI.FI route" is the branch `memecoin_evm_holdings.sell_once` takes when NO
                # LADDER IS WIRED — it is the message for "one venue was asked", so reading it as
                # "no venue quotes it" hides the button on the strength of a question never put.
                # UNIFROG proved it: its only failure is that message, recorded 2026-09-22T18:04Z
                # before the ladder existed, and its whole bag quotes 0.0012983 ETH today on the
                # native-paired V4 pool 0xe29f7e68… — a pool DexScreener does not index, so
                # `liquidity_usd` is None and the guard fired on both clauses at once.
                # LIAM and BFONE carry the "both refused" message and neither has a V4 quote, so
                # they stay hidden, which is the behaviour the guard was built for.
                sale = row.get("sell") or {}
                refused = (sale.get("state") == "failed"
                           and sale.get("reason") ==
                           "no route: LI.FI (no_route) and the venue ladder both refused")
                no_market = bool(refused) and not row.get("liquidity_usd")
                row["quote_symbol"] = ("USDC" if chain == "solana" else
                                       QUOTE_SYMBOL.get(chain, "USDC")
                                       if chain in CHAIN_QUOTE else None)
                row["sellable"] = chain in supported and not no_market
                row["why_not"] = ("holding sales are unavailable on this chain" if chain not in supported
                                  else "no market — no venue quotes it" if no_market else None)
                if no_market:
                    # An unquoted reference mark is not realizable holdings value. Keep the
                    # balance and intent; the stored snapshot remains untouched.
                    row["price_usd"] = None
                    row["value_usd"] = None
            net['total_usd'] = sum(float(r.get('value_usd') or 0) for r in net['rows'])
            net['unpriced_count'] = sum(r.get('price_usd') is None for r in net['rows'])
        return jsonify({"as_of": _now_iso(), "chains": chains,
                        "unpriced_count": sum(int(n.get("unpriced_count") or 0)
                                              for n in chains.values()),
                        "scanned_chains": sorted(chains) or ["solana"],
                        "csrf_token": _csrf_token()})

    @bp.post("/api/memecoin/holdings/sell")
    def other_holdings_sell():
        """Queue one sell of a leftover token. The runner sells it under the wallet lock."""
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        d = request.get_json(force=True, silent=True) or {}
        from sniper.services.memecoin_chains import canonical_chain_id
        network_id = canonical_chain_id(d.get("network_id"))
        mint = str(d.get("mint") or "").strip()
        allow_small = d.get("allow_small", False)
        if not isinstance(allow_small, bool):
            return jsonify({"ok": False, "reason": "allow_small must be a boolean"}), 400
        if network_id not in _holding_sell_networks() or not mint:
            return jsonify({"ok": False, "reason": "holding sales are unavailable on this chain"}), 400
        if network_id != "solana":
            mint = mint.lower()
        held = {r["mint"] if network_id == "solana" else r["mint"].lower()
                for r in (m.other_holdings(conn).get(network_id) or {}).get("rows", [])}
        if mint not in held:
            return jsonify({"ok": False, "reason": "not in the last wallet scan"}), 404
        try:
            intent_id = m.enqueue_holding_sell(conn, network_id, mint, now_iso=_now_iso(),
                                               allow_small=allow_small)
        except ValueError as exc:
            return jsonify({"ok": False, "reason": str(exc)}), 409
        return jsonify({"ok": True, "intent_id": intent_id, "state": "queued"}), 202

    @bp.get("/api/memecoin/real/exit_intent/<intent_id>")
    def real_exit_intent_status(intent_id):
        from sniper.services.memecoin_manual_exit_intents import read_status
        row = read_status(conn, intent_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown intent"}), 404
        return jsonify({"ok": True, "intent": row})

    @bp.post("/api/memecoin/positions/<int:pid>/close")
    def manual_close(pid):
        from sniper.execution.solana_quote import dexscreener_stats, modeled_fill
        from sniper.services.memecoin_copy_trader import _realized_from_exits
        pos = m.get_position(conn, pid)
        if not pos or not pos.get("is_open"):
            return jsonify({"ok": False, "error": "not open"}), 404
        # SAFETY: a real (on-chain) position must NEVER be closed by a modeled DexScreener fill —
        # that books a fictional PnL while the tokens still sit on-chain. Route the operator's
        # take-profit button through the real exit executor (chain-delta PnL, slippage ladder,
        # stuck-bag on failure). Fail LOUD (503) if no real exit fn is bound rather than fall back.
        if pos.get("execution_mode") == "real":
            # THE WEB DOES NOT SELL (2026-09-05). A real close crosses to the runner as an INTENT and
            # the runner sells with ITS executors and ITS config; this process only writes the row.
            # Same rule the manual buy adopted on 2026-08-26, for the same reason (position 44706).
            # 202 + status 'queued': the dashboard polls /real/exit_intent/<id> for the outcome.
            (ctx.get("ensure_real_fns") or (lambda: None))()
            enqueue_exit = ctx.get("real_exit_intent_fn")
            if enqueue_exit is None:
                return jsonify({"ok": False, "error": "real execution not configured"}), 503
            d = request.get_json(force=True, silent=True) or {}
            # "Sell at any price" on a row the anchor-checked exit holds (spec 2026-10-01 §3.2.5).
            action = "close_any_price" if d.get("any_price") is True else "close"
            res = enqueue_exit(conn, pos, action, actor=(d.get("actor") or "operator")) or {}
            if res.get("status") != "queued":
                return jsonify({"ok": False, **res}), 400
            _notify_operator(f"MANUAL CLOSE QUEUED pos#{pid}",
                             f"{pos.get('ticker') or pos.get('mint')} {pos.get('network_id')} "
                             f"intent={str(res.get('intent_id'))[:8]}")
            return jsonify({"ok": False, **res}), 202
        m.set_experiment_status(conn, pid, "operator_modified")
        if not m.claim_close(conn, pid):          # lost the race to a bracket/mirror close
            return jsonify({"ok": False, "error": "already closed"}), 409
        try:
            st = dexscreener_stats(pos["mint"], network_id=pos.get("network_id")) or {}
        except TypeError as exc:
            if "network_id" not in str(exc) or "unexpected keyword" not in str(exc):
                raise
            st = dexscreener_stats(pos["mint"]) or {}
        mid = st.get("price")
        qty = m.open_qty(conn, pid)  # remaining after any partial sells
        # modeled exit fill; entry-price flat only as a last resort so the operator's
        # explicit close still completes when no live quote is available.
        price = (modeled_fill(mid, mid * qty, st.get("liquidity"), "sell")
                 if mid and mid > 0 else pos["entry_price"])
        now = _now_iso()
        m.insert_fill(conn, position_id=pid, kind="manual_close", price=price, qty=qty,
                      notional=price * qty, fill_source="dexscreener",
                      fraction=1.0, created_at=now)
        m.finalize_close(conn, pid, now, _realized_from_exits(conn, pid, pos["entry_price"]),
                         exit_reason="manual")
        return jsonify({"ok": True})

    @bp.post("/api/memecoin/positions/<int:pid>/sell")
    def partial_sell(pid):
        from sniper.services.memecoin_copy_trader import partial_exit
        from sniper.execution.solana_quote import dexscreener_stats
        d = request.get_json(force=True, silent=True) or {}
        try:
            fraction = float(d.get("fraction", 1.0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad fraction"}), 400
        rpos = m.get_position(conn, pid)
        if rpos and rpos.get("execution_mode") == "real":
            # REAL partial take-profit (2026-07-18): sell `fraction` on-chain via the SAME exact-balance
            # derisk executor the runner uses (slippage ladder, claim guard, kind='derisk' fill); the
            # position stays open. Was rejected before as 'not supported' — never a modeled fake fill on
            # a real bag. A full exit still routes through /close.
            if not rpos.get("is_open"):
                return jsonify({"ok": False, "error": "not open"}), 404
            if not (0.0 < fraction < 1.0):
                return jsonify({"ok": False,
                                "error": "fraction must be in (0,1) — use Close for a full exit"}), 400
            # THE WEB DOES NOT SELL (2026-09-05): the partial crosses to the runner as an intent.
            (ctx.get("ensure_real_fns") or (lambda: None))()
            enqueue_exit = ctx.get("real_exit_intent_fn")
            if enqueue_exit is None:
                return jsonify({"ok": False, "error": "real execution not configured"}), 503
            res = enqueue_exit(conn, rpos, "partial", fraction,
                               actor=(d.get("actor") or "operator")) or {}
            if res.get("status") != "queued":
                return jsonify({"ok": False, **res}), 400
            _notify_operator(f"MANUAL PARTIAL QUEUED pos#{pid}",
                             f"{rpos.get('ticker') or rpos.get('mint')} {rpos.get('network_id')} "
                             f"fraction={fraction} intent={str(res.get('intent_id'))[:8]}")
            return jsonify({"ok": False, **res}), 202
        if not rpos or not rpos.get("is_open"):
            return jsonify({"ok": False, "error": "not open"}), 404
        # Exclude before any modeled fill can be booked. A failed quote remains excluded because
        # the operator intervention, not the automatic policy, now owns the position's lifecycle.
        m.set_experiment_status(conn, pid, "operator_modified")
        r = partial_exit(conn, pid, fraction, _now_iso(),
                         price_fn=ctx.get("price_fn", dexscreener_stats))
        if r is None:
            return jsonify({"ok": False, "error": "not open"}), 404
        return jsonify({"ok": True, **r})

    @bp.post("/api/memecoin/manual-buy")
    def manual_buy_route():
        import os as _os
        from sniper.services.memecoin_copy_trader import manual_buy
        from sniper.execution.solana_quote import honest_fill
        d = request.get_json(force=True, silent=True) or {}
        uid = (d.get("source_user_id") or "").strip()
        mint = (d.get("mint") or "").strip()
        ticker = (d.get("ticker") or "").strip()
        if not uid or not mint:
            return jsonify({"ok": False, "error": "source_user_id and mint required"}), 400
        try:
            size = float(d.get("size_usd") or _os.getenv("MEMECOIN_COPY_SIZE_USD", "100"))
        except (TypeError, ValueError):
            size = 100.0
        pid = manual_buy(conn, uid, mint, ticker, size, _now_iso(),
                         fill_fn=ctx.get("fill_fn", honest_fill))
        if not pid:
            return jsonify({"ok": False, "error": "already open or fill failed"}), 400
        return jsonify({"ok": True, "position_id": pid})

    @bp.get("/api/memecoin/traders/performance")
    def traders_performance():
        return jsonify({"traders": m.trader_performance(conn)})

    @bp.get("/api/memecoin/traders/arm-matrix")
    def traders_arm_matrix():
        # Wallet x arm closed-copy PnL matrix (read-only), scoped to ONE chain. rows=wallets,
        # cols=exit-policy arms. There is no pooled scope — chains are never mixed (2026-07-30).
        from sniper.services.memecoin_chains import resolve_chain
        chain = resolve_chain(request.args.get("chain"))
        if chain is None:
            return jsonify({"error": "chain_required",
                            "detail": "chains are never pooled; name one"}), 400
        matrix = m.trader_arm_matrix(conn, chain=chain)
        # OPEN exposure, merged in (2026-08-04). The matrix itself is CLOSED-only by design — arm
        # comparison needs completed outcomes — but that made a chain whose losses sit in open
        # inventory unattributable: robin's paper report reads ~-$1,017 across 143 open rows while
        # this table showed +$218 from 10 closes, and its three biggest losers had no closed row so
        # they were absent entirely. The operator asked "where do the losses come from" and the
        # table could not answer. Closed and open stay SEPARATE columns — never summed into one
        # number — because only the closed side is comparable across arms.
        from sniper.services.memecoin_paper_report import open_exposure_by_wallet
        try:
            exposure = open_exposure_by_wallet(conn, chain)
            matrix["open_exposure_available"] = True
        except Exception:
            # An empty dict would render as "every wallet holds nothing", which is a CLAIM. A
            # failure must say unavailable instead — the board cannot distinguish the two on its
            # own, and "no inventory" is the more dangerous of the two to state falsely.
            logger.exception("open exposure unavailable for chain=%s", chain)
            exposure = {}
            matrix["open_exposure_available"] = False
        seen = set()
        for wallet in matrix.get("wallets") or []:
            seen.add(wallet["source_user_id"])
            wallet.update(exposure.get(wallet["source_user_id"]) or {
                "open_positions": 0, "open_marked_pnl": None,
                "open_unpriced": 0, "open_stale": 0})
        # A wallet carrying open inventory but no comparable closed row is exactly the case that
        # vanished. It cannot join the arm comparison, so it is reported separately rather than
        # forced into a table whose columns it has no values for.
        matrix["open_only"] = sorted(
            ({"source_user_id": uid, **data}
             for uid, data in exposure.items()
             if uid not in seen and data.get("open_positions")),
            key=lambda row: (row["open_marked_pnl"] is None, row["open_marked_pnl"] or 0.0))
        priced = [data["open_marked_pnl"] for data in exposure.values()
                  if data.get("open_marked_pnl") is not None]
        matrix["open_marked_total"] = round(sum(priced), 2) if priced else None
        # Scope, stated: this is the WHOLE open ledger on the chain. The graded paper report
        # covers only the active exact cohort, so the two do NOT reconcile and must not be read
        # as the same quantity.
        matrix["open_scope"] = "whole_ledger"
        return jsonify(matrix)

    @bp.get("/api/memecoin/wallet-map/herders")
    def wallet_map_herders():
        # Read-only herders table (spec 2026-09-30-memecoin-wallet-map-overhaul-design.md, Gamma CLEAN
        # d16c454df). Its own read-only connections per request: never the shared writable `conn`,
        # never a funded seam, never a network call.
        from sniper.services.memecoin_wallet_map import build_herders, open_graph_readonly, own_address_set
        from sniper.persistence import db_core
        from sniper.util.state_paths import state_path
        import sniper.settings.memecoin as mc_settings
        chain = str(request.args.get("chain") or "solana").strip().lower()
        if chain != "solana":
            return jsonify({"stamp": {"chain": chain, "error": None, "graph_state": "not_measured",
                                      "note": "no link data for this chain yet"},
                            "herders": [], "shared": [], "probes": []})
        graph = ledger = None
        try:
            try:
                graph = open_graph_readonly(state_path("data", "memecoin_wallet_graph.db"))
            except sqlite3.Error:
                # NEVER AN EMPTY STAND-IN (Wallet Map D1, 2026-10-07): None reads graph_unavailable.
                logger.exception("wallet map: graph DB open failed")
            ledger = db_core.get_readonly_connection()
            payload = build_herders(graph, ledger, now_iso=_now_iso(),
                                    own_addresses=own_address_set(mc_settings.MEMECOIN_OWN_ADDRESSES))
        finally:
            for handle in (graph, ledger):
                if handle is not None:
                    handle.close()
        return jsonify(payload)

    @bp.get("/api/memecoin/fill-timing/today")
    def fill_timing_today():
        """The Copy tab's "Fills today" (fill timing, 2026-10-02): every submitted funded entry since
        00:00 UTC with its S+n and why, from the resolver's rows in data/memecoin_fill_timing.db
        (read-only) joined to the attempts. Submitted attempts only: refusals never sent."""
        from sniper.services import memecoin_fill_timing as ft
        timing = ft.open_readonly()
        if timing is None:
            return jsonify({"rows": [], "note": "no timing file yet"})
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        try:
            rows = [dict(r) for r in timing.execute(
                "SELECT * FROM memecoin_fill_timing WHERE completed_at >= ? ORDER BY completed_at DESC LIMIT 300",
                (midnight,))]
        finally:
            timing.close()
        ids = [r["attempt_id"] for r in rows]
        attempts, refreshed = {}, {}
        if ids:
            with m.operator_read_connection(conn) as observer:
                attempts = {r["attempt_id"]: dict(r) for r in observer.execute(
                    "SELECT attempt_id, ticker, mint, network_id, signal_source, delivery_lane, source_handle, "
                    "terminal_status, terminal_reason, completed_at, origin_event_id, observed_at, tx_sig, "
                    "transaction_submitted_at, chain_confirmation_completed_at, "
                    "trace_json FROM memecoin_entry_attempts "
                    "WHERE attempt_id IN (%s)" % ",".join("?" * len(ids)), ids)}
                refreshed = ft.view_details(observer, list(attempts.values()))
        out = []
        for r in rows:
            a = attempts.get(r["attempt_id"]) or {}
            current = refreshed.get(r["attempt_id"]) or {}
            if "cause" not in current:
                current = {}  # A failed refresh must preserve the known cached landing and explanation together.
            out.append({"attempt_id": r["attempt_id"], "at": r.get("completed_at"), "ticker": a.get("ticker"),
                        "mint": a.get("mint"), "network_id": r.get("network_id"), "lane": r.get("lane"),
                        "handle": a.get("source_handle"), "status": a.get("terminal_status"),
                        "source_slot": r.get("source_slot"), "landing_slot": r.get("landing_slot"),
                        "gap": r.get("gap"), "gap_s": r.get("gap_s"), "cause": current.get("cause", r.get("cause")),
                        "detail": current.get("detail", r.get("cause_detail")), "state": r.get("state"),
                        "stages": current.get("stages", json.loads(r.get("stages_json") or "{}")),
                        "line": current.get("line") or ft.line_from_row(r)})
        return jsonify({"rows": out})

    @bp.get("/api/memecoin/wallet-map/funnel")
    def wallet_map_funnel():
        from sniper.util.state_paths import state_path
        from sniper.services.memecoin_wallet_map import funnel_live_overlay
        from sniper.settings import memecoin as _mc_cfg
        from sniper.util.clock import real_utc_now_iso
        from sniper.persistence import db_core
        if request.path == "/api/memecoin/wallet-map/funnel" and request.args.get("diagnostic_only") == "1":
            from sniper.research.wallet_execution.publication import legacy_eligibility
            path = state_path("data", "wallet_follow_funnel.json")
            try:
                if path.stat().st_size > 2_000_000:
                    raise ValueError("archive exceeds bound")
                body = json.loads(path.read_text())
                if not isinstance(body, dict) or not isinstance(body.get("stamp"), dict) or not isinstance(body.get("funnel"), list):
                    raise ValueError("invalid archive")
                body["stamp"]["age_s"] = round(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)
            except FileNotFoundError:
                body = {"stamp": {"error": "not_generated"}}
            except (OSError, ValueError):
                body = {"stamp": {"error": "unreadable"}}
            body["research_eligibility"] = legacy_eligibility()
            body["publication"] = "diagnostic_only"
            body.pop("clips", None)
            body["stamp"].pop("authority_state", None)
            return jsonify(body)
        body = {"stamp": {"mode": "live_authority"}}
        stamp = body.setdefault("stamp", {})
        ledger = None
        try:
            # Memory fixtures have no second connection. File-backed requests own their handle.
            filename = conn.execute("PRAGMA database_list").fetchone()[2]
            ledger = db_core.get_readonly_connection(filename) if filename else conn
            body = funnel_live_overlay(body, ledger, _mc_cfg, real_utc_now_iso())
        except Exception:
            logger.exception("wallet map: live authority unavailable")
            stamp.update(live_error=True, authority_state="unavailable")
        finally:
            if ledger is not None and ledger is not conn:
                ledger.close()
        return jsonify(body)

    @bp.get("/api/memecoin/wallet-map/execution-research")
    def wallet_execution_research():
        from sniper.services.memecoin_chains import canonical_chain_id
        from sniper.services.wallet_execution_research import read_coverage
        from sniper.util.state_paths import state_path
        from sniper.util.clock import real_utc_now_iso
        chain = canonical_chain_id(request.args.get('chain', 'solana'))
        if chain is None:
            return jsonify(error='unknown_chain'), 400
        wallet = request.args.get('wallet')
        if wallet is not None and len(wallet) > 160:
            return jsonify(error='invalid_wallet'), 400
        return jsonify(read_coverage(state_path('data', 'wallet_execution_coverage.json'),
                                     chain=chain, wallet=wallet, now=real_utc_now_iso()))

    @bp.route("/api/memecoin/wallet-map/dossier", methods=["GET", "POST"])
    def wallet_map_dossier():
        from sniper.services.memecoin_chains import canonical_chain_id
        from sniper.services.wallet_execution_research import read_dossier
        from sniper.persistence import db_wallet_execution as research_store
        from sniper.util.state_paths import state_path
        from sniper.util.clock import real_utc_now_iso
        chain = canonical_chain_id(request.args.get('chain', 'solana'))
        wallet = request.args.get('wallet', '').strip()
        if chain is None:
            return jsonify(error='unknown_chain'), 400
        if not wallet or len(wallet) > 160 or any(ord(c) < 32 for c in wallet):
            return jsonify(error='invalid_wallet'), 400
        path = state_path('data', 'wallet_execution_research.db')
        now = real_utc_now_iso()
        if request.method == 'POST':
            if not _csrf_ok():
                return jsonify(ok=False, error='csrf rejected'), 403
            payload = request.get_json(silent=True)
            text = payload.get('text') if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                return jsonify(ok=False, error='note must contain 1 to 4000 characters'), 400
            try:
                from contextlib import closing
                with closing(research_store.open_store(path)) as research:
                    with research:
                        note = research_store.append_dossier_note(research, wallet=wallet, chain=chain, text=text, now=now)
            except (OSError, sqlite3.Error, ValueError) as exc:
                logger.warning('wallet dossier note unavailable: %s', exc)
                return jsonify(ok=False, error='research_store_unavailable'), 503
            return jsonify(ok=True, note=note), 201
        return jsonify(read_dossier(path, wallet=wallet, chain=chain, now=now))

    @bp.get("/api/memecoin/wallet-map/universe")
    def wallet_map_universe():
        from sniper.services.memecoin_chains import canonical_chain_id
        from sniper.util.state_paths import state_path
        chain = canonical_chain_id(request.args.get('chain', 'solana'))
        if chain is None:
            return jsonify(error='unknown_chain'), 400
        # THE LIVE GRAPH, NOT THE RETIRED SNAPSHOT (Wallet Map D1, spec 2026-10-07-wallet-map-
        # overhaul-design.md). data/wallet_universe_<chain>.json lost its publisher on 2026-10-05,
        # so this route served "0 links" for a graph that held ~880 edges. Edges now come from
        # memecoin_wallet_edges read-only, each wallet's funded record from real_stats_by_wallet.
        from sniper.services.memecoin_wallet_map import LINK_TYPES, live_universe_edges
        from sniper.util.clock import real_utc_now_iso
        live = wallet_map_funnel().get_json()
        excluded=set(live.get('excluded_uids', []))
        links = live_universe_edges(state_path('data', 'memecoin_wallet_graph.db'), chain=chain,
                                    now_iso=real_utc_now_iso(), excluded=excluded)
        graph = links['graph']
        body = dict(version=1, chain=chain, wallets=[], edges=links['edges'], graph_state=graph['state'],
                    graph=graph, coverage_by_ref=links['coverage_by_ref'], generated_at=graph['run_finished_at'],
                    hypothesis_export=dict(state='not_available', reason='legacy setups retired'),
                    funded_scope=("funded robot entries on this chain (execution_mode='real', grade "
                                  "exclusions applied); only wallets we armed have rows"))
        from sniper.services.memecoin_chains import CHAINS
        body['selection_gates'] = sorted(m.EXEMPTIBLE_GATE_FAMILIES)
        body['chains'] = [dict(id=c['network_ids'][0], label=c['label']) for c in CHAINS if c['key'] != 'unresolved']
        body['clips'] = live.get('clips', {})
        body['stamp'] = live.get('stamp', {})
        try:
            with m.operator_read_connection(conn) as observer:
                funded = m.real_stats_by_wallet(observer, chain)
        except (sqlite3.Error, ValueError):
            logger.exception("wallet map universe: funded record read failed chain=%s", chain)
            funded = None
        for row in live.get('funnel', []):
            wallet = dict(uid=row['uid'], chain=chain, handle=row.get('handle') or row['uid'],
                          paper=None, setups=[], coverage={})
            wallet['funded'] = (dict(state='unavailable') if funded is None
                                else dict(state='ok', **funded[row['uid']]) if row['uid'] in funded
                                else dict(state='none', open_count=0, closed_count=0))
            mine = (links['link_counts'] or {}).get(row['uid'], {})
            wallet['links'] = dict(state=graph['state'], by_type=None if links['link_counts'] is None
                                   else {k: mine.get(k, 0) for k in LINK_TYPES})
            body['wallets'].append(wallet)
            wallet.setdefault('coverage', {})['address'] = dict(state='unavailable' if row.get('proof_state')=='unavailable' else 'ok' if chain in row.get('address_chains', []) else 'not_proven', source='live verified registry')
            wallet.update(from_roster=True,armed=chain in row.get('armed', []),
                          effective=chain in row.get('effective_chains', []), paused=row.get('paused', False))
        from sniper.services.wallet_execution_research import read_coverage
        body['execution_research'] = read_coverage(
            state_path('data', 'wallet_execution_coverage.json'), chain=chain, now=real_utc_now_iso())
        for wallet in body['wallets']:
            # A cached map artifact cannot restore the retired paper-to-policy selector.
            wallet['strict_policy'] = dict(arm=None, blocked_reason='Research needs complete execution paths and future validation')
        from sniper.services.wallet_universe_metrics import read_metrics
        from sniper.services.wallet_observation import observation_owners
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.persistence.db_wallet_identity import read_account_claims
        from sniper.services.wallet_identity import read_identity
        identity=dict(state='unavailable',reason='proof_unavailable',wallets={})
        proof=None
        try:
            proof=get_readonly_connection()
            try:
                identity=read_identity(state_path('data','wallet_identity.json'),read_account_claims(proof),
                                       chain=chain,now_iso=real_utc_now_iso())
            except (sqlite3.Error,ValueError):
                logger.exception('wallet map universe: identity proof read failed chain=%s',chain)
            owners=observation_owners(proof,{w['uid'] for w in body['wallets']})
            metrics=read_metrics(state_path('data','wallet_universe_metrics.json'),owners,chain=chain,now=real_utc_now_iso())
        except (sqlite3.Error,ValueError):
            metrics=dict(wallets={},collection=dict(state='proof_unavailable'))
        finally:
            if proof is not None:proof.close()
        body['measurements']={k:v for k,v in metrics.items() if k!='wallets'}
        body['identity_research']={k:v for k,v in identity.items() if k!='wallets'}
        for wallet in body['wallets']:
            wallet['metrics']=metrics['wallets'].get(wallet['uid'],dict(capital=dict(state='address_unproven',usd=None),impact=dict(state='address_unproven',value_usd=None)))
            wallet['identity']=identity['wallets'].get(wallet['uid'],dict(state='not_measured' if chain!='solana' else identity['state'],
                verified_addresses=[],address_conflicts=[],related_wallets=None,coverage=dict(state='unavailable')))
        return jsonify(body)

    @bp.get("/api/memecoin/wallet-map/wallet")
    def wallet_map_wallet():
        # One herder read left to right in time (the same spec, slice 3). Read-only, as the table.
        from sniper.services.memecoin_wallet_map import build_wallet_view, open_graph_readonly, own_address_set
        from sniper.persistence import db_core
        from sniper.util.state_paths import state_path
        import sniper.settings.memecoin as mc_settings
        from sniper.services.memecoin_chains import canonical_chain_id
        from sniper.services.memecoin_wallet_map import shared_with, wallet_gaps
        wallet = str(request.args.get("id") or "").strip()
        if not wallet:
            return jsonify({"error": "id_required"}), 400
        chain = canonical_chain_id(request.args.get("chain") or "solana")
        if chain is None:
            return jsonify({"error": "unknown_chain"}), 400
        if chain != "solana":
            # AVAILABILITY, NOT AN ERROR (Wallet Map D2): the link graph measures Solana only, so the
            # page renders "not measured" from a 200 instead of failing on a 400.
            return jsonify({"graph_state": "not_measured",
                            "stamp": {"chain": chain, "error": None, "graph_state": "not_measured",
                                      "note": "no link data for this chain yet"},
                            "wallet": None, "before": {"state": "not_measured_yet"}, "herd": None,
                            "sells_into_it": [], "it_sells_into": [], "coins": [], "patterns": None,
                            "details": None, "gaps": wallet_gaps({"graph_state": "not_measured"})})
        other = str(request.args.get("with") or "").strip()
        graph = ledger = None
        try:
            try:
                graph = open_graph_readonly(state_path("data", "memecoin_wallet_graph.db"))
            except sqlite3.Error:
                # NEVER AN EMPTY STAND-IN (Wallet Map D1, 2026-10-07): None reads graph_unavailable.
                logger.exception("wallet map: graph DB open failed")
            ledger = db_core.get_readonly_connection()
            payload = build_wallet_view(graph, ledger, wallet=wallet, now_iso=_now_iso(),
                                        own_addresses=own_address_set(mc_settings.MEMECOIN_OWN_ADDRESSES))
            payload["graph_state"] = payload["stamp"]["graph_state"]
            if other:
                payload["with"] = shared_with(graph, wallet, other, now_iso=_now_iso())
        finally:
            for handle in (graph, ledger):
                if handle is not None:
                    handle.close()
        return jsonify(payload)

    @bp.get("/api/memecoin/traders/analytics")
    def traders_analytics():
        # Raw per-trader stats from fomo swap ledgers (populated by
        # scripts/analyze_fomo_accounts.py). Read-only; shared conn, never closed.
        return jsonify({"rows": m.list_trader_analytics(conn)})

    @bp.get("/api/memecoin/traders/combined")
    def traders_combined():
        # curated wallets + analyzer stats + copy perf + last-signal + selection gate
        return jsonify({"rows": _decorate_rows(m.list_curated_with_stats(conn), _now_iso())})

    def _j7_tracked_set():
        """Every wallet J7 currently tracks, or an empty set when no list is stored.

        Empty means UNKNOWN here, never "J7 tracks nobody": both boards render the marker only
        when this is non-empty, so a runner that has not yet stored a list cannot paint the whole
        roster as unreachable.
        """
        try:
            return m.j7_tracked_ids(conn)
        except Exception:
            return set()

    def _pump_map(uids=None):
        # per-trader "do their buys pump" read (matured post-buy outcomes); merged into
        # the trader/candidate surfaces. min_signals=1 so the UI can show n and grey thin rows.
        return {p["source_user_id"]: p for p in m.trader_pump_profile(
            conn, min_signals=1, source_user_ids=uids)}

    def _gate_for(uid, now, arow=None, snaps=None):
        """Full gate for one trader. `arow`/`snaps` may be pre-fetched (batched) to avoid an
        N+1; when None they fall back to a per-uid query (single-trader callers)."""
        from sniper.analysis.fomo_account.selection_gate import selection_gate
        row = arow if arow is not None else (m.get_trader_analytics(conn, uid) or {"source_user_id": uid})
        w7 = m.windowed_realized(conn, uid, 7, now, snaps=snaps)
        w30 = m.windowed_realized(conn, uid, 30, now, snaps=snaps)
        return {"gate": selection_gate(row, w7d=w7, w30d=w30),
                "fomo_pnl_delta_7d": m.fomo_pnl_delta(conn, uid, 7, now, snaps=snaps),
                "fomo_pnl_delta_30d": m.fomo_pnl_delta(conn, uid, 30, now, snaps=snaps),
                "w7d": w7, "w30d": w30}

    def _decorate_rows(rows, now):
        """Batch-decorate a list of trader/candidate rows with gate + pump + latest thesis in
        O(1) queries instead of N+1 per-row fetches."""
        from sniper.analysis.memecoin_thesis import thesis_stance
        uids = [r["source_user_id"] for r in rows]
        asnap = m.batch_snapshots(conn, uids)
        aana = m.batch_analytics(conn, uids)
        pm = _pump_map(uids)
        theses = m.latest_thesis_by_trader(conn, uids)
        tracked = _j7_tracked_set()
        for r in rows:
            uid = r["source_user_id"]
            # NOT ON J7 = CANNOT SIGNAL. J7 caps 50 wallets per account and relays only those;
            # fomo is banned and the global stream ended 2026-09-11. None (not False) while no
            # list is stored, so the UI shows nothing rather than marking every wallet dead.
            r["j7_tracked"] = (uid in tracked) if tracked else None
            r.update(_gate_for(uid, now, arow=aana.get(uid) or {"source_user_id": uid},
                               snaps=asnap.get(uid, [])))
            r["pump"] = pm.get(uid)
            th = theses.get(uid)
            r["latest_thesis"] = ({**th, "stance": thesis_stance(th.get("their_pnl_pct"))}
                                  if th else None)
        return rows

    @bp.get("/api/memecoin/traders/gated")
    def traders_gated():
        # THE selection review surface: every screened/leaderboard trader with its raw stats
        # + the transparent green/red gate + forward-snapshot windows. No score, no verdict.
        now = _now_iso()
        return jsonify({"rows": _decorate_rows(m.list_trader_analytics(conn), now), "as_of": now})

    @bp.get("/api/memecoin/traders/pump")
    def traders_pump():
        # per-trader post-buy-pump profile (does their large buy move price). Ranking defaults to
        # MEMECOIN_PUMP_MIN_SIGNALS (thin samples excluded); override with ?n= (e.g. n=1 to show all).
        from sniper.settings.memecoin import MEMECOIN_PUMP_MIN_SIGNALS
        n = int(request.args.get("n", MEMECOIN_PUMP_MIN_SIGNALS))
        return jsonify({"rows": m.trader_pump_profile(conn, min_signals=n)})

    @bp.get("/api/memecoin/theses")
    def theses():
        # curated traders' posted reasoning (display/context only). stance = objective PnL
        # bucket. uid/mint filters; with a mint, also return the confluence count.
        from sniper.analysis.memecoin_thesis import decorate_thesis
        uid = request.args.get("uid") or None
        mint = request.args.get("mint") or None
        try:
            limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        except (TypeError, ValueError):
            limit = 50
        rows = [decorate_thesis(r) for r in m.list_theses(conn, uid=uid, mint=mint, limit=limit)]
        out = {"rows": rows}
        if mint:
            out["token_thesis_count"] = m.token_thesis_counts(
                conn, [mint], now_iso=_now_iso()).get(mint, 0)
        return jsonify(out)

    @bp.get("/api/memecoin/traders/<uid>/theses")
    def trader_theses(uid):
        from sniper.analysis.memecoin_thesis import decorate_thesis
        return jsonify({"rows": [decorate_thesis(r) for r in m.list_theses(conn, uid=uid, limit=50)]})

    @bp.get("/api/memecoin/alert-latency")
    def alert_latency():
        # our detection->send profile for the front-run test (the fomo-push comparison is a
        # manual phone read). n, median feed lag, median send ms, ok-rate, recent rows.
        return jsonify(m.alert_latency_summary(conn))

    @bp.get("/api/memecoin/study/holdband")
    def study_holdband():
        # pre-registered hold-band forward study — MONITORING ONLY (verdicts wait for gates).
        from sniper.analysis.memecoin_holdband import hold_band_readout, gates_met, decision
        now = _now_iso()
        r = hold_band_readout(conn, now_iso=now)
        met, reasons = gates_met(r, today=now[:10])
        # decision() is computed for transparency but is only VALID once gates.met is true.
        return jsonify({**r, "gates": {"met": met, "unmet": reasons},
                        "decision": decision(r) if met else {"h1": "gated", "reason": "gates not met"}})

    @bp.get("/api/memecoin/signal-sources")
    def signal_sources():
        # A/B: copy PnL by signal_source (global_feed real-time vs follow_activity late)
        return jsonify({"rows": m.signal_source_summary(conn)})

    @bp.get("/api/memecoin/strategies")
    def strategies():
        # execution-arm A/B/control: PnL + exit-liquidity rate per exit policy.
        # `rows` counts every HONEST position; `matched` is the apples-to-apples subset (events
        # where ALL arms entered) — use `matched` for the cross-arm read so a fast arm's re-buys,
        # which slow arms skip as a dup while still holding, can't skew the comparison.
        # Both drop fomo-priced fills, so they agree in sign with the arming metric. `contaminated`
        # is the old all-fills read, kept ONLY so the gap is auditable — never rank arms on it.
        #
        # `?chain=` scopes every read to ONE chain. Chains are never pooled (2026-07-30 per-chain
        # evidence doctrine): 4663 is DexScreener-blind, so its closes routinely take the censored
        # `_noquote` path whose shadow books a full -entry_notional. Pooled, that moved arm PnL by
        # ~$98 on a two-position book purely by which chain an arm happened to trade. `scope` is
        # echoed so a pooled board can never be misread as a per-chain one.
        chain = request.args.get("chain") or None
        return jsonify({"scope": chain or "pooled-all-chains",
                        "rows": m.strategy_summary(conn, network_id=chain),
                        "matched": m.strategy_summary_matched(conn, network_id=chain),
                        "contaminated": m.strategy_summary(conn, honest_only=False,
                                                           network_id=chain)})

    @bp.get("/api/memecoin/feed-live")
    def feed_live():
        from sniper.services.memecoin_runner import read_feed_snapshot_status
        hb = read_heartbeat(ctx.get("heartbeat_path", HEARTBEAT_PATH))
        return jsonify({**read_feed_snapshot_status(), "source_health": _source_health(hb)})

    @bp.get("/api/memecoin/real/incoming")
    def real_incoming():
        """Pre-trade board: recent large buys from ARMED wallets (the ones that trigger real entries),
        grouped by coin with consensus + whether we already hold it. Read-only — the 'what might I
        trade next' half of the cockpit, distinct from the full Radar firehose."""
        mins = min(360, max(5, int(request.args.get("mins", 90) or 90)))
        try:
            since = (datetime.fromisoformat(_now_iso()) - timedelta(minutes=mins)).isoformat()
        except (ValueError, TypeError):
            return jsonify({"rows": [], "window_mins": mins})
        rows = m.recent_armed_signals(conn, since_iso=since, limit=15)
        held = {(r["network_id"], r["mint"]) for r in conn.execute(
            "SELECT network_id, mint FROM memecoin_positions WHERE execution_mode='real' AND is_open=1")}
        for r in rows:
            r["held"] = (r.get("network_id"), r.get("mint")) in held
        return jsonify({"rows": rows, "window_mins": mins})

    @bp.post("/api/memecoin/screen")
    def screen():
        # fire-and-forget analyzer run (rate-limited, minutes) — populates trader_analytics.
        import fcntl
        import subprocess, sys as _sys, os as _os, tempfile as _tempfile
        nonlocal screen_process
        d = request.get_json(force=True, silent=True) or {}
        mode = d.get("mode", "candidates")
        if mode not in ("candidates", "all", "uid"):
            return jsonify({"ok": False, "error": "invalid_mode"}), 400
        try:
            n = str(min(200, max(1, int(d.get("n", 20)))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid_n"}), 400
        uid = str(d.get("uid", "")).strip()
        if mode == "uid" and not uid:
            return jsonify({"ok": False, "error": "uid_required"}), 400
        root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        if mode == "candidates":
            # Screen the rows the operator is ACTUALLY LOOKING AT. `--candidates N` re-queries
            # with the CLI's own defaults (min_usd=0, status='new', offset=0), so it screened a
            # different top-N than the filtered/paged board and the columns never moved on the
            # rows in front of them. Resolve the uids here with the board's own query instead.
            try:
                cand_min_usd = max(0.0, float(d.get("min_usd", 0) or 0))
                cand_offset = max(0, int(d.get("offset", 0) or 0))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid_candidate_query"}), 400
            cand_status = d.get("status", "new")
            if cand_status not in {"new", "ignored", "promoted", "excluded"}:
                return jsonify({"ok": False, "error": "invalid_candidate_status"}), 400
            targets = [r["source_user_id"] for r in m.list_candidates(
                conn, min_usd=cand_min_usd, limit=int(n), offset=cand_offset,
                status=cand_status, now_iso=_now_iso())]
            if not targets:
                return jsonify({"ok": False, "error": "no_candidates_match"}), 409
            arg = ["--ids", ",".join(targets)]
        else:
            targets = ([w["source_user_id"] for w in m.list_wallets(conn)]
                       if mode == "all" else [uid])
            arg = ["--all"] if mode == "all" else ["--uid", uid]
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        lock_path = ctx.get("screen_lock_path") or _os.path.join(
            _os.path.dirname(db_path) if db_path else _tempfile.gettempdir(),
            ".memecoin_fomo_screen.lock")
        with screen_process_lock:
            if screen_process is not None and screen_process.poll() is None:
                return jsonify({"ok": False, "error": "already_running"}), 409
            lock_file = open(lock_path, "a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                return jsonify({"ok": False, "error": "already_running"}), 409
            try:
                log = open(ctx.get("screen_log_path") or
                           _os.path.join(root, "logs", "fomo_screen_ui.log"), "a")
            except Exception:
                log = subprocess.DEVNULL
            try:
                screen_process = subprocess.Popen(
                    [_sys.executable, _os.path.join(root, "scripts", "analyze_fomo_accounts.py"),
                     *arg, "--sleep", "6"], cwd=root, stdout=log, stderr=log,
                    pass_fds=(lock_file.fileno(),), start_new_session=True)
            finally:
                lock_file.close()
                if hasattr(log, "close"):
                    log.close()
            screen_run.clear()
            # Keep the target uids: "did it work" is "did THESE traders' analytics advance",
            # which a global MAX(computed_at) cannot answer.
            screen_run.update({"mode": mode, "targets": len(targets),
                               "target_ids": list(targets), "started_at": _now_iso()})
        return jsonify({"ok": True, "started": True, "mode": mode, "n": n,
                        "targets": len(targets)})

    @bp.get("/api/memecoin/screen")
    def screen_status():
        """Is a screen running, did OUR last one accomplish anything, and when did the analyzer
        last write? Without this the buttons are unfalsifiable — the run takes minutes and
        nothing on the board changes until it lands, which reads identically to a dead button.

        Honest completion (audit 2026-08-03). "Process stopped" is not "it worked": the analyzer
        exits 0 having skipped every trader on 429s, and exits nonzero immediately on an auth
        failure. So this reports the local `exit_code` and `updated` — how many of THIS run's
        target traders actually have an analytics row newer than the run's start. The UI may
        only claim an update when `updated > 0`.

        `owner` distinguishes OUR subprocess from a run holding the flock that we did not
        start (CLI, or a previous web process). For an external run we know nothing about its
        mode, targets or start time, so those are null rather than the stale values from our
        own last run — which is what made an external lock report a finished local run's
        metadata as if it were live.
        """
        import fcntl
        import os as _os, tempfile as _tempfile
        ours = screen_process is not None and screen_process.poll() is None
        external = False
        if not ours:
            db_path = conn.execute("PRAGMA database_list").fetchone()[2]
            lock_path = ctx.get("screen_lock_path") or _os.path.join(
                _os.path.dirname(db_path) if db_path else _tempfile.gettempdir(),
                ".memecoin_fomo_screen.lock")
            try:
                probe = open(lock_path, "a+")
            except OSError:
                probe = None
            if probe is not None:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                except BlockingIOError:
                    external = True
                finally:
                    probe.close()
        last = conn.execute(
            "SELECT MAX(computed_at) FROM memecoin_trader_analytics").fetchone()[0]
        body = {"running": ours or external,
                "owner": "local" if ours else ("external" if external else None),
                "last_run": last, "mode": None, "targets": None, "started_at": None,
                "exit_code": None, "updated": None}
        started = screen_run.get("started_at")
        ids = screen_run.get("target_ids") or []
        if ours or (not external and started):
            # Ours — live, or finished and still the most recent thing we know about.
            body.update({"mode": screen_run.get("mode"),
                         "targets": screen_run.get("targets"), "started_at": started})
            if not ours:
                proc = screen_process
                body["exit_code"] = proc.poll() if proc is not None else None
            if ids and started:
                body["updated"] = conn.execute(
                    "SELECT COUNT(*) FROM memecoin_trader_analytics "
                    "WHERE computed_at > ? AND source_user_id IN (%s)"
                    % ",".join("?" * len(ids)), (started, *ids)).fetchone()[0]
        return jsonify(body)

    @bp.get("/api/memecoin/armed")
    def armed_roster():
        """Funded-authority board for ONE FUNDED chain: who may open real positions here, the
        paper evidence that would grant it on THIS chain, and their real record on THIS chain.

        Three states are reported SEPARATELY and must not be collapsed (audit 2026-08-03):
          - `real_approved`  — the wallet-level approval flag.
          - `armed_here`     — this chain is in the configured authority list. THIS is what the
                               arm/disarm control acts on.
          - `effective`      — approved AND configured here AND not paused AND not excluded, i.e.
                               a buy on this chain would actually be eligible to open.
        Using `effective` as the button state made a paused wallet already configured for Solana
        render "arm", so clicking wrote a redundant audit row and still showed off.

        Every curated wallet is listed, not just approved ones: disarming the last chain clears
        `real_approved`, and a board filtered on that flag dropped the wallet entirely, so its own
        "approve, then arm it here" instruction was not implementable through it. `relevant`
        marks the rows worth showing first; the UI collapses the rest behind a count.
        """
        from sniper.settings import memecoin as cfg
        from sniper.services.memecoin_chains import (
            canonical_chain_id, funded_chain_keys, network_ids_for, resolve_chain)
        chain = resolve_chain(request.args.get("chain"))
        if chain is None:
            return jsonify({"error": "chain_required",
                            "detail": "chains are never pooled; name one"}), 400
        # Funded authority does not exist for research chains or the `unresolved` bucket, so
        # this board must not offer them even as a read — a board that renders an arm control
        # for BSC implies BSC authority is a thing.
        if chain not in funded_chain_keys():
            return jsonify({"error": "chain_not_funded",
                            "detail": f"{chain} is research-only; funded chains are "
                                      f"{', '.join(funded_chain_keys())}"}), 400
        armed_effective = m.real_approved_user_ids(conn, network_id=chain)
        real = m.real_stats_by_wallet(conn, chain)
        matrix = {w["source_user_id"]: w
                  for w in m.trader_arm_matrix(conn, chain=chain)["wallets"]}
        # Legacy sampled-paper cells cannot select funded rails. No cohort
        # projection is needed to report this known blocked capability.
        from sniper.services.memecoin_copy_arm import select_from_cells
        strict_cells, strict_available = {}, True
        # Last signal must be scoped to THIS chain. Pooled, a Robin buy rendered as the
        # wallet's Solana signal, which is the same cross-chain read the doctrine forbids.
        # ONE grouped pass, not one query per wallet. The singular `alpha_b_copy_stats(chain=)`
        # cost 27ms and this loop runs over every curated wallet, so the board spent 14.4s of its
        # 16s here (527 wallets, measured 2026-09-08). Same predicate, same chain scope — the two
        # share `_alpha_b_scope_sql`, so the board cannot report a different number than the gate.
        # dense=True so every curated wallet is present: base and bsc return an EMPTY sparse
        # dict, and this number gates funded authority through real_approval_preview, so
        # "absent" must never be readable as anything but the explicit zero.
        gate_all = m.alpha_b_copy_stats_all(conn, chain=chain, dense=True)
        nets = tuple(network_ids_for(chain))
        last_signal = {r["source_user_id"]: r["last_signal"] for r in conn.execute(
            "SELECT source_user_id,MAX(created_at) AS last_signal FROM memecoin_events "
            "WHERE %s IN (%s) GROUP BY source_user_id"
            % (m.COIN_NETWORK_SQL, ",".join("?" * len(nets))), nets)}
        rows = []
        _armed_tracked = _j7_tracked_set()

        def _row_exit_rails(uid, chain):
            try:
                view = _exit_rails_view(uid, chain, with_audit=False)
            except Exception:
                logger.exception("exit rails unavailable wallet=%s chain=%s", uid, chain)
                return None
            return {**view["effective"], "source": view["source"],
                    "policy_id": view["default"]["policy_id"]}

        for w in m.list_wallets(conn):
            uid = w["source_user_id"]
            if w.get("excluded"):
                continue                      # exclusion is permanent; never re-armable
            configured = m.wallet_armed_chains(conn, uid)
            cells = (matrix.get(uid) or {}).get("by_arm") or {}
            # Best paper arm on this chain = highest realized PnL among arms with a close, ties
            # resolved toward the fallback then by name. Ranked by the SAME function the funded
            # copy-arm policy selects with, so this column can never name a different arm than the
            # one real money runs — with a plain max() a 7-way tie broke by dict order and they
            # disagreed. Still never the arming criterion; the gate reads chain-scoped alpha_b.
            from sniper.services.memecoin_copy_arm import best_arm_cell
            best = best_arm_cell(cells)
            # `funded_arm` is the decision; `best_arm` above is the survivor DIAGNOSTIC and is
            # never what gets frozen into a contract.
            funded = select_from_cells(strict_cells.get(uid), chain=chain) if strict_available \
                else None
            # `configured` holds the FUNDED network ids ('4663'); `chain` is the canonical key
            # ('robin'). They coincide for Solana and differ everywhere else, so comparing them
            # directly reports every Robin/Base arm as unarmed. Compare on the stored identity.
            armed_here = canonical_chain_id(chain) in configured
            r = real.get(uid)
            # absent key == no honest closes, which is what the singular returns for that wallet.
            gate_stats = gate_all.get(uid) or {"n_closes": 0, "pnl": 0.0}
            rows.append({
                "source_user_id": uid, "handle": w.get("handle"),
                # ACTIVE SELECTION-GATE EXEMPTIONS for this wallet (2026-08-28). Config only —
                # the panel grants and revokes, the RUNNER decides admission. Chain-independent:
                # an exemption waives a selection judgement about the coin, not authority to trade
                # a chain, so unlike armed_chains it is not per-chain.
                "gate_exemptions": sorted(m.wallet_gate_exemptions(conn, uid)),
                # The operator's per-wallet TP/stop on THIS chain, shown on every row, armed or
                # not, so re-arming a wallet never surprises anyone with rails set weeks ago.
                "exit_override": m.wallet_exit_override(conn, uid, network_id=chain),
                "exit_rails": _row_exit_rails(uid, chain),
                "real_approved": bool(w.get("real_approved")),
                # The approval flag with NO chain list at all: it renders as approved on every
                # board while carrying zero authority anywhere, because NULL/blank real_chains is
                # armed-nowhere. 12 of 32 approvals were in this state on 2026-08-05. Named per
                # row so the gap is visible where it is actionable, not just in a total.
                "approved_nowhere": bool(w.get("real_approved")) and not configured,
                "armed_here": armed_here,
                "effective": uid in armed_effective and armed_here,
                # FUNDED AUTHORITY OVER A WALLET THAT CANNOT SIGNAL IS NOT AUTHORITY. J7 relays
                # only its tracked 50 per account, so an armed wallet off every list never fires.
                # None while no list is stored — unknown, not "untracked".
                "j7_tracked": (uid in _armed_tracked) if _armed_tracked else None,
                "paused": bool(w.get("paused")),
                "armed_chains": configured,             # stored network ids (pinned contract)
                "armed_chain_keys": sorted(             # canonical keys, for labelling
                    {k for k in (resolve_chain(x) for x in configured) if k}),
                # The exact number the gate will read when this chain is armed.
                "gate": gate_stats,
                "best_arm": best,                      # survivor diagnostic — NOT the decision
                "funded_arm": funded,                  # what the funded selector actually returns
                "funded_arm_available": strict_available,
                "paper_pnl": (matrix.get(uid) or {}).get("total_pnl"),
                "paper_closed": (matrix.get(uid) or {}).get("total_closed"),
                "real": r,
                "last_signal": last_signal.get(uid),
                # Worth showing first: already configured here, approved, has a funded record
                # here, or has chain-scoped paper evidence the gate would read. A wallet whose
                # gate already qualifies is the single most actionable row on this board.
                "relevant": bool(armed_here or w.get("real_approved") or r
                                 or gate_stats["n_closes"]),
            })
        rows.sort(key=lambda r: (not r["armed_here"], not r["relevant"],
                                 -((r["real"] or {}).get("realized_pnl") or 0.0),
                                 -(r["gate"]["pnl"] or 0.0)))
        gate_cfg = {"min_closes": cfg.MEMECOIN_REAL_PROMOTE_MIN_CLOSES,
                    "min_pnl": cfg.MEMECOIN_REAL_PROMOTE_MIN_PNL_USD}
        # Whether `best_arm` is DESCRIPTIVE or LOAD-BEARING. With the copy-arm policy on it
        # supplies the exit rails of every new funded position from that wallet, so the board must
        # say which of the two it currently is rather than leaving a column that quietly changed
        # meaning (2026-08-03 lesson: a badge that cannot express its own scope misleads).
        return jsonify({"chain": chain, "rows": rows, "gate": gate_cfg,
                        "copy_arm_policy": False,
                        "copy_arm_policy_configured": bool(cfg.MEMECOIN_COPY_ARM_POLICY),
                        "copy_arm_policy_blocked_reason": "verified_study_receipt_unavailable",
                        "funded_chains": list(funded_chain_keys()),
                        "armed_count": sum(1 for r in rows if r["armed_here"]),
                        "effective_count": sum(1 for r in rows if r["effective"]),
                        "approved_count": sum(1 for r in rows if r["real_approved"]),
                        # `approved_count` alone overstates funded authority: it counts the flag,
                        # and a flag with no chain list authorises nothing. These two split it, so
                        # the board can say "20 armed somewhere, 12 approved nowhere" instead of a
                        # bare 32 that no chain agrees with (audit 2026-08-05).
                        "armed_anywhere_count": len(m.real_armed_any_chain_user_ids(conn)),
                        "approved_nowhere_count": sum(1 for r in rows if r["approved_nowhere"]),
                        "roster_count": len(rows)})

    @bp.get("/api/memecoin/candidates")
    def candidates():
        # Discovery funnel: non-curated large buyers from the feed firehose, LEFT JOINed
        # with their analyzer screen. Rank = feed activity (front-runnable), NOT skill —
        # read the analyzer columns for skill. min_sightings/min_usd filter noise.
        try:
            ms = max(1, int(request.args.get("min_sightings", 1)))
            mu = max(0.0, float(request.args.get("min_usd", 0)))
            limit = min(200, max(1, int(request.args.get("limit", 200))))
            offset = max(0, int(request.args.get("offset", 0)))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_candidate_query", "rows": []}), 400
        status = request.args.get("status", "new")
        if status not in {"new", "ignored", "promoted", "excluded"}:
            return jsonify({"error": "invalid_candidate_status", "rows": []}), 400
        now = _now_iso()
        rows = m.list_candidates(
            conn, min_sightings=ms, min_usd=mu, limit=limit, offset=offset,
            status=status, now_iso=now)
        return jsonify({
            "rows": _decorate_rows(rows, now),
            "total": m.count_candidates(conn, ms, mu, status, now_iso=now),
            "limit": limit, "offset": offset, "status": status,
        })

    @bp.post("/api/memecoin/candidates/<uid>/ignore")
    def candidate_ignore(uid):
        if not m.get_candidate(conn, uid):
            return jsonify({"ok": False, "error": "candidate_not_found"}), 404
        m.set_candidate_status(conn, uid, "ignored")
        return jsonify({"ok": True})

    @bp.post("/api/memecoin/candidates/<uid>/reconsider")
    def candidate_reconsider(uid):
        if not m.get_candidate(conn, uid):
            return jsonify({"ok": False, "error": "candidate_not_found"}), 404
        m.set_candidate_status(conn, uid, "new")
        return jsonify({"ok": True})

    @bp.post("/api/memecoin/candidates/<uid>/promote")
    def candidate_promote(uid):
        # operator-gated: add the discovered wallet to the curated copy set + mark promoted.
        result = m.promote_candidate(conn, uid)
        if result["ok"]:
            return jsonify(result)
        status = 404 if result["error"] == "candidate_not_found" else 409
        return jsonify(result), status

    @bp.post("/api/memecoin/sync-follows")
    def sync_follows():
        from sniper.services.memecoin_follow_sync import sync_followed
        r = sync_followed(conn, _get_auth(),
                          user_fn=ctx.get("follow_user_fn"),
                          following_fn=ctx.get("follow_following_fn"),
                          follow_fn=ctx.get("follow_target_fn"))
        return jsonify(r)

    # ---- V6 live-paper surface (separate book; never mixed into /real/positions) --------------
    # GET routes open the isolated V6 DB READ-ONLY; the censor route opens it writable through the
    # V6 persistence helper only. No funded controls, no production DB connection, is ever exposed.
    V6_FORBIDDEN_KEYS = {
        "reservation_id", "tx_sig", "signed_payload", "private_key",
        "manual_sell_url", "mirror_url", "override_url", "kill_switch_url",
    }

    def _v6_scrub(value):
        if isinstance(value, dict):
            return {k: _v6_scrub(v) for k, v in value.items() if k not in V6_FORBIDDEN_KEYS}
        if isinstance(value, list):
            return [_v6_scrub(v) for v in value]
        return value

    def _v6_path():
        from sniper.settings import memecoin as _cfg
        from sniper.persistence import db_memecoin_v6_paper as _v6
        return _cfg.MEMECOIN_V6_PAPER_DB or _v6._default_path()

    def _v6_ro():
        from sniper.persistence import db_memecoin_v6_paper as _v6
        return _v6.connect_existing(_v6_path())      # raises when the book file is absent

    def _v6_arm():
        from sniper.settings import memecoin as _cfg
        return {
            "state": "COLLECTING" if bool(_cfg.MEMECOIN_V6_PAPER_ENABLED) else "NOT RUNNING",
            "identity": {"source": "funded_copy_system"},
            "notional_usd": float(_cfg.MEMECOIN_V6_PAPER_NOTIONAL_USD),
        }

    def _v6_empty_cockpit():
        return {"mode": "v6_paper", "funded": False,
                "fill_label": "modeled_live_quote", "positions": [], "runtime": None,
                "summary": None, "arm": _v6_arm()}

    @bp.get("/api/memecoin/v6-paper/positions")
    def v6_paper_positions():
        from sniper.services import memecoin_v6_paper_report as rpt

        def _build():
            try:
                v6c = _v6_ro()
            except Exception:
                return _v6_empty_cockpit()
            try:
                snapshot = _v6_scrub(rpt.cockpit_snapshot(v6c))
            finally:
                v6c.close()
            snapshot["arm"] = _v6_arm()
            return snapshot

        return jsonify({**_v6_cached("positions", _build), "csrf_token": _csrf_token()})

    @bp.get("/api/memecoin/v6-paper/stream")
    def v6_paper_stream():
        from sniper.services import memecoin_v6_paper_report as rpt

        once = request.args.get("once") == "1"
        csrf_token = _csrf_token()

        @stream_with_context
        def generate():
            v6c = None
            last_revision = None
            last_keepalive = _time.monotonic()
            try:
                try:
                    v6c = _v6_ro()
                except Exception:
                    payload = {**_v6_empty_cockpit(), "csrf_token": csrf_token}
                    yield "event: snapshot\ndata: " + json.dumps(payload, separators=(",", ":")) + "\n\n"
                    return
                while True:
                    arm = _v6_arm()
                    revision = (
                        rpt.cockpit_revision(v6c),
                        json.dumps(arm, sort_keys=True, separators=(",", ":")),
                    )
                    if revision != last_revision:
                        payload = _v6_scrub(rpt.cockpit_snapshot(v6c))
                        payload["arm"] = arm
                        payload["csrf_token"] = csrf_token
                        yield "event: snapshot\ndata: " + json.dumps(
                            payload, separators=(",", ":")) + "\n\n"
                        last_revision = revision
                        last_keepalive = _time.monotonic()
                        if once:
                            return
                    _time.sleep(0.5)
                    if _time.monotonic() - last_keepalive >= 15.0:
                        yield ": keepalive\n\n"
                        last_keepalive = _time.monotonic()
            except GeneratorExit:
                return
            finally:
                if v6c is not None:
                    v6c.close()

        return Response(generate(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @bp.get("/api/memecoin/v6-paper/positions/<position_id>")
    def v6_paper_position_detail(position_id):
        from sniper.services import memecoin_v6_paper_report as rpt
        try:
            options = {
                "observation_limit": int(request.args.get("observation_limit", "1")),
                "decision_limit": int(request.args.get("decision_limit", "12")),
                "before_observation": (
                    int(request.args["before_observation"])
                    if "before_observation" in request.args else None),
                "before_decision": (
                    int(request.args["before_decision"])
                    if "before_decision" in request.args else None),
            }
        except (TypeError, ValueError):
            return jsonify({"error": "invalid timeline pagination"}), 400
        try:
            v6c = _v6_ro()
        except Exception:
            return jsonify({"error": "not found"}), 404
        try:
            detail = rpt.position_detail(v6c, position_id, **options)
        except ValueError:
            return jsonify({"error": "invalid timeline pagination"}), 400
        except KeyError:
            return jsonify({"error": "not found"}), 404
        finally:
            v6c.close()
        return jsonify(_v6_scrub(detail))

    @bp.get("/api/memecoin/v6-paper/summary")
    def v6_paper_summary():
        from sniper.services import memecoin_v6_paper_report as rpt

        def _build():
            try:
                v6c = _v6_ro()
            except Exception:
                return {"mode": "v6_paper", "funded": False,
                        "fill_label": "modeled_live_quote", "empty": True}
            try:
                data = rpt.summary(v6c)
            finally:
                v6c.close()
            return _v6_scrub(data)

        return jsonify(_v6_cached("summary", _build))

    @bp.post("/api/memecoin/v6-paper/positions/<position_id>/censor")
    def v6_paper_censor(position_id):
        from sniper.persistence import db_memecoin_v6_paper as _v6
        if not _csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        body = request.get_json(force=True, silent=True) or {}
        reason = str(body.get("reason") or "").strip()
        if len(reason) < 8:
            return jsonify({"ok": False, "error": "reason must be at least 8 characters"}), 400
        try:
            v6c = _v6.connect(_v6_path())            # writable via the V6 persistence helper only
        except Exception:
            return jsonify({"ok": False, "error": "v6 book unavailable"}), 503
        try:
            pos = _v6.censor_position(v6c, position_id, reason=reason, at=_now_iso())
        except KeyError:
            return jsonify({"ok": False, "error": "not found"}), 404
        except _v6.StateConflict:
            return jsonify({"ok": False, "error": "position is not open"}), 409
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        finally:
            v6c.close()
        _invalidate_v6_snapshots()
        return jsonify({"ok": True, "status": pos.status, "censored": True})

    @bp.get("/memecoin")
    def memecoin_page():
        # Standalone page retired — the board is a native dashboard tab now.
        return redirect("/?tab=memecoin", code=302)

    return bp
