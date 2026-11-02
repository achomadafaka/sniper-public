#!/usr/bin/env python3
"""Price refused coins on our own EVM swap tape. MEASUREMENT ONLY; writes only its own database.

    python3 scripts/memecoin_refusal_shadow_loop.py [--once] [--dry-run] [--interval 60]

Spec: Alpha, 2026-09-23, HANDOFF.md "REFUSAL SHADOW LANE SPEC". Module doctrine and every limit of
the numbers: `sniper/services/memecoin_refusal_shadow.py`.

It reads the funded ledger and the tape READ-ONLY and never touches the funded entry path. It learns
about a refusal from `memecoin_entry_attempts`, the durable record the runner already writes.

NO CURSOR, ON PURPOSE. Each pass rescans the last `--rescan-minutes` of attempts and relies on
UNIQUE (network_id, mint, origin_event_id) plus INSERT OR IGNORE. A cursor held in memory restarts
at nothing and a cursor on disk is one more thing to corrupt; a rescan of ~100 rows costs nothing
and is self-healing across a restart, a deploy and a crash.

WINDOWS ARE BUILT IN PYTHON WITH A 'T' SEPARATOR. SQLite's `datetime('now')` renders a SPACE, and
every 'T' stamp sorts AFTER it, which silently turns a window into a whole day.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files                          # noqa: E402
from sniper.util.state_paths import state_root, state_path               # noqa: E402
load_env_files(str(state_root()))

import sniper.settings.memecoin as cfg                                   # noqa: E402
from sniper.persistence.db_core import get_readonly_connection           # noqa: E402
from sniper.services import memecoin_refusal_shadow as shadow            # noqa: E402
from sniper.services.memecoin_funded_policy import _follow3050_policy    # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "research"))
from refusal_family_outcomes import family                               # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("memecoin_refusal_shadow")

LEDGER = "sniper_tracker.db"
TAPE = "data/memecoin_onchain_volume.db"
STORE = "data/memecoin_refusal_shadow.db"
ENQUEUE_STATUSES = ("refused", "rejected", "aborted")


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def epoch_of(iso):
    """Epoch seconds for an ISO stamp, or None. Never returns 0 for an unparseable stamp."""
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def enqueue_pass(ledger, store, *, now, rescan_minutes, dry_run=False):
    """Insert every qualifying refusal from the rescan window. Returns (added, seen, skipped)."""
    since = (now - dt.timedelta(minutes=rescan_minutes)).isoformat()
    rows = ledger.execute(
        "SELECT a.network_id, a.mint, a.origin_event_id, a.attempt_id, a.terminal_reason, "
        "       a.terminal_status, a.decision_started_at, "
        "       e.their_size_usd, COALESCE(e.received_at_normalized, e.received_at) "
        "  FROM memecoin_entry_attempts a "
        "  LEFT JOIN memecoin_events e ON e.fomo_event_id = a.origin_event_id "
        " WHERE a.decision_started_at > ? AND a.mint IS NOT NULL "
        "   AND a.origin_event_id IS NOT NULL "
        f"  AND a.terminal_status IN ({','.join('?' for _ in ENQUEUE_STATUSES)})",
        (since, *ENQUEUE_STATUSES)).fetchall()
    added = skipped = 0
    for (net, mint, origin, attempt, reason, status, at, size, received) in rows:
        fam = family(reason)
        if not shadow.is_market_family(fam):
            skipped += 1
            continue
        # A MISSING SOURCE SIZE IS NOT A SMALL ONE. `their_size_usd` is the live source buy size and
        # the join can miss (75 of 8,931 attempts had no event row over 7 days). An unknown size
        # cannot clear a $700 floor, so it is skipped and counted, never read as 0.
        if size is None or float(size) < cfg.MEMECOIN_REFUSAL_SHADOW_MIN_SOURCE_USD:
            skipped += 1
            continue
        on_tape = str(net) in cfg.MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS
        # AN OLD ROW AT FIRST SIGHT IS NOT A MISSED POLL. The rescan window is longer than the
        # sampling window, so the first pass after a start or a deploy sees refusals that were
        # already past the tape's reach. Labelling those `window_expired` would book our own
        # start-up as lost uptime; they get their own reason and the census row is still kept.
        age_s = (now - dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))).total_seconds() \
            if epoch_of(at) is not None else None
        too_old = age_s is not None and age_s > cfg.MEMECOIN_REFUSAL_SHADOW_MAX_AGE_S
        item = {"network_id": net, "mint": mint, "origin_event_id": origin, "attempt_id": attempt,
                "family": fam, "terminal_reason": reason, "terminal_status": status,
                "source_size_usd": float(size), "decision_started_at": at,
                "signal_received_at": received}
        if dry_run:
            added += 1
            continue
        if not on_tape:
            sampled, reason = 0, "no_tape_for_chain"
        elif too_old:
            sampled, reason = 0, "enqueued_after_window"
        else:
            sampled, reason = -1, None
        if shadow.enqueue(store, item, enqueued_at=now.isoformat(),
                          sampled=sampled, sample_reason=reason):
            added += 1
    return added, len(rows), skipped


def pools_for_mint(tape, chain_id, mint):
    """Pools whose token0 or token1 is this mint, with the quote side and both decimals."""
    return tape.execute(
        "SELECT pool, token0, token1, quote_side, token0_decimals, token1_decimals "
        "  FROM pool_meta WHERE chain_id=? AND quote_side IS NOT NULL "
        "   AND (LOWER(token0)=LOWER(?) OR LOWER(token1)=LOWER(?))",
        (str(chain_id), mint, mint)).fetchall()


def ticks_for_pool(tape, chain_id, pool, *, start_iso, end_iso):
    return tape.execute(
        "SELECT observed_at, sqrt_price_x96, liquidity FROM pool_ticks "
        " WHERE chain_id=? AND pool=? AND observed_at >= ? AND observed_at <= ? "
        " ORDER BY observed_at", (str(chain_id), pool, start_iso, end_iso)).fetchall()


def quote_usd_for(tape, chain_id, token):
    row = tape.execute(
        "SELECT usd FROM volume_quote_prices WHERE chain_id=? AND LOWER(token)=LOWER(?)",
        (str(chain_id), token)).fetchone()
    return row[0] if row and row[0] and row[0] > 0 else None


def sample_one(tape, row, *, policy, now):
    """Build the fields for one pending row. Returns a dict for `record_sample`.

    THE POOL IS CHOSEN ONCE AND RECORDED. A mint on chain 4663 carries dozens of pools; switching
    pools inside one series would compare prices in different units and different quote assets,
    which is the same defect that stopped the Solana replay. The pool with the MOST ticks in the
    window wins, `pools_seen` records how many others existed, and `pool` records which one spoke.
    """
    (row_id, net, mint, decided_at) = row
    decision_epoch = epoch_of(decided_at)
    if decision_epoch is None:
        return {"sampled": 0, "sample_reason": "bad_decision_stamp", "sampled_at": now.isoformat()}
    start = dt.datetime.fromtimestamp(decision_epoch - 120, dt.timezone.utc).isoformat()
    end = dt.datetime.fromtimestamp(
        decision_epoch + cfg.MEMECOIN_REFUSAL_SHADOW_HORIZON_S + 120, dt.timezone.utc).isoformat()
    pools = pools_for_mint(tape, net, mint)
    if not pools:
        return {"sampled": 0, "sample_reason": "no_pool_meta", "sampled_at": now.isoformat(),
                "pools_seen": 0}
    best = None
    for (pool, token0, token1, quote_side, dec0, dec1) in pools:
        ticks = ticks_for_pool(tape, net, pool, start_iso=start, end_iso=end)
        if not ticks:
            continue
        if best is None or len(ticks) > len(best[1]):
            best = ((pool, token0, token1, quote_side, dec0, dec1), ticks)
    if best is None:
        return {"sampled": 0, "sample_reason": "no_tape", "sampled_at": now.isoformat(),
                "pools_seen": len(pools)}
    (pool, token0, token1, quote_side, dec0, dec1), ticks = best
    bars = shadow.bars_1s([(epoch_of(t[0]), t[1], t[2]) for t in ticks],
                          quote_side=quote_side, token0_decimals=dec0, token1_decimals=dec1)
    basis = shadow.price_basis(quote_side, dec0, dec1)
    if not bars:
        return {"sampled": 0, "sample_reason": "no_readable_price", "sampled_at": now.isoformat(),
                "pool": pool, "pools_seen": len(pools), "ticks_used": len(ticks),
                "price_basis": basis}
    quote_token = token0 if int(quote_side) == 0 else token1
    quote_usd = quote_usd_for(tape, net, quote_token)
    quote_decimals = dec0 if int(quote_side) == 0 else dec1
    at_signal, fill, impact = shadow.model_fill(
        bars, decision_epoch=decision_epoch,
        fill_lag_s=cfg.MEMECOIN_REFUSAL_SHADOW_FILL_LAG_S,
        clip_usd=cfg.MEMECOIN_REFUSAL_SHADOW_CLIP_USD,
        quote_is_token0=(int(quote_side) == 0), quote_decimals=quote_decimals, quote_usd=quote_usd)
    out = {"sampled": 1, "sampled_at": now.isoformat(), "pool": pool, "pools_seen": len(pools),
           "ticks_used": len(ticks), "price_basis": basis, "quote_usd": quote_usd,
           "price_at_signal": at_signal,
           "fill_price": fill, "fill_impact_pct": impact,
           "clip_usd": cfg.MEMECOIN_REFUSAL_SHADOW_CLIP_USD,
           "sample_reason": "ok" if impact is not None else "ok_impact_unknown"}
    if not fill:
        out.update({"sampled": 0, "sample_reason": "no_bar_at_fill"})
        return out
    fill_epoch = decision_epoch + cfg.MEMECOIN_REFUSAL_SHADOW_FILL_LAG_S
    exit_price, exit_reason, after = shadow.simulate_exit(
        bars, fill_epoch=fill_epoch, fill_price=fill, policy=policy,
        sweep_s=cfg.MEMECOIN_REFUSAL_SHADOW_SWEEP_S,
        horizon_s=cfg.MEMECOIN_REFUSAL_SHADOW_HORIZON_S)
    pnl_usd, pnl_pct = (None, None)
    if exit_reason in ("open_at_horizon", "no_fill"):
        # A ROW THAT NEVER EXITED HAS NO EXIT PRICE. `simulate_exit` hands back the last bar it saw
        # so a caller can inspect it, but writing that into `exit_price` would read as a close the
        # policy never took. The last price is already in `horizons_json` under the widest horizon.
        exit_price = None
    else:
        pnl_usd, pnl_pct = shadow.pnl(fill, exit_price, cfg.MEMECOIN_REFUSAL_SHADOW_CLIP_USD)
    out.update({"exit_price": exit_price, "exit_reason": exit_reason, "exit_after_s": after,
                "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
                "horizons_json": shadow.dumps_horizons(
                    shadow.horizons(bars, fill_epoch=fill_epoch))})
    return out


def sample_pass(tape, store, *, now, policy, dry_run=False):
    """Sample pending rows aged into the window, and expire the ones the tape can no longer cover."""
    lo = (now - dt.timedelta(seconds=cfg.MEMECOIN_REFUSAL_SHADOW_MAX_AGE_S)).isoformat()
    hi = (now - dt.timedelta(seconds=cfg.MEMECOIN_REFUSAL_SHADOW_MIN_AGE_S)).isoformat()
    # AN EXPIRY IS NOT A NO_TAPE. A row the poller never reached in time is missing evidence about
    # OUR uptime; a no_tape row is missing evidence about the coin. Collapsing them would let a
    # stopped worker read as a silent market.
    expired = store.execute(
        "SELECT id FROM memecoin_refusal_shadow WHERE sampled=-1 AND decision_started_at < ?",
        (lo,)).fetchall()
    if not dry_run:
        for (row_id,) in expired:
            shadow.record_sample(store, row_id, sampled=0, sample_reason="window_expired",
                                 sampled_at=now.isoformat())
    due = store.execute(
        "SELECT id, network_id, mint, decision_started_at FROM memecoin_refusal_shadow "
        " WHERE sampled=-1 AND decision_started_at >= ? AND decision_started_at <= ? "
        " ORDER BY decision_started_at", (lo, hi)).fetchall()
    sampled = 0
    for row in due:
        if dry_run:
            sampled += 1
            continue
        try:
            fields = sample_one(tape, row, policy=policy, now=now)
        except Exception:
            logger.exception("refusal shadow: sample raised for row %s", row[0])
            continue
        if shadow.record_sample(store, row[0], **fields):
            sampled += 1
    return sampled, len(expired), len(due)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written; opens the store read-only and writes nothing")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--rescan-minutes", type=float, default=90.0)
    args = ap.parse_args()

    if not cfg.MEMECOIN_REFUSAL_SHADOW_ENABLED:
        logger.info("MEMECOIN_REFUSAL_SHADOW_ENABLED is off; nothing to do")
        return 0
    policy = _follow3050_policy(coin_band="nano")
    logger.info("policy %s tp=%s stop=%s debounce=%s catastrophe=%s clip=%s lag=%ss",
                policy.policy_id, policy.tp_pct, policy.stop_pct, policy.stop_debounce_sweeps,
                policy.catastrophe_pct, cfg.MEMECOIN_REFUSAL_SHADOW_CLIP_USD,
                cfg.MEMECOIN_REFUSAL_SHADOW_FILL_LAG_S)
    ledger = get_readonly_connection(str(state_path(LEDGER)))
    tape = shadow.connect(str(state_path(TAPE)), readonly=True)
    # A DRY RUN MUST NOT CREATE THE STORE. `mode=ro` cannot open a file that is not there, and
    # creating the schema to prove we would not write to it is still a write, so an absent store
    # becomes an in-memory one for the pass.
    store_path = str(state_path(STORE))
    if args.dry_run:
        store = shadow.connect(store_path, readonly=True) if os.path.exists(store_path) \
            else shadow.connect(":memory:")
    else:
        store = shadow.connect(store_path)
    while True:
        now = now_utc()
        added, seen, skipped = enqueue_pass(ledger, store, now=now,
                                            rescan_minutes=args.rescan_minutes,
                                            dry_run=args.dry_run)
        sampled, expired, due = sample_pass(tape, store, now=now, policy=policy,
                                            dry_run=args.dry_run)
        logger.info("enqueue seen=%d added=%d skipped=%d | sample due=%d done=%d expired=%d%s",
                    seen, added, skipped, due, sampled, expired,
                    " (dry run, nothing written)" if args.dry_run else "")
        if args.once:
            return 0
        time.sleep(max(5.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
