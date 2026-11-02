#!/usr/bin/env python3
"""4663 fast lane M0 read (spec 2026-10-01-memecoin-4663-fast-lane-design.md §2.1, §2.8, §6).

Read-only joins over the shadow's rows (data/memecoin_rh_feed_shadow.db) and the funded ledger:
- PROOF: does the watcher hold a receipt-proven detection for the same tx and mint
  (`onchain_wallet_detections.signature = '4663:<tx>:<mint>'`)? An unproven signal is a source that
  reverted, a fill the watcher refused, or one it never saw.
- ARRIVAL: the watcher's socket arrival (`detected_at`) minus our feed arrival, in ms. Both stamps
  come from the corrected clock in two processes, so differences under ~50 ms are noise.
- WATCHER LOG: the 4663 watcher records a refusal only in its log, so an unproven signal is split
  by logs/onchain_evm_armed.log (matched on its 10-hex tx prefix): refused by the watcher (with the
  decode reason, e.g. unsupported_relayer_swap), or never seen by it (a revert, or a gap).
- J7: a J7 buy for the same (wallet, mint) on 4663 whose Fomo `created_at` (real UTC, ~5 s before
  the confirming block) is within ±120 s of the feed arrival. A signal with
  none is the upper bound on gifts and other-initiator fills (§2.1).
- IMPACT: the source's post-trade sqrtPrice against the pre-state, as a price ratio.

    python3 scripts/research/rh_feed_shadow_read.py [--since 2026-10-01T00:00:00+00:00]
    python3 scripts/research/rh_feed_shadow_read.py --selftest
"""
import argparse
import collections
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return None if not xs else xs[min(len(xs) - 1, int(p * len(xs)))]


def ms_between(a, b):
    """b - a in ms for two ISO stamps, or None."""
    try:
        return (datetime.fromisoformat(b).timestamp() - datetime.fromisoformat(a).timestamp()) * 1000
    except (TypeError, ValueError):
        return None


def price_ratio(pre_sqrt, post_sqrt, quote_side):
    """The coin's post/pre price in quote terms, or None.

    sqrtPriceX96 squared is token1 per token0. quote_side 1 (token1 is the quote): the coin's price
    is that, so post/pre. quote_side 0 (token0 is the quote, e.g. native ETH): the coin's price is
    its inverse, so pre/post. Unknown orientation is None, never a guess."""
    try:
        a, b = int(pre_sqrt), int(post_sqrt)
    except (TypeError, ValueError):
        return None
    if a <= 0 or b <= 0 or quote_side not in (0, 1):
        return None
    return (b / a) ** 2 if quote_side == 1 else (a / b) ** 2


_LOG_LINE = re.compile(r"\[onchain-evm\] (transfer|detection) (0x[0-9a-f]{10})[0-9a-f]{0,54} arrival=(\S+)(.*)")


def watcher_log(path):
    """{10-hex tx prefix: (kind, arrival, outcome, decode)} from the watcher's log; {} if absent.
    `detection` lines are proven buys; `transfer` lines carry outcome= and decode=."""
    out = {}
    try:
        fh = open(path, errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            m = _LOG_LINE.search(line)
            if not m:
                continue
            rest = m.group(4)
            outcome = re.search(r"outcome=(\S+)", rest)
            decode = re.search(r"decode=(\S+)", rest)
            out.setdefault(m.group(2), (m.group(1), m.group(3),
                                        outcome.group(1) if outcome else None,
                                        decode.group(1) if decode else None))
    return out


def j7_match(ledger, uid, mint, arrival):
    if not uid:
        return None
    t = datetime.fromisoformat(arrival)
    lo, hi = (t - timedelta(seconds=120)).isoformat(), (t + timedelta(seconds=120)).isoformat()
    return ledger.execute(
        "SELECT created_at FROM memecoin_events INDEXED BY idx_mc_events_mint WHERE mint=? "
        "AND source_user_id=? AND network_id='4663' AND side='buy' AND created_at BETWEEN ? AND ? "
        "ORDER BY created_at LIMIT 1", (mint, uid, lo, hi)).fetchone()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-01T00:00:00+00:00")
    ap.add_argument("--db", default=str(REPO / "data" / "memecoin_rh_feed_shadow.db"))
    ap.add_argument("--ledger", default=os.environ.get("DB_PATH") or str(REPO / "sniper_tracker.db"))
    ap.add_argument("--tape", default=str(REPO / "data" / "memecoin_onchain_volume.db"))
    ap.add_argument("--watcher-log", default=str(REPO / "logs" / "onchain_evm_armed.log"))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        assert abs(ms_between("2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00.250+00:00") - 250) < 1e-6
        assert abs(price_ratio("100", "110", 1) - 1.21) < 1e-9
        assert abs(price_ratio("110", "100", 0) - 1.21) < 1e-9          # a buy lowers sqrtP
        assert price_ratio("0", "5", 1) is None and price_ratio(None, "5", 1) is None
        assert price_ratio("100", "110", None) is None
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("2026 [onchain-evm] transfer 0xc3212d04c4 arrival=2026-10-01T20:23:27.0+00:00 "
                     "outcome=no_buy retries=0 decode=unsupported_relayer_swap\n"
                     "[onchain-evm] detection 0xbeb76d1939 arrival=2026-10-01T20:23:01.0+00:00 x\n")
        got = watcher_log(fh.name)
        assert got["0xc3212d04c4"] == ("transfer", "2026-10-01T20:23:27.0+00:00", "no_buy",
                                       "unsupported_relayer_swap")
        os.unlink(fh.name)
        assert got["0xbeb76d1939"][0] == "detection" and watcher_log("/nonexistent") == {}
        print("selftest ok")
        return 0
    shadow = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    ledger = sqlite3.connect("file:%s?mode=ro" % a.ledger, uri=True, timeout=10)
    rows = shadow.execute(
        "SELECT tx_hash, mint, source_user_id, handle, arrival_utc, reason, signal, decode_ms, "
        "recover_ms, pre_sqrt_price_x96, post_sqrt_price_x96, post_checked, post_pool "
        "FROM rh_feed_signals "
        "WHERE arrival_utc >= ? ORDER BY arrival_utc", (a.since,)).fetchall()
    print("candidates %d since %s; by reason %s" % (
        len(rows), a.since, dict(collections.Counter(r[5] for r in rows))))
    sig = [r for r in rows if r[6]]
    log = watcher_log(a.watcher_log)
    proven, arrival, j7, impact = 0, [], 0, []
    unproven = collections.Counter()
    for tx, mint, uid, _handle, at, *_rest in sig:
        det = ledger.execute("SELECT detected_at FROM onchain_wallet_detections WHERE signature=?",
                             ("4663:%s:%s" % (tx, mint),)).fetchone()
        seen = log.get(tx[:12])
        if det:
            proven += 1
            arrival.append(ms_between(at, det[0]))
        elif seen:
            unproven["watcher_refused:%s" % (seen[3] or seen[2])] += 1
            arrival.append(ms_between(at, seen[1]))
        else:
            unproven["watcher_never_saw"] += 1
        j7 += j7_match(ledger, uid, mint, at) is not None
    tape = sqlite3.connect("file:%s?mode=ro" % a.tape, uri=True, timeout=5)
    for r in sig:
        meta = tape.execute("SELECT quote_side FROM pool_meta WHERE chain_id='4663' AND pool=?",
                            (r[12],)).fetchone() if r[12] else None
        impact.append(price_ratio(r[9], r[10], meta[0] if meta else None))
    n = len(sig) or 1
    print("signals %d: watcher-proven %d (%.0f%%), J7-corroborated %d (%.0f%%); unproven %s" % (
        len(sig), proven, 100.0 * proven / n, j7, 100.0 * j7 / n, dict(unproven)))
    print("watcher arrival minus feed arrival, ms: p10 %s p50 %s p90 %s (n=%d; >0 = feed earlier)" % (
        *(None if pct(arrival, q) is None else round(pct(arrival, q)) for q in (.1, .5, .9)),
        len(arrival)))
    print("source impact post/pre price: p50 %s p90 %s (n=%d)" % (
        pct(impact, .5), pct(impact, .9), len([x for x in impact if x])))
    print("decode ms p50/p99 %s/%s, recover ms p50/p99 %s/%s" % (
        pct([r[7] for r in rows], .5), pct([r[7] for r in rows], .99),
        pct([r[8] for r in rows], .5), pct([r[8] for r in rows], .99)))
    # Heartbeat counters are cumulative since the runner started; the window's frame max is not.
    last = shadow.execute(
        "SELECT at, frames, blocks, txs, relay_txs, candidates, signals, seq_gaps, reconnects, "
        "queue_drops, cpu_ms FROM rh_feed_heartbeat ORDER BY at DESC LIMIT 1").fetchone()
    worst = shadow.execute("SELECT max(frame_ms_max) FROM rh_feed_heartbeat WHERE at >= ?",
                           (a.since,)).fetchone()[0]
    print("latest heartbeat (cumulative since start): %s; worst frame ms in window %s" % (
        last, worst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
