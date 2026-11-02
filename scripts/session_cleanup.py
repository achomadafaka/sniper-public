#!/usr/bin/env python3
"""Session cleanup — checkpoint old sessions to Supabase then delete.

Runs daily via cron. For each session file older than MAX_AGE_DAYS:
1. Extract a summary (first/last prompts, total messages, tool calls)
2. Write a brief session log to Supabase
3. Delete the JSONL file

This ensures no session work is lost before cleanup.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_AGE_DAYS = int(os.getenv("SESSION_CLEANUP_MAX_AGE_DAYS", "3"))
SESSIONS_DIR = Path.home() / ".claude" / "projects"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env():
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


def _summarize_session(path: Path) -> str:
    """Extract a brief summary from a JSONL session file."""
    lines = []
    try:
        with open(path) as f:
            lines = f.readlines()
    except Exception:
        return ""

    if not lines:
        return ""

    total = len(lines)
    prompts = []
    tool_names = set()
    for line in lines:
        try:
            obj = json.loads(line)
            msg_type = obj.get("type", "")
            if msg_type == "human" and obj.get("message"):
                text = str(obj["message"])[:100]
                if text and text not in ("", "/reset", "/start"):
                    prompts.append(text)
            elif msg_type == "tool_use":
                tool_names.add(obj.get("name", "unknown"))
        except Exception:
            continue

    if not prompts:
        return ""

    first_prompt = prompts[0] if prompts else "?"
    last_prompt = prompts[-1] if len(prompts) > 1 else ""
    tools_str = ", ".join(sorted(tool_names)[:10]) if tool_names else "none"

    summary = (
        f"Archived session ({total} messages, {len(prompts)} prompts, tools: {tools_str}). "
        f"First: {first_prompt}"
    )
    if last_prompt and last_prompt != first_prompt:
        summary += f" Last: {last_prompt}"

    return summary[:500]


def main():
    _load_env()

    cutoff = time.time() - (MAX_AGE_DAYS * 86400)
    old_files = []

    if not SESSIONS_DIR.exists():
        return

    for f in SESSIONS_DIR.rglob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            old_files.append(f)

    if not old_files:
        print(f"No sessions older than {MAX_AGE_DAYS} days")
        return

    # Try to checkpoint to Supabase
    sb_available = False
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "bot"))
        import supabase_helper as sb
        sb_available = bool(os.getenv("SUPABASE_URL") and (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")))
    except Exception:
        pass

    cleaned = 0
    logged = 0
    for f in old_files:
        summary = _summarize_session(f)

        if sb_available and summary:
            try:
                sb.write_session_log(
                    summary=summary,
                    source="session-cleanup",
                    files_changed=str(f.name),
                )
                logged += 1
            except Exception:
                pass  # still delete even if Supabase fails

        try:
            f.unlink()
            cleaned += 1
        except Exception:
            pass

    print(f"Cleaned {cleaned} sessions (>{ MAX_AGE_DAYS}d), logged {logged} to Supabase")


if __name__ == "__main__":
    main()
