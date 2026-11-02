#!/usr/bin/env python3
"""Analyze live stink-bid shadow JSONL queue sensitivity."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _empty_model() -> dict:
    return {
        "events": 0,
        "price_crossed": 0,
        "size_qualified": 0,
        "queue_fillable": 0,
    }


def analyze_shadow_file(path: str | Path) -> dict:
    models: dict[str, dict] = {}
    book_snapshots = 0
    trade_evaluations = 0
    markets = set()
    tokens = set()
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("condition_id"):
                markets.add(row["condition_id"])
            if row.get("token_id"):
                tokens.add(row["token_id"])
            event_type = row.get("event_type")
            if event_type == "book_snapshot":
                book_snapshots += 1
            elif event_type == "trade_evaluation":
                trade_evaluations += 1
                queue_models = row.get("queue_models") or []
                if not queue_models and row.get("reason"):
                    queue_models = [{"queue_model": "pessimistic", **row}]
                for model_row in queue_models:
                    model_name = model_row.get("queue_model") or "unknown"
                    stats = models.setdefault(model_name, _empty_model())
                    stats["events"] += 1
                    if model_row.get("price_crossed"):
                        stats["price_crossed"] += 1
                    if model_row.get("size_qualified"):
                        stats["size_qualified"] += 1
                    if model_row.get("queue_fillable"):
                        stats["queue_fillable"] += 1
    for stats in models.values():
        events = stats["events"]
        stats["queue_fillable_rate"] = stats["queue_fillable"] / events if events else 0.0
    return {
        "path": str(path),
        "markets": len(markets),
        "tokens": len(tokens),
        "book_snapshots": book_snapshots,
        "trade_evaluations": trade_evaluations,
        "models": dict(sorted(models.items())),
    }


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    out = analyze_shadow_file(args.path)
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
