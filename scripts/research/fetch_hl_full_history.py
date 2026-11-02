#!/usr/bin/env python3
"""Fetch every 1h candle Hyperliquid will serve, for the whole perp universe.

`candleSnapshot` returns at most ~5000 candles anchored to the END of a coin's series and does
NOT page backwards. So:

  * a LIVE coin yields only its most recent ~208 days;
  * a DELISTED coin yields the final ~208 days BEFORE its delisting.

The delisted set is therefore the only pre-2026 Hyperliquid history obtainable from the official
endpoint, and it is exactly the population where a short-side run-up strategy either wins big
(the contract dies) or is destroyed (a squeeze forces it out). Every prior study in this repo ran
on the live-coin window only and never saw it.

Provider symbol casing is preserved verbatim (`kPEPE`, not `KPEPE`) — uppercasing 500s the
endpoint and silently deletes the largest meme perps.

Read-only public endpoint. Writes only its own cache directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import requests  # noqa: E402

from sniper.markets.hyperliquid_api import INFO_URL, _HEADERS, _post_info  # noqa: E402

WIDE_START_MS = 1_500_000_000_000      # 2017-07; before any HL listing
WIDE_END_MS = 1_900_000_000_000        # 2030-03; after any plausible run
_MAX_RETRY = 4


def _post(payload, *, pace_s):
    """One paced POST with backoff on the provider's 429."""
    delay = pace_s
    for attempt in range(_MAX_RETRY):
        response = requests.post(INFO_URL, json=payload, headers=_HEADERS, timeout=40)
        if response.status_code == 429:
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("provider rate limit did not clear")


def fetch_universe():
    """[(coin, is_delisted)] with provider casing intact."""
    meta, _ctxs = _post_info({"type": "metaAndAssetCtxs"})
    return [(str(asset["name"]), bool(asset.get("isDelisted")))
            for asset in meta["universe"] if asset.get("name")]


def fetch_candles(coin, *, pace_s):
    body = _post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": WIDE_START_MS, "endTime": WIDE_END_MS}}, pace_s=pace_s)
    return body if isinstance(body, list) else []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cache full available HL 1h history")
    parser.add_argument("--cache-dir", default="data/research/hl_full_history")
    parser.add_argument("--pace-seconds", type=float, default=0.35)
    parser.add_argument("--limit", type=int, default=0, help="0 = whole universe")
    args = parser.parse_args(argv)

    cache = Path(args.cache_dir)
    (cache / "candles").mkdir(parents=True, exist_ok=True)

    universe = fetch_universe()
    if args.limit:
        universe = universe[:args.limit]
    manifest = {"source": "hyperliquid candleSnapshot 1h", "coins": {},
                "universe_n": len(universe), "delisted_n": sum(1 for _c, d in universe if d)}

    for index, (coin, is_delisted) in enumerate(universe, 1):
        path = cache / "candles" / f"candles_{coin}.json"
        try:
            candles = fetch_candles(coin, pace_s=args.pace_seconds)
        except Exception as exc:
            manifest["coins"][coin] = {"error": f"{type(exc).__name__}: {exc}"[:120],
                                       "is_delisted": is_delisted, "n": 0}
            print(f"[{index}/{len(universe)}] {coin}: ERROR {type(exc).__name__}", flush=True)
            continue
        payload = json.dumps(candles, separators=(",", ":"), sort_keys=True)
        path.write_text(payload, encoding="utf-8")
        manifest["coins"][coin] = {
            "n": len(candles), "is_delisted": is_delisted,
            "first_ms": candles[0]["t"] if candles else None,
            "last_ms": candles[-1]["t"] if candles else None,
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }
        print(f"[{index}/{len(universe)}] {coin}: {len(candles)} candles"
              f"{' (delisted)' if is_delisted else ''}", flush=True)
        time.sleep(args.pace_seconds)

    (cache / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    served = sum(1 for v in manifest["coins"].values() if v.get("n"))
    print(f"\ncached {served}/{len(universe)} coins -> {cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
