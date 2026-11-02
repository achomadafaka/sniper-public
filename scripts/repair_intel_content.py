#!/usr/bin/env python3
"""Repair legacy Intel / Finance content in Supabase.

Default mode is dry-run. Use ``--apply`` to write changes.

Repairs:
- verbose legacy ``world_events.title`` values
- legacy ``markets/state`` / ``world_narrative`` report headings in ``memories``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from bot.supabase_helper import _request
from sniper.markets.content_normalization import normalize_event_title, normalize_memory_content


TARGET_MEMORY_NAMES = {"markets/state", "world_narrative"}
TARGET_MEMORY_TYPES = {"reference", "knowledge"}


def _repair_world_events(limit: int, apply: bool) -> dict:
    rows = _request(
        "GET",
        f"world_events?select=id,slug,title,last_updated&order=last_updated.desc&limit={limit}",
    ) or []
    changes = []
    applied = 0
    for row in rows:
        original = str(row.get("title") or "")
        normalized = normalize_event_title(original)
        if not original or normalized == original:
            continue
        change = {
            "id": row.get("id"),
            "slug": row.get("slug"),
            "before": original,
            "after": normalized,
        }
        changes.append(change)
        if apply:
            _request("PATCH", f"world_events?id=eq.{row['id']}", body={"title": normalized})
            applied += 1
    return {
        "scanned": len(rows),
        "changes": len(changes),
        "applied": applied,
        "items": changes[:25],
    }


def _repair_memories(limit: int, apply: bool) -> dict:
    rows = _request(
        "GET",
        f"memories?select=id,name,type,content,updated_at&order=updated_at.desc&limit={limit}",
    ) or []
    changes = []
    applied = 0
    for row in rows:
        name = str(row.get("name") or "")
        type_name = str(row.get("type") or "")
        if name not in TARGET_MEMORY_NAMES or type_name not in TARGET_MEMORY_TYPES:
            continue
        original = str(row.get("content") or "")
        normalized = normalize_memory_content(type_name, name, original)
        if normalized == original:
            continue
        change = {
            "id": row.get("id"),
            "name": name,
            "type": type_name,
            "before_preview": original[:160],
            "after_preview": normalized[:160],
        }
        changes.append(change)
        if apply:
            _request("PATCH", f"memories?id=eq.{row['id']}", body={"content": normalized})
            applied += 1
    return {
        "scanned": len(rows),
        "changes": len(changes),
        "applied": applied,
        "items": changes[:25],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the normalized values back to Supabase.")
    parser.add_argument("--limit", type=int, default=1000, help="Max rows to scan per source (default: 1000).")
    parser.add_argument("--skip-world-events", action="store_true", help="Do not scan/repair world_events titles.")
    parser.add_argument("--skip-memories", action="store_true", help="Do not scan/repair memories content.")
    args = parser.parse_args()

    result: dict[str, object] = {"mode": "apply" if args.apply else "dry_run"}
    if not args.skip_world_events:
        result["world_events"] = _repair_world_events(args.limit, args.apply)
    if not args.skip_memories:
        result["memories"] = _repair_memories(args.limit, args.apply)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
