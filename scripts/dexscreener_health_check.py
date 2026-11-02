#!/usr/bin/env python3
"""Exit 0 only when stdin is a DexScreener token payload carrying real pair data.

A 200 is not health. Through the 2026-08-24 outage the static routes answered instantly while every
data route hung, so the signal has to be the DATA: at least one pair with a usable priceUsd.

BOTH container shapes are accepted. `/latest/dex/tokens/` documents `{"pairs": [...]}`, but during
the 2026-08-24 partial recovery the same route answered a BARE LIST for one mint and an empty
`pairs` dict for another, both fast and both empty. A predicate that knew only the dict shape would
refuse a healthy array response forever and the watcher would never fire. Accepting both cannot
produce a false HEALTHY: the price check below still has to pass either way.
"""
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)

if isinstance(payload, list):
    pairs = payload
elif isinstance(payload, dict):
    pairs = payload.get("pairs")
else:
    sys.exit(1)

if not isinstance(pairs, list) or not pairs or not isinstance(pairs[0], dict):
    sys.exit(1)
try:
    price = float(pairs[0].get("priceUsd"))
except (TypeError, ValueError):
    sys.exit(1)
sys.exit(0 if price > 0 else 1)
