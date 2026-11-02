#!/usr/bin/env python3
"""
Supabase observation tool for strategy agents.

Used by agents via Bash to read/write persistent observations.

Usage:
    # Write a new observation
    python3 agents/tools/obs_tool.py write \
        --strategy insider_copy_v1 \
        --type diagnosis \
        --confidence high \
        --content "3 of 5 tracked wallets show score decline..."

    # Read past observations
    python3 agents/tools/obs_tool.py read \
        --strategy insider_copy_v1 \
        --limit 10

    # Read specific type
    python3 agents/tools/obs_tool.py read \
        --strategy insider_copy_v1 \
        --type thesis \
        --limit 3

Outputs JSON to stdout.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
TABLE = "strategy_agent_observations"

VALID_TYPES = [
    "diagnosis", "thesis", "wallet_research", "pattern",
    "recommendation", "action_taken", "run_summary"
]
VALID_CONFIDENCE = ["low", "medium", "high"]


def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _check_supabase() -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(json.dumps({"error": "SUPABASE_URL or SUPABASE_ANON_KEY not set in environment"}))
        return False
    return True


def write_observation(
    *,
    strategy: str,
    obs_type: str,
    content: str,
    confidence: str = "medium",
    wallets: list[str] | None = None,
    strategy_ids: list[int] | None = None,
) -> dict:
    """Write one observation row and return the inserted payload metadata."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_ANON_KEY not set in environment")
    if obs_type not in VALID_TYPES:
        raise ValueError(f"Invalid type '{obs_type}'. Must be one of: {VALID_TYPES}")
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"Invalid confidence '{confidence}'.")

    payload = {
        "strategy_type": strategy,
        "obs_type": obs_type,
        "content": content,
        "confidence": confidence,
        "wallet_addresses": json.dumps(wallets or []),
        "strategy_ids": json.dumps(strategy_ids or []),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    url = f"{SUPABASE_URL}/rest/v1/{TABLE}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    for k, v in _headers().items():
        req.add_header(k, v)
    req.add_header("Prefer", "return=representation")

    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
        rows = json.loads(body)
        result = rows[0] if rows else {}
        return {"ok": True, "id": result.get("id"), "obs_type": obs_type}


def read_observations(
    *,
    strategy: str,
    obs_type: str | None = None,
    limit: int = 15,
) -> list[dict]:
    """Read recent observations for one strategy."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_ANON_KEY not set in environment")
    limit = min(int(limit or 15), 50)
    params = [
        f"strategy_type=eq.{strategy}",
        "order=created_at.desc",
        f"limit={limit}",
    ]
    if obs_type:
        params.append(f"obs_type=eq.{obs_type}")

    url = f"{SUPABASE_URL}/rest/v1/{TABLE}?" + "&".join(params)
    req = urllib.request.Request(url)
    for k, v in _headers().items():
        req.add_header(k, v)
    req.add_header("Accept", "application/json")

    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
        rows = json.loads(body)
        for row in rows:
            if len(row.get("content", "")) > 800:
                row["content"] = row["content"][:800] + "… [truncated]"
        return rows


def cmd_write(args) -> int:
    if not _check_supabase():
        return 1

    try:
        result = write_observation(
            strategy=args.strategy,
            obs_type=args.type,
            content=args.content,
            confidence=args.confidence,
            wallets=args.wallets,
            strategy_ids=args.strategy_ids,
        )
        print(json.dumps(result))
        return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(json.dumps({"error": f"HTTP {exc.code}: {body[:300]}"}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


def cmd_read(args) -> int:
    if not _check_supabase():
        return 1

    try:
        rows = read_observations(
            strategy=args.strategy,
            obs_type=args.type,
            limit=args.limit,
        )
        print(json.dumps(rows, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy agent observation tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # write subcommand
    w = sub.add_parser("write", help="Write a new observation")
    w.add_argument("--strategy", required=True, help="Strategy type (e.g. insider_copy_v1)")
    w.add_argument("--type", required=True, choices=VALID_TYPES, help="Observation type")
    w.add_argument("--content", required=True, help="Observation text")
    w.add_argument("--confidence", default="medium", choices=VALID_CONFIDENCE)
    w.add_argument("--wallets", nargs="*", help="Relevant wallet addresses")
    w.add_argument("--strategy-ids", nargs="*", type=int, dest="strategy_ids",
                   help="Relevant strategy IDs")

    # read subcommand
    r = sub.add_parser("read", help="Read past observations")
    r.add_argument("--strategy", required=True, help="Strategy type")
    r.add_argument("--type", choices=VALID_TYPES, default=None,
                   help="Filter by observation type (omit for all)")
    r.add_argument("--limit", type=int, default=15)

    args = parser.parse_args()
    if args.cmd == "write":
        return cmd_write(args)
    elif args.cmd == "read":
        return cmd_read(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
