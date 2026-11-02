#!/usr/bin/env python3
"""
sync_session_findings.py — Mirror Claude Code session memory into the Supabase brain.

Session work (findings, feedback, project state) lives in the local Claude Code
project memory dir (``~/.claude/projects/<slug>/memory/*.md``) and is invisible
to the shared Supabase brain that other agents/sessions read. This script mirrors
that corpus into the ``memories`` table so it is discoverable — now including via
the full-text ``search_memories_fts()`` RPC (migration 028).

History / why this was rewritten (2026-07-08):
    The previous version hardcoded ``CLAUDE_MEMORY_DIR`` to the retired VPS path
    ``-home-sniper-sniper`` and a frozen 4-file March manifest. When the machine
    moved local (``-home-bigpapi-Desktop-Sniper``) it silently reported every file
    MISSING and wrote nothing — the direct cause of the ~10x collapse in Supabase
    write volume (148/mo in March → ~10/mo by July). It now auto-discovers the
    live memory dir and mirrors the WHOLE corpus, frontmatter-aware.

Mapping:
    - Each ``memory/*.md`` (except the ``MEMORY.md`` index) becomes one memory.
    - ``name``        ← frontmatter ``name`` (else filename stem), namespaced under
                        ``session/`` so mirrored entries never collide with or
                        clobber human-/agent-curated brain entries.
    - ``type``        ← frontmatter ``metadata.type`` (user|feedback|project|
                        reference|knowledge; ``finding`` → ``knowledge``); default
                        ``reference``.
    - ``description`` ← frontmatter ``description`` (else first heading / line).
    - ``content``     ← full file text.
    - ``source``      ← ``local`` (honest provenance).
    Idempotent: unchanged files are skipped (content compare against the brain).

Usage:
    python3 scripts/sync_session_findings.py                 # mirror whole corpus
    python3 scripts/sync_session_findings.py --dry-run       # preview, write nothing
    python3 scripts/sync_session_findings.py --list          # what mirrors already exist
    python3 scripts/sync_session_findings.py --dir PATH      # override memory dir
    python3 scripts/sync_session_findings.py --file p.md --name key   # one arbitrary file

Env override:
    CLAUDE_MEMORY_DIR=/abs/path/to/memory   # skip auto-discovery
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

from bot.supabase_helper import upsert_memory, search_memories, get_memory  # noqa: E402

VALID_TYPES = {"user", "feedback", "project", "reference", "knowledge"}
_TYPE_ALIASES = {"finding": "knowledge", "memory": "reference"}
VALID_STATUSES = {"active", "superseded", "retired", "draft", "archived", "rejected", "needs_review"}
# The index file is not a memory; it is regenerated from the others.
SKIP_FILES = {"MEMORY.md"}
NAMESPACE = "session/"


# ── Memory-dir discovery ──────────────────────────────────────────────────────

def discover_memory_dir(explicit: str | None = None) -> Path | None:
    """Locate the live Claude Code project memory dir.

    Order: explicit arg → CLAUDE_MEMORY_DIR env → cwd/repo-derived slug →
    glob of ~/.claude/projects/*/memory (newest MEMORY.md wins).
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None

    env = os.getenv("CLAUDE_MEMORY_DIR")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p

    projects = Path.home() / ".claude" / "projects"
    # Derive the slug from the repo root ("/home/x/Sniper" → "-home-x-Sniper").
    slug = "-" + str(ROOT).replace("/", "-").lstrip("-")
    derived = projects / slug / "memory"
    if derived.is_dir():
        return derived

    # Fallback: any projects/*/memory, pick the one with the freshest MEMORY.md.
    candidates = sorted(
        (d for d in projects.glob("*/memory") if d.is_dir()),
        key=lambda d: (d / "MEMORY.md").stat().st_mtime if (d / "MEMORY.md").exists() else 0,
        reverse=True,
    )
    return candidates[0] if candidates else None


# ── Frontmatter parsing ───────────────────────────────────────────────────────

def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Empty dict if no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    front, body = m.group(1), m.group(2)
    try:
        import yaml
        data = yaml.safe_load(front) or {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    return data, body


def _first_line_desc(body: str, limit: int = 200) -> str:
    for raw in body.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            return line[:limit]
    return ""


def parse_memory_file(path: Path) -> dict:
    """Read a memory file → {name, type, description, content, status, superseded_by}.

    Keys on the file STEM (always a clean slug) rather than the frontmatter
    ``name`` (which is sometimes prose). Honors frontmatter ``metadata.status`` /
    ``superseded_by`` so retracted/superseded findings are NOT mirrored as active —
    body keywords (NULL/KILLED/CLOSED/REFUTED) are deliberately ignored because
    they usually describe valid negative knowledge, not a retired memory.
    """
    text = path.read_text(encoding="utf-8")
    front, body = _split_frontmatter(text)
    meta = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}

    name = f"{NAMESPACE}{path.stem}"

    raw_type = str((meta.get("type") if meta else "") or front.get("type") or "").strip().lower()
    mtype = _TYPE_ALIASES.get(raw_type, raw_type)
    if mtype not in VALID_TYPES:
        mtype = "reference"

    status = str((meta.get("status") if meta else "") or front.get("status") or "active").strip().lower()
    if status not in VALID_STATUSES:
        status = "active"
    superseded_by = (meta.get("superseded_by") if meta else None) or front.get("superseded_by") or None
    superseded_by = str(superseded_by).strip() if superseded_by else None

    description = str(front.get("description") or "").strip() or _first_line_desc(body)
    if not description:
        description = f"Session memory mirrored from {path.name}"

    return {
        "name": name, "type": mtype, "description": description[:400], "content": text,
        "status": status, "superseded_by": superseded_by,
    }


# ── Sync ──────────────────────────────────────────────────────────────────────

def _age_str(iso_ts: str | None) -> str:
    if not iso_ts:
        return "never"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        return f"{delta.days}d {delta.seconds // 3600}h ago" if delta.days else f"{delta.seconds // 3600}h ago"
    except Exception:
        return iso_ts[:19]


def sync_corpus(memory_dir: Path, dry_run: bool = False) -> None:
    files = sorted(f for f in memory_dir.glob("*.md") if f.name not in SKIP_FILES)
    if not files:
        print(f"No memory files found in {memory_dir}")
        return

    print(f"Mirroring {len(files)} file(s) from {memory_dir}\n")
    written = unchanged = failed = inactive = 0
    for path in files:
        rec = parse_memory_file(path)
        # active_only=False so an already-mirrored superseded entry is still found.
        existing = get_memory(rec["name"], type_filter=rec["type"], active_only=False)
        if existing and existing.get("content") == rec["content"] and existing.get("status") == rec["status"]:
            unchanged += 1
            continue

        st = "" if rec["status"] == "active" else f" ⟨{rec['status']}⟩"
        if rec["status"] != "active":
            inactive += 1
        verb = "NEW" if not existing else f"UPDATE (was {_age_str(str(existing.get('updated_at','')))})"
        tag = "🔍 DRY-RUN" if dry_run else "📤 WRITE"
        print(f"  {tag}  [{rec['type']:9}] {rec['name']}{st}  — {verb}")
        if dry_run:
            written += 1
            continue
        try:
            upsert_memory(
                rec["type"], rec["name"], rec["description"], rec["content"],
                source="local", status=rec["status"], superseded_by=rec["superseded_by"],
            )
            written += 1
        except Exception as e:
            print(f"       ❌ FAILED: {e}")
            failed += 1

    print(
        f"\n{'(dry-run) ' if dry_run else ''}Done: {written} written "
        f"({inactive} non-active), {unchanged} unchanged, {failed} failed"
    )


def sync_file(source_path: str, brain_name: str, brain_type: str = "reference",
              description: str = "", dry_run: bool = False) -> None:
    path = Path(source_path)
    if not path.exists():
        print(f"ERROR: file not found: {source_path}")
        sys.exit(1)
    content = path.read_text(encoding="utf-8")
    name = brain_name if brain_name.startswith(NAMESPACE) else f"{NAMESPACE}{brain_name}"
    description = description or f"Session finding synced from {path.name}"

    existing = get_memory(name, type_filter=brain_type)
    if existing and existing.get("content") == content:
        print(f"✅ UNCHANGED: {name} (last synced {_age_str(str(existing.get('updated_at','')))})")
        return
    print(f"{'🔍 DRY-RUN' if dry_run else '📤 WRITE'} [{brain_type}] {name} ({len(content)} chars)")
    if not dry_run:
        upsert_memory(brain_type, name, description, content, source="local")
        print("✅ written")


def list_synced() -> None:
    mirrors = search_memories(NAMESPACE)
    print(f"{len(mirrors)} mirrored session entr(y/ies) in brain:\n")
    for m in sorted(mirrors, key=lambda x: str(x.get("name", ""))):
        print(f"  [{m.get('type','?'):9}] {m.get('name','?')}  — {_age_str(str(m.get('updated_at','')))}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror Claude Code session memory into the Supabase brain")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written without writing")
    parser.add_argument("--list", action="store_true", help="List mirrored session entries already in the brain")
    parser.add_argument("--dir", metavar="PATH", help="Override the memory dir (skips auto-discovery)")
    parser.add_argument("--file", metavar="PATH", help="Sync a specific file (requires --name)")
    parser.add_argument("--name", metavar="BRAIN_KEY", help="Brain key name for --file mode")
    parser.add_argument("--type", metavar="TYPE", default="reference", choices=sorted(VALID_TYPES),
                        help="Brain type for --file mode (default: reference)")
    parser.add_argument("--description", metavar="DESC", default="", help="Description for --file mode")
    args = parser.parse_args()

    if args.list:
        list_synced()
        return

    if args.file:
        if not args.name:
            parser.error("--file requires --name")
        sync_file(args.file, args.name, args.type, args.description, args.dry_run)
        return

    memory_dir = discover_memory_dir(args.dir)
    if not memory_dir:
        print("ERROR: could not locate a Claude Code memory dir. Pass --dir or set CLAUDE_MEMORY_DIR.")
        sys.exit(1)

    print(f"Syncing session memory to Supabase brain ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")
    sync_corpus(memory_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
