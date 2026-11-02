#!/usr/bin/env python3
"""Compile durable knowledge pages from SQLite operational DB state.

Emits markdown into ``knowledge/strategies/{active,retired}/`` and
``knowledge/wallets/``. The machine-owned body of each page is regenerated
from SQLite on every run; an operator-owned ``<!-- HUMAN:START --> ... <!--
HUMAN:END -->`` block is preserved across recompiles so human notes survive.

Runs locally for smoke tests; designed to run on VPS nightly via systemd
user timer (see docs/superpowers/specs/2026-04-18-knowledge-compilation-design.md).
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


HUMAN_START = "<!-- HUMAN:START -->"
HUMAN_END = "<!-- HUMAN:END -->"

_HUMAN_START_RE = re.compile(r"<!--\s*HUMAN:START\s*-->", re.IGNORECASE)
_HUMAN_END_RE = re.compile(r"<!--\s*HUMAN:END\s*-->", re.IGNORECASE)

# Default scaffold text injected into a fresh HUMAN block. Used to decide
# whether a page has operator-authored content or is still untouched.
# Public (no underscore) so tests can import rather than duplicate the literal.
HUMAN_SCAFFOLD_COMMENT = (
    "<!-- Operator-owned freeform notes. Preserved across recompiles. -->"
)

WALLET_CAP_DEFAULT = 100


class HumanBlockCorruptedError(RuntimeError):
    """Raised when existing file has an opening HUMAN marker without a closing
    one (or vice versa). Caller must skip the page rather than overwrite."""


# ---------------------------------------------------------------------------
# Schema tolerance — handle columns that may not exist on older/stale DBs
# ---------------------------------------------------------------------------


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def _safe_get(row: Optional[sqlite3.Row], key: str, default=None):
    """Row lookup that tolerates missing columns."""
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


# ---------------------------------------------------------------------------
# HUMAN block preservation — highest-risk operation
# ---------------------------------------------------------------------------


def _parse_human_structure(content: str) -> Optional[tuple[int, int]]:
    """Locate the outer HUMAN block via depth-based marker parsing.

    Returns ``(inner_start, inner_end)`` — character offsets of the content
    BETWEEN the outer markers — or ``None`` when no markers are present at all.
    Raises ``HumanBlockCorruptedError`` on any malformed structure:
    - START without matching END
    - END without matching START
    - Two or more top-level HUMAN blocks (a second START appears after the
      first block closed — this traps the review's "balanced but corrupt"
      case where machine-body content sits between separate blocks)

    Nested markers *inside* an operator note (e.g. quoted in a code fence)
    are fine — depth just goes 0→1→2→1→0 and the outer span is returned.
    """
    markers: list[tuple[int, bool, int]] = []
    for m in _HUMAN_START_RE.finditer(content):
        markers.append((m.start(), True, m.end()))
    for m in _HUMAN_END_RE.finditer(content):
        markers.append((m.start(), False, m.end()))
    if not markers:
        return None
    markers.sort(key=lambda x: x[0])

    depth = 0
    outer_start_content: Optional[int] = None
    outer_end_content: Optional[int] = None

    for pos, is_start, end_pos in markers:
        if is_start:
            if depth == 0:
                if outer_end_content is not None:
                    raise HumanBlockCorruptedError(
                        "multiple top-level HUMAN blocks detected — file "
                        "has machine content between separate blocks. "
                        "Refusing to overwrite."
                    )
                outer_start_content = end_pos  # first char after "-->"
            depth += 1
        else:
            if depth == 0:
                raise HumanBlockCorruptedError(
                    "HUMAN:END with no matching START. Refusing to overwrite."
                )
            depth -= 1
            if depth == 0:
                outer_end_content = pos  # first char of closing "<!--"

    if depth != 0:
        raise HumanBlockCorruptedError(
            "unclosed HUMAN block (START without END). Refusing to overwrite."
        )
    if outer_start_content is None or outer_end_content is None:
        raise HumanBlockCorruptedError("internal: marker parse inconsistent")
    return outer_start_content, outer_end_content


def extract_human_block(content: str) -> str:
    """Return the content between the outer HUMAN markers.

    Empty string when markers are absent OR when the structure is malformed.
    This is the LENIENT reader path — ``splice_human_block`` is the gate that
    raises on corruption before overwriting a file.
    """
    try:
        span = _parse_human_structure(content)
    except HumanBlockCorruptedError:
        return ""
    if span is None:
        return ""
    inner = content[span[0]:span[1]]
    return inner.strip("\n")


def has_operator_notes(content: str) -> bool:
    """True when the HUMAN block contains anything beyond whitespace and the
    default scaffold comment. Used by destructive cleanup paths to avoid
    silent deletion of operator-authored notes."""
    try:
        span = _parse_human_structure(content)
    except HumanBlockCorruptedError:
        # A corrupt file MIGHT contain notes — treat as "has notes" to be safe.
        return True
    if span is None:
        return False
    inner = content[span[0]:span[1]].strip()
    if not inner:
        return False
    # Strip the default scaffold comment line(s) and see if anything remains.
    residual = inner.replace(HUMAN_SCAFFOLD_COMMENT, "").strip()
    return bool(residual)


def splice_human_block(*, original: str, new_body: str) -> str:
    """Combine a preserved HUMAN block with a newly compiled body.

    - If ``original`` has no markers (new page or empty file): prepend a
      scaffold HUMAN block to ``new_body``.
    - If ``original`` has exactly one well-formed HUMAN block: reuse its inner
      content verbatim, then append ``new_body``.
    - On any malformed structure (mismatched markers, multiple top-level
      blocks, unclosed block): raise ``HumanBlockCorruptedError``. Caller
      must skip the page rather than overwrite.
    """
    span = _parse_human_structure(original)  # raises on corruption

    if span is None:
        return (
            f"{HUMAN_START}\n{HUMAN_SCAFFOLD_COMMENT}\n{HUMAN_END}\n\n{new_body}"
        )

    inner = original[span[0]:span[1]].strip("\n")
    return f"{HUMAN_START}\n{inner}\n{HUMAN_END}\n\n{new_body}"


# ---------------------------------------------------------------------------
# Strategy page
# ---------------------------------------------------------------------------


def is_retired_eligible(conn: sqlite3.Connection, *, strategy_id: int) -> bool:
    """Spec gate: only strategies with real runtime history get retired pages.

    Eligible when ANY of:
    - closed `strategy_positions` count >= 1
    - `strategies.pnl_usdc` != 0
    - `strategies.pause_reason` is non-empty
    """
    cols = _table_columns(conn, "strategies")
    select_cols = ["pnl_usdc"]
    if "pause_reason" in cols:
        select_cols.append("pause_reason")
    row = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None:
        return False
    if (_safe_get(row, "pnl_usdc") or 0.0) != 0.0:
        return True
    if (_safe_get(row, "pause_reason") or "").strip():
        return True
    closed = conn.execute(
        "SELECT COUNT(*) AS c FROM strategy_positions WHERE strategy_id = ? AND state = 'closed'",
        (str(strategy_id),),
    ).fetchone()
    return bool(closed and closed["c"] >= 1)


def _strategy_row(conn: sqlite3.Connection, strategy_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()


def _research_row(conn: sqlite3.Connection, slug: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strategy_research WHERE slug = ?",
        (slug,),
    ).fetchone()


def _strategy_position_stats(conn: sqlite3.Connection, strategy_id: int) -> dict:
    """Aggregate open/closed counts and realized PnL for a strategy."""
    cols = _table_columns(conn, "strategy_positions")
    select_cols = ["state"]
    if "realized_pnl" in cols:
        select_cols.append("realized_pnl")
    if "pnl_usdc" in cols:
        select_cols.append("pnl_usdc")
    rows = conn.execute(
        f"""SELECT {', '.join(select_cols)}
            FROM strategy_positions
            WHERE strategy_id = ?""",
        (str(strategy_id),),
    ).fetchall()
    open_count = sum(1 for r in rows if r["state"] == "open")
    closed_rows = [r for r in rows if r["state"] == "closed"]
    closed_count = len(closed_rows)
    def _pnl_of(r):
        return (_safe_get(r, "realized_pnl") or _safe_get(r, "pnl_usdc") or 0.0)
    closed_pnl = sum(_pnl_of(r) for r in closed_rows)
    wins = sum(1 for r in closed_rows if _pnl_of(r) > 0)
    win_rate = (wins / closed_count) if closed_count else 0.0
    return {
        "open_count": open_count,
        "closed_count": closed_count,
        "closed_pnl": closed_pnl,
        "wins": wins,
        "win_rate": win_rate,
    }


def render_strategy_page(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    existing_content: str = "",
) -> str:
    """Render the full markdown for one strategy page. Existing HUMAN block
    (if any) is spliced in."""
    s = _strategy_row(conn, strategy_id)
    if s is None:
        raise ValueError(f"strategy_id={strategy_id} not found")

    r = _research_row(conn, s["strategy_type"])
    stats = _strategy_position_stats(conn, strategy_id)

    lines: list[str] = []
    lines.append(f"# Strategy #{_safe_get(s, 'id')} — {_safe_get(s, 'label', '(unlabelled)')}")
    lines.append("")
    lines.append(f"**Type:** `{_safe_get(s, 'strategy_type', '(unknown)')}`")
    lifecycle_stage = _safe_get(s, "lifecycle_stage") or "(n/a)"
    lines.append(
        f"**Status:** {_safe_get(s, 'status', '(unknown)')}  •  **Lifecycle:** {lifecycle_stage}"
    )
    instance_tag = _safe_get(s, "instance_tag") or ""
    lines.append(
        f"**Wallet:** `{_safe_get(s, 'wallet', '')}`  •  **Instance tag:** `{instance_tag}`"
    )
    pause_reason = _safe_get(s, "pause_reason")
    if pause_reason:
        lines.append(f"**Pause reason:** {pause_reason}")
    lines.append("")
    lines.append("## Performance (live, from DB)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Paper trades triggered | {_safe_get(s, 'trades_triggered', 0)} |")
    lines.append(f"| Realized PnL (USDC) | {(_safe_get(s, 'pnl_usdc') or 0.0):+.2f} |")
    lines.append(f"| Open positions | {stats['open_count']} |")
    lines.append(f"| Closed positions | {stats['closed_count']} |")
    lines.append(f"| Closed position PnL | {stats['closed_pnl']:+.2f} |")
    lines.append(
        f"| Win rate (closed) | {stats['win_rate']:.1%} "
        f"({stats['wins']}/{stats['closed_count']}) |"
    )
    lines.append("")

    if r is None:
        lines.append("## Research")
        lines.append("(no research entry yet)")
    else:
        lines.append("## Hypothesis & edge")
        lines.append(_safe_get(r, "hypothesis") or "(empty)")
        edge_summary = _safe_get(r, "edge_summary")
        if edge_summary:
            lines.append("")
            lines.append(edge_summary)
        lines.append("")
        lines.append("## Entry logic")
        lines.append(_safe_get(r, "entry_logic") or "(empty)")
        lines.append("")
        lines.append("## Exit logic")
        lines.append(_safe_get(r, "exit_logic") or "(empty)")
        lines.append("")
        lines.append("## Risk notes")
        lines.append(_safe_get(r, "risk_notes") or "(empty)")
        lines.append("")
        lines.append("## Evidence")
        lines.append(
            (_safe_get(r, "evidence") or "(empty)")
            + f"  (sample size: {_safe_get(r, 'sample_size', 0)})"
        )
    lines.append("")

    lines.append(
        f"Created: {_safe_get(s, 'created_at', '?')}  •  "
        f"Updated: {_safe_get(s, 'updated_at', '?')}"
    )
    lines.append("")
    lines.append("---")
    lines.append(
        "*Compiled automatically from `strategies`, `strategy_research`, "
        "`strategy_positions`. Do not hand-edit outside the HUMAN block — "
        "changes will be overwritten.*"
    )
    body = "\n".join(lines) + "\n"
    return splice_human_block(original=existing_content, new_body=body)


# ---------------------------------------------------------------------------
# Wallet page
# ---------------------------------------------------------------------------


def curated_wallets(conn: sqlite3.Connection, *, cap: int = WALLET_CAP_DEFAULT) -> list[str]:
    """``followed_wallets ∪ watchlist_wallets`` — dedup, capped, ordered
    followed-first then watchlist by ``added_ts`` DESC."""
    seen: set[str] = set()
    out: list[str] = []
    for row in conn.execute(
        "SELECT wallet FROM followed_wallets ORDER BY added_at DESC"
    ):
        w = row["wallet"]
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= cap:
            return out
    for row in conn.execute(
        "SELECT wallet FROM watchlist_wallets ORDER BY added_ts DESC"
    ):
        w = row["wallet"]
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= cap:
            break
    return out


def _followed_row(conn: sqlite3.Connection, wallet: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM followed_wallets WHERE wallet = ?", (wallet,)
    ).fetchone()


def _watchlist_row(conn: sqlite3.Connection, wallet: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM watchlist_wallets WHERE wallet = ?", (wallet,)
    ).fetchone()


def _analysis_row(conn: sqlite3.Connection, wallet: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM user_analysis WHERE wallet = ?", (wallet,)
    ).fetchone()


def _profile_row(conn: sqlite3.Connection, wallet: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM wallet_profiles WHERE wallet = ?", (wallet,)
    ).fetchone()


def render_wallet_page(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    existing_content: str = "",
) -> str:
    """Render the full markdown for one wallet page."""
    followed = _followed_row(conn, wallet)
    watch = _watchlist_row(conn, wallet)
    analysis = _analysis_row(conn, wallet)
    profile = _profile_row(conn, wallet)

    if followed is None and watch is None:
        raise ValueError(f"wallet {wallet} not in followed or watchlist")

    source_list = []
    if followed is not None:
        source_list.append("followed")
    if watch is not None:
        source_list.append("watchlist")

    label = (
        _safe_get(followed, "label")
        or _safe_get(watch, "label")
        or _safe_get(profile, "username")
        or "(unlabelled)"
    )
    short = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 12 else wallet

    lines: list[str] = []
    lines.append(f"# `{short}` — {label}")
    lines.append("")
    lines.append(f"**Wallet:** `{wallet}`")
    lines.append(f"**Source list:** {' & '.join(source_list)}")
    if followed is not None:
        lines.append(
            f"**Added:** {_safe_get(followed, 'added_at', '?')}  •  "
            f"**Added reason:** {_safe_get(followed, 'added_reason') or '(none)'}"
        )
    profile_username = _safe_get(profile, "username")
    if profile_username:
        joined = _safe_get(profile, "profile_joined_ts")
        lines.append(
            f"**Profile:** {profile_username} @ Polymarket"
            + (f"  •  joined ts={joined}" if joined else "")
        )
    lines.append("")

    lines.append("## Scoring (from user_analysis)")
    lines.append("")
    if analysis is None:
        lines.append("(not yet analyzed)")
    else:
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Classification | {_safe_get(analysis, 'classification', '?')} |")
        lines.append(f"| Tier | {_safe_get(analysis, 'tier', '?')} |")
        lines.append(f"| Insider score | {(_safe_get(analysis, 'insider_score') or 0.0):.1f} |")
        lines.append(f"| Speculator score | {(_safe_get(analysis, 'speculator_score') or 0.0):.1f} |")
        lines.append(
            f"| Win rate | {(_safe_get(analysis, 'win_rate') or 0.0):.1%} "
            f"({_safe_get(analysis, 'wins', 0)}/{_safe_get(analysis, 'closed_sample_size', 0)}) |"
        )
        lines.append(f"| Strategy group | {_safe_get(analysis, 'strategy_group', '?')} |")
        lines.append(f"| Net PnL | {(_safe_get(analysis, 'pnl') or 0.0):+,.0f} |")
        lb_pnl = _safe_get(analysis, "lb_pnl")
        if lb_pnl is not None:
            lines.append(f"| Lifetime PnL (Polymarket) | {lb_pnl:+,.0f} |")
        lines.append(f"| ROI | {(_safe_get(analysis, 'roi_percent') or 0.0):+.1f}% |")
        lines.append(f"| Qualifying PnL | {(_safe_get(analysis, 'qualifying_pnl') or 0.0):+,.0f} |")
    lines.append("")

    if followed is not None:
        lines.append("## Copy performance (from followed_wallets)")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Positions copied | {_safe_get(followed, 'total_positions', 0)} |")
        lines.append(f"| Copy PnL | {(_safe_get(followed, 'total_pnl_usdc') or 0.0):+.2f} |")
        lines.append(
            f"| Last copied | {_safe_get(followed, 'last_copied_at') or '(never)'} |"
        )
        lines.append("")

    profile_bio = _safe_get(profile, "bio")
    if profile_bio:
        lines.append("## Profile")
        lines.append(profile_bio)
        lines.append("")

    lines.append("---")
    lines.append(
        "*Compiled from `followed_wallets`, `watchlist_wallets`, "
        "`user_analysis`, `wallet_profiles`. Do not hand-edit outside the HUMAN block.*"
    )
    body = "\n".join(lines) + "\n"
    return splice_human_block(original=existing_content, new_body=body)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if the file is missing or differs.

    Returns True when a write occurred. Uses atomic tmp+replace so a crash
    cannot leave a half-written page.
    """
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _strategy_page_path(knowledge_root: Path, strategy_id: int, strategy_type: str, *, retired: bool) -> Path:
    bucket = "retired" if retired else "active"
    return knowledge_root / "strategies" / bucket / f"{strategy_id:03d}-{strategy_type}.md"


def _is_strategy_retired_now(status: str) -> bool:
    return (status or "").lower() in {"stopped", "paused", "retired"}


def compile_strategies(
    conn: sqlite3.Connection,
    knowledge_root: Path,
    *,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Compile every strategy page. Returns (changed_count, skipped_count)."""
    changed = 0
    skipped = 0
    rows = conn.execute(
        "SELECT id, strategy_type, status FROM strategies ORDER BY id"
    ).fetchall()
    for row in rows:
        sid = row["id"]
        slug = row["strategy_type"]
        retired_now = _is_strategy_retired_now(row["status"])
        retired_eligible = is_retired_eligible(conn, strategy_id=sid)

        active_path = _strategy_page_path(knowledge_root, sid, slug, retired=False)
        retired_path = _strategy_page_path(knowledge_root, sid, slug, retired=True)

        if retired_now and retired_eligible:
            # Retired pages are frozen once written — skip recompile if already exists.
            # But if a stale active twin somehow remains (interrupted old run,
            # manual restore), clean it up so agents don't see contradictory pages.
            # Safety gate: if the stale twin has operator notes the retired page
            # never absorbed (operator edited it AFTER retirement), refuse to
            # delete — same rule every other destructive path follows.
            if retired_path.exists():
                if active_path.exists():
                    try:
                        prior = active_path.read_text(encoding="utf-8")
                    except OSError:
                        prior = ""
                    if has_operator_notes(prior):
                        print(
                            f"WARN: stale active twin {active_path} has operator "
                            f"notes not reflected in the retired page — refusing "
                            f"to delete. Merge the notes into the retired page or "
                            f"remove the file by hand.",
                            file=sys.stderr,
                        )
                        skipped += 1
                    elif dry_run:
                        print(f"[dry-run] WOULD delete stale active twin {active_path}")
                        changed += 1
                    else:
                        active_path.unlink()
                        changed += 1
                else:
                    skipped += 1
                continue
            # First transition: copy HUMAN block from active page (if any), then
            # render body with retired banner, delete the active page.
            prior = active_path.read_text(encoding="utf-8") if active_path.exists() else ""
            try:
                body = render_strategy_page(conn, strategy_id=sid, existing_content=prior)
            except HumanBlockCorruptedError as e:
                print(f"WARN: skipping retired transition for #{sid} ({slug}) — {e}",
                      file=sys.stderr)
                skipped += 1
                continue
            banner = "> Frozen snapshot. Not recompiled. Update by hand if lessons evolve.\n\n"
            body = _inject_after_human_block(body, banner)
            if dry_run:
                print(f"[dry-run] WOULD write {retired_path}")
                changed += 1
                if active_path.exists():
                    print(f"[dry-run] WOULD delete {active_path}")
                    changed += 1
            else:
                wrote = write_if_changed(retired_path, body)
                if wrote:
                    changed += 1
                if active_path.exists():
                    active_path.unlink()
                    changed += 1
            continue

        if retired_now and not retired_eligible:
            # Abandoned experiment — no page, clean up any existing active page.
            # Safety gate: if the operator left notes, warn and skip — don't
            # silently nuke human content.
            if active_path.exists():
                try:
                    prior = active_path.read_text(encoding="utf-8")
                except OSError:
                    prior = ""
                if has_operator_notes(prior):
                    print(
                        f"WARN: abandoned strategy #{sid} ({slug}) has operator "
                        f"notes in {active_path} — refusing to delete. Move the "
                        f"notes elsewhere or remove the file by hand.",
                        file=sys.stderr,
                    )
                    skipped += 1
                    continue
                if dry_run:
                    print(f"[dry-run] WOULD delete {active_path}")
                    changed += 1
                else:
                    active_path.unlink()
                    changed += 1
            else:
                skipped += 1
            continue

        # Active strategy
        prior = active_path.read_text(encoding="utf-8") if active_path.exists() else ""
        try:
            body = render_strategy_page(conn, strategy_id=sid, existing_content=prior)
        except HumanBlockCorruptedError as e:
            print(f"WARN: skipping strategy #{sid} ({slug}) — {e}", file=sys.stderr)
            skipped += 1
            continue
        if dry_run:
            if not active_path.exists() or active_path.read_text(encoding="utf-8") != body:
                print(f"[dry-run] WOULD write {active_path}")
                changed += 1
            else:
                skipped += 1
        else:
            if write_if_changed(active_path, body):
                changed += 1
            else:
                skipped += 1
    return changed, skipped


def _inject_after_human_block(body: str, injection: str) -> str:
    """Insert `injection` right after the HUMAN:END marker (plus one blank line)."""
    idx = body.find(HUMAN_END)
    if idx < 0:
        return injection + body
    end = idx + len(HUMAN_END)
    # Skip any trailing newlines after the marker to avoid double-blank.
    tail_start = end
    while tail_start < len(body) and body[tail_start] == "\n":
        tail_start += 1
    return body[:end] + "\n\n" + injection + body[tail_start:]


def compile_wallets(
    conn: sqlite3.Connection,
    knowledge_root: Path,
    *,
    cap: int = WALLET_CAP_DEFAULT,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Compile wallet pages for the curated set. Removes pages for wallets
    dropped from both lists."""
    changed = 0
    skipped = 0
    wallets_dir = knowledge_root / "wallets"
    if not dry_run:
        wallets_dir.mkdir(parents=True, exist_ok=True)

    target_wallets = set(curated_wallets(conn, cap=cap))
    for wallet in sorted(target_wallets):
        path = wallets_dir / f"{wallet}.md"
        prior = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            body = render_wallet_page(conn, wallet=wallet, existing_content=prior)
        except HumanBlockCorruptedError as e:
            print(f"WARN: skipping wallet {wallet} — {e}", file=sys.stderr)
            skipped += 1
            continue
        if dry_run:
            if prior != body:
                print(f"[dry-run] WOULD write {path}")
                changed += 1
            else:
                skipped += 1
        else:
            if write_if_changed(path, body):
                changed += 1
            else:
                skipped += 1

    # Prune pages for wallets no longer curated. Skip operator sentinels
    # (README.md, anything starting with '_' or uppercase) — only touch files
    # that look like wallet pages (stem starts with '0x'). Also: if the page
    # contains operator notes, warn and skip — don't silently nuke human content.
    for md in sorted(wallets_dir.glob("*.md")):
        if not md.stem.startswith("0x"):
            continue
        wallet = md.stem
        if wallet in target_wallets:
            continue
        try:
            prior = md.read_text(encoding="utf-8")
        except OSError:
            prior = ""
        if has_operator_notes(prior):
            print(
                f"WARN: wallet {wallet} no longer curated but {md} has "
                f"operator notes — refusing to delete. Move the notes "
                f"elsewhere or remove the file by hand.",
                file=sys.stderr,
            )
            skipped += 1
            continue
        if dry_run:
            print(f"[dry-run] WOULD delete {md}")
            changed += 1
        else:
            md.unlink()
            changed += 1
    return changed, skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Compile knowledge/*.md pages from operational DB state."
    )
    p.add_argument("--db", default=str(REPO_ROOT / "sniper_tracker.db"),
                   help="Path to SQLite DB (default: ./sniper_tracker.db)")
    p.add_argument("--knowledge-root", default=str(REPO_ROOT / "knowledge"),
                   help="Where to write pages (default: ./knowledge)")
    p.add_argument("--strategies-only", action="store_true")
    p.add_argument("--wallets-only", action="store_true")
    p.add_argument("--cap", type=int, default=WALLET_CAP_DEFAULT,
                   help=f"Max wallet pages (default {WALLET_CAP_DEFAULT})")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2
    knowledge_root = Path(args.knowledge_root).resolve()

    conn = _connect_db(db_path)
    total_changed = 0
    total_skipped = 0
    if not args.wallets_only:
        c, s = compile_strategies(conn, knowledge_root, dry_run=args.dry_run)
        total_changed += c
        total_skipped += s
        print(f"strategies: changed={c} skipped={s}")
    if not args.strategies_only:
        c, s = compile_wallets(conn, knowledge_root, cap=args.cap, dry_run=args.dry_run)
        total_changed += c
        total_skipped += s
        print(f"wallets: changed={c} skipped={s}")

    print(f"TOTAL: changed={total_changed} skipped={total_skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
