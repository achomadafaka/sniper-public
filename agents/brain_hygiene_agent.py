#!/usr/bin/env python3
"""Proposal-only brain hygiene scanner.

Scans Supabase memories plus durable repo knowledge/research/audit docs for
signals that an entry may now be superseded or retired. It never applies status
updates; the output is a review queue for a human-approved batch update.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


SCAN_DIRS = (
    ROOT / "knowledge",
    ROOT / "docs" / "research",
    ROOT / "docs" / "audits",
)

REFERENCE_FILES = (
    ROOT / "docs" / "audits" / "2026-04-24-legacy-taxonomy-readers.md",
    ROOT / "docs" / "design" / "2026-04-24-operator-surface-redesign.md",
    ROOT / "docs" / "superpowers" / "plans" / "2026-04-24-wallet-intelligence-rearchitecture.md",
)

PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    (
        "legacy_wallet_insider_score",
        r"\b(user_analysis|flagged_users|wallet-level `?insider_score`?|wallet insider_score|high insider_score|insider_score\s*[>=])",
        "superseded",
        "Mentions legacy wallet-level insider_score/user_analysis framing deprecated by ADR-006.",
    ),
    (
        "hl_liquidation_retired",
        r"\b(HL liquidation|hl_liquidation|hyperliquid liquidation|liquidation dead-end)\b",
        "retired",
        "Mentions the HL liquidation direction, now retired/dead-ended.",
    ),
    (
        "inverted_paper_trade_edge",
        # Case-sensitive, tight forms. Drop "proof bar" and "WR >=" — those
        # appear in current docs too. Keep only legacy-specific phrasings.
        r"\b(paper[- ]trade edge|score[- ]band paper|win rate.*insider_score|insider[_ ]score\s*(band|tier|>=|>))",
        "superseded",
        "Mentions pre-cutover paper-trade edge tied to wallet-level insider_score bands.",
    ),
    (
        "legacy_taxonomy_terms",
        # Case-sensitive on BUILDING/ADDING/TWAP so English prose ("building a file",
        # "adding a feature", "crisis building") does not collide. `Scout pipeline`
        # and `legacy taxonomy` stay case-insensitive via the flag at call site.
        r"(\bTWAP\b|(?-i:\bBUILDING\b)|(?-i:\bADDING\b)|\bfresh-account\b|\bScout pipeline\b|\blegacy taxonomy\b)",
        "superseded",
        "Mentions legacy wallet taxonomy terms replaced by archetype/tag analytics.",
    ),
)

# Files with frontmatter memory_type=reference AND dated after this cutoff
# are treated as current context — never flag them. Matches YYYY-MM-DD
# anywhere in the filename.
RECENT_REFERENCE_CUTOFF = "2026-04-15"


@dataclass
class Proposal:
    target_kind: str
    identifier: str
    current_status: str
    proposed_status: str
    reason_code: str
    reason: str
    evidence: str
    superseded_by: str | None = None


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _current_commit() -> str:
    return _git(["rev-parse", "HEAD"]).strip()


def _deleted_files() -> set[str]:
    raw = _git(["log", "--diff-filter=D", "--summary", "--name-only", "--pretty=format:"])
    return {line.strip() for line in raw.splitlines() if line.strip()}


def _retired_strategy_slugs() -> set[str]:
    retired_dir = ROOT / "knowledge" / "strategies" / "retired"
    if not retired_dir.exists():
        return set()
    slugs: set[str] = set()
    for path in retired_dir.glob("*.md"):
        slug = re.sub(r"^\d+-", "", path.stem)
        slugs.add(slug)
        slugs.add(path.stem)
    return slugs


# Markdown variations for an explicit active declaration. Tolerates bold
# (`**Status:** active`), plain (`Status: active`), and the markdown
# bullet/seperator characters operators tend to use in strategy cards.
_STATUS_ACTIVE_RE = re.compile(
    r"\*{0,2}Status:?\*{0,2}\s*[:•]?\s*active\b",
    flags=re.IGNORECASE,
)


def _strategy_file_self_declares_active(path: Path, text: str) -> bool:
    """Return True if a file authoritatively declares itself as active
    or as already-retired, so the retired-slug substring heuristic
    should be skipped.

    Two signals — either suppresses the proposal:

      1. Path location: `knowledge/strategies/active/...` (active by
         location) or `knowledge/strategies/retired/...` (already
         retired, so proposing `retired` is redundant noise).
      2. Body content: a `**Status:** active` line anywhere (tolerant
         on markdown bold and bullet punctuation).

    Reviewer-driven fix: two consecutive hygiene passes flagged active
    strategies #37 and #40 as retired because the slug `insider_copy_v1`
    is shared between retired strategy IDs (002/005/006/...) and the
    plugin TYPE used by current active strategies. The substring match
    fired without checking the file's own declaration.
    """
    parts = path.parts
    try:
        idx = parts.index("strategies")
    except ValueError:
        pass
    else:
        if idx + 1 < len(parts) and parts[idx + 1] in {"active", "retired"}:
            return True
    return bool(_STATUS_ACTIVE_RE.search(text))


def _iter_scan_files() -> Iterable[Path]:
    for directory in SCAN_DIRS:
        if directory.exists():
            yield from sorted(p for p in directory.rglob("*.md") if p.is_file())


def _line_for_match(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].strip()[:240]


def _scan_text(target_kind: str, identifier: str, text: str, current_status: str) -> list[Proposal]:
    proposals: list[Proposal] = []
    seen: set[str] = set()
    for code, pattern, proposed_status, reason in PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match or code in seen:
            continue
        seen.add(code)
        proposals.append(
            Proposal(
                target_kind=target_kind,
                identifier=identifier,
                current_status=current_status,
                proposed_status=proposed_status,
                reason_code=code,
                reason=reason,
                evidence=_line_for_match(text, match.start()),
                superseded_by="wallet archetype/tag analytics pipeline" if "insider" in code or "taxonomy" in code else None,
            )
        )
    return proposals


def _scan_memories() -> tuple[list[Proposal], int]:
    _load_env()
    try:
        from bot.supabase_helper import read_memories

        memories = read_memories(active_only=False)
    except Exception as exc:
        print(f"Supabase memory scan skipped: {exc}", file=sys.stderr)
        return [], 0

    proposals: list[Proposal] = []
    for memory in memories:
        status = str(memory.get("status") or "active")
        if status != "active":
            continue
        # Skip recently-authored memories — they are current context, including
        # intentional summaries of what's being retired.
        updated_at = str(memory.get("updated_at") or "")
        if updated_at[:10] >= RECENT_REFERENCE_CUTOFF:
            continue
        ident = f"{memory.get('type')}:{memory.get('name')}"
        text = "\n".join(
            str(memory.get(k) or "")
            for k in ("name", "description", "content")
        )
        proposals.extend(_scan_text("memory", ident, text, status))
    return proposals, len(memories)


def _is_recent_reference_doc(path: Path, text: str) -> bool:
    """Skip: files whose filename contains a date >= RECENT_REFERENCE_CUTOFF.
    Recent docs are current context — even ones that mention legacy terms
    while discussing the deprecation itself. Historical reference checks are
    handled via the pattern scan on older files."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if not m:
        return False
    return m.group(1) >= RECENT_REFERENCE_CUTOFF


def _scan_files(retired_slugs: set[str], deleted: set[str]) -> tuple[list[Proposal], int]:
    proposals: list[Proposal] = []
    files = list(_iter_scan_files())
    reference_rels = {p.relative_to(ROOT).as_posix() for p in REFERENCE_FILES if p.exists()}
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        if rel not in reference_rels and not _is_recent_reference_doc(path, text):
            proposals.extend(_scan_text("file", rel, text, "active"))

        stem = path.stem
        slug_matches = any(slug and slug in stem for slug in retired_slugs)
        # Suppress the substring-only match when the file self-declares
        # active (knowledge/strategies/active/, **Status:** active body)
        # or is already in retired/. The `deleted` branch is a stronger
        # signal (git resurrection) so honor it even against a Status:
        # active declaration — the operator should explicitly re-add a
        # previously-deleted file rather than silently restoring it.
        if rel in deleted or (
            slug_matches and not _strategy_file_self_declares_active(path, text)
        ):
            proposals.append(
                Proposal(
                    target_kind="file",
                    identifier=rel,
                    current_status="active",
                    proposed_status="retired",
                    reason_code="retired_or_deleted_artifact",
                    reason="Matches a retired strategy slug or a path previously deleted in git history.",
                    evidence=stem,
                )
            )
    return _dedupe(proposals), len(files)


def _dedupe(proposals: list[Proposal]) -> list[Proposal]:
    by_key: dict[tuple[str, str, str], Proposal] = {}
    for proposal in proposals:
        key = (proposal.target_kind, proposal.identifier, proposal.reason_code)
        by_key.setdefault(key, proposal)
    return sorted(by_key.values(), key=lambda p: (p.target_kind, p.identifier, p.reason_code))


def _load_reference_summary() -> list[str]:
    lines: list[str] = []
    for path in REFERENCE_FILES:
        if not path.exists():
            continue
        rel = path.relative_to(ROOT).as_posix()
        for line in path.read_text(errors="replace").splitlines():
            if "deprecated" in line.lower() or "retire" in line.lower() or "ADR-006" in line:
                lines.append(f"{rel}: {line.strip()[:180]}")
                break
    return lines


def _write_finding(proposals: list[Proposal], output_path: Path | None) -> None:
    try:
        from bot.supabase_helper import save_finding
    except Exception as exc:
        print(f"Finding write skipped: {exc}", file=sys.stderr)
        return

    counts: dict[str, int] = {}
    for proposal in proposals:
        counts[proposal.proposed_status] = counts.get(proposal.proposed_status, 0) + 1
    # resolve() first so a relative --out doesn't crash relative_to(ROOT); fall back to the
    # raw path if --out points outside the repo.
    out_str = None
    if output_path:
        try:
            out_str = str(output_path.resolve().relative_to(ROOT))
        except ValueError:
            out_str = str(output_path)
    content = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "valid_as_of_commit": _current_commit(),
        "proposal_count": len(proposals),
        "counts_by_status": counts,
        "output_path": out_str,
        "note": "Proposal-only scan. No memory status changes were applied.",
        "top_proposals": [asdict(p) for p in proposals[:25]],
    }
    try:
        save_finding(
            "brain-hygiene/latest",
            "Brain hygiene proposal-only stale-content scan",
            json.dumps(content, indent=2, sort_keys=True),
            tags=["brain-hygiene", "stale-content", "proposal-only"],
        )
    except Exception as exc:
        print(f"Finding write skipped: {exc}", file=sys.stderr)


def run(write_finding: bool, output_path: Path | None) -> dict:
    retired_slugs = _retired_strategy_slugs()
    deleted = _deleted_files()
    memory_proposals, memory_count = _scan_memories()
    file_proposals, file_count = _scan_files(retired_slugs, deleted)
    proposals = _dedupe(memory_proposals + file_proposals)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "valid_as_of_commit": _current_commit(),
        "mode": "proposal_only",
        "scanned": {
            "memories": memory_count,
            "files": file_count,
            "retired_strategy_slugs": sorted(retired_slugs),
            "deleted_files_seen": len(deleted),
        },
        "references": _load_reference_summary(),
        "proposals": [asdict(p) for p in proposals],
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if write_finding:
        _write_finding(proposals, output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Proposal-only brain hygiene scanner")
    parser.add_argument("--out", type=Path, help="Write JSON proposal report to this path")
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    parser.add_argument("--no-write-finding", action="store_true", help="Do not write the scan summary to Supabase")
    args = parser.parse_args()

    report = run(write_finding=not args.no_write_finding, output_path=args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        proposals = report["proposals"]
        print(f"Brain hygiene scan complete: {len(proposals)} proposed status changes, none applied.")
        if args.out:
            print(f"Report: {args.out}")
        for proposal in proposals[:20]:
            print(
                f"- {proposal['proposed_status']}: {proposal['target_kind']} "
                f"{proposal['identifier']} ({proposal['reason_code']})"
            )
        if len(proposals) > 20:
            print(f"... {len(proposals) - 20} more in the JSON report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
