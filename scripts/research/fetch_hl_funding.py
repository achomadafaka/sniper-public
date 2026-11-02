#!/usr/bin/env python3
"""Page Hyperliquid hourly funding for every coin in the cached candle corpus.

`fundingHistory` returns at most ~500 rows per call, so the span is walked forward from
`startTime`. Long pays positive funding; a SHORT RECEIVES it. Alt funding is persistently
positive, so omitting it from a short-side study is not a conservative rounding — over a 96h hold
it is the same order of magnitude as the entire price effect.

Read-only public endpoint. Writes only its own cache.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.markets.hyperliquid_api import fetch_funding_history  # noqa: E402

PAGE_ROWS = 500
HOUR_MS = 3_600_000


def page_funding(coin, start_ms, end_ms, *, pace_s, max_pages=40):
    """Walk forward; each call returns <= ~500 hourly rows. Stops when a page fails to advance."""
    out, cursor, pages = {}, int(start_ms), 0
    while cursor < end_ms and pages < max_pages:
        for attempt in range(4):
            try:
                rows = fetch_funding_history(coin, cursor, end_ms)
                break
            except Exception:
                time.sleep(pace_s * (2 ** attempt))
        else:
            break
        pages += 1
        if not rows:
            break
        for row in rows:
            out[int(row["time"]) // HOUR_MS * HOUR_MS] = float(row["rate"])
        newest = max(int(r["time"]) for r in rows)
        if newest <= cursor:
            break
        cursor = newest + HOUR_MS
        time.sleep(pace_s)
        if len(rows) < PAGE_ROWS // 2:
            break
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="data/research/hl_full_history")
    parser.add_argument("--pace-seconds", type=float, default=0.25)
    args = parser.parse_args(argv)

    cache = Path(args.cache_dir)
    (cache / "funding").mkdir(parents=True, exist_ok=True)
    manifest = json.loads((cache / "manifest.json").read_text())
    coins = {c: v for c, v in manifest["coins"].items() if v.get("n")}

    written = 0
    for index, (coin, info) in enumerate(sorted(coins.items()), 1):
        path = cache / "funding" / f"funding_{coin}.json"
        if path.exists():
            continue
        rates = page_funding(coin, info["first_ms"], info["last_ms"] + HOUR_MS,
                             pace_s=args.pace_seconds)
        path.write_text(json.dumps(rates, separators=(",", ":"), sort_keys=True))
        written += 1
        print(f"[{index}/{len(coins)}] {coin}: {len(rates)} hourly rates", flush=True)
    print(f"\nwrote funding for {written} coins -> {cache / 'funding'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
