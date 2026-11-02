#!/usr/bin/env python3
"""Pre-entry pool discovery lane — resolve a Solana mint's pool on FIRST SIGHTING.

WHY THIS EXISTS. The Solana tape resolves a pool when a POSITION opens
(`solana_tape_pass` walks `memecoin_positions`). Measured 2026-08-25 over 207 resolutions: the
pool was resolved a median +16.1s AFTER the position opened and ZERO of 207 before it. So the
tape is entirely post-entry and cannot inform an entry decision. This lane resolves the same
pools from the signal instead, so the tape already holds flow for a coin by the time we decide
on it.

Lead time from our first sighting of a mint to a funded fill on it (n=254 since 2026-08-01):
p25 2.9s, median 22.6s, p75 443.7s; >=5s on 61%, >=15s on 54%, >=30s on 43%. A resolve costs
<=8 RPC calls, measured 0.43s typical and 1.28s worst against the research key, so it finishes
inside the lead window on essentially every signal that has any lead at all. The unreachable
remainder is the 6% of mints whose first-ever sighting IS our signal.

WHAT IT DOES NOT DO. It never touches the entry path and nothing in the funded runner waits on
it. If this process is dead, stopped, or slow, entry behaves exactly as it does today: the gate
finds no tape and fails open. That is the whole safety argument — this lane can only ADD
information, never withhold or delay a trade.

IT WATCHES WIDER THAN WE TRADE, deliberately. Mints below `MEMECOIN_REAL_MIN_MCAP_USD` are still
resolved. The mcap floor is an operator judgement that may be revisited, and a lane that only
observes what today's gate admits can never produce the evidence to revisit it.

RPC. Names `MEMECOIN_RESEARCH_HELIUS_RPC_URL` explicitly and refuses to start without it. Do NOT
let this inherit `MEMECOIN_SOL_TAPE_RPC_URL` or `MEMECOIN_SOLANA_READ_RPC_URL`: on 2026-08-25
both pointed at throttled public endpoints (publicnode 429s a dozen ad-hoc calls;
api.mainnet-beta is slower and also throttled), and the research key exists precisely so
research traffic stops competing with the funded path — see
finding-research-process-burns-shared-funded-rpc-quota-2026-08-21.

Workload is small: ~159 new Solana mints/day measured over 08-17..08-25, so at most ~1,270
calls/day, about one call every 68 seconds.
"""
import logging
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _REPO_ROOT)

from sniper.util.envfiles import load_env_files  # noqa: E402

load_env_files(_REPO_ROOT)

from sniper.persistence import db_ticks as t  # noqa: E402
from sniper.services.memecoin_solana_amounts import get_transaction_fn  # noqa: E402
from sniper.services.memecoin_research_rpc import build_research_call  # noqa: E402
from sniper.services.memecoin_solana_tape import (  # noqa: E402
    get_signatures_fn, resolve_position_pool, tape_pass_for_vault,
)
from sniper.util.clock import real_utc_now_iso  # noqa: E402

logger = logging.getLogger("memecoin.pool_prewarm")

# Every bound is an env knob so a runaway lane can be throttled without a deploy.
DEFAULTS = {
    "MEMECOIN_POOL_PREWARM_ENABLED": "0",     # default OFF; opt in explicitly
    "MEMECOIN_POOL_PREWARM_LOOKBACK_MIN": "30",
    "MEMECOIN_POOL_PREWARM_MAX_MINTS": "40",  # per run; drops are LOGGED, never silent
    "MEMECOIN_POOL_PREWARM_MAX_FETCH": "4",   # getTx budget per mint (resolve_position_pool)
    "MEMECOIN_POOL_PREWARM_TAPE_BUDGET": "6",  # getTx budget per vault for the first tape draw
    "MEMECOIN_POOL_PREWARM_MIN_CALL_S": "0.2",
    # A pre-warmed pool is taped ONCE at discovery and then nothing keeps it current, so it falls
    # out of the flow read's 300s window within minutes. These re-tap it while it is still a
    # plausible entry candidate.
    "MEMECOIN_POOL_PREWARM_KEEP_WARM_MIN": "45",
    "MEMECOIN_POOL_PREWARM_MAX_REFRESH": "25",
    # Scope to ARMED wallets. Only an armed wallet can produce a fill (`not_approved` refuses the
    # rest before the market read), so an unarmed wallet's coin can never yield a paired
    # observation. Measured 2026-08-25: this drops 68-91% of candidates per day and it IMPROVES the
    # experiment, because armed-wallet attempts run 742 refused to 51 filled over 311 mints — a
    # 14.5:1 matched control on the same wallets and the same signal path, differing only in which
    # gate fired. A refused attempt has NO fill, so its price window carries no footprint of ours.
    # Set to 0 only for a deliberate wide research sweep; it multiplies the RPC bill by ~5x.
    "MEMECOIN_POOL_PREWARM_ARMED_ONLY": "1",
}


def _cfg(name):
    return os.getenv(name, DEFAULTS[name])


def _int(name):
    try:
        return int(float(_cfg(name)))
    except (TypeError, ValueError):
        return int(float(DEFAULTS[name]))


def _enabled():
    return _cfg("MEMECOIN_POOL_PREWARM_ENABLED").strip().lower() in {"1", "true", "yes", "on"}


def _iso_minutes_ago(minutes, *, now_iso=None):
    """ISO cutoff built in PYTHON, never SQLite `datetime('now', ...)`.

    SQLite renders a space separator and our timestamps use 'T'; ' ' < 'T', so a string compare
    against a SQLite-rendered bound silently reaches further back than asked — 69% overcount when
    that bug was measured. See finding-sqlite-datetime-now-space-separator-2026-08-23.
    """
    from datetime import datetime, timedelta
    base = datetime.fromisoformat(now_iso or real_utc_now_iso())
    return (base - timedelta(minutes=minutes)).isoformat()


def _floor_for(first_seen_iso, *, now_iso=None):
    """Backward-walk bound for a pre-warmed vault, in epoch seconds.

    Mirrors the position path: `opened_at - history`, capped by a rolling `now - history` so a
    stale first sighting cannot ask for an unbounded drain. First sighting stands in for the
    position open, because that is the moment this lane is pre-warming from.
    """
    from datetime import datetime
    from sniper.services.memecoin_solana_tape import _bootstrap_history_s
    history_s = _bootstrap_history_s()
    try:
        now_epoch = datetime.fromisoformat(now_iso or real_utc_now_iso()).timestamp()
    except (TypeError, ValueError):
        return None
    rolling = now_epoch - history_s
    try:
        seen = datetime.fromisoformat(str(first_seen_iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return rolling                      # unparseable sighting -> the rolling floor still bounds it
    return max(seen - history_s, rolling)


def armed_user_ids(mconn):
    """Currently armed, non-excluded wallets, or None if the roster could not be READ.

    An EMPTY SET and a FAILED LOOKUP are different states and conflating them inverts the lane.
    Empty means nothing can fill, so there is nothing worth pre-warming. Failed means we do not
    know, and going blind silently would stop all evidence collection without saying so.
    """
    try:
        return {r[0] for r in mconn.execute(
            "SELECT source_user_id FROM memecoin_wallets "
            "WHERE real_approved=1 AND (excluded IS NULL OR excluded=0)") if r[0]}
    except Exception:
        logger.exception("pool prewarm: armed-wallet lookup FAILED; running unscoped this pass")
        return None


def unresolved_recent_mints(mconn, tc, *, since_iso, limit, armed_only=True):
    """Solana mints sighted since `since_iso` that have no resolved pool yet.

    Returns (mints, dropped). `dropped` is how many candidates the cap discarded — the caller
    LOGS it. A silent cap reads as full coverage when it is not.

    `armed_only` restricts to wallets that can actually fill. See the knob's comment: it is both
    ~5x cheaper and a better experiment. Falls back to UNSCOPED if the roster cannot be read,
    because a lookup failure must not silently blind the lane to every coin.
    """
    where = ("side='buy' AND received_at >= ? AND mint IS NOT NULL "
             "AND (network_id IS NULL OR network_id='solana')")
    params = [since_iso]
    if armed_only:
        ids = armed_user_ids(mconn)
        if ids is None:
            pass                            # lookup FAILED -> unscoped, and it logged why
        elif not ids:
            return [], 0                    # roster genuinely EMPTY -> nothing can fill
        else:
            where += " AND source_user_id IN (%s)" % ",".join("?" * len(ids))
            params.extend(sorted(ids))
    rows = mconn.execute(
        "SELECT mint, MIN(received_at) AS first_seen FROM memecoin_events "
        "WHERE " + where + " GROUP BY mint ORDER BY first_seen DESC", params).fetchall()
    seen = [(str(r[0]), r[1]) for r in rows if r[0]]
    if not seen:
        return [], 0
    # Resolved-set lives in the TAPE db, candidates in the LEDGER db — filter in Python rather
    # than ATTACH, so neither connection needs write access to the other's file.
    resolved = set()
    for chunk_start in range(0, len(seen), 900):
        chunk = [m for m, _ in seen[chunk_start:chunk_start + 900]]
        qs = ",".join("?" * len(chunk))
        for row in tc.execute(
                f"SELECT mint FROM sol_pool_vaults WHERE mint IN ({qs})", chunk):
            resolved.add(str(row[0]))
    todo = [(m, ts) for m, ts in seen if m not in resolved]
    return todo[:limit], max(0, len(todo) - limit)


def stale_prewarmed_pools(tc, *, since_iso, limit, keep_mints=None):
    """Pools this lane discovered that NOTHING is keeping current.

    `solana_tape_pass` only re-taps vaults reached from open positions, so a pool we pre-warmed
    is taped exactly once and then goes stale. That is fatal to the whole point: the flow read
    uses a 300s window, so a pool whose newest tick is 12 minutes old reads as None — UNKNOWN —
    and the gate fails open exactly as if the lane had never run. Measured directly: a pool with
    10 ticks and a 12-minute-old head returned None from `observe_pre_entry_flow`.

    Excludes pools bound to a position, because the tape worker already owns those.

    `keep_mints`, when given, restricts warming to those mints. Without it the refresh keeps
    paying for coins the discovery half has already been scoped away from — measured: discovery
    fell to 0 calls under armed-only scoping while refresh still spent 28, warming pools no armed
    wallet had touched. The refresh must carry the same scope or it becomes the entire bill.
    """
    rows = tc.execute(
        "SELECT v.pool, v.mint FROM sol_pool_vaults v "
        "LEFT JOIN sol_position_pools p ON p.pool = v.pool "
        "WHERE v.resolved_at >= ? AND p.pool IS NULL "
        "ORDER BY v.resolved_at DESC", (since_iso,)).fetchall()
    out = [(str(r[0]), str(r[1])) for r in rows]
    if keep_mints is not None:
        out = [(p, m) for p, m in out if m in keep_mints]
    return out[:limit], max(0, len(out) - limit)


def refresh_once(tc, get_tx, get_sigs, *, keep_warm_min, max_pools, tape_budget,
                 keep_mints=None, now_iso=None):
    """Re-tap recently pre-warmed pools so their tape stays inside the read window."""
    stats = {"refresh_candidates": 0, "refreshed": 0, "refresh_calls": 0, "refresh_dropped": 0,
             "refresh_errors": 0}
    if tape_budget <= 0 or max_pools <= 0:
        return stats
    since = _iso_minutes_ago(keep_warm_min, now_iso=now_iso)
    pools, dropped = stale_prewarmed_pools(tc, since_iso=since, limit=max_pools,
                                           keep_mints=keep_mints)
    stats["refresh_candidates"] = len(pools)
    stats["refresh_dropped"] = dropped
    for pool, mint in pools:
        try:
            n = tape_pass_for_vault(tc, mint, pool, get_sigs, get_tx,
                                    now_iso or real_utc_now_iso(),
                                    tx_budget=tape_budget,
                                    floor_ts=_floor_for(None, now_iso=now_iso))
            if n and n > 0:
                stats["refreshed"] += 1
                stats["refresh_calls"] += int(n)
        except Exception:
            stats["refresh_errors"] += 1
            logger.exception("pool refresh failed for pool=%s (skipped)", pool[:16])
    return stats


def prewarm_once(mconn, tc, get_tx, get_sigs, call, *, lookback_min, max_mints, max_fetch,
                 tape_budget=0, armed_only=True, now_iso=None):
    """One pass. Returns a stats dict; never raises for a single mint's failure.

    RESOLVING A POOL IS ONLY HALF THE JOB. `solana_tape_pass` taps vaults reached from OPEN
    POSITIONS, so a pool we resolve here would carry pool_meta and still have zero ticks — the
    flow read would find the pool and an empty window, return None, and fail open. That is
    indistinguishable from having no lane at all. So after resolving we draw the vault's tape
    once, with its own bounded budget, which is what actually puts flow in front of the gate.
    """
    since = _iso_minutes_ago(lookback_min, now_iso=now_iso)
    todo, dropped = unresolved_recent_mints(mconn, tc, since_iso=since, limit=max_mints,
                                            armed_only=armed_only)
    stats = {"candidates": len(todo), "dropped": dropped, "resolved": 0, "unresolved": 0,
             "errors": 0, "rpc_calls": 0, "taped": 0, "tape_calls": 0}
    # The per-mint body lives in sniper/services/memecoin_pool_resolve.py so the signal-time
    # resolver reuses it rather than forking it. Same call, same synthetic no-fill position, same
    # tape draw and floor — the output rows are identical by construction, which is what lets every
    # existing sol_pool_vaults consumer work unchanged regardless of which lane resolved the mint.
    from sniper.services.memecoin_pool_resolve import resolve_one_mint
    for mint, first_seen in todo:
        got = resolve_one_mint(
            tc, mconn, mint, first_seen=first_seen, get_tx=get_tx, get_sigs=get_sigs, call=call,
            max_fetch=max_fetch, tape_budget=tape_budget, now_iso=now_iso,
            floor_fn=lambda fs: _floor_for(fs, now_iso=now_iso),
            # this module's own names, so existing patches of them still apply
            resolve_fn=resolve_position_pool, tape_fn=tape_pass_for_vault)
        for key in ("resolved", "unresolved", "errors", "rpc_calls", "taped", "tape_calls"):
            stats[key] += got.get(key, 0)
    return stats


def main(*, iterations=1):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not _enabled():
        logger.info("pool prewarm OFF (MEMECOIN_POOL_PREWARM_ENABLED unset)")
        return 0
    try:
        min_call_s = float(_cfg("MEMECOIN_POOL_PREWARM_MIN_CALL_S"))
    except (TypeError, ValueError):
        min_call_s = 0.2
    # Fail CLOSED on a missing research key rather than inheriting a funded or public endpoint.
    # Silently borrowing the funded RPC is the failure this lane must not have. A cron process has
    # no Telegram dispatcher, so the credit page is sent directly.
    from sniper.services.memecoin_telegram import send_telegram
    call, pool = build_research_call("pool_prewarm", min_call_s=min_call_s, send_fn=send_telegram)
    if call is None:
        logger.error("pool prewarm: MEMECOIN_RESEARCH_HELIUS_RPC_URL(S) unset or shared with a "
                     "funded key; refusing to start")
        return 2
    get_sigs = get_signatures_fn(call=call)
    get_tx = get_transaction_fn(call=call)
    from sniper.persistence.db_core import get_readonly_connection
    mconn = get_readonly_connection()      # ledger: READ ONLY, this lane never writes it
    tc = t.connect_sol_tape()              # tape: writes pool_meta / sol_pool_vaults
    lookback = _int("MEMECOIN_POOL_PREWARM_LOOKBACK_MIN")
    max_mints = _int("MEMECOIN_POOL_PREWARM_MAX_MINTS")
    max_fetch = _int("MEMECOIN_POOL_PREWARM_MAX_FETCH")
    tape_budget = _int("MEMECOIN_POOL_PREWARM_TAPE_BUDGET")
    keep_warm = _int("MEMECOIN_POOL_PREWARM_KEEP_WARM_MIN")
    max_refresh = _int("MEMECOIN_POOL_PREWARM_MAX_REFRESH")
    armed_only = _cfg("MEMECOIN_POOL_PREWARM_ARMED_ONLY").strip().lower() in {"1","true","yes","on"}
    done = 0
    while True:
        started = time.monotonic()
        try:
            s = prewarm_once(mconn, tc, get_tx, get_sigs, call, lookback_min=lookback,
                             max_mints=max_mints, max_fetch=max_fetch, tape_budget=tape_budget,
                             armed_only=armed_only)
            # Refresh AFTER discovery: a newly resolved pool is already current, so spending the
            # refresh budget on it first would waste the pass.
            # Same scope as discovery. Without this the refresh warms coins no armed wallet
            # touched and quietly becomes the whole RPC bill.
            keep = None
            if armed_only:
                ids = armed_user_ids(mconn)
                if ids:
                    qs = ",".join("?" * len(ids))
                    keep = {row[0] for row in mconn.execute(
                        "SELECT DISTINCT mint FROM memecoin_events WHERE side='buy' "
                        "AND received_at >= ? AND source_user_id IN (%s)" % qs,
                        [_iso_minutes_ago(keep_warm), *sorted(ids)])}
                elif ids is not None:
                    keep = set()            # roster empty -> warm nothing
            r = refresh_once(tc, get_tx, get_sigs, keep_warm_min=keep_warm,
                             max_pools=max_refresh, tape_budget=tape_budget, keep_mints=keep)
            logger.info(
                "[pool-prewarm] candidates=%d resolved=%d taped=%d unresolved=%d dropped=%d "
                "errors=%d rpc_calls=%d tape_calls=%d | refresh cand=%d ok=%d calls=%d "
                "dropped=%d err=%d | elapsed=%.1fs",
                s["candidates"], s["resolved"], s["taped"], s["unresolved"], s["dropped"],
                s["errors"], s["rpc_calls"], s["tape_calls"],
                r["refresh_candidates"], r["refreshed"], r["refresh_calls"],
                r["refresh_dropped"], r["refresh_errors"], time.monotonic() - started)
            logger.info("[pool-prewarm] research keys %s", " ".join(
                "%s=%s/%d" % (k["key"], k["state"], k["calls"]) for k in pool.key_status()))
        except Exception:
            logger.exception("pool prewarm pass failed (cursor-free; next run retries)")
        done += 1
        if iterations is not None and done >= iterations:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
