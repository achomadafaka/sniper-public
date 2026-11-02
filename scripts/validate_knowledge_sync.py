#!/usr/bin/env python3
"""KSG Phase 5: knowledge-sync advisory validator.

Six checks per spec §7. All checks are ADVISORY in this phase — the script
exits with a meaningful code but does not block CI as `fail`. Phase 5+
ratchets to enforcement once adoption is stable.

Checks:
    1. Local files exist (AGENTS.md, CLAUDE.md, MEMORY.md).
    2. Forbidden-current phrases absent from AGENTS.md / CLAUDE.md.
    3. Active-supersession invariant in memories table (DB check; skipped
       in CI environments without Supabase access).
    4. Provenance completeness — every entry created after KSG_CUTOFF has
       source_branch or source_pr set (DB check; skipped without access).
    5. PR body has Knowledge Sync block (skipped outside a PR context).
    6. Schema validator still passes (delegates to validate_schema_refs.py).

Run modes:
    --mode local      Run checks 1, 2, 6 only (no DB or PR context required)
    --mode full       Run all 6 (needs Supabase env + PR context)
    --mode ci         Run 1, 2, 5, 6 (PR body fetched from GITHUB_EVENT_PATH)

Exit codes:
    0  — all checks passed
    1  — advisory issues found (logged, but does not fail CI in this phase)
    2  — usage error
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Project-root import: bot/ contains shared text utilities used by both
# KSG validators.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.ksg_text import strip_fenced_code, find_forbidden_assertions  # noqa: E402


# ----------------------------------------------------------------------
# Phrases that must NOT appear as current truth in operating files.
# Match case-insensitively. Each phrase represents a doctrine that was
# explicitly retired; presence as current truth = doctrine drift.
# ----------------------------------------------------------------------

FORBIDDEN_CURRENT_PHRASES = (
    "wallet analyzer is a copy bot",
    "copy this wallet",
    "recommended trigger",
    "expected pnl",
    "insider_score is source of truth",
    "low_buy_trades is the primary stream",
)

REQUIRED_LOCAL_FILES = ("AGENTS.md", "CLAUDE.md", "MEMORY.md")

# KSG cutoff — entries created after this commit's date are expected to
# carry source_branch or source_pr provenance. Earlier entries are
# grandfathered.
KSG_CUTOFF_DATE = "2026-05-15"


# ----------------------------------------------------------------------
# Check 1: local files exist
# ----------------------------------------------------------------------

def check_local_files_exist() -> dict[str, Any]:
    missing: list[str] = []
    for f in REQUIRED_LOCAL_FILES:
        if not Path(f).is_file():
            missing.append(f)
    return {
        "check": "local_files_exist",
        "ok": not missing,
        "missing": missing,
    }


# ----------------------------------------------------------------------
# Check 2: forbidden phrases absent from operating files
# ----------------------------------------------------------------------

def check_no_forbidden_phrases() -> dict[str, Any]:
    """Scan AGENTS.md / CLAUDE.md for forbidden current-truth phrases.

    Uses bot.ksg_text.find_forbidden_assertions for the core scan, which:
      * strips fenced code blocks (a phrase in ```...``` is doctrine-example,
        not a current-truth assertion);
      * applies token-window negation detection ("must not copy this wallet"
        does not flag).

    Adds an additional file-specific heuristic: lines starting with list
    markers (`-`, `*`) or comment markers (`#`, `//`) inside the "forbidden
    phrases" sections of these very files are clearly meta-references and
    not assertions. The negation detector catches most of these via the
    "forbidden" / "must not" tokens, but the list-marker check is belt-and-
    braces for AGENTS.md / CLAUDE.md's own scope-guard sections.
    """
    findings: list[dict[str, Any]] = []
    for f in ("AGENTS.md", "CLAUDE.md"):
        p = Path(f)
        if not p.is_file():
            continue
        raw = p.read_text(errors="replace")
        # Primary scan: code-fence-aware, negation-aware
        hits = find_forbidden_assertions(raw, FORBIDDEN_CURRENT_PHRASES)
        for hit in hits:
            # Compute line number from position
            line_no = raw[: hit["position"]].count("\n") + 1
            # Belt-and-braces: skip if the matched line is itself a meta
            # reference (a bulleted/numbered entry in a forbidden-phrases
            # listing). find_forbidden_assertions already accounted for
            # negation context, but operating files have unusually dense
            # discussions of the forbidden phrases.
            line_text = raw.splitlines()[line_no - 1] if line_no <= len(raw.splitlines()) else ""
            line_stripped = line_text.strip().lower()
            if any(line_stripped.startswith(prefix) for prefix in ("- ", "* ", "// ", "<!--")):
                continue
            findings.append({
                "file": f,
                "line": line_no,
                "phrase": hit["phrase"],
                "context": hit["context"][:120],
            })
    return {
        "check": "no_forbidden_phrases",
        "ok": not findings,
        "findings": findings,
    }


# ----------------------------------------------------------------------
# Check 3: active-supersession invariant (DB check)
# ----------------------------------------------------------------------

def check_active_supersession_invariant() -> dict[str, Any]:
    try:
        from bot.supabase_helper import _request  # type: ignore[attr-defined]
    except Exception as exc:
        return {"check": "active_supersession_invariant", "ok": True, "skipped": f"no supabase access: {exc}"}

    issues: list[dict[str, Any]] = []
    try:
        # Active rows with non-null superseded_by — invariant violation
        rows = _request(
            "GET",
            "memories?select=id,name,status,superseded_by"
            "&status=eq.active"
            "&superseded_by=not.is.null"
            "&limit=200",
        )
        for r in rows or []:
            issues.append({"violation": "active_with_superseded_by", "row": r})
    except Exception as exc:
        return {"check": "active_supersession_invariant", "ok": False, "error": str(exc)}

    try:
        # Superseded rows with null superseded_by — orphan supersession
        rows2 = _request(
            "GET",
            "memories?select=id,name,status,superseded_by"
            "&status=eq.superseded"
            "&superseded_by=is.null"
            "&limit=200",
        )
        for r in rows2 or []:
            issues.append({"violation": "superseded_without_target", "row": r})
    except Exception as exc:
        return {"check": "active_supersession_invariant", "ok": False, "error": str(exc)}

    return {
        "check": "active_supersession_invariant",
        "ok": not issues,
        "issues": issues,
    }


# ----------------------------------------------------------------------
# Check 4: provenance completeness (DB check)
# ----------------------------------------------------------------------

def check_provenance_completeness() -> dict[str, Any]:
    try:
        from bot.supabase_helper import _request  # type: ignore[attr-defined]
    except Exception as exc:
        return {"check": "provenance_completeness", "ok": True, "skipped": f"no supabase access: {exc}"}

    # Probe whether Phase 3 schema is applied; if not, skip.
    try:
        _ = _request("GET", "memories?select=source_branch,source_pr&limit=1")
    except Exception as exc:
        return {
            "check": "provenance_completeness",
            "ok": True,
            "skipped": f"Phase 3 schema not yet applied ({exc})",
        }

    missing_provenance: list[dict[str, Any]] = []
    try:
        rows = _request(
            "GET",
            f"memories?select=id,name,created_at,source_branch,source_pr"
            f"&status=eq.active"
            f"&created_at=gte.{KSG_CUTOFF_DATE}"
            f"&source_branch=is.null"
            f"&source_pr=is.null"
            f"&limit=200",
        )
        for r in rows or []:
            missing_provenance.append(r)
    except Exception as exc:
        return {"check": "provenance_completeness", "ok": False, "error": str(exc)}

    return {
        "check": "provenance_completeness",
        "ok": not missing_provenance,
        "missing_provenance": missing_provenance,
    }


# ----------------------------------------------------------------------
# Check 5: PR body has Knowledge Sync block
# ----------------------------------------------------------------------

KSG_BLOCK_MARKER = re.compile(r"##\s+Knowledge\s+Sync\b", re.IGNORECASE)
KSG_ATTESTATION = re.compile(r"\*\*Delta:\*\*", re.IGNORECASE)
# Detects literal "[y/n]" placeholders that weren't filled in.
KSG_UNFILLED_PLACEHOLDER = re.compile(r"\[y/n\]", re.IGNORECASE)
# Detects the literal "Delta: yes | none" placeholder (operator did not pick one).
KSG_UNFILLED_DELTA = re.compile(r"\*\*Delta:\*\*\s*yes\s*\|\s*none\s*$", re.IGNORECASE | re.MULTILINE)


def _read_pr_body() -> tuple[str | None, str | None]:
    """Read PR body from GITHUB_EVENT_PATH if present. Returns (body, source)."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        try:
            event = json.loads(Path(event_path).read_text())
            pr = event.get("pull_request")
            if pr and pr.get("body"):
                return pr["body"], "GITHUB_EVENT_PATH"
        except Exception:
            pass
    # Fallback for local dev: PR_BODY env var.
    if os.environ.get("PR_BODY"):
        return os.environ["PR_BODY"], "PR_BODY env"
    return None, None


def check_pr_body_has_ksg_block() -> dict[str, Any]:
    body, src = _read_pr_body()
    if body is None:
        return {"check": "pr_body_has_ksg_block", "ok": True, "skipped": "no PR context available"}

    # Strip fenced code blocks before scanning. A KSG block inside a
    # markdown fence (e.g. someone quoting the template in a code block
    # for documentation purposes) is NOT a real attestation.
    body_no_fences = strip_fenced_code(body)

    has_marker = bool(KSG_BLOCK_MARKER.search(body_no_fences))
    has_attestation = bool(KSG_ATTESTATION.search(body_no_fences))

    # Catch the "operator pasted the template but didn't fill it in" case.
    # Count placeholders in the un-fenced text (so a sample template
    # inside a code block doesn't inflate the count).
    unfilled_placeholders = len(KSG_UNFILLED_PLACEHOLDER.findall(body_no_fences))
    unfilled_delta = bool(KSG_UNFILLED_DELTA.search(body_no_fences))
    template_untouched = unfilled_placeholders >= 3 or unfilled_delta

    # Diagnostic: report whether code-fence stripping removed something
    # that would have matched. Helps debug "I added the section but the
    # validator says it's missing" cases.
    fenced_marker_present = bool(KSG_BLOCK_MARKER.search(body)) and not has_marker

    return {
        "check": "pr_body_has_ksg_block",
        "ok": has_marker and has_attestation and not template_untouched,
        "source": src,
        "has_marker": has_marker,
        "has_attestation": has_attestation,
        "unfilled_placeholder_count": unfilled_placeholders,
        "unfilled_delta": unfilled_delta,
        "template_untouched": template_untouched,
        "fenced_marker_present_but_not_counted": fenced_marker_present,
    }


# ----------------------------------------------------------------------
# Check 6: schema validator still passes
# ----------------------------------------------------------------------

def check_schema_validator() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["python3", "scripts/validate_schema_refs.py"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return {"check": "schema_validator", "ok": False, "error": str(exc)}
    return {
        "check": "schema_validator",
        "ok": result.returncode == 0,
        "stdout_tail": result.stdout[-500:] if result.stdout else "",
        "stderr_tail": result.stderr[-500:] if result.stderr else "",
    }


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------

MODE_CHECKS = {
    "local": (
        check_local_files_exist,
        check_no_forbidden_phrases,
        check_schema_validator,
    ),
    "ci": (
        check_local_files_exist,
        check_no_forbidden_phrases,
        check_pr_body_has_ksg_block,
        check_schema_validator,
    ),
    "full": (
        check_local_files_exist,
        check_no_forbidden_phrases,
        check_active_supersession_invariant,
        check_provenance_completeness,
        check_pr_body_has_ksg_block,
        check_schema_validator,
    ),
}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="KSG advisory validator (Phase 5)")
    p.add_argument(
        "--mode",
        choices=sorted(MODE_CHECKS),
        default="local",
        help="Check set to run (default: local)",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress passing checks; only show issues",
    )
    args = p.parse_args(argv)

    checks = MODE_CHECKS[args.mode]
    results = [check() for check in checks]
    any_fail = any(not r.get("ok", False) for r in results if "skipped" not in r)

    out = {"mode": args.mode, "all_ok": not any_fail, "results": results}
    if args.quiet:
        out["results"] = [r for r in results if not r.get("ok", False)]
    print(json.dumps(out, indent=2, default=str))

    return 0 if not any_fail else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
