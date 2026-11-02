#!/usr/bin/env python3
"""KSG Phase 7: 90-day PR backfill.

Walks closed PRs from the last N days, applies the 11-question
Knowledge Delta Report heuristics retroactively, and surfaces:

    1. PRs likely to have introduced unrecorded knowledge.
    2. Candidate `memories` entry drafts (as JSON, for operator review).
    3. Candidate supersession pairs (where one PR likely retired an
       earlier doctrine / spec / module).

The output is a REVIEW QUEUE. No DB writes. Operator decides which
candidates become real upserts via the existing scripts/knowledge_sync.py
upsert path.

Usage:
    # Default 90 days, write to /tmp/ksg_backfill_<timestamp>/
    python3 scripts/ksg_backfill_prs.py

    # Custom window
    python3 scripts/ksg_backfill_prs.py --days 30

    # Filter to specific PR numbers
    python3 scripts/ksg_backfill_prs.py --pr 38 41 42 43

Requires `gh` CLI on PATH and an authenticated session.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------
# Heuristic classification — mirrors the SKILL.md table in
# .claude/skills/knowledge-sync/SKILL.md §"Step 2"
# --------------------------------------------------------------------

PATH_HEURISTICS = (
    # (regex on path, list of triggered questions, suggested entry_type)
    (re.compile(r"^AGENTS\.md$"),                                       [1, 10],            "doctrine"),
    (re.compile(r"^CLAUDE\.md$"),                                       [1, 10],            "doctrine"),
    (re.compile(r"^MEMORY\.md$"),                                       [10],               None),
    (re.compile(r"^STATUS\.md$"),                                       [11],               "legacy_retirement"),
    (re.compile(r"^sniper/analysis/wallet_protocol/"),                   [3],                "finding"),
    (re.compile(r"^sniper/analysis/strategy_pattern_audit/"),            [3],                "finding"),
    (re.compile(r"^sniper/analysis/"),                                   [3],                "finding"),
    (re.compile(r"^sniper/"),                                            [2],                "architecture"),
    (re.compile(r"^tests/"),                                             [4],                None),
    (re.compile(r"^scripts/validate_"),                                  [4],                "test_command"),
    (re.compile(r"^scripts/"),                                           [2],                "architecture"),
    (re.compile(r"^\.claude/skills/"),                                   [5],                "workflow"),
    (re.compile(r"^\.github/workflows/"),                                [5],                "workflow"),
    (re.compile(r"^\.github/pull_request_template"),                     [5],                "workflow"),
    (re.compile(r"^migrations/supabase_"),                               [8],                "architecture"),
    (re.compile(r"^bot/supabase_helper\.py$"),                           [8],                "architecture"),
    (re.compile(r"^docs/audits/"),                                       [7],                "bug_lesson"),
    (re.compile(r"^docs/research/wallet"),                               [9],                "wallet_insight"),
    (re.compile(r"^docs/research/"),                                     [9],                "research_note"),
    (re.compile(r"^docs/superpowers/specs/.+\.md$"),                     [2, 5],             "workflow"),
    (re.compile(r"^docs/superpowers/plans/.+\.md$"),                     [2],                "workflow"),
    (re.compile(r"^knowledge/wallets/0x[0-9a-f]+\.md$"),                 [9],                "wallet_insight"),
    (re.compile(r"^knowledge/strategies/"),                              [9],                "strategy_thesis"),
)

COMMIT_MSG_HEURISTICS = (
    (re.compile(r"\bR\d+\b|^fix\(.*\):", re.MULTILINE),                  [7],                "semantic_fix"),
    (re.compile(r"^fix:", re.MULTILINE | re.IGNORECASE),                 [7],                "bug_lesson"),
    (re.compile(r"\bretire\b|\bdeprecat", re.IGNORECASE),                [11],               "legacy_retirement"),
)


# --------------------------------------------------------------------
# GitHub PR queries via gh CLI
# --------------------------------------------------------------------

def _detect_repo_root() -> Path:
    """Walk upward from this file to find the repo root (the dir containing .git)."""
    here = Path(__file__).resolve()
    for ancestor in (here.parent, *here.parents):
        if (ancestor / ".git").exists():
            return ancestor
    raise RuntimeError(f"could not detect repo root from {here}")


_REPO_ROOT = _detect_repo_root()


def _run_gh(args: list[str]) -> dict | list:
    cmd = ["gh", *args]
    try:
        # Always run from repo root so `gh` can locate the GitHub remote
        # context regardless of caller's cwd.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(_REPO_ROOT))
    except FileNotFoundError:
        raise RuntimeError("gh CLI not found on PATH")
    if result.returncode != 0:
        raise RuntimeError(f"gh failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else []


def list_closed_prs(days: int, only: list[int] | None = None) -> list[dict[str, Any]]:
    if only:
        return [_fetch_pr(n) for n in only]

    since = datetime.now(timezone.utc) - timedelta(days=days)
    raw = _run_gh([
        "pr", "list",
        "--state", "closed",
        "--limit", "200",
        "--json", "number,title,body,closedAt,mergeCommit,baseRefName,headRefName,files,labels",
    ])
    out: list[dict[str, Any]] = []
    for pr in raw:  # type: ignore
        closed = pr.get("closedAt")
        if closed:
            t = datetime.fromisoformat(closed.replace("Z", "+00:00"))
            if t >= since and pr.get("mergeCommit"):  # only merged PRs
                out.append(pr)
    return out


def _fetch_pr(num: int) -> dict[str, Any]:
    return _run_gh([
        "pr", "view", str(num),
        "--json", "number,title,body,closedAt,mergeCommit,baseRefName,headRefName,files,labels",
    ])  # type: ignore


# --------------------------------------------------------------------
# Already-recorded check: is this PR in `memories.source_pr`?
# --------------------------------------------------------------------

def _already_recorded(pr_num: int) -> bool:
    try:
        from bot.supabase_helper import _request  # type: ignore[attr-defined]
        rows = _request("GET", f"memories?select=id&source_pr=eq.{pr_num}&limit=1")
        return bool(rows)
    except Exception:
        return False  # be conservative — if we can't check, treat as not-recorded


# --------------------------------------------------------------------
# Heuristic classification per PR
# --------------------------------------------------------------------

def classify_pr(pr: dict[str, Any]) -> dict[str, Any]:
    files = pr.get("files") or []
    triggered_questions: set[int] = set()
    entry_type_candidates: list[str] = []
    matched_files: list[str] = []

    for f in files:
        path = f.get("path") or ""
        for pat, qs, etype in PATH_HEURISTICS:
            if pat.search(path):
                triggered_questions.update(qs)
                matched_files.append(path)
                if etype:
                    entry_type_candidates.append(etype)
                break

    body = (pr.get("body") or "")
    title = pr.get("title") or ""
    text = f"{title}\n{body}"
    for pat, qs, etype in COMMIT_MSG_HEURISTICS:
        if pat.search(text):
            triggered_questions.update(qs)
            if etype:
                entry_type_candidates.append(etype)

    # Knowledge Sync block presence — if present and well-formed, the PR
    # self-attested and we don't need to flag it as "unrecorded".
    has_ksg = bool(re.search(r"##\s+Knowledge\s+Sync\b", body, re.IGNORECASE))
    has_attestation = bool(re.search(r"\*\*Delta:\*\*", body, re.IGNORECASE))

    # Pick the most common entry_type as the leading suggestion.
    entry_type = max(set(entry_type_candidates), key=entry_type_candidates.count) if entry_type_candidates else "finding"

    return {
        "pr": pr.get("number"),
        "title": title,
        "merge_sha": (pr.get("mergeCommit") or {}).get("oid"),
        "head_ref": pr.get("headRefName"),
        "closed_at": pr.get("closedAt"),
        "triggered_questions": sorted(triggered_questions),
        "delta_likely": len(triggered_questions) > 0,
        "has_ksg_block": has_ksg,
        "has_attestation": has_attestation,
        "self_attested": has_ksg and has_attestation,
        "entry_type_suggestion": entry_type,
        "matched_files_sample": matched_files[:10],
        "n_files_touched": len(files),
    }


# --------------------------------------------------------------------
# Candidate memories draft per unrecorded delta-likely PR
# --------------------------------------------------------------------

def draft_candidate(pr: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any] | None:
    """Produce a candidate memories draft for a PR. Returns None if not delta-likely."""
    if not classification["delta_likely"]:
        return None
    if classification["self_attested"]:
        return None  # Already declared its own delta via KSG block

    n = pr.get("number")
    title = pr.get("title") or ""
    merge_sha = (pr.get("mergeCommit") or {}).get("oid") or ""
    head_ref = pr.get("headRefName") or ""

    # Slug: derive from title or head_ref.
    slug_part = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-")[:60] or f"pr-{n}"
    name = f"finding/backfill/pr{n}-{slug_part}"

    return {
        "name": name,
        "entry_type": classification["entry_type_suggestion"],
        "status": "draft",  # backfill candidates land as draft for review
        "description": f"[backfill candidate] PR #{n}: {title}",
        "summary": f"Retroactive Knowledge Sync candidate generated from PR #{n} (merged {classification['closed_at']}).",
        "content": (
            f"Auto-generated backfill candidate for PR #{n}.\n\n"
            f"Title: {title}\n\n"
            f"Triggered questions: {classification['triggered_questions']}\n"
            f"Files touched (sample): {classification['matched_files_sample']}\n"
            f"Total files: {classification['n_files_touched']}\n\n"
            f"This candidate has NOT been reviewed by an operator. The slug, "
            f"entry_type, and content should all be revised before promoting "
            f"to status='active'. If the PR's actual knowledge delta is "
            f"already captured in another entry, this candidate should be "
            f"rejected (status='rejected')."
        ),
        "tags": [f"backfill", f"pr-{n}"],
        "source_branch": head_ref,
        "source_pr": n,
        "source_files": classification["matched_files_sample"],
        "valid_as_of_commit": merge_sha,
        "confidence": "low",
    }


# --------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="KSG Phase 7 — backfill PRs into the knowledge layer")
    p.add_argument("--days", type=int, default=90, help="How many days back to walk (default: 90)")
    p.add_argument("--pr", type=int, nargs="*", help="Explicit PR numbers (overrides --days)")
    p.add_argument("--out-dir", default=None, help="Output directory (default: /tmp/ksg_backfill_<timestamp>)")
    p.add_argument("--include-recorded", action="store_true", help="Also surface PRs already in memories.source_pr")
    args = p.parse_args(argv)

    ts = int(time.time())
    out_dir = Path(args.out_dir or f"/tmp/ksg_backfill_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Backfilling PRs into {out_dir}")
    print(f"Window: {'PRs ' + ' '.join(map(str, args.pr)) if args.pr else f'last {args.days} days'}")

    prs = list_closed_prs(args.days, only=args.pr)
    print(f"Found {len(prs)} merged PRs in scope")

    candidates: list[dict[str, Any]] = []
    self_attested: list[dict[str, Any]] = []
    no_delta: list[dict[str, Any]] = []
    already_recorded: list[dict[str, Any]] = []

    for pr in prs:
        cls = classify_pr(pr)
        if not args.include_recorded and _already_recorded(pr["number"]):
            already_recorded.append(cls)
            continue
        if cls["self_attested"]:
            self_attested.append(cls)
        elif not cls["delta_likely"]:
            no_delta.append(cls)
        else:
            cand = draft_candidate(pr, cls)
            if cand:
                candidates.append(cand)

    summary = {
        "window": f"last {args.days} days" if not args.pr else f"PRs {args.pr}",
        "total_prs": len(prs),
        "candidates_for_review": len(candidates),
        "self_attested": len(self_attested),
        "no_delta_detected": len(no_delta),
        "already_recorded_in_memories": len(already_recorded),
    }

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    (out_dir / "candidates.json").write_text(json.dumps({"entries": candidates}, indent=2, default=str) + "\n")
    (out_dir / "self_attested.json").write_text(json.dumps(self_attested, indent=2, default=str) + "\n")
    (out_dir / "no_delta.json").write_text(json.dumps(no_delta, indent=2, default=str) + "\n")
    (out_dir / "already_recorded.json").write_text(json.dumps(already_recorded, indent=2, default=str) + "\n")

    # Human-readable review queue
    md_lines = [
        f"# KSG Backfill Review Queue\n",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        f"**Window:** {summary['window']}",
        f"**Total merged PRs in window:** {summary['total_prs']}",
        f"",
        f"## Summary",
        f"- Candidates needing operator review: **{summary['candidates_for_review']}**",
        f"- Already self-attested via Knowledge Sync block: {summary['self_attested']}",
        f"- No knowledge delta detected: {summary['no_delta_detected']}",
        f"- Already recorded in `memories.source_pr`: {summary['already_recorded_in_memories']}",
        f"",
        f"## Candidates",
        f"",
    ]
    for c in candidates:
        md_lines += [
            f"### PR #{c['source_pr']} — {c['description'].replace('[backfill candidate] ', '')}",
            f"",
            f"- **Proposed slug:** `{c['name']}`",
            f"- **Entry type:** `{c['entry_type']}`",
            f"- **Status:** `{c['status']}` (review before promoting)",
            f"- **Anchor commit:** `{c['valid_as_of_commit'][:10] if c['valid_as_of_commit'] else 'n/a'}`",
            f"- **Source files (sample):** {', '.join(f'`{f}`' for f in c['source_files'][:5])}",
            f"",
        ]
    (out_dir / "review_queue.md").write_text("\n".join(md_lines) + "\n")

    # Print summary to stdout
    print(json.dumps(summary, indent=2))
    print()
    print(f"Review the candidates:")
    print(f"  {out_dir / 'review_queue.md'}")
    print(f"  {out_dir / 'candidates.json'}")
    print()
    print(f"To promote selected candidates to active memories:")
    print(f"  1. Edit {out_dir / 'candidates.json'} — set status='active', revise slug/content")
    print(f"  2. Run: python3 scripts/knowledge_sync.py validate --from <edited.json>")
    print(f"  3. Run: python3 scripts/knowledge_sync.py upsert --from <edited.json> --commit")
    print(f"     (requires Phase 3 schema migration applied)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
