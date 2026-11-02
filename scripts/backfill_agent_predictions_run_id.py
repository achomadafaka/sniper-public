#!/usr/bin/env python3
"""One-shot backfill: link historical agent_predictions to their parent agent_runs.

Required for calibration loop v1 eligibility filter (spec §5.2.1). Reads only
predictions with run_id IS NULL; matches to runs by (agent_type, created_at)
within a symmetric +/-1h bracket window; updates only on unambiguous
single-match.

Scoped to CALIBRATION_ENABLED_AGENTS in web_settings.py — rejects any other
agent_type to prevent wasted ambiguity work on out-of-scope agents.

Usage:
    python3 scripts/backfill_agent_predictions_run_id.py --agent-type insider_copy
    python3 scripts/backfill_agent_predictions_run_id.py --agent-type insider_copy --dry-run

Exit codes:
    0 — success
    2 — agent_type not in CALIBRATION_ENABLED_AGENTS allow-list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")

# 1h window bracketing the prediction's created_at. Historical rows were
# logged at run completion, while the new start-run path writes the parent row
# before predictions. The symmetric window supports both shapes.
WINDOW_SECONDS = 3600


def _request(method: str, path: str, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode()) if resp.length != 0 else []


def _parse_ts(value: str):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def backfill(agent_type: str, dry_run: bool = False) -> dict:
    """Run the backfill for a single agent_type. Returns stats dict."""
    try:
        import sniper.settings.web as ws
        enabled = ws.CALIBRATION_ENABLED_AGENTS
    except Exception:
        enabled = ["insider_copy"]

    if agent_type not in enabled:
        print(json.dumps({
            "ok": False,
            "error": f"agent_type {agent_type!r} not in CALIBRATION_ENABLED_AGENTS ({enabled}); "
                     f"add it to web_settings before running backfill",
        }))
        sys.exit(2)

    # Fetch all predictions for the agent (we filter run_id IS NULL locally so
    # we can also count already-linked rows for reporting).
    preds_path = (
        f"agent_predictions?select=id,created_at,run_id"
        f"&agent_type=eq.{urllib.parse.quote(agent_type)}"
        f"&order=created_at.asc&limit=10000"
    )
    preds = _request("GET", preds_path) or []

    # Fetch all runs for the agent — bounded because only one agent at a time
    # and ~hundreds of rows typical.
    runs_path = (
        f"agent_runs?select=id,created_at"
        f"&agent_type=eq.{urllib.parse.quote(agent_type)}"
        f"&order=created_at.asc&limit=10000"
    )
    runs = _request("GET", runs_path) or []

    matched = 0
    ambiguous = 0
    orphaned = 0
    already_linked = 0

    for p in preds:
        if p.get("run_id"):
            already_linked += 1
            continue
        try:
            p_ts = _parse_ts(p["created_at"])
        except Exception:
            orphaned += 1
            continue

        # Match runs whose created_at is within +/-1h of prediction time.
        candidates = []
        for r in runs:
            try:
                r_ts = _parse_ts(r["created_at"])
            except Exception:
                continue
            delta = (p_ts - r_ts).total_seconds()
            if abs(delta) <= WINDOW_SECONDS:
                candidates.append(r)

        if len(candidates) == 1:
            if not dry_run:
                _request("PATCH",
                         f"agent_predictions?id=eq.{p['id']}",
                         body={"run_id": candidates[0]["id"]})
            matched += 1
        elif len(candidates) > 1:
            ambiguous += 1
        else:
            orphaned += 1

    return {
        "ok": True,
        "agent_type": agent_type,
        "dry_run": dry_run,
        "matched": matched,
        "ambiguous": ambiguous,
        "orphaned": orphaned,
        "already_linked": already_linked,
        "total_scanned": len(preds),
    }


def main():
    parser = argparse.ArgumentParser(description="Backfill agent_predictions.run_id")
    parser.add_argument("--agent-type", required=True,
                        help="Only processes agents in CALIBRATION_ENABLED_AGENTS.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report matches without issuing UPDATEs.")
    args = parser.parse_args()
    result = backfill(args.agent_type, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
