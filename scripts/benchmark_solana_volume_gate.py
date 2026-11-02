#!/usr/bin/env python3
"""Isolated gate benchmark; no live files, providers, credentials, or activation."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.persistence import db_sol_volume as db
from sniper.services.memecoin_solana_volume_cache import VolumeCache
from sniper.services.memecoin_slot_lane import cached_volume_decision
from sniper.services.memecoin_real_runner import cached_solana_volume_decision
from sniper.services.memecoin_slot_signal import PUMP_AMM_PROGRAM
from sniper.services.memecoin_live_marks import publish_sol_usd
from sniper.services import memecoin_slot_skips as skips

WSOL = "So11111111111111111111111111111111111111112"
SIGNATURE = "2" * 88


def fixture(amount, *, signature="Other", stale=False):
    now, mono = time.time(), time.monotonic()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    db.ensure_tables(conn)
    conn.execute("INSERT INTO sol_volume_coverage_v1 "
                 "(pool,mint,venue,market_pool,quote_mint,verified_since,through,complete,reason) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 ("vault", "Mint", "pump_amm", "Pool", WSOL, now - 600, now, 1, "ok"))
    conn.execute("INSERT INTO sol_volume_swaps_v1 "
                 "(pool,signature,event_index,block_ts,quote_atomic,maker,payer) VALUES (?,?,?,?,?,?,?)",
                 ("vault", signature, 0, now - 1, str(amount), "Trader", "Trader"))
    conn.commit()
    cache = VolumeCache()
    cache.refresh(conn, now=now, own_owners={"Us"}, published_mono=mono - (6 if stale else 0))
    return conn, cache, now


def measure(fn, samples, expected):
    for _ in range(500):
        result = fn()
        assert getattr(result, "state", result) == expected
    costs = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        result = fn()
        costs.append(time.perf_counter_ns() - started)
    assert getattr(result, "state", result) == expected
    costs.sort()
    return {"n": samples, "p50_us": round(costs[samples // 2] / 1000, 3),
            "p90_us": round(costs[int(samples * .9)] / 1000, 3),
            "max_us": round(costs[-1] / 1000, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20000)
    args = parser.parse_args()
    if not 1000 <= args.samples <= 100000:
        parser.error("samples must be between 1000 and 100000")
    buy = SimpleNamespace(mint="Mint", venue="pump_amm", pool="Pool", signature=SIGNATURE)
    facts, snap = {"mint": "Mint", "mcap_usd": 10000.}, SimpleNamespace(sol_usd=100.)
    connections, cases = [], {}
    stop = threading.Event()
    publisher = None
    try:
        cases["disabled_native"] = measure(
            lambda: cached_volume_decision(None, buy, facts, snap), args.samples, None)
        missing = VolumeCache()
        cases["missing_native"] = measure(
            lambda: cached_volume_decision(missing, buy, facts, snap), args.samples, "unknown")
        skips.note(SIGNATURE, "rejected", "solana_volume_under_floor")
        with skips._LAST_LOCK:
            cases["fallback_policy_peek_writer_locked"] = measure(
                lambda: skips.peek_decision_for(SIGNATURE), args.samples,
                ("rejected", "solana_volume_under_floor"))
        for name, amount, signature, stale, expected in (
            ("refuse_native", 10 ** 9, "Other", False, "refuse"),
            ("allow_native", 3 * 10 ** 9, "Other", False, "allow"),
            ("source_excluded_native", 3 * 10 ** 9, SIGNATURE, False, "refuse"),
            ("stale_native", 10 ** 9, "Other", True, "unknown"),
        ):
            conn, cache, _ = fixture(amount, signature=signature, stale=stale)
            connections.append(conn)
            cases[name] = measure(lambda: cached_volume_decision(cache, buy, facts, snap),
                                  args.samples, expected)
        conn, cache, now = fixture(3 * 10 ** 9)
        connections.append(conn)
        now_iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
        publish_sol_usd(100., now_iso)
        ev = SimpleNamespace(mint="Mint", source_program=PUMP_AMM_PROGRAM,
                             source_pool="Pool", fomo_event_id="onchain:" + SIGNATURE)
        cases["allow_ordinary"] = measure(
            lambda: cached_solana_volume_decision(cache, ev, mcap_usd=10000., now_iso=now_iso),
            args.samples, "allow")

        def publish():
            while not stop.wait(.001):
                cache.refresh(conn, now=now, own_owners={"Us"})

        publisher = threading.Thread(target=publish)
        publisher.start()
        cases["allow_native_concurrent_publication"] = measure(
            lambda: cached_volume_decision(cache, buy, facts, snap), args.samples, "allow")
    finally:
        stop.set()
        if publisher is not None:
            publisher.join()
        for conn in connections:
            conn.close()
    passed = all(result["p90_us"] < 20 for result in cases.values())
    print(json.dumps({"benchmark": "solana_volume_gate_v1", "p90_budget_us": 20,
                      "passed": passed, "activation_changed": False, "cases": cases}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
