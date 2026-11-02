#!/usr/bin/env python3
# scripts/far_whale_threshold_sweep.py
"""§4 threshold sweep for fwlm-v0. Reports asset x threshold -> episode-units/day from
the captured snapshot pool, then (operator decision) freezes exactly one threshold per
asset. Counts EPISODE-UNITS (whale x contiguous presence, §3.1 gap rule) — raw snapshot
rows would inflate with poll cadence. Changing a frozen threshold later = new study_version."""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive.far_whale import EPISODE_GAP_MS  # noqa: E402
from sniper.hyperlive import far_whale_store as S      # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"
DEFAULT_THRESHOLDS = [250_000.0, 500_000.0, 1_000_000.0, 2_000_000.0]


class AlreadyFrozenError(RuntimeError):
    """Raised when --freeze is attempted but a freeze report already exists.
    The freeze is the pre-registration contract — re-tuning requires a new
    study_version, never a silent overwrite (spec §4)."""


def sweep_counts(conn, *, thresholds: list, since_ms: int, until_ms: int) -> dict:
    """asset -> threshold -> {episodes_per_day, distinct_whales}. Counts EPISODE-UNITS
    (whale x contiguous presence, §3.1 gap rule applied to snapshot timestamps), NOT raw
    snapshot rows — a whale parked at a level regenerates a row every poll, so a row
    count would inflate with poll cadence. NOTE: this sizing estimate splits only on the
    60-min gap, not on the live >25bps liq-drift split assign_episode also applies, so it
    is a LOWER BOUND on the study's true episode count — fine for threshold selection."""
    rows = S.sweep_snapshot_rows(conn, since_ms, until_ms)
    days = max((until_ms - since_ms) / 86_400_000.0, 1e-9)
    out: dict = {}
    for thr in thresholds:
        # episode-unit = contiguous presence (gap <= EPISODE_GAP_MS) of one whale >= thr
        units = defaultdict(int)
        whales = defaultdict(set)
        last_seen: dict = {}
        for r in rows:
            if r["position_notional"] < thr:
                continue
            key = (r["asset"], r["address"])
            prev = last_seen.get(key)
            # §3.1 boundary matches the store's assign_episode: a gap of >= EPISODE_GAP_MS
            # starts a new episode-unit (exactly-60-min gap splits), for a consistent count.
            if prev is None or r["ts_ms"] - prev >= EPISODE_GAP_MS:
                units[r["asset"]] += 1
                whales[r["asset"]].add(r["address"])
            last_seen[key] = r["ts_ms"]
        for asset in set(list(units) + list(whales)):
            out.setdefault(asset, {})[thr] = {
                "episodes_per_day": round(units[asset] / days, 2),
                "distinct_whales": len(whales[asset]),
            }
    return out


def freeze(conn, thresholds: dict, *, now_ms: int) -> None:
    """One-shot: writes the freeze report. Refuses if a freeze already exists —
    the freeze is the pre-registration contract; re-tuning = a new study_version,
    never a silent overwrite."""
    if S.load_freeze(conn) is not None:
        raise AlreadyFrozenError(
            "a freeze already exists for this study_version — changing thresholds "
            "requires a new study_version (spec §4), not a re-freeze.")
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {k: float(v) for k, v in thresholds.items()},
                           "campaign_start_ts_ms": int(now_ms)},
                  ts_ms=int(now_ms))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="fwlm-v0 §4 threshold sweep / freeze")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--freeze", default=None,
                    help='e.g. "BTC=1000000,ETH=1000000,SOL=250000" — writes the freeze report')
    args = ap.parse_args(argv)
    conn = S.connect(args.db)
    S.ensure_schema(conn)
    now_ms = int(time.time() * 1000)
    if args.freeze:
        thresholds = dict(kv.split("=") for kv in args.freeze.split(","))
        try:
            freeze(conn, {k.upper(): float(v) for k, v in thresholds.items()}, now_ms=now_ms)
        except AlreadyFrozenError as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 1
        print(f"frozen: {thresholds} campaign_start_ts_ms={now_ms}")
        return 0
    since = now_ms - int(args.days * 86_400_000)
    table = sweep_counts(conn, thresholds=DEFAULT_THRESHOLDS, since_ms=since, until_ms=now_ms)
    print(f"asset x threshold -> episode-units/day (last {args.days:g}d, §3.1 gap rule)")
    for asset in sorted(table):
        for thr in DEFAULT_THRESHOLDS:
            cell = table[asset].get(thr)
            if cell:
                print(f"  {asset:8s} >=${thr/1e6:.2f}M  {cell['episodes_per_day']:6.2f}/day"
                      f"  whales={cell['distinct_whales']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
