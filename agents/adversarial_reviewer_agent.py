#!/usr/bin/env python3
"""
Adversarial Reviewer — attacks trade ideas before they are allowed to execute.

Uses Codex CLI (codex exec) via the user's Codex subscription — no API key needed;
authenticates through `codex login --device-auth`. Model: ADVERSARIAL_REVIEW_MODEL.

The reviewer sees the open paper book alongside each idea, so it can flag a
candidate that merely adds exposure to a bet we already hold.

Brain section: adversarial/*
  adversarial/run_stats          — cumulative + per-run stats (approval rate, avg score)
  adversarial/rejection_patterns — last 20 rejection summaries for pattern awareness
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from brain import Brain, ensure_trade_ideas_schema
from agents.tools.obs_tool import write_observation

try:
    from bot.supabase_helper import get_memory as _sb_get_memory
    from bot.supabase_helper import upsert_memory as _sb_upsert_memory
    _SUPABASE_AVAILABLE = True
except Exception:
    _sb_get_memory = None  # type: ignore[assignment]
    _sb_upsert_memory = None  # type: ignore[assignment]
    _SUPABASE_AVAILABLE = False

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(ROOT / "sniper_tracker.db"))
PRIMARY_MODEL = os.getenv("ADVERSARIAL_REVIEW_MODEL", "gpt-5.6-terra")
MAX_IDEAS_PER_RUN = max(1, int(os.getenv("ADVERSARIAL_REVIEW_MAX_IDEAS", "12")))
MAX_APPROVAL_SCORE = max(0, min(100, int(os.getenv("ADVERSARIAL_APPROVAL_MAX_SCORE", "39"))))
CODEX_TIMEOUT_SECONDS = max(30, int(os.getenv("ADVERSARIAL_REVIEW_TIMEOUT_SECONDS", "180")))
CODEX_CMD = os.getenv("CODEX_CMD", "npx @openai/codex")

# ---------------------------------------------------------------------------
# Brain section helpers — graceful Supabase access for context + stats writes
# ---------------------------------------------------------------------------

# Brain key constants
_KEY_RUN_STATS = "adversarial/run_stats"
_KEY_REJECTION_PATTERNS = "adversarial/rejection_patterns"
_BRAIN_TYPE = "knowledge"

# source_agent field value -> (brain memory key, type_filter)
_SOURCE_AGENT_BRAIN_KEY: dict[str, tuple[str, str]] = {
    "world_analyst": ("world_state", _BRAIN_TYPE),
    "finance_analyst": ("markets/state", _BRAIN_TYPE),
}
# Max chars injected from a brain context snippet (keeps Codex prompt tight)
_CONTEXT_MAX_CHARS = 900


def _try_get_memory(name: str, type_filter: str) -> Optional[str]:
    """Read a Supabase brain memory safely; returns None on any error."""
    if not _SUPABASE_AVAILABLE:
        return None
    try:
        m = _sb_get_memory(name, type_filter=type_filter)
        return m["content"] if m else None
    except Exception:
        logger.debug("_try_get_memory(%s) failed", name, exc_info=True)
        return None


def _try_upsert_memory(name: str, description: str, content: str) -> None:
    """Write a 'knowledge' brain memory safely; swallows all errors."""
    if not _SUPABASE_AVAILABLE:
        return
    try:
        _sb_upsert_memory(_BRAIN_TYPE, name, description, content, source="vps")
    except Exception:
        logger.debug("_try_upsert_memory(%s) failed", name, exc_info=True)


def _fetch_context_for_source(source_agent: Optional[str]) -> Optional[str]:
    """Return relevant world/market context for the given source agent, or None."""
    if not source_agent:
        return None
    mapping = _SOURCE_AGENT_BRAIN_KEY.get(source_agent)
    if not mapping:
        return None
    key, type_filter = mapping
    content = _try_get_memory(key, type_filter)
    if not content:
        return None
    stripped = content.strip()
    snippet = stripped[:_CONTEXT_MAX_CHARS]
    if len(stripped) > _CONTEXT_MAX_CHARS:
        snippet += "\n[...truncated]"
    return snippet


SYSTEM_PROMPT = """You are Sniper's Adversarial Reviewer.

Your job is to find reasons NOT to trade.
Default stance: reject. Approval is rare and must be earned.

Attack each idea on:
1. Whether the thesis is already obvious and priced in.
2. Whether the market is crowded, reactive, or headline-driven.
3. Whether the causal chain is weak, generic, or timing-insensitive.
4. Whether the trade is too late given current price and public information.
5. Whether the source confidence and evidence are too thin for execution.
6. Whether the opposite side has the better risk/reward.

Biases to avoid:
- Do not rubber-stamp because the idea sounds smart.
- Do not optimize for action rate.
- If evidence is ambiguous, reject.

Scoring rubric — use the FULL range, not a default cluster near 85:
  0–20  Exceptional edge: genuine information asymmetry, crowd clearly wrong, tight causal
        chain, strong timing, evidence NOT in public headlines.
  21–39 Viable edge: some asymmetry, not fully priced in, causal chain holds under scrutiny,
        timing makes sense. APPROVE these.
  40–60 Marginal: partially priced in, causal chain has real gaps, timing uncertain.
  61–80 Weak: mostly public information, obvious headline play, timing too late or vague.
  81–100 Reject: fully priced in, crowded, no edge beyond what the market already knows.

Return JSON only with:
- adversarial_score: integer 0-100 (use the full range — a score of 87 for every idea means
  your calibration is broken)
- approved: boolean (true only if score <= 39 AND you genuinely cannot tear the thesis apart)
- reasoning: concise string, 1-4 sentences, focused on the specific blocker or why edge exists
"""


def _extract_json_payload(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("model returned empty output")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"model output was not JSON: {text[:200]}")
    return json.loads(text[start : end + 1])


def _normalize_review(payload: dict[str, Any]) -> dict[str, Any]:
    score_raw = payload.get("adversarial_score", 100)
    try:
        score = int(round(float(score_raw)))
    except Exception as exc:
        raise ValueError(f"invalid adversarial_score: {score_raw!r}") from exc
    score = max(0, min(100, score))
    approved = bool(payload.get("approved", False)) and score <= MAX_APPROVAL_SCORE
    reasoning = str(payload.get("reasoning") or "").strip()
    if not reasoning:
        raise ValueError("review reasoning is empty")
    return {
        "adversarial_score": score,
        "adversarial_approved": approved,
        "adversarial_reasoning": reasoning,
    }


def _fetch_open_positions_block(conn, exclude_idea_id: int) -> Optional[str]:
    """Open paper positions, rendered for the prompt. None on any failure.

    The reviewer needs the book to judge an idea in context: a fresh entry on an event
    we already hold is a bigger position, not a second independent bet. The idea's OWN
    position is excluded — reviews can run after the position exists, and showing the
    reviewer the result of the idea under review makes it reject the idea as a
    duplicate of itself.
    """
    try:
        from sniper.services.intel_positions_context import format_positions_block
        return format_positions_block(conn, exclude_idea_id=exclude_idea_id) or None
    except Exception:
        logger.debug("could not load open positions for reviewer", exc_info=True)
        return None


def _build_review_prompt(
    idea: dict[str, Any],
    context_block: Optional[str] = None,
    extra_prompt: Optional[str] = None,
    positions_block: Optional[str] = None,
) -> str:
    parts: list[str] = []

    if positions_block:
        parts.append(
            "Paper positions we ALREADY HOLD (entry -> current price):\n"
            f"---\n{positions_block}\n---\n"
            "If this idea duplicates or correlates with something above, say so — it is "
            "added exposure to one bet, not a new independent one. Judge the idea on its "
            "own merit; existing exposure is a reason for scrutiny, not an automatic "
            "rejection.\n"
            "REJECT OUTRIGHT if the idea cannot both win alongside a position above — "
            "the same market in the opposite direction, or a nested/mutually exclusive "
            "outcome of the same event (\"no X by Aug 31\" already implies \"no X by "
            "Jul 31\"). A self-excluding pair pays the spread twice to learn nothing, "
            "so that is a mechanical reject, not a judgement call.\n"
        )

    if context_block:
        parts.append(
            "Context from the source agent's brain section (use this to check whether the "
            "thesis is already reflected in the current world/market picture):\n"
            f"---\n{context_block}\n---\n"
        )

    parts.append(
        f"""Review this trade idea adversarially.

Trade idea:
- idea_id: {idea['id']}
- source_agent: {idea.get('source_agent') or 'unknown'}
- market_title: {idea.get('market_title') or ''}
- condition_id: {idea.get('condition_id') or ''}
- direction: {idea.get('direction') or ''}
- outcome: {idea.get('outcome') or ''}
- confidence: {idea.get('confidence') or ''}
- current_price: {idea.get('current_price')}
- news_source: {idea.get('news_source') or ''}
- reasoning: {idea.get('reasoning') or ''}

Decide whether this should be allowed to execute.
Be explicitly skeptical of crowded breaking-news trades, obvious geopolitical headlines, \
and ideas whose edge depends on information the market already absorbed.
"""
    )

    if extra_prompt:
        parts.append(f"Additional operator instruction:\n{extra_prompt.strip()}\n")

    return "\n".join(parts)


def _call_codex_review(
    idea: dict[str, Any],
    context_block: Optional[str] = None,
    extra_prompt: Optional[str] = None,
    positions_block: Optional[str] = None,
) -> dict[str, Any]:
    """Call the reviewer model via Codex CLI (Codex subscription, no API key needed)."""
    full_prompt = SYSTEM_PROMPT + "\n\n" + _build_review_prompt(
        idea, context_block, extra_prompt, positions_block
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as out_f:
        out_path = out_f.name

    try:
        cmd = CODEX_CMD.split() + [
            "exec",
            "--skip-git-repo-check",
            "-m", PRIMARY_MODEL,
            "-o", out_path,
            "-",  # read prompt from stdin
        ]
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=CODEX_TIMEOUT_SECONDS,
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "401" in stderr or "Unauthorized" in stderr:
                raise RuntimeError(
                    "Codex CLI not authenticated. Run: npx @openai/codex login --device-auth"
                )
            raise RuntimeError(f"Codex exec failed (rc={result.returncode}): {stderr[:300]}")

        output_text = Path(out_path).read_text().strip()
        if not output_text:
            # Fall back to stdout if -o file is empty
            output_text = (result.stdout or "").strip()
        if not output_text:
            raise RuntimeError("Codex returned empty output")

        parsed = _extract_json_payload(output_text)
        return _normalize_review(parsed)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _write_observation_safe(*, obs_type: str, confidence: str, content: str) -> None:
    try:
        write_observation(
            strategy="adversarial_reviewer",
            obs_type=obs_type,
            confidence=confidence,
            content=content,
        )
    except Exception:
        logger.debug("Adversarial reviewer observation write failed", exc_info=True)


def _update_brain_stats(
    *,
    run_reviewed: int,
    run_approved: int,
    run_scores: list[int],
) -> None:
    """Merge this run's stats into adversarial/run_stats in the brain."""
    existing_raw = _try_get_memory(_KEY_RUN_STATS, _BRAIN_TYPE)
    try:
        existing: dict[str, Any] = json.loads(existing_raw) if existing_raw else {}
    except Exception:
        existing = {}

    total_reviewed = int(existing.get("total_reviewed", 0)) + run_reviewed
    total_approved = int(existing.get("total_approved", 0)) + run_approved
    total_rejected = total_reviewed - total_approved
    approval_rate = round(100.0 * total_approved / total_reviewed, 1) if total_reviewed else 0.0
    avg_score_run = round(sum(run_scores) / len(run_scores), 1) if run_scores else None

    stats: dict[str, Any] = {
        "total_reviewed": total_reviewed,
        "total_approved": total_approved,
        "total_rejected": total_rejected,
        "approval_rate_pct": approval_rate,
        "avg_score_last_run": avg_score_run,
        "last_run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "last_run_reviewed": run_reviewed,
        "last_run_approved": run_approved,
        "last_run_rejected": run_reviewed - run_approved,
        "model": PRIMARY_MODEL,
    }
    _try_upsert_memory(
        _KEY_RUN_STATS,
        "Adversarial Reviewer — cumulative stats and last-run summary",
        json.dumps(stats, indent=2),
    )


def _update_rejection_patterns(new_rejections: list[dict[str, Any]]) -> None:
    """Append new rejections to adversarial/rejection_patterns (keep last 20)."""
    if not new_rejections:
        return
    existing_raw = _try_get_memory(_KEY_REJECTION_PATTERNS, _BRAIN_TYPE)
    try:
        patterns: list[dict[str, Any]] = json.loads(existing_raw) if existing_raw else []
    except Exception:
        patterns = []

    patterns.extend(new_rejections)
    patterns = patterns[-20:]  # keep most recent 20
    _try_upsert_memory(
        _KEY_REJECTION_PATTERNS,
        "Adversarial Reviewer — last 20 rejection summaries for pattern awareness",
        json.dumps(patterns, indent=2),
    )


def review_pending_trade_ideas(
    *,
    db_path: str = DB_PATH,
    limit: int = MAX_IDEAS_PER_RUN,
    extra_prompt: str | None = None,
) -> dict[str, Any]:
    brain = Brain(db_path=db_path)
    conn = brain._get_conn()
    ensure_trade_ideas_schema(conn)
    ideas = brain.get_pending_adversarial_reviews(limit=limit)
    # An idea we already papered needs a verdict even if it has left status='open',
    # otherwise its position is stuck at 'pending' and drops out of the cohort split.
    _seen = {int(i["id"]) for i in ideas}
    for extra in brain.get_unreviewed_papered_ideas(limit=limit):
        if len(ideas) >= limit:
            break
        if int(extra["id"]) not in _seen:
            _seen.add(int(extra["id"]))
            ideas.append(extra)
    reviewed = 0
    approved = 0
    rejected = 0
    failures: list[str] = []
    run_scores: list[int] = []
    new_rejections: list[dict[str, Any]] = []

    # Cache context lookups — one Supabase fetch per unique source_agent type per run
    context_cache: dict[str, Optional[str]] = {}

    for idea in ideas:
        idea_id = int(idea["id"])
        source_agent = idea.get("source_agent") or ""

        if source_agent not in context_cache:
            context_cache[source_agent] = _fetch_context_for_source(source_agent)
        context_block = context_cache[source_agent]

        try:
            review = _call_codex_review(
                idea, context_block=context_block, extra_prompt=extra_prompt,
                # Per idea, not per run: each must exclude its own position.
                positions_block=_fetch_open_positions_block(conn, idea_id),
            )
            brain.record_adversarial_review(
                idea_id,
                adversarial_score=review["adversarial_score"],
                adversarial_reasoning=review["adversarial_reasoning"],
                adversarial_approved=review["adversarial_approved"],
            )
            # The paper position opened before this review existed, so carry the
            # verdict onto it — otherwise its reviewer_verdict stays 'pending' forever
            # and the approved-vs-rejected cohorts stay empty.
            try:
                from sniper.persistence import intel_paper as _ip
                _ip.apply_reviewer_verdict(
                    conn, idea_id,
                    "approved" if review["adversarial_approved"] else "rejected",
                    review["adversarial_score"],
                )
            except Exception:
                logger.exception("could not attach verdict to paper position %s", idea_id)

            run_scores.append(review["adversarial_score"])
            reviewed += 1
            if review["adversarial_approved"]:
                approved += 1
                verdict = "APPROVED"
                obs_confidence = "medium"
            else:
                rejected += 1
                verdict = "REJECTED"
                obs_confidence = "high"
                new_rejections.append({
                    "idea_id": idea_id,
                    "market_title": (idea.get("market_title") or "")[:80],
                    "source_agent": source_agent,
                    "score": review["adversarial_score"],
                    "reason": review["adversarial_reasoning"][:120],
                    "rejected_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                })
            _write_observation_safe(
                obs_type="diagnosis",
                confidence=obs_confidence,
                content=(
                    f"Idea {idea_id} {verdict} by adversarial reviewer "
                    f"(score={review['adversarial_score']}, model={PRIMARY_MODEL}). "
                    f"{review['adversarial_reasoning']}"
                ),
            )
        except Exception as exc:
            failures.append(f"{idea_id}: {exc}")
            logger.exception("Adversarial review failed for idea %s", idea_id)

    # Brain section writes — only when something was actually reviewed
    if reviewed > 0:
        _update_brain_stats(
            run_reviewed=reviewed,
            run_approved=approved,
            run_scores=run_scores,
        )
        _update_rejection_patterns(new_rejections)

    summary = (
        f"Adversarial reviewer run: pending={len(ideas)}, reviewed={reviewed}, "
        f"approved={approved}, rejected={rejected}, failed={len(failures)}. "
        f"Model={PRIMARY_MODEL}, effort=codex-cli."
    )
    if failures:
        summary += f" Failures: {' | '.join(failures[:5])}"
    _write_observation_safe(
        obs_type="run_summary",
        confidence="high",
        content=summary,
    )
    return {
        "pending": len(ideas),
        "reviewed": reviewed,
        "approved": approved,
        "rejected": rejected,
        "failed": len(failures),
        "failures": failures,
        "model": PRIMARY_MODEL,
        "effort": "codex-cli",
    }


async def run(custom_prompt: str | None = None, verbose: bool = False) -> str:
    if verbose:
        logger.setLevel(logging.INFO)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        )
    result = review_pending_trade_ideas(extra_prompt=custom_prompt)
    return json.dumps(result, sort_keys=True)


if __name__ == "__main__":
    import anyio
    import argparse

    parser = argparse.ArgumentParser(description="Run the adversarial trade-idea reviewer")
    parser.add_argument(
        "--prompt", help="Additional operator instruction appended to the review prompt"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print(anyio.run(run, args.prompt, args.verbose))
