#!/usr/bin/env python3
"""KSG Phase 4: knowledge-sync upsert tool.

Reads a JSON draft of memories entries (produced by the knowledge-sync
skill in `supabase_draft_entries` mode), validates each entry against
the spec's vocabularies, and upserts via the existing
`bot.supabase_helper.upsert_memory` API with supersession handling and
an audit log.

Spec: docs/superpowers/specs/2026-05-15-branch-close-knowledge-sync-design.md

Modes:
    validate   — schema check only, no DB writes
    upsert     — write to Supabase (requires Phase 3 schema migration applied)

Safety:
    * Default mode is `validate`. `upsert` requires explicit --commit.
    * Schema-check refuses to run if the live `memories` table is missing
      the Phase 3 columns (entry_type, tags, source_branch, etc.).
    * Every write is logged to /tmp/ksg_audit_<timestamp>.log with the
      JSON payload and the returned row id.
    * Supersession is atomic per pair: new row inserted active, old row
      patched to superseded, both within the same script run; if the
      patch fails, the script flags but does not auto-revert (operator
      review required).

Usage:
    # Validate a draft JSON (no DB calls):
    python3 scripts/knowledge_sync.py validate --from /tmp/ksg_<branch>.json

    # Upsert (requires Phase 3 schema applied):
    python3 scripts/knowledge_sync.py upsert --from /tmp/ksg_<branch>.json --commit
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Project-root import: bot/ contains shared text utilities.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.ksg_text import find_forbidden_assertions  # noqa: E402


# ----------------------------------------------------------------------
# Spec-defined vocabularies (kept in sync with spec §5.2 and §5.3)
# ----------------------------------------------------------------------

VALID_ENTRY_TYPES = {
    "doctrine", "architecture", "workflow", "test_command",
    "bug_lesson", "semantic_fix", "wallet_insight", "strategy_thesis",
    "simulation_candidate", "legacy_retirement", "open_question",
    "research_note", "finding",
}

# Keep this set identical to bot.supabase_helper.VALID_STATUSES and the
# DB CHECK constraint (migrations/supabase_022_memories_status_widen.sql).
VALID_STATUSES = {
    "active", "superseded", "retired",
    "draft", "archived", "rejected", "needs_review",
}

VALID_CONFIDENCES = {"low", "medium", "high", "locked"}

REQUIRED_FIELDS = ("name", "entry_type", "description", "content")

# Phase 3 columns this script depends on. Used to detect whether the
# migration has been applied before attempting any write.
PHASE3_COLUMNS = (
    "entry_type", "tags", "source_branch", "source_pr",
    "source_files", "supersedes_ids", "valid_to_commit",
    "confidence", "reviewed_by", "reviewed_at", "metadata",
)


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------

class ValidationError(Exception):
    """Raised when a draft entry fails schema validation."""


def validate_entry(entry: dict[str, Any], idx: int) -> list[str]:
    """Return a list of validation error messages for one entry. Empty list = valid."""
    errors: list[str] = []
    where = f"entries[{idx}]"

    # Required fields
    for f in REQUIRED_FIELDS:
        if entry.get(f) in (None, ""):
            errors.append(f"{where}: missing required field {f!r}")

    # entry_type vocabulary
    et = entry.get("entry_type")
    if et is not None and et not in VALID_ENTRY_TYPES:
        errors.append(
            f"{where}.entry_type={et!r} not in vocabulary {sorted(VALID_ENTRY_TYPES)}"
        )

    # status vocabulary
    status = entry.get("status", "active")
    if status not in VALID_STATUSES:
        errors.append(
            f"{where}.status={status!r} not in vocabulary {sorted(VALID_STATUSES)}"
        )

    # confidence vocabulary
    conf = entry.get("confidence")
    if conf is not None and conf not in VALID_CONFIDENCES:
        errors.append(
            f"{where}.confidence={conf!r} not in vocabulary {sorted(VALID_CONFIDENCES)}"
        )

    # name slug shape
    name = entry.get("name") or ""
    if name and (" " in name or name != name.lower()):
        errors.append(
            f"{where}.name={name!r} should be lowercase-kebab/slash slug (no spaces, lowercase only)"
        )

    # No-orphan invariant: active rows cannot carry superseded_by
    if status == "active" and entry.get("superseded_by"):
        errors.append(
            f"{where}: status='active' but superseded_by is set "
            f"(active-supersession invariant violated)"
        )
    if status == "superseded" and not entry.get("superseded_by"):
        errors.append(
            f"{where}: status='superseded' but superseded_by is not set "
            f"(orphan supersession)"
        )

    # Tags must be a list of non-empty strings
    tags = entry.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            errors.append(f"{where}.tags must be a list of strings")
        elif any(not t.strip() for t in tags):
            errors.append(f"{where}.tags contains empty-string entries (use null or omit, don't pass empty strings)")

    # source_pr must be positive int or null (PR numbers are 1-indexed positive)
    spr = entry.get("source_pr")
    if spr is not None:
        if not isinstance(spr, int) or isinstance(spr, bool):
            errors.append(f"{where}.source_pr must be an integer or null")
        elif spr <= 0:
            errors.append(f"{where}.source_pr={spr} must be positive (GitHub PR numbers start at 1)")

    # source_files must be a list of strings if provided
    sf = entry.get("source_files")
    if sf is not None and not (isinstance(sf, list) and all(isinstance(s, str) for s in sf)):
        errors.append(f"{where}.source_files must be a list of strings")

    # Forbidden framing — KSG inherits the wallet-analyzer doctrine.
    # Scans content + summary + description. Uses bot.ksg_text:
    #   * strips fenced code blocks (a phrase inside ```...``` is an
    #     example, not an assertion)
    #   * checks token-window negation context ("no copy this wallet",
    #     "must not copy this wallet" → not flagged)
    # See bot/ksg_text.py for the algorithms and CommonMark notes.
    forbidden = (
        "copy this wallet",
        "recommended trigger",
        "expected pnl",
        "buy signal",
        "sell signal",
        "deployable trigger",
    )
    scan_text = "\n".join((
        entry.get("content") or "",
        entry.get("summary") or "",
        entry.get("description") or "",
    ))
    hits = find_forbidden_assertions(scan_text, forbidden)
    for hit in hits:
        errors.append(
            f"{where}: forbidden framing {hit['phrase']!r} asserted in entry "
            f"(context: {hit['context']!r})"
        )

    return errors


def validate_draft(draft: dict[str, Any]) -> list[str]:
    """Validate the whole draft. Returns aggregated error list."""
    errors: list[str] = []

    entries = draft.get("entries") or []
    supersessions = draft.get("supersessions") or []

    if not isinstance(entries, list):
        errors.append("draft.entries must be a list")
        return errors
    if not isinstance(supersessions, list):
        errors.append("draft.supersessions must be a list")

    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            errors.append(f"entries[{i}] is not an object")
            continue
        errors.extend(validate_entry(e, i))

    # Validate supersessions
    old_slugs_seen: set[str] = set()
    for i, s in enumerate(supersessions):
        if not isinstance(s, dict):
            errors.append(f"supersessions[{i}] is not an object")
            continue
        for f in ("old_slug", "new_slug"):
            if not s.get(f):
                errors.append(f"supersessions[{i}]: missing {f!r}")
        old_s = s.get("old_slug")
        new_s = s.get("new_slug")
        if old_s and new_s and old_s == new_s:
            errors.append(
                f"supersessions[{i}]: self-supersession (old_slug == new_slug == {old_s!r}) is invalid"
            )
        if old_s:
            if old_s in old_slugs_seen:
                errors.append(
                    f"supersessions[{i}]: old_slug {old_s!r} appears in multiple supersessions "
                    f"(one old row cannot be superseded by multiple new rows)"
                )
            old_slugs_seen.add(old_s)

    # Chained-supersession detection: if a name appears both as a new entry
    # AND as an old_slug in supersessions within the same draft, the entry's
    # active state is inconsistent. This is rejected — operator must split
    # into separate runs (write the new active row first; then in a later
    # batch retire it).
    new_entry_names = {e.get("name") for e in entries if isinstance(e, dict) and e.get("name") and (e.get("status") or "active") == "active"}
    for s in supersessions:
        if isinstance(s, dict):
            old_s = s.get("old_slug")
            if old_s and old_s in new_entry_names:
                errors.append(
                    f"draft: chained supersession — {old_s!r} appears both as a new active "
                    f"entry AND as the old side of a supersession; split into two runs"
                )

    # Duplicate-name detection within the same draft
    names = [e.get("name") for e in entries if isinstance(e, dict) and e.get("name")]
    if len(names) != len(set(names)):
        seen: set[str] = set()
        for n in names:
            if n in seen:
                errors.append(f"draft: duplicate name {n!r} in entries")
            seen.add(n)

    return errors


# ----------------------------------------------------------------------
# Schema-applied probe
# ----------------------------------------------------------------------

def _phase3_schema_applied() -> tuple[bool, str]:
    """Return (applied, message). Reads the live memories table columns."""
    try:
        from bot.supabase_helper import _request  # type: ignore[attr-defined]
    except Exception as exc:
        return False, f"cannot import bot.supabase_helper: {exc}"

    try:
        # PostgREST: SELECT one row, request all columns; missing columns
        # would cause an exception on a real read.
        resp = _request(
            "GET",
            "memories?select=entry_type,tags,source_branch,source_pr,confidence,metadata&limit=1",
        )
    except Exception as exc:
        msg = str(exc)
        # PGRST204 / column does not exist style errors mean Phase 3 not applied.
        if "column" in msg.lower() or "PGRST" in msg:
            return False, f"Phase 3 schema not applied: {msg}"
        return False, f"schema probe failed: {msg}"
    return True, "Phase 3 schema present"


# ----------------------------------------------------------------------
# Upsert orchestration
# ----------------------------------------------------------------------

def _audit_log_path() -> Path:
    return Path(f"/tmp/ksg_audit_{int(time.time())}.log")


def _audit_write(path: Path, event: str, payload: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps({"event": event, **payload}, default=str) + "\n")


def upsert_draft(draft: dict[str, Any], *, commit: bool) -> dict[str, Any]:
    """Validate and upsert a draft. Returns a summary report.

    If commit is False, only validation runs (no DB writes).
    If commit is True, writes via bot.supabase_helper.upsert_memory and
    issues supersession PATCHes for each old_slug.
    """
    errors = validate_draft(draft)
    if errors:
        return {"ok": False, "phase": "validate", "errors": errors}

    if not commit:
        return {
            "ok": True,
            "phase": "validate",
            "n_entries": len(draft.get("entries") or []),
            "n_supersessions": len(draft.get("supersessions") or []),
            "message": "validation passed; no writes performed",
        }

    # Pre-write schema check
    applied, msg = _phase3_schema_applied()
    if not applied:
        return {
            "ok": False,
            "phase": "schema_probe",
            "error": f"refusing to write: {msg}",
            "remediation": "Apply migrations/supabase_020_memories_ksg_extension.sql via Supabase Dashboard first.",
        }

    from bot.supabase_helper import upsert_ksg_entry, get_memory  # type: ignore

    audit = _audit_log_path()
    written: list[dict[str, Any]] = []
    errors_out: list[str] = []

    # Entries first. Route through `upsert_ksg_entry` (not the legacy
    # `upsert_memory`) so the Phase 3 metadata columns the KSG spec
    # mandates — `entry_type`, `tags`, `source_branch`, `source_pr`,
    # `source_files`, `supersedes_ids`, `valid_to_commit`, `confidence`,
    # `reviewed_by`, `reviewed_at`, `metadata` — actually land in
    # Supabase. The legacy entrypoint silently dropped them (Codex
    # deep-review finding #4).
    for e in draft.get("entries") or []:
        try:
            row = upsert_ksg_entry(
                type=e.get("type") or "knowledge",
                name=e["name"],
                description=e["description"],
                content=e["content"],
                source=e.get("source") or "local",
                status=e.get("status") or "active",
                superseded_by=e.get("superseded_by"),
                valid_as_of_commit=e.get("valid_as_of_commit"),
                # KSG Phase 3 columns — preserved end-to-end.
                slug=e.get("slug"),
                entry_type=e.get("entry_type"),
                summary=e.get("summary"),
                tags=e.get("tags"),
                source_branch=e.get("source_branch"),
                source_pr=e.get("source_pr"),
                source_files=e.get("source_files"),
                supersedes_ids=e.get("supersedes_ids"),
                valid_to_commit=e.get("valid_to_commit"),
                confidence=e.get("confidence"),
                reviewed_by=e.get("reviewed_by"),
                reviewed_at=e.get("reviewed_at"),
                metadata=e.get("metadata"),
            )
            written.append({"name": e["name"], "id": row.get("id")})
            _audit_write(audit, "upsert", {"entry": e, "row": row})
        except Exception as exc:
            errors_out.append(f"upsert {e.get('name')!r} failed: {exc}")
            _audit_write(audit, "upsert_failed", {"entry": e, "error": str(exc)})

    # Then supersessions (after new entries exist)
    for s in draft.get("supersessions") or []:
        try:
            existing = get_memory(s["old_slug"], active_only=False)  # type: ignore
            if not existing:
                errors_out.append(f"supersede: old_slug {s['old_slug']!r} not found")
                continue
            new_row = get_memory(s["new_slug"], active_only=False)  # type: ignore
            if not new_row:
                errors_out.append(f"supersede: new_slug {s['new_slug']!r} not found (was it upserted?)")
                continue
            # Patch the old row to status=superseded with superseded_by pointing at the new row.
            from bot.supabase_helper import _request  # type: ignore
            patched = _request(
                "PATCH",
                f"memories?id=eq.{existing['id']}",
                body={
                    "status": "superseded",
                    "superseded_by": new_row.get("name"),
                    "valid_to_commit": s.get("valid_to_commit"),
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                extra_headers={"Prefer": "return=representation"},
            )
            _audit_write(audit, "supersede", {"supersession": s, "patched": patched})
        except Exception as exc:
            errors_out.append(f"supersede {s.get('old_slug')!r} → {s.get('new_slug')!r} failed: {exc}")
            _audit_write(audit, "supersede_failed", {"supersession": s, "error": str(exc)})

    return {
        "ok": len(errors_out) == 0,
        "phase": "upsert",
        "n_written": len(written),
        "n_supersessions": len(draft.get("supersessions") or []),
        "errors": errors_out,
        "audit_log": str(audit),
        "written": written,
    }


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="KSG knowledge-sync upsert tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="Validate a draft JSON (no DB writes).")
    v.add_argument("--from", dest="src", required=True, help="Path to draft JSON")

    u = sub.add_parser("upsert", help="Upsert entries + supersessions (writes to DB).")
    u.add_argument("--from", dest="src", required=True, help="Path to draft JSON")
    u.add_argument("--commit", action="store_true", help="Required: explicit commit flag")

    args = p.parse_args(argv)

    src = Path(args.src)
    if not src.exists():
        print(json.dumps({"ok": False, "error": f"draft file not found: {src}"}, indent=2))
        return 2
    try:
        draft = json.loads(src.read_text())
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON: {exc}"}, indent=2))
        return 2

    if args.cmd == "validate":
        result = upsert_draft(draft, commit=False)
    elif args.cmd == "upsert":
        if not args.commit:
            print(json.dumps({
                "ok": False,
                "error": "upsert requires --commit (explicit operator confirmation)",
            }, indent=2))
            return 2
        result = upsert_draft(draft, commit=True)
    else:  # pragma: no cover
        result = {"ok": False, "error": f"unknown command {args.cmd!r}"}

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
