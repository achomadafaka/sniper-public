#!/usr/bin/env python3
"""How much of a held Solana position's time the ticker shows a live on-chain price (read-only).

Owed after the 2026-09-27 display batch (c45f96ef no tape fallback, 2b682408/965c93be carry,
1c4769b5 on-chain liquidity). Samples the runner's live-marks snapshot exactly as the web reads it
(read_snapshot + fresh_marks, corrected UTC clock) while any funded Solana position is open.

DENOMINATOR: samples taken while a funded Solana position is open in the ledger (is_open=1,
execution_mode='real', network_id='solana'). That is our own held book, which is the population the
question is about: the operator's screen during our holds, not the market.

Per position it counts samples with a live mark (anything else is the route price or unknown on the
screen), carried marks, and on-chain liquidity; plus the snapshot's pool_owners counters. The summary
is rewritten atomically on every ledger poll, so a stop keeps what was measured.

    python3 -u scripts/research/live_mark_coverage.py --duration 10800 --out <dir>

No network call; one indexed ledger read every --poll-s; the snapshot file read every --interval-s.
"""
import argparse
import contextlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sniper.util.state_paths import state_root  # noqa: E402
from sniper.util.envfiles import load_env_files  # noqa: E402

load_env_files(str(state_root()))

from sniper.persistence.db_core import get_readonly_connection  # noqa: E402
from sniper.services import memecoin_live_marks as L  # noqa: E402
from sniper.util.clock import real_utc_now_iso  # noqa: E402


def open_solana_positions():
    with contextlib.closing(get_readonly_connection()) as c:
        return {int(r[0]): {"mint": r[1], "ticker": r[2], "opened_recorded_at": r[3]}
                for r in c.execute(
                    "SELECT id, mint, ticker, opened_recorded_at FROM memecoin_positions "
                    "WHERE is_open=1 AND execution_mode='real' AND network_id='solana'")}


def tally(stats, pid, pos, mark, owners):
    s = stats.setdefault(str(pid), {"mint": pos["mint"], "ticker": pos["ticker"],
                                    "opened_recorded_at": pos["opened_recorded_at"],
                                    "samples": 0, "live": 0, "carried": 0, "liq_onchain": 0,
                                    "reserve_known": 0, "sources": {}, "venues": {}, "pools": []})
    s["samples"] += 1
    if mark:
        s["live"] += 1
        s["carried"] += bool(mark.get("carried"))
        s["liq_onchain"] += mark.get("liquidity_basis") == "onchain"
        s["sources"][mark.get("source")] = s["sources"].get(mark.get("source"), 0) + 1
        # WHY on-chain liquidity is absent (2026-09-27: 0 of 2 held positions had it): the
        # publisher needs a reserve on the mark AND, for an AMM, a constant-product owner program.
        s["reserve_known"] += mark.get("reserve_quote") is not None
        s["venues"][mark.get("venue")] = s["venues"].get(mark.get("venue"), 0) + 1
        if mark.get("pool") and mark["pool"] not in s["pools"]:
            s["pools"].append(mark["pool"])
    s["pool_owners"] = owners


def write_summary(path, stats, started_at):
    body = {"started_at": started_at, "written_at": real_utc_now_iso(), "positions": stats}
    for s in stats.values():
        n = s["samples"] or 1
        s["live_share"] = round(s["live"] / n, 4)
        s["carried_share_of_live"] = round(s["carried"] / s["live"], 4) if s["live"] else None
        s["onchain_liq_share_of_live"] = round(s["liq_onchain"] / s["live"], 4) if s["live"] else None
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(body, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=10800.0)
    ap.add_argument("--interval-s", type=float, default=0.5)
    ap.add_argument("--poll-s", type=float, default=5.0)
    ap.add_argument("--path", default=L.default_path())
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    summary = os.path.join(a.out, "live_mark_coverage_summary.json")
    started_at, stats = real_utc_now_iso(), {}
    print("live mark coverage: path=%s exists=%s start=%s" % (a.path, os.path.exists(a.path), started_at))
    end = time.monotonic() + a.duration
    held, next_poll = {}, 0.0
    while time.monotonic() < end:
        now_m = time.monotonic()
        if now_m >= next_poll:
            try:
                held = open_solana_positions()
            except Exception as exc:          # a failed ledger read skips sampling; never a zero
                print("ledger read failed: %r" % (exc,))
                held = {}
            write_summary(summary, stats, started_at)
            next_poll = now_m + a.poll_s
        if held:
            snap = L.read_snapshot(a.path)
            marks = L.fresh_marks(snap, real_utc_now_iso())
            owners = snap.get("pool_owners") if isinstance(snap, dict) else None
            for pid, pos in held.items():
                tally(stats, pid, pos, marks.get("solana:%s" % pos["mint"]), owners)
        time.sleep(a.interval_s)
    write_summary(summary, stats, started_at)
    print(json.dumps(stats, sort_keys=True))


if __name__ == "__main__":
    main()
