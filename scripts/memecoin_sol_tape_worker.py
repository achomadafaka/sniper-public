#!/usr/bin/env python3
"""Dedicated, default-OFF Solana pull-tape worker. Read RPC only; no custody imports."""
import logging
import math
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.persistence import db_ticks as t  # noqa: E402
from sniper.services.memecoin_solana_amounts import get_transaction_fn  # noqa: E402
from sniper.services.memecoin_solana_blocktime import build_read_rpc_call  # noqa: E402
from sniper.services.memecoin_solana_tape import get_signatures_fn, solana_tape_pass  # noqa: E402
from sniper.util.clock import real_utc_now  # noqa: E402

logger = logging.getLogger("memecoin")


def _enabled():
    return os.getenv("MEMECOIN_SOL_TAPE_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"}


def _period_s():
    try:
        value = float(os.getenv("MEMECOIN_SOL_TAPE_PERIOD_S", "300") or 300)
    except (TypeError, ValueError):
        return 300.0
    return value if math.isfinite(value) and value > 0 else 300.0


def _open_period_s():
    """The period to use while a funded Solana position is OPEN.

    THE 300 s PERIOD CANNOT SEE A SHORT POSITION (2026-09-22, BOBCOIN 63654). That coin was bought
    at 20:18:26 on an exotic-quote pool, stopped out at 20:19:30 for -$70.17, and lived 63 seconds.
    The pull tape wrote 20:15:02..20:15:05 and then nothing until 21:00:02, so the entire position
    fell between two passes and produced ZERO rows -- while the fastlane held 29 continuous marks
    on the same coin. The lane was not broken and the exotic-quote fix was not at fault; the
    cadence simply could not reach it.

    Funded money is made in the first 60 seconds (2026-09-18: <60 s is +$4,435 at 83% win over
    n=219, every longer bucket loses), so the one window the pull tape must cover is exactly the
    one a five-minute period misses.

    ONLY WHILE SOMETHING IS OPEN. A flat book has nothing to poll, so it keeps the long period and
    spends no signature quota. This is a floor on ATTENTION, not an extra budget: `tx_budget` is
    unchanged, so a faster pass cannot spend more transactions per pass than before.
    """
    try:
        value = float(os.getenv("MEMECOIN_SOL_TAPE_OPEN_PERIOD_S", "20") or 20)
    except (TypeError, ValueError):
        return 20.0
    return value if math.isfinite(value) and value > 0 else 20.0


def _open_fast_window_s():
    """How long after `opened_at` the fast floor applies. Default 300 s.

    THE FLOOR IS BOUNDED BECAUSE THE QUOTA IS (2026-09-22, operator). At 20 s the ceiling is 180
    passes an hour against 12, and with tx_budget 40 that is up to 7,200 getTransaction an hour
    while a position is open -- about 14,300 a day at 1.99 open-hours, on the funded custody key,
    the same order as the 2026-08-21 incident that burned ~7k of it.

    Measured on 153 funded Solana closes over 7 days: hold p50 106 s, p90 553 s, 30% close inside
    60 s and 73% inside 300 s. So a five-minute window covers nearly all of the exposure the
    finding is about at roughly a third of the calls, and it self-limits on long holds -- which are
    exactly the positions the 300 s cadence already covers well.
    """
    try:
        value = float(os.getenv("MEMECOIN_SOL_TAPE_OPEN_FAST_WINDOW_S", "300") or 300)
    except (TypeError, ValueError):
        return 300.0
    return value if math.isfinite(value) and value > 0 else 300.0


def _funded_solana_open(mconn, *, now=None, window_s=None):
    """True when a REAL Solana position opened INSIDE the fast window is still open.

    The cutoff is formatted here rather than in SQL: `datetime('now')` renders a SPACE separator
    while `opened_at` is ISO with a `T`, and ' ' sorts below 'T', so a SQL-side comparison is wrong
    at the boundary (2026-08-23). One indexed read; never raises.
    """
    window_s = _open_fast_window_s() if window_s is None else float(window_s)
    now = time.time() if now is None else float(now)
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - window_s))
    try:
        row = mconn.execute(
            "SELECT 1 FROM memecoin_positions WHERE is_open=1 AND execution_mode='real' "
            "AND network_id='solana' AND COALESCE(tracking_paused_at,'')='' "
            "AND opened_at>=? LIMIT 1", (cutoff,)).fetchone()
        return row is not None
    except Exception:
        logger.exception("open-position check failed; holding the configured period")
        return False


def _v6_period_s():
    try:
        value = float(os.getenv("MEMECOIN_SOL_TAPE_V6_PERIOD_S", "5") or 5)
    except (TypeError, ValueError):
        return 5.0
    return value if math.isfinite(value) and value > 0 else 5.0


def _v6_backfill_period_s():
    try:
        value = float(os.getenv("MEMECOIN_SOL_TAPE_V6_BACKFILL_PERIOD_S", "0.5") or 0.5)
    except (TypeError, ValueError):
        return 0.5
    return value if math.isfinite(value) and value > 0 else 0.5


def _budget():
    try:
        value = int(os.getenv("MEMECOIN_SOL_TAPE_MAX_TX_PER_TICK", "40") or 40)
    except (TypeError, ValueError):
        return 40
    return max(0, value)


def _v6_budget():
    try:
        value = int(os.getenv("MEMECOIN_SOL_TAPE_V6_MAX_TX_PER_TICK", "40") or 40)
    except (TypeError, ValueError):
        return 40
    return max(0, value)


def _v6_fetch_workers():
    try:
        value = int(os.getenv("MEMECOIN_SOL_TAPE_V6_FETCH_WORKERS", "8") or 8)
    except (TypeError, ValueError):
        return 8
    return max(1, min(value, 32))


def _funded_only():
    from sniper.settings.memecoin import MEMECOIN_FUNDED_ONLY
    return MEMECOIN_FUNDED_ONLY


def _build_rpc_call():
    try:
        cadence = float(os.getenv("MEMECOIN_SOL_TAPE_MIN_CALL_S", "1.2") or 1.2)
    except (TypeError, ValueError):
        cadence = 1.2
    url = os.getenv("MEMECOIN_SOL_TAPE_RPC_URL", "").strip() or None
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == "shyft":
        from sniper.services.memecoin_solana_provider import provider_urls
        url = provider_urls("read", {"MEMECOIN_SOLANA_INFRA_PROVIDER": "shyft",
            "MEMECOIN_SOLANA_READ_RPC_URL": url or cfg.solana_read_rpc()})[0]
    return build_read_rpc_call(url=url, min_call_s=cadence)


def _connect_ledger():
    from sniper.persistence.db_core import get_readonly_connection
    return get_readonly_connection()


def _connect_sol_tape():
    return t.connect_sol_tape()


def v6_capture_targets(network_id="solana"):
    """Open the default-OFF V6 paper book READ-ONLY and return its open positions as tape targets.
    Returns ``[]`` when V6 paper is disabled or its DB is absent — the tape pass then behaves
    byte-identically to before. The V6 connection is opened with ``connect_existing`` (mode=ro) and
    closed immediately; the tape only ever reads the V6 book."""
    from sniper.settings.memecoin_v6_paper import (
        MEMECOIN_V6_PAPER_DB, MEMECOIN_V6_PAPER_ENABLED,
    )
    from sniper.settings.memecoin import MEMECOIN_FUNDED_ONLY
    if MEMECOIN_FUNDED_ONLY or not MEMECOIN_V6_PAPER_ENABLED:
        return []
    from sniper.persistence import db_memecoin_v6_paper as v6db
    from sniper.services.memecoin_v6_observations import v6_capture_positions
    try:
        conn = v6db.connect_existing(MEMECOIN_V6_PAPER_DB or None)
    except Exception:
        return []
    try:
        return v6_capture_positions(conn, network_id)
    except Exception:
        logger.exception("v6 capture targets query failed; tape pass runs without them")
        return []
    finally:
        conn.close()


def _v6_capture_enabled():
    from sniper.settings.memecoin_v6_paper import MEMECOIN_V6_PAPER_ENABLED
    from sniper.settings.memecoin import MEMECOIN_FUNDED_ONLY
    return bool(MEMECOIN_V6_PAPER_ENABLED and not MEMECOIN_FUNDED_ONLY)


def _v6_global_scope_targets(mconn, targets):
    """Add every active main-book consumer for V6 mints before narrowing a shared tape floor."""
    by_mint = {}
    for raw in targets:
        target = dict(raw)
        mint = str(target.get("mint") or "")
        if not mint:
            continue
        current = by_mint.get(mint)
        if current is None or str(target.get("opened_at") or "") < str(
                current.get("opened_at") or ""):
            by_mint[mint] = target
    mints = sorted(by_mint)
    if not mints:
        return [], True
    placeholders = ",".join("?" for _mint in mints)
    try:
        rows = mconn.execute(
            "SELECT id,mint,network_id,opened_at FROM memecoin_positions "
            "WHERE is_open=1 AND COALESCE(network_id,'') IN ('','solana') "
            f"AND mint IN ({placeholders}) ORDER BY id",
            tuple(mints),
        ).fetchall()
    except Exception:
        logger.exception("v6 capture scope query failed; floor contraction disabled")
        return list(by_mint.values()), False
    for row in rows:
        target = by_mint[str(row[1])]
        if str(row[3] or "") < str(target.get("opened_at") or ""):
            target["opened_at"] = row[3]
    return [by_mint[mint] for mint in mints], True


def _v6_targets_incomplete(tc, targets):
    """True until every V6 target has a resolved vault and a gap-free cursor."""
    try:
        for target in targets:
            row = tc.execute(
                "SELECT p.pool,c.vault,c.frontier_before FROM sol_position_pools p "
                "LEFT JOIN sol_sig_cursor c ON c.vault=p.pool "
                "WHERE p.position_id=? AND p.mint=?",
                (target["id"], target["mint"]),
            ).fetchone()
            if row is None or row[1] is None or row[2]:
                return True
    except Exception:
        return True
    return False


def _sleep_watching_for_a_position(remaining, mconn, open_period, *, sleep=None):
    """Sleep `remaining` seconds in SLICES, returning early when a funded Solana position opens.

    THE FLOOR WAS DECIDED ONCE PER PASS, WHICH MADE IT NEARLY USELESS (2026-09-22, Quagsire 63795).
    The period was chosen at the END of a pass from the book at that instant, then slept in one
    block. Quagsire opened at 22:27:12 and closed at 22:29:13 -- a 121-second hold that began and
    ended INSIDE a 300 s sleep chosen while the book was flat. `_funded_solana_open` was never
    called while it was open, the tape wrote nothing, and that is the BOBCOIN case repeating with
    the 20 s floor already deployed.

    The floor could only ever engage on the SECOND pass of a position that was already open when a
    pass ended. Measured holds are p50 106 s with 73% closing inside 300 s, so most positions open
    and die inside one sleep and the floor never sees them.

    Slicing costs one indexed SELECT per slice and no RPC, so an idle book pays three cheap reads a
    minute to cut the worst-case latency from the whole period to one slice.
    """
    sleep = sleep or time.sleep
    remaining = max(0.0, float(remaining))
    # Never longer than the fast floor itself: a slice that outlives the window it protects would
    # reintroduce the same blind spot in miniature.
    step = max(0.5, min(float(open_period), 20.0))
    # ACCOUNTED, NOT CLOCK-DRIVEN. Deciding when to stop by reading a clock means a caller whose
    # sleep does not advance one -- every test that injects a no-op sleep -- spins here forever.
    # Counting what we asked for cannot hang and needs no clock at all.
    slept = 0.0
    while slept < remaining:
        chunk = min(step, remaining - slept)
        sleep(chunk)
        slept += chunk
        if _funded_solana_open(mconn):
            return True                      # a position opened mid-sleep; start a pass now
    return False


def main(*, iterations=None):
    from dotenv import load_dotenv
    state_root = os.getenv("SNIPER_STATE_ROOT", _REPO_ROOT)
    load_dotenv(os.path.join(state_root, ".env"))
    if not _enabled():
        logger.info("Solana tape worker OFF (MEMECOIN_SOL_TAPE_ENABLED unset)")
        return 0
    call = _build_rpc_call()
    if call is None:
        logger.error("Solana tape worker enabled without a read RPC; exiting fail-closed")
        return 2
    get_sigs = get_signatures_fn(call=call)
    get_tx = get_transaction_fn(call=call)
    mconn = _connect_ledger()
    tc = _connect_sol_tape()
    period = _period_s()
    open_period = _open_period_s()
    v6_period = _v6_period_s()
    v6_backfill_period = _v6_backfill_period_s()
    budget = _budget()
    v6_budget = _v6_budget()
    v6_fetch_workers = _v6_fetch_workers()
    passes = 0
    while True:
        pass_started = time.monotonic()
        now = real_utc_now()
        v6_enabled = _v6_capture_enabled()
        v6_targets = v6_capture_targets("solana")
        v6_scope_targets, v6_scope_complete = _v6_global_scope_targets(mconn, v6_targets)
        v6_incomplete = bool(v6_targets)
        try:
            v6_spent = 0
            if v6_targets and v6_budget:
                v6_spent = solana_tape_pass(
                    mconn, tc, get_sigs, get_tx,
                    time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                    tx_budget=v6_budget, call_fn=call, positions_override=v6_scope_targets,
                    tx_fetch_workers=v6_fetch_workers,
                    allow_floor_contraction=v6_scope_complete,
                )
            # Transaction spend alone cannot prove catch-up: a bounded signature-enumeration pass
            # can spend zero while its durable cursor still records a gap. Keep V6 first until all
            # target cursors are resolved and complete, then resume the normal funded scan.
            v6_incomplete = bool(v6_targets) and (
                v6_spent >= v6_budget or _v6_targets_incomplete(tc, v6_targets))
            if not v6_incomplete:
                deadline = pass_started + v6_period if v6_enabled else None
                solana_tape_pass(mconn, tc, get_sigs, get_tx,
                                 time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                                 tx_budget=budget, call_fn=call,
                                 deadline_monotonic=deadline,
                                 **({"include_paper": False} if _funded_only() else {}))
        except Exception:
            logger.exception("Solana tape worker pass failed; cursor held")
        passes += 1
        if iterations is not None and passes >= iterations:
            break
        target_period = (v6_backfill_period if v6_incomplete else v6_period) \
            if v6_enabled else period
        # AN OPEN FUNDED POSITION SHORTENS THE PERIOD, never lengthens it. `min` is deliberate:
        # when the V6 lane is already running faster than this floor it keeps its own cadence, and
        # a V6 backfill still outranks everything. See `_open_period_s`.
        if _funded_solana_open(mconn):
            target_period = min(target_period, open_period)
        # SLICED, NOT ONE BLOCK: a position that opens mid-sleep must not wait out the period.
        _sleep_watching_for_a_position(
            max(0.0, target_period - (time.monotonic() - pass_started)), mconn, open_period)
    return passes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
