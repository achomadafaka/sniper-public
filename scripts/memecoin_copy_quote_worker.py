#!/usr/bin/env python3
"""Long-lived signal copy-quote worker for ONE frozen wallet cohort. MEASUREMENT ONLY.

Runs standalone, not as a runner thread: a thread would share the funded process's failure
domain, so a quote-provider stall or a crash here could take the funded leg with it. Cron cannot
drive it either — the schedule needs 5-second resolution. So: cron starts a watchdog every
minute, the watchdog's lock permits exactly one worker, and the worker stays up and polls.

It never signs, never submits, and never opens a wallet key. It reads the funded ledger through
`get_readonly_connection()` and writes only to its own measurement database.

Default OFF. Set MEMECOIN_COPY_QUOTE_ENABLED=1 to run it.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sniper.util.envfiles import load_env_files
from sniper.util.state_paths import state_path, state_root

load_env_files(str(state_root()))

from sniper.persistence.db_core import get_readonly_connection      # noqa: E402
from sniper.services import memecoin_signal_copy_quotes as cq       # noqa: E402
# QUOTE SEAMS ONLY. `jupiter_swap_tx` and the signing helpers live in the same module and must
# never be imported here — this process has no business constructing a transaction.
from sniper.execution.solana_real_seams import (                    # noqa: E402
    jupiter_entry_quote, _typed_jupiter_exit_quote, USDC_MINT, _http)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s copy-quote %(message)s")
logger = logging.getLogger(__name__)

# Cohort is a list of WHOLE wallet-chains, `handle:chain` comma-separated. Whole wallet-chains
# only: sampling signals inside an account would make the capture's own coverage a selection
# effect, which is the failure the capture exists to remove.
COHORT_SPEC = os.getenv("MEMECOIN_COPY_QUOTE_COHORT", "frankdegods:solana")
POLL_S = float(os.getenv("MEMECOIN_COPY_QUOTE_POLL_S", "1"))
# Discovery cadence bounds how late the entry quote is. At 30s the measured entry was 0-30s
# after the signal and the first captured episode showed source->quote of 13.3s, most of it
# poll wait. 3s keeps the discovery query cheap while making the entry quote near-immediate;
# the delay that remains is recorded on every row rather than assumed away.
DISCOVER_EVERY_S = float(os.getenv("MEMECOIN_COPY_QUOTE_DISCOVER_S", "3"))
# Episodes older than this are never captured: a stale event's fast window has already passed and
# quoting it would silently produce a curve with no early points.
MAX_EPISODE_AGE_S = float(os.getenv("MEMECOIN_COPY_QUOTE_MAX_AGE_S", "120"))
# Coin-type cohort: capture by COIN property regardless of which wallet bought it. Off unless set.
COIN_FILTER = os.getenv("MEMECOIN_COPY_QUOTE_COIN_FILTER") == "1"
# Chains are captured SEPARATELY and never pooled; this is a list so robin is not structurally
# excluded from every coin-level question. their_market_cap is 100% populated on both chains.
COIN_CHAINS = tuple(x.strip() for x in os.getenv(
    "MEMECOIN_COPY_QUOTE_COIN_CHAINS", "solana").split(",") if x.strip())
STOP = False


def _stop(_signum, _frame):
    global STOP
    STOP = True
    logger.info("SIGTERM received; finishing the current leg then exiting")


def parse_cohort(spec):
    """`handle:chain,handle:chain` -> [(handle, chain)]. Refuses a bare handle: a wallet is only
    ever armed, screened or captured per chain, and a chainless member would silently pool them."""
    members = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise SystemExit(f"cohort member {part!r} needs an explicit chain, e.g. {part}:solana")
        handle, chain = part.split(":", 1)
        members.append((handle.strip(), chain.strip()))
    if not members:
        raise SystemExit("cohort is empty")
    return members


def resolve_cohort(ro, spec=COHORT_SPEC):
    """(handle, chain) -> (uid, chain, handle). A missing wallet is fatal, never skipped: a
    silently shrunk cohort would look identical to a wallet that simply stopped trading."""
    out = []
    for handle, chain in parse_cohort(spec):
        row = ro.execute("SELECT source_user_id FROM memecoin_wallets WHERE handle=?",
                         (handle,)).fetchone()
        if not row:
            raise SystemExit(f"cohort wallet {handle!r} is not in memecoin_wallets")
        out.append((row[0], chain, handle))
    return out


def discover(ro, mdb, cohort, watermark):
    """Queue entry quotes for new episodes across every cohort member. Skips anything already
    past its fast window — a stale event's early points are unrecoverable, and a curve missing
    its first 60 seconds is worse than no curve."""
    now = cq.utc_now()
    found = 0
    for uid, chain, handle in cohort:
        for ep in cq.due_episodes(ro, uid, chain, watermark):
            created = cq.parse_iso(ep.get("created_at"))
            if created and (now - created).total_seconds() > MAX_EPISODE_AGE_S:
                continue
            # Safety envelope is checked per episode, not per batch: a burst must be refused
            # part-way rather than admitted whole. Refusals are logged, never silent.
            blocked = cq.admission_block(mdb, now)
            if blocked:
                logger.warning("refusing new episode %s/%s: %s", handle, ep["mint"][:10], blocked)
                return found
            cq.schedule_entry(mdb, ep, now=now, cohort="wallet")
            found += 1
    for chain in (COIN_CHAINS if COIN_FILTER else ()):
        for ep in cq.coin_filter_episodes(ro, chain, watermark):
            created = cq.parse_iso(ep.get("created_at"))
            if created and (now - created).total_seconds() > MAX_EPISODE_AGE_S:
                continue
            blocked = cq.admission_block(mdb, now)
            if blocked:
                logger.warning("refusing coin-filter episode %s: %s", ep["mint"][:10], blocked)
                break
            before = mdb.total_changes
            cq.schedule_entry(mdb, ep, now=now, cohort="coin_filter")
            if mdb.total_changes > before:      # INSERT OR IGNORE: only log a genuine admission
                logger.info("coin-filter episode %s mcap=$%.0fk buyers=%d",
                            ep["mint"][:10], (ep["their_market_cap"] or 0) / 1e3,
                            ep["prior_buyers"])
                found += 1
    return found


_HANDLES = {}


def handle_for(row):
    """uid -> handle, for the per-wallet tail extension. Cached: the cohort is fixed for the
    process lifetime, and a lookup per leg would hit the funded database 21 times per episode."""
    return _HANDLES.get(row["source_user_id"], "")


def run_leg(ro, mdb, row):
    """Execute one scheduled quote leg and record it. Never raises into the poll loop."""
    started = cq.utc_now()
    ep = dict(ro.execute(
        "SELECT received_at, real_decision_at FROM memecoin_events WHERE fomo_event_id=?",
        (row["origin_event_id"],)).fetchone() or {})
    try:
        if row["leg"] == "entry":
            # An episode with no entry quote has NO TRUE ENTRY: every exit leg is then refused,
            # so the reason matters. A rate limit and an unquotable token are opposite
            # conclusions and must not both read as 'entry_quote_unavailable'.
            observed = {}

            def tracked(*args, **kwargs):
                try:
                    result = _http(*args, **kwargs)
                except Exception as exc:                   # noqa: BLE001 - recorded, then re-raised
                    observed["exception"] = exc
                    raise
                observed["response"] = result
                return result

            raw = jupiter_entry_quote(row["mint"], cq.ENTRY_USD, http=tracked)
            failure = None if raw else cq.classify_quote_failure(observed)
            quote = cq.normalise_quote(raw, input_asset=USDC_MINT, output_asset=row["mint"])
        else:
            amount = cq.expected_tokens(mdb, row["origin_event_id"])
            if amount is None:
                cq.record(mdb, row, status="failed", started=started,
                          failure="no_entry_quantity", ep=ep,
                          anchor=cq.parse_iso(row.get("anchor_at")))
                return
            raw, typed = _typed_jupiter_exit_quote(row["mint"], amount, 1.0)
            failure = None if raw else (getattr(typed, "value", None) or str(typed or "unknown"))
            quote = cq.normalise_quote(raw, input_asset=row["mint"], output_asset=USDC_MINT)
    except Exception as exc:                      # a provider fault is data, not a crash
        logger.exception("quote leg raised: %s %s", row["origin_event_id"], row["leg"])
        cq.record(mdb, row, status="failed", started=started, failure=f"exception:{type(exc).__name__}",
                  ep=ep, anchor=cq.parse_iso(row.get("anchor_at")))
        return

    quoted = cq.utc_now()
    anchor = cq.parse_iso(row.get("anchor_at")) or quoted
    if quote is None:
        cq.record(mdb, row, status="failed", started=started, quoted=quoted,
                  failure=failure or "malformed_quote", ep=ep, anchor=anchor)
        return
    cq.record(mdb, row, status="ok", started=started, quoted=quoted, quote=quote, ep=ep,
              anchor=anchor)
    if row["leg"] == "entry":
        # horizons run from THIS moment, so a slow funded hook shifts the curve visibly
        legs = cq.legs_for(f"{handle_for(row)}:{row['network_id']}")
        cq.schedule_exits(mdb, row, quoted, legs=legs)
        logger.info("entry quoted %s $%s -> %s tokens; %d exit legs (tail through %ds)",
                    row["mint"][:10], cq.ENTRY_USD, quote["output_amount_atomic"],
                    len(legs), legs[-1])


def main():
    if os.getenv("MEMECOIN_COPY_QUOTE_ENABLED") != "1":
        logger.info("MEMECOIN_COPY_QUOTE_ENABLED != 1; exiting without capturing")
        return 0
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # THIS PROCESS SPENDS THE MEASUREMENT KEY. It imports the funded quote functions, so without
    # this it would quote on MEMECOIN_JUPITER_API_KEY — the funded exit brain's budget — the moment
    # MEMECOIN_COPY_QUOTE_ENABLED=1. Declared before the first quote can be issued.
    from sniper.execution.solana_real_seams import declare_measurement_process
    declare_measurement_process()

    mdb = cq.open_measurement_db(
        os.getenv("MEMECOIN_COPY_QUOTE_DB") or state_path("data", "memecoin_copy_quotes.db"))
    ro = get_readonly_connection()
    cohort = resolve_cohort(ro)
    for uid, chain, handle in cohort:
        _HANDLES[uid] = handle
        legs = cq.legs_for(f"{handle}:{chain}")
        logger.info("cohort member %s (%s) on %s | %d exit legs, tail through %ds",
                    handle, uid[:8], chain, len(legs), legs[-1])
    if COIN_FILTER:
        logger.info("coin-filter cohort ON: %s | mcap<$%.0fk, %d-%d prior buyers (per chain, "
                    "never pooled)", ",".join(COIN_CHAINS),
                    cq.COIN_MAX_MCAP / 1e3, cq.COIN_CONSENSUS_MIN, cq.COIN_CONSENSUS_MAX)
    logger.info("safety: max_active=%d max_daily_quotes=%d min_interval=%.2fs",
                cq.MAX_ACTIVE_EPISODES, cq.MAX_QUOTES_PER_DAY, cq.MIN_QUOTE_INTERVAL_S)
    logger.info("db=%s | %d member(s)",
                os.getenv("MEMECOIN_COPY_QUOTE_DB") or "state:data/memecoin_copy_quotes.db",
                len(cohort))

    watermark = cq.iso(cq.utc_now() - timedelta(seconds=MAX_EPISODE_AGE_S))
    last_discover = 0.0
    while not STOP:
        loop_started = time.monotonic()
        try:
            if loop_started - last_discover >= DISCOVER_EVERY_S:
                n = discover(ro, mdb, cohort, watermark)
                if n:
                    logger.info("queued %d new episode(s)", n)
                watermark = cq.iso(cq.utc_now() - timedelta(seconds=MAX_EPISODE_AGE_S))
                last_discover = loop_started
            for row in cq.claim_due(mdb):
                run_leg(ro, mdb, row)
                if STOP:
                    break
                time.sleep(cq.MIN_QUOTE_INTERVAL_S)   # spacing so a burst cannot self-429
        except Exception:
            logger.exception("poll iteration failed; continuing")
        time.sleep(max(0.0, POLL_S - (time.monotonic() - loop_started)))
    logger.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
