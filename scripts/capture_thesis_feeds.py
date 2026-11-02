#!/usr/bin/env python3
"""Operator-triggered thesis-feed capture (Phase 1 skeleton — dry-run only, no live loop)."""
from __future__ import annotations

import argparse
import json


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture thesis feeds (operator-triggered).")
    ap.add_argument("--source", choices=["clob_ws", "goldsky_fills", "gamma_universe"], required=True)
    ap.add_argument("--out", default="runs/feed_archive/raw")
    ap.add_argument("--dry-run", action="store_true", default=True)
    args = ap.parse_args()
    print(json.dumps({"source": args.source, "out": args.out, "dry_run": args.dry_run},
                     sort_keys=True))


if __name__ == "__main__":
    main()
