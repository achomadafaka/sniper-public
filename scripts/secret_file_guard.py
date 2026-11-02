#!/usr/bin/env python3
"""PreToolUse hook: block Edit/Write on sensitive files (path-based).

Reads TOOL_INPUT from env var (with stdin fallback), extracts file_path,
checks against blocked path patterns. Exit 0 = allow, exit 1 = block.

Known limitation: blocks Edit/Write tool usage only. Does not stop
Bash-based file mutation (sed, python -c, echo >>).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import PurePosixPath


def _is_blocked(file_path: str) -> tuple[bool, str]:
    """Check if a file path matches any blocked pattern.

    Returns (blocked: bool, rule_description: str).
    """
    # Normalize: resolve relative paths
    path = PurePosixPath(file_path)
    name = path.name
    parts = path.parts

    # Rule 1: .env exact or .env.* (except .env.example, .env.sample)
    if name == ".env":
        return True, ".env files"
    if name.startswith(".env."):
        if name in (".env.example", ".env.sample"):
            return False, ""
        return True, f".env variant files ({name})"

    # Rule 2: *.pem, *.key
    if name.endswith(".pem"):
        return True, "*.pem private key / certificate files"
    if name.endswith(".key"):
        return True, "*.key private key files"

    # Rule 3: anything under secrets/ or credentials/ at any depth
    for part in parts:
        if part in ("secrets", "credentials"):
            return True, f"files under {part}/ directory"

    return False, ""


def main() -> int:
    # Read input: TOOL_INPUT env var first, stdin fallback
    raw = os.environ.get("TOOL_INPUT", "")
    if not raw:
        try:
            raw = sys.stdin.read()
        except Exception:
            pass

    if not raw or not raw.strip():
        return 0  # no input = not our concern

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return 0  # unparseable = not our concern

    file_path = payload.get("file_path", "")
    if not file_path:
        return 0  # no file_path in payload = not a file edit

    blocked, rule = _is_blocked(str(file_path))
    if blocked:
        msg = (
            f"BLOCKED: Direct edits to sensitive files are disabled.\n"
            f"Matched rule: {rule}\n"
            f"If you need to update this file, ask the user to edit it "
            f"directly or use .env.example as a template."
        )
        print(msg, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
