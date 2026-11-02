#!/usr/bin/env python3
"""A BOUNDED LIVE PROBE of the scanner-scalp idea. Not the study. PAPER ONLY, research key.

WHAT IT IS. The operator asked to SEE two or three real tries rather than read a design. So this
takes the next qualifying scanner signals, prices a paper entry at an exact route quote, samples
the exit quote every few seconds against three rails, and prints one story per trade. It stops
dead at 20 trades.

WHAT IT IS NOT. It is not the preregistered study, it produces no verdict, and nothing it shows
changes the study's design — signal-birth anchor, paged stratum, three cells, negative prior. It
is unblinded, unpowered and self-selected by whatever happens to fire in the next hour. Every
output says PROBE for that reason.

THE PRIOR IT IS RUN AGAINST, so nobody reads two green trades as an edge: the same qualifying
population, priced the same way, is NEGATIVE at every horizon and carried by a long right tail.
The probe MEASURES that prior from the signals table at startup and prints it, rather than
carrying a copied constant — a hard-coded prior goes stale silently and a stale prior is worse
than none, because it still reads as evidence.

QUOTE SPACE, ONCE: entry and every exit sample are route quotes for the exact bag, so the tape is
internally consistent. It ranks nothing against a fill and prices no dollars.

TWO RETURNS, AND THEY ANSWER DIFFERENT QUESTIONS. The moment the entry quote lands, the probe
sells the bag back on paper and keeps that number. That t0 round trip is what the position is
worth before the coin has moved at all, so it is the cost of the route and nothing else. The
rails fire on the move away from THAT, because a stop referenced to the $100 that went in fires
on our own spread — 21.8% of this population is already at or below -20% at 15 seconds, so a
cash-referenced -20% stop would close one trade in five at entry and file it as a fall. The cash
return is still reported, because it is the honest dollar answer; it is just not what a rail
should read.

BUDGET. ~1.5 positions concurrent at the measured qualifying rate and a 10-minute cap; one entry quote plus a
sample every SAMPLE_S. At the 20-trade cap that is a few thousand quotes total, on the PAPER key
(MEMECOIN_PAPER_JUPITER_API_KEY), never the funded one.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Quarantined (Codex W11 review, 2026-10-06): an unpowered paper probe that prints a historical prior
# and writes probe evidence. A bare launch is refused; run it with --diagnostic-only.
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch  # noqa: E402
_legacy_launch(__name__)

from sniper.util.envfiles import load_env_files
from sniper.util.state_paths import state_path, state_root

load_env_files(state_root())

from sniper.execution.solana_real_seams import (            # noqa: E402
    PAPER_JUP_API_KEY_VAR, declare_measurement_process, jup_key_configured,
    jupiter_entry_quote, jupiter_exit_quote)
from sniper.util.clock import real_utc_now_iso              # noqa: E402

logger = logging.getLogger("scalp_probe")

SIGNALS_DB = "data/runner_signals.db"
PROBE_DB = "data/memecoin_scalp_probe.db"
ENTRY_USD = 100.0
TP_PCT = 25.0
STOP_PCT = -20.0          # against the t0 ROUND TRIP, not against cash — see _run_trade
CAP_S = 600.0
SAMPLE_S = 3.0

# THE DENSE WINDOW (2026-08-28, second approval). The first twenty tapes could not tell a price
# that TRAVERSES the stop from one that ARRIVES past it: three stops went from above -5% to below
# -22% inside one 3-second step. Those have opposite implications — if the move is continuous a
# finer interval catches the intermediate value and the ~8-point stop overshoot is recoverable; if
# one sell empties a thin pool there is no intermediate value in observable time and no interval
# helps. One second for the first fifteen, then back to three, which is where the firings cluster.
DENSE_S = 1.0
DENSE_UNTIL_S = 15.0
# Stamped on every row. A 3s sampler and a 1s sampler produce DIFFERENT effective stops, so they
# are different instruments measuring different policies and their tapes must never pool.
SAMPLER_ID = "dense-1s-15s-then-3s"

# 20 approved 2026-08-28 for the first run, +20 approved the same day for the traverse-vs-gap
# question. Counted against the TABLE, so a restart cannot spend the allowance twice.
MAX_TRADES = 44
MAX_CONCURRENT = 6

# Stamped on every row. The first five rows judged their rails against the $100 that went in; from
# 0bd273fd they judge the move away from the t0 round trip, which is a DIFFERENT contract and a
# different effective stop. Without this column a restart mixes both in one table with nothing to
# separate them, and the mixing is silent.
RAIL_POLICY = "roundtrip-baseline-v1"

_LOCK = threading.Lock()
_STATE = {"opened": 0, "closed": 0, "active": 0, "bad_baseline": 0}


_PRIOR = "unmeasured"

# The qualifying predicate, written once. _qualifying() selects rows with it and _measure_prior()
# describes the same population, so the probe can never state a prior for a different cohort.
# `golden_buyers` is a COUNT, not a flag — it runs 0 to 25. `=1` admitted only signals with
# exactly one golden buyer and rejected the 456 rows with more, which is the strongest golden
# evidence in the population. Alpha ruled >=1 on 2026-08-28. It widens accrual by 30% (1959 vs
# 1503) and does not move the prior (60s -8.94% vs -9.10%).
QUALIFIES = "(golden_buyers>=1 OR conviction>=9)"
ENTRY_ATOMIC = 100_000_000    # $100 of USDC, 6dp. The exits are USDC too; entry_out is TOKENS.


def _measure_prior(conn):
    """Describe the population this probe samples, from its own table, at run time.

    The horizon columns are USDC out for the same bag, so the return is against the USDC that
    went IN, never against entry_out_atomic — that column is tokens, and dividing by it gives
    -100% on every row. Reports the span in days as measured, because a rate needs its own
    denominator and an assumed one is how 209/day became 105/day.
    """
    import statistics
    mn, mx = conn.execute("SELECT MIN(created_at), MAX(created_at) FROM runner_signals").fetchone()
    if not mn or not mx:
        return "no signals yet"
    from datetime import datetime
    span = max(1e-6, (datetime.fromisoformat(mx) - datetime.fromisoformat(mn)).total_seconds() / 86400.0)
    n_qual = conn.execute(
        "SELECT COUNT(*) FROM runner_signals WHERE %s" % QUALIFIES).fetchone()[0]
    rows = conn.execute(
        "SELECT exit15_out_atomic x15, exit60_out_atomic x60 FROM runner_signals "
        "WHERE %s AND CAST(entry_out_atomic AS INTEGER)>0" % QUALIFIES).fetchall()
    def pct(key):
        return sorted((int(r[key]) / ENTRY_ATOMIC - 1.0) * 100.0
                      for r in rows if r[key] and int(r[key]) > 0)
    a15, a60 = pct("x15"), pct("x60")
    if not a60:
        return "%d qualifying over %.1fd, no priced horizons yet" % (n_qual, span)
    p90 = a60[min(len(a60) - 1, int(0.90 * len(a60)))]
    return ("%d qualifying over %.1fd (%.0f/day); median %+.2f%% at 15s (n=%d), "
            "%+.2f%% at 60s (n=%d), p90 %+.0f%% [rows with an entry quote and that horizon; "
            "the prereg's stricter rule needs BOTH exits and an uncensored row and reads a few "
            "tenths lower — the two must not be mixed inside one figure]"
            % (n_qual, span, n_qual / span, statistics.median(a15) if a15 else float("nan"),
               len(a15), statistics.median(a60), len(a60), p90))


def _db():
    c = sqlite3.connect(PROBE_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS probe_trades (
        id INTEGER PRIMARY KEY, signal_id INTEGER UNIQUE, mint TEXT, ticker TEXT,
        golden INTEGER, conviction REAL, paged INTEGER,
        signal_t0 REAL, entry_at TEXT, entry_latency_s REAL,
        entry_out_atomic TEXT, entry_roundtrip_usd REAL, samples INTEGER,
        exit_at TEXT, exit_usd REAL, held_s REAL, rail TEXT,
        return_pct REAL, move_pct REAL, path TEXT, rail_policy TEXT,
        roundtrip_lag_s REAL, sampler TEXT, last_obs_pct REAL, rail_step_pct REAL)""")
    # The first run created the table without these. Add them rather than start a new file, so
    # the cash-referenced trades stay readable beside the round-trip-referenced ones.
    have = {r[1] for r in c.execute("PRAGMA table_info(probe_trades)")}
    for col in ("entry_roundtrip_usd REAL", "move_pct REAL", "rail_policy TEXT",
                "roundtrip_lag_s REAL", "sampler TEXT", "last_obs_pct REAL",
                "rail_step_pct REAL"):
        if col.split()[0] not in have:
            c.execute("ALTER TABLE probe_trades ADD COLUMN %s" % col)
    c.commit()
    return c


def _qualifying(conn, after_id, limit=20):
    return conn.execute(
        "SELECT id, mint, ticker, t0, golden_buyers, conviction, candidate_alerted_at "
        "FROM runner_signals WHERE id > ? AND %s "
        "ORDER BY id LIMIT ?" % QUALIFIES, (after_id, limit)).fetchall()


def _run_trade(sig):
    """One paper trade, start to finish, on its own connection. Never raises out."""
    conn = None
    try:
        entry = jupiter_entry_quote(sig["mint"], ENTRY_USD)
        if not entry:
            logger.info("PROBE  %-10s no entry route — skipped", sig["ticker"] or sig["mint"][:8])
            return
        tokens = int(entry.get("outAmount") or 0)
        if tokens <= 0:
            return
        t_entry = time.monotonic()
        entry_at_iso = real_utc_now_iso()      # stamped HERE, at entry, not reused at close
        latency = max(0.0, time.time() - float(sig["t0"] or time.time()))

        # THE t0 ROUND TRIP. Sell the bag back immediately and see what it is worth. This is what
        # the position is worth before the coin has done anything, so it is the cost of the round
        # trip and nothing else. Rails are judged against it. Judging them against the $100 that
        # went in makes the stop fire on our own spread: 21.8% of this population is already at or
        # below -20% at 15 seconds, so a cash-referenced -20% stop closes one trade in five at
        # entry and records a spread measurement as if the coin had fallen.
        rt = jupiter_exit_quote(sig["mint"], tokens, 1.0)
        rt_lag = time.monotonic() - t_entry          # how far from t0 the baseline actually is
        rt_usd = (int(rt.get("outAmount") or 0) / 1e6) if rt else 0.0
        label0 = sig["ticker"] or sig["mint"][:8]
        if rt_usd <= 0:
            logger.info("PROBE  %-10s no t0 round trip — skipped, the rails would be unreadable",
                        label0)
            return
        # A ROUND TRIP CANNOT PROFIT. Buying and selling the same bag in the same instant pays two
        # spreads, so a sell-back worth more than the cash that went in is not a route measurement
        # — the two quotes are not contemporaneous, and the baseline is a later, higher price.
        # Tape 8 read $122.36 on a $100 probe, a 22% instant gain, against 94-96 on every other
        # tape; its move_pct was computed from that inflated reference and its whole tape was an
        # artifact. Refuse it rather than record a position no figure can use.
        if rt_usd >= ENTRY_USD:
            with _LOCK:
                _STATE["bad_baseline"] += 1
            logger.info("PROBE  %-10s BAD BASELINE $%.2f back on $%.0f in after %.1fs — a round "
                        "trip cannot profit, so this is a late quote. Skipped.",
                        label0, rt_usd, ENTRY_USD, rt_lag)
            return
        spread = (rt_usd / ENTRY_USD - 1.0) * 100.0
        with _LOCK:
            _STATE["opened"] += 1
            n = _STATE["opened"]
        label = sig["ticker"] or sig["mint"][:8]
        logger.info("PROBE  #%02d %-10s ENTER $%.0f  latency %.0fs  %s  round trip %+.1f%% "
                    "($%.2f back, +%.1fs)", n, label, ENTRY_USD, latency,
                    "paged" if sig["candidate_alerted_at"] else "unpaged", spread, rt_usd, rt_lag)

        path, rail, exit_usd, samples = [], "timecap", None, 0
        prev_move = None          # the last observation BEFORE the one that fires a rail
        last_obs = None
        while True:
            held = time.monotonic() - t_entry
            if held >= CAP_S:
                break
            time.sleep(DENSE_S if held < DENSE_UNTIL_S else SAMPLE_S)
            q = jupiter_exit_quote(sig["mint"], tokens, 1.0)
            samples += 1
            if not q:
                continue
            usd = int(q.get("outAmount") or 0) / 1e6
            if usd <= 0:
                continue
            exit_usd = usd
            move = (usd / rt_usd - 1.0) * 100.0        # the coin, measured from the t0 round trip
            path.append("%.1fs:%+.1f%%" % (time.monotonic() - t_entry, move))
            prev_move, last_obs = last_obs, move
            if move >= TP_PCT:
                rail = "take_profit"
                break
            if move <= STOP_PCT:
                rail = "stop"
                break
        held = time.monotonic() - t_entry
        ret = ((exit_usd / ENTRY_USD - 1.0) * 100.0) if exit_usd else None    # the dollars
        move = ((exit_usd / rt_usd - 1.0) * 100.0) if exit_usd else None      # the coin
        # THE DECISIVE STEP. `prev_move` is the last observation before the firing one, so the
        # difference is how far the price travelled in the single interval that crossed the rail.
        # This is the mixture measurement the pre-registration asks for: a small step means the
        # price traversed and a finer interval would have caught it; a large one means it arrived
        # on the far side and no interval would. Stored as a number rather than left inside `path`,
        # because a proportion computed by parsing a text column is a proportion nobody recomputes.
        step = ((move - prev_move) if (move is not None and prev_move is not None
                                       and rail in ("take_profit", "stop")) else None)
        conn = _db()
        conn.execute(
            "INSERT OR IGNORE INTO probe_trades(signal_id, mint, ticker, golden, conviction, "
            "paged, signal_t0, entry_at, entry_latency_s, entry_out_atomic, entry_roundtrip_usd, "
            "samples, exit_at, exit_usd, held_s, rail, return_pct, move_pct, path, rail_policy, "
            "roundtrip_lag_s, sampler, last_obs_pct, rail_step_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig["id"], sig["mint"], sig["ticker"], sig["golden_buyers"], sig["conviction"],
             1 if sig["candidate_alerted_at"] else 0, sig["t0"], entry_at_iso, latency,
             str(tokens), rt_usd, samples, real_utc_now_iso(), exit_usd, held, rail, ret, move,
             " ".join(path[:40]), RAIL_POLICY, rt_lag, SAMPLER_ID, prev_move, step))
        conn.commit()
        with _LOCK:
            _STATE["closed"] += 1
        logger.info("PROBE  #%02d %-10s %-12s coin %s | cash %s after %.0fs over %d samples",
                    n, label, rail.upper(),
                    ("%+.1f%%" % move) if move is not None else "no exit quote",
                    ("%+.1f%%" % ret) if ret is not None else "n/a", held, samples)
        if path:
            logger.info("PROBE       path: %s", " ".join(path[:14]))
    except Exception:
        logger.exception("PROBE  trade died for %s", (sig["mint"] or "")[:10])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with _LOCK:
            _STATE["active"] -= 1


def _remaining_budget(limit):
    """Trades left against the LIFETIME cap, counting rows already in the table.

    The approved bound is twenty trades total, not twenty per launch. A per-run counter turns one
    approval into an unbounded series of twenties, and the probe has already been restarted once.
    """
    c = _db()
    try:
        done = c.execute("SELECT COUNT(*) FROM probe_trades").fetchone()[0]
    finally:
        c.close()
    return max(0, min(limit, MAX_TRADES) - done), done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-trades", type=int, default=MAX_TRADES)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if a.max_trades > MAX_TRADES:
        logger.error("PROBE  --max-trades %d exceeds the approved bound of %d; refusing.",
                     a.max_trades, MAX_TRADES)
        return 2
    declare_measurement_process(PAPER_JUP_API_KEY_VAR)
    # A MISSING KEY STOPS THE RUN. Falling back to the keyless public route does not fail — it
    # moves this load onto the shared IP that funded entry quotes depend on, quietly, which is the
    # exact outcome the separate measurement key exists to prevent. Loud beats polite here.
    if not jup_key_configured():
        logger.error("PROBE  %s is not set. Refusing to run on the shared keyless route — "
                     "funded entry quotes share that IP budget.", PAPER_JUP_API_KEY_VAR)
        return 2
    sig = sqlite3.connect("file:%s?mode=ro" % SIGNALS_DB, uri=True)
    sig.row_factory = sqlite3.Row
    global _PRIOR
    _PRIOR = _measure_prior(sig)
    budget, already = _remaining_budget(a.max_trades)
    if budget <= 0:
        # TWO different reasons, said separately. Collapsing them reported "the bound is spent"
        # at 7 of 20 rows, which is the opposite of what a bound message is for.
        if already >= MAX_TRADES:
            logger.info("PROBE  the approved %d-trade bound is spent (%d rows). Nothing to do.",
                        MAX_TRADES, already)
        else:
            logger.info("PROBE  asked for %d trades; %d of the approved %d are recorded.",
                        a.max_trades, already, MAX_TRADES)
        _summary()
        return 0
    if already:
        logger.info("PROBE  %d trades already recorded; %d left of the approved %d.",
                    already, budget, MAX_TRADES)
    logger.info("PROBE  scalp probe starting — PAPER ONLY, research key, %d trades this run, "
                "hard lifetime stop at %d.", budget, MAX_TRADES)
    logger.info("PROBE  prior (measured now): %s", _PRIOR)
    logger.info("PROBE  Two or three tries cannot distinguish an edge from that.")
    after = sig.execute("SELECT COALESCE(MAX(id),0) FROM runner_signals").fetchone()[0]
    logger.info("PROBE  watching for qualifying signals after id %d", after)
    try:
        while _STATE["opened"] < budget:
            for row in _qualifying(sig, after):
                after = row["id"]
                with _LOCK:
                    if _STATE["opened"] >= budget:
                        break
                    if _STATE["active"] >= MAX_CONCURRENT:
                        logger.info("PROBE  at the %d-worker cap, skipping %s",
                                    MAX_CONCURRENT, row["ticker"] or row["mint"][:8])
                        continue
                    _STATE["active"] += 1
                threading.Thread(target=_run_trade, args=(dict(row),), daemon=True).start()
            time.sleep(2.0)
        while _STATE["active"] > 0:
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("PROBE  interrupted")
    _summary()
    return 0


def _decisive_step(row):
    """How far the price moved in the single interval that crossed a rail.

    Read from `rail_step_pct` when the row has it. DERIVED from the stored `path` when it does not
    — the pre-column rows are the 3-second cohort, and that cohort is the control this comparison
    is against, so throwing away a number already present in their data would leave the experiment
    with nothing to compare to. Derived, not invented: the two observations are both in `path`.
    """
    if row["rail_step_pct"] is not None:
        return float(row["rail_step_pct"])
    if row["rail"] not in ("take_profit", "stop"):
        return None
    marks = []
    for token in (row["path"] or "").split():
        _, _, pct = token.partition(":")
        try:
            marks.append(float(pct.rstrip("%")))
        except ValueError:
            return None
    return (marks[-1] - marks[-2]) if len(marks) >= 2 else None


def _summary():
    c = _db()
    every = c.execute("SELECT rail, return_pct, move_pct, entry_roundtrip_usd, entry_latency_s, "
                      "paged, rail_policy, roundtrip_lag_s, sampler, last_obs_pct, "
                      "rail_step_pct, path FROM probe_trades "
                      "WHERE return_pct IS NOT NULL").fetchall()
    if not every:
        logger.info("PROBE  SUMMARY: no completed trades.")
        return
    # ONE CONTRACT PER SUMMARY. The first rows judged their rails against the $100 that went in;
    # the current ones judge the move away from the t0 round trip. Those are different effective
    # stops, so a median over both describes a policy nothing ran. The older rows are reported as
    # a count and nothing else.
    # WHICH CONTRACT A ROW RAN is decided by the t0 round trip, not by the policy tag. The tag
    # was added after the contract changed, so the rows written in between carry a NULL tag and a
    # populated round trip — trusting the tag alone would file them as cash-referenced, which is
    # the opposite of the truth. Only the round-trip code writes that column, so its presence is
    # the reliable discriminator for anything older than the tag.
    def _is_current(r):
        return r["rail_policy"] == RAIL_POLICY or (
            r["rail_policy"] is None and r["entry_roundtrip_usd"] is not None)

    current = [r for r in every if _is_current(r)]
    # AND DROP AN IMPOSSIBLE BASELINE WHEREVER IT IS STORED. The guard at entry stops new ones;
    # rows written before it exist still carry one, and their move_pct is computed against an
    # inflated reference, so they move the rail medians while looking like ordinary tapes.
    clean = [r for r in current
             if not (r["entry_roundtrip_usd"] and r["entry_roundtrip_usd"] >= ENTRY_USD)]
    # ONE SAMPLER PER FIGURE. A 1s sampler and a 3s sampler realize DIFFERENT effective stops on
    # the same price path, so they measure different policies and a median over both describes
    # neither. Rows written before the sampler column carry NULL and are the original 3s cohort.
    rows = [r for r in clean if (r["sampler"] or "3s-flat") == SAMPLER_ID]
    if not rows:
        rows = [r for r in clean if (r["sampler"] or "3s-flat") != SAMPLER_ID]
        _sampler_label = "3s-flat (the earlier cohort; no rows yet under %s)" % SAMPLER_ID
    else:
        _sampler_label = SAMPLER_ID
    otherscohort = len(clean) - len(rows)
    if otherscohort:
        logger.info("PROBE  %d trade(s) ran a DIFFERENT sampler and are excluded from every figure "
                    "below; a different cadence is a different effective stop.", otherscohort)
    bad = len(current) - len(rows)
    if bad:
        logger.info("PROBE  %d recorded trade(s) EXCLUDED for an impossible t0 baseline — the "
                    "sell-back was worth more than the cash in, so the move is measured from a "
                    "later, higher price and is an artifact.", bad)
    stale = len(every) - len(current)
    logger.info("PROBE  sampler: %s", _sampler_label)
    if stale:
        logger.info("PROBE  %d earlier trade(s) judged their rails against the CASH that went in "
                    "and are EXCLUDED from every figure below; different contract, different "
                    "effective stop.", stale)
    if not rows:
        logger.info("PROBE  SUMMARY: no completed trades under %s.", RAIL_POLICY)
        return
    rets = sorted(r["return_pct"] for r in rows)
    moves = sorted(r["move_pct"] for r in rows if r["move_pct"] is not None)
    spreads = sorted((r["entry_roundtrip_usd"] / ENTRY_USD - 1.0) * 100.0
                     for r in rows if r["entry_roundtrip_usd"])
    med = rets[len(rets) // 2]
    wins = sum(1 for r in rets if r > 0)
    rails = {}
    for r in rows:
        rails[r["rail"]] = rails.get(r["rail"], 0) + 1
    lat = sorted(r["entry_latency_s"] for r in rows if r["entry_latency_s"] is not None)
    logger.info("PROBE  SUMMARY (%s) n=%d | cash median %+.1f%% | %d up / %d down | rails %s | "
                "entry latency median %.0fs | paged %d",
                RAIL_POLICY, len(rows), med, wins, len(rows) - wins, rails,
                lat[len(lat) // 2] if lat else -1, sum(r["paged"] for r in rows))
    # WHAT THE RAILS ACTUALLY REALIZED. A rail is a threshold, but the exit happens at the next
    # sample after it is crossed, so the realized number overshoots the nominal one — and on this
    # tape the overshoot is large in both directions. The probe therefore does NOT measure a
    # TP+25/stop-20 policy; it measures that policy sampled every SAMPLE_S seconds. An arm built
    # at these nominal values on a different cadence will not reproduce these numbers.
    for name, nominal in (("take_profit", TP_PCT), ("stop", STOP_PCT)):
        hit = sorted(r["move_pct"] for r in rows
                     if r["rail"] == name and r["move_pct"] is not None)
        if hit:
            logger.info("PROBE  RAIL %s nominal %+.0f%% -> realized median %+.1f%% "
                        "(worst overshoot %+.1f%%) over n=%d under sampler %s",
                        name, nominal, hit[len(hit) // 2],
                        (hit[0] if name == "stop" else hit[-1]) - nominal, len(hit),
                        _sampler_label)
    # THE MIXTURE, which is what the second twenty exists to measure. The question is not whether
    # the price traverses or gaps — the population does both — but in what proportion, because that
    # is the fraction of the stop overshoot a finer interval could recover.
    steps = sorted(abs(x) for x in (_decisive_step(r) for r in rows) if x is not None)
    if steps:
        traversed = sum(1 for x in steps if x < 10.0)
        logger.info("PROBE  DECISIVE STEP (last observation -> the one that fired): median %.1f pts, "
                    "worst %.1f pts, n=%d", steps[len(steps) // 2], steps[-1], len(steps))
        logger.info("PROBE  MIXTURE: %d/%d firings crossed in a step under 10 points (the price was "
                    "there to be seen); %d arrived on the far side. A finer interval can only "
                    "recover the second group's overshoot if they are the first kind.",
                    traversed, len(steps), len(steps) - traversed)
    if _STATE["bad_baseline"]:
        logger.info("PROBE  %d signal(s) skipped on a BAD BASELINE (sell-back worth more than the "
                    "cash in). Those are not losses and not wins — they never opened.",
                    _STATE["bad_baseline"])
    lags = sorted(r["roundtrip_lag_s"] for r in rows if r["roundtrip_lag_s"] is not None)
    if lags:
        logger.info("PROBE  t0 baseline lag: median %.1fs, worst %.1fs. The baseline is a quote "
                    "taken THIS far after entry, so every move is measured from there.",
                    lags[len(lags) // 2], lags[-1])
    if moves and spreads:
        # Split the dollars into the two things that made them. The round trip is what the route
        # took; the move is what the coin did. A cash loss made entirely of round trip says the
        # signal is fine and the execution is not, and the opposite says the reverse.
        logger.info("PROBE  SPLIT: t0 round trip median %+.1f%% (worst %+.1f%%) | "
                    "coin move median %+.1f%% | the cash figure is the sum of the two",
                    spreads[len(spreads) // 2], spreads[0], moves[len(moves) // 2])
    logger.info("PROBE  This is a PROBE. No verdict, quote-space only, and n=%d against a "
                "population measured as: %s", len(rows), _PRIOR)


from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports  # noqa: E402
_quarantine_exports(globals(), ('main',))


if __name__ == "__main__":
    raise SystemExit(main())
