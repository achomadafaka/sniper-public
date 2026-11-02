#!/usr/bin/env python3
"""Seed Supabase `memories` with reference entries pointing at markdown docs.

Reads YAML frontmatter from each tracked doc (plans, specs, audits, research,
design). Upserts a `reference`-type memory per doc so agents can discover them
via the Supabase skill without grepping the working tree.

Frontmatter shape:

    ---
    memory_name: plan/wallet-intelligence-rearchitecture
    memory_type: reference   # reference | knowledge | project
    memory_description: one-line tl;dr for agent discovery
    memory_tags: [wallet, architecture, discovery]
    ---

Docs without a `memory_name` key are skipped.

Usage:
    python3 scripts/seed_doc_brain_entries.py           # seed everything
    python3 scripts/seed_doc_brain_entries.py --dry-run # show what would change
    python3 scripts/seed_doc_brain_entries.py path1 ... # seed specific paths
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.supabase_helper import upsert_memory  # noqa: E402

VALID_TYPES = {"reference", "knowledge", "project"}
DEFAULT_SCAN_DIRS = [
    ROOT / "docs" / "superpowers" / "plans",
    ROOT / "docs" / "superpowers" / "specs",
    ROOT / "docs" / "audits",
    ROOT / "docs" / "research",
    ROOT / "docs" / "design",
]

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict | None:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    block = match.group(1)
    out: dict = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            out[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            out[key] = value.strip().strip("'\"")
    return out


def build_content(path: Path, frontmatter: dict, body: str) -> str:
    """Compose the memory content. Keep it short; body-of-doc is on disk.

    Format: pointer line → tldr → tags → first ~500 chars of body for skim.
    """
    rel = path.relative_to(ROOT)
    desc = frontmatter.get("memory_description") or "(no description)"
    tags = frontmatter.get("memory_tags") or []
    tags_line = f"**Tags:** {', '.join(tags)}" if tags else ""
    snippet = body[:500].rstrip()
    return (
        f"**Doc path:** `{rel}`\n\n"
        f"**TL;DR:** {desc}\n\n"
        f"{tags_line}\n\n"
        f"---\n\n"
        f"_First ~500 chars of body:_\n\n"
        f"{snippet}\n\n"
        f"(Read the full doc at `{rel}` for details.)"
    )


def discover_paths(extra_paths: list[Path]) -> list[Path]:
    if extra_paths:
        return [Path(p).resolve() for p in extra_paths]
    out: list[Path] = []
    for d in DEFAULT_SCAN_DIRS:
        if not d.exists():
            continue
        out.extend(sorted(d.rglob("*.md")))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="*", help="Specific docs to seed (default: scan dirs)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    paths = discover_paths([Path(x) for x in args.paths])
    print(f"Scanning {len(paths)} candidate docs...")

    seeded = 0
    skipped = 0
    errors = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"  SKIP {path}: {exc}")
            skipped += 1
            continue

        fm = parse_frontmatter(text)
        if not fm or "memory_name" not in fm:
            skipped += 1
            continue

        mtype = fm.get("memory_type", "reference")
        if mtype not in VALID_TYPES:
            print(f"  SKIP {path}: invalid memory_type={mtype!r}")
            skipped += 1
            continue

        name = fm["memory_name"]
        desc = fm.get("memory_description", "")[:200]
        body = text[fm.get("_frontmatter_end", 0):] if "_frontmatter_end" in fm else _FRONTMATTER_RE.sub("", text, count=1)
        content = build_content(path, fm, body)
        source = "local" if os.uname().nodename != "sniper-vps" else "vps"

        if args.dry_run:
            print(f"  DRY-RUN {mtype}/{name} ← {path.relative_to(ROOT)}")
            seeded += 1
            continue

        try:
            upsert_memory(type=mtype, name=name, description=desc, content=content, source=source)
            print(f"  OK    {mtype}/{name}")
            seeded += 1
        except Exception as exc:
            print(f"  ERROR {path}: {exc}")
            errors += 1

    print()
    print(f"Seeded: {seeded}  Skipped: {skipped}  Errors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
