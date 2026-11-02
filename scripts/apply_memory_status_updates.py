#!/usr/bin/env python3
"""Apply human-approved memory lifecycle updates.

Input is the JSON report from agents/brain_hygiene_agent.py. Only memory
proposals are applied, and only with --apply; the default is a dry run.

THE IDENTIFIER CONTRACT (undocumented until 2026-09-20, and the exact gap that
let a zero-row apply report success). Each proposal's `identifier` is the
string "<type>:<name>", split on the FIRST colon. <type> is the memories
table's own `type` COLUMN -- one of user / feedback / project / reference /
knowledge (bot.supabase_helper.VALID_TYPES) -- never the leading word of a
path-shaped `name`. <name> is everything after that first colon, slashes
included.

WORKED EXAMPLE. A memory saved as type="project", name=
"session/finding-voldeath-exit-is-roughly-neutral-2026-09-18" is addressed as:

    project:session/finding-voldeath-exit-is-roughly-neutral-2026-09-18

NOT "session:finding-voldeath-exit-is-roughly-neutral-2026-09-18" -- "session"
is not a memory type, so that identifier's PATCH filters on a type column
value that no row has. It matches zero rows, changes nothing, and (before
this fix) still printed a success line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _memory_parts(identifier: str) -> tuple[str, str]:
    """(type, name) for a memory identifier, or raise if <type> is not a real type column value.

    See the module docstring's IDENTIFIER CONTRACT for the worked example this guards against.
    """
    if ":" not in identifier:
        raise ValueError(f"memory identifier must be '<type>:<name>', got {identifier!r}")
    memory_type, name = identifier.split(":", 1)
    from bot.supabase_helper import VALID_TYPES
    if memory_type not in VALID_TYPES:
        raise ValueError(
            f"{memory_type!r} is not a memory type ({sorted(VALID_TYPES)}). The identifier is "
            f"'<type>:<name>' where <type> is the memories.type COLUMN, not the leading word of "
            f"a path-shaped name -- got {identifier!r}"
        )
    return memory_type, name


def _memory_filter_path(memory_type: str, name: str) -> str:
    return (
        "memories"
        f"?type=eq.{urllib.parse.quote(memory_type)}"
        f"&name=eq.{urllib.parse.quote(name)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply approved memory status updates")
    parser.add_argument("report", type=Path, help="JSON report from brain_hygiene_agent.py")
    parser.add_argument("--apply", action="store_true", help="Actually PATCH Supabase. Omit for dry run.")
    parser.add_argument(
        "--only",
        choices=("superseded", "retired", "all"),
        default="all",
        help="Restrict which proposed statuses are applied",
    )
    args = parser.parse_args()

    report = json.loads(args.report.read_text())
    selected = [
        p for p in report.get("proposals", [])
        if p.get("target_kind") == "memory"
        and p.get("proposed_status") in {"superseded", "retired"}
        and (args.only == "all" or p.get("proposed_status") == args.only)
    ]
    proposals_by_identifier = {}
    priority = {"superseded": 1, "retired": 2}
    for proposal in selected:
        current = proposals_by_identifier.get(proposal["identifier"])
        if current is None or priority[proposal["proposed_status"]] > priority[current["proposed_status"]]:
            proposals_by_identifier[proposal["identifier"]] = proposal
    proposals = list(proposals_by_identifier.values())
    if not proposals:
        print("No memory proposals selected.")
        return 0

    _load_env()
    from bot.supabase_helper import _request

    print(f"{'Applying' if args.apply else 'Dry run'} {len(proposals)} memory status update(s)")
    problems = 0
    for proposal in proposals:
        identifier = proposal["identifier"]
        try:
            memory_type, name = _memory_parts(identifier)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            problems += 1
            if args.apply:
                print("Stopping: an unresolved identifier must not be followed by more PATCHes "
                      "in the same run.")
                return 1
            continue

        path = _memory_filter_path(memory_type, name)
        payload = {
            "status": proposal["proposed_status"],
            "superseded_by": proposal.get("superseded_by"),
        }

        if args.apply:
            rows = _request(
                "PATCH", path, body=payload, extra_headers={"Prefer": "return=representation"},
            ) or []
            if len(rows) != 1:
                # A zero-row PATCH looks IDENTICAL to a successful one on the wire (200, valid
                # JSON) unless the representation is actually read -- the exact defect this fixes.
                # More than one row is equally wrong: a status change must never fan out.
                print(
                    f"ERROR: {identifier} matched {len(rows)} row(s), expected exactly 1 "
                    f"(type={memory_type!r}, name={name!r}). Nothing else in this run was applied "
                    f"after this."
                )
                return 1
            print(f"- {identifier} -> {payload['status']} (1 row updated)")
        else:
            # THE SAME LOOKUP A PATCH WOULD MAKE, read-only. A dry run that cannot tell you the
            # identifier is wrong is how this defect survived: it printed the same "Dry run N
            # update(s)" line whether the identifiers were real or not.
            rows = _request("GET", path + "&select=id") or []
            if len(rows) == 1:
                print(f"- {identifier} -> {payload['status']} (matches 1 row)")
            else:
                print(f"- {identifier} -> {payload['status']} "
                      f"[WOULD FAIL: matches {len(rows)} row(s), expected exactly 1]")
                problems += 1

    if not args.apply:
        if problems:
            print(f"{problems} identifier problem(s) found above. Fix them before --apply.")
            return 1
        print("No changes applied. Re-run with --apply after review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
