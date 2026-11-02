#!/usr/bin/env python3
"""Supabase performance + security advisor lint — reads the PAT from env, never hardcoded.

Replaces the inline PAT blob that used to live in CLAUDE.md. The access token is
read from SUPABASE_ACCESS_TOKEN (or .env). Project ref from SUPABASE_PROJECT_REF
(defaults to the Sniper project).

Usage:
    python3 scripts/supabase_advisors.py
Requires in .env (gitignored) or environment:
    SUPABASE_ACCESS_TOKEN=sbp_...
    SUPABASE_PROJECT_REF=nqtebcfwutftgaylmaon   # optional
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

PAT = os.getenv("SUPABASE_ACCESS_TOKEN", "")
PROJECT = os.getenv("SUPABASE_PROJECT_REF", "nqtebcfwutftgaylmaon")

if not PAT:
    sys.exit("ERROR: SUPABASE_ACCESS_TOKEN not set (put it in .env, gitignored).")


def fetch(kind: str):
    r = subprocess.run(
        ["curl", "-s", f"https://api.supabase.com/v1/projects/{PROJECT}/advisors/{kind}",
         "-H", f"Authorization: Bearer {PAT}"],
        capture_output=True, text=True,
    )
    d = json.loads(r.stdout)
    return d["lints"] if isinstance(d, dict) and "lints" in d else d


def main() -> None:
    for kind in ("performance", "security"):
        lints = fetch(kind)
        warns = [x for x in lints if x.get("level") in ("ERROR", "WARN")]
        infos = [x for x in lints if x.get("level") == "INFO"]
        print(f"=== {kind.upper()} — WARN/ERROR: {len(warns)}  INFO: {len(infos)} ===")
        for i in warns:
            print(f"  [{i['level']}] {i['name']} — {i.get('detail', '')[:120]}")
        for i in infos:
            print(f"  [INFO] {i['name']} — {i.get('detail', '')[:100]}")
        if not warns and not infos:
            print("  ✓ CLEAN")


if __name__ == "__main__":
    main()
