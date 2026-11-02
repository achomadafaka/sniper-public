"""Shared content normalization helpers for Intel / Finance delivery.

These helpers are intentionally conservative. They normalize verbose titles and
unstable section headings into compact, UI-friendly forms without inventing new
claims or changing the body content.
"""

from __future__ import annotations

import re
from typing import Any

_MONTH_TAIL_RE = re.compile(
    r"\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}(?:,?\s+\d{4})?$",
    re.IGNORECASE,
)
_CYCLE_TAIL_RE = re.compile(r"\s+(?:day|week|month|quarter)\s+\d+$", re.IGNORECASE)
_SCORE_TAIL_RE = re.compile(r"\s+[+-]\d+(?:\.\d+)?$", re.IGNORECASE)
_NUMERIC_TOKEN_RE = re.compile(r"\b\$?\d+(?:\.\d+)?(?:%|k|m|b)?\b", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"\s*(?::|\s-\s)\s*")


def collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _looks_like_detail_suffix(text: str) -> bool:
    suffix = collapse_spaces(text)
    if not suffix:
        return False
    token_count = len(suffix.split())
    numeric_tokens = len(_NUMERIC_TOKEN_RE.findall(suffix))
    lowered = suffix.lower()
    if _MONTH_TAIL_RE.search(suffix) or _SCORE_TAIL_RE.search(suffix):
        return True
    if any(marker in lowered for marker in ("highest since", "lowest since", "briefly", "update", "airstrike")):
        return True
    if numeric_tokens:
        return True
    return token_count >= 5


def normalize_event_title(title: str) -> str:
    """Make world-event titles scannable without inventing new semantics."""
    original = collapse_spaces(title)
    if not original:
        return ""

    normalized = (
        original.replace("—", " - ")
        .replace("–", " - ")
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
    )
    normalized = collapse_spaces(normalized).strip(" -:;,.\t\n\r")

    parts = _SEPARATOR_RE.split(normalized, maxsplit=1)
    if len(parts) == 2 and _looks_like_detail_suffix(parts[1]):
        normalized = parts[0].strip()

    normalized = _CYCLE_TAIL_RE.sub("", normalized)
    normalized = _MONTH_TAIL_RE.sub("", normalized)
    normalized = _SCORE_TAIL_RE.sub("", normalized)
    normalized = collapse_spaces(normalized).strip(" -:;,.\t\n\r")
    return normalized or original


def normalize_world_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a world-event payload with a compact, UI-friendly title.

    The original title is preserved in ``raw_title`` when the normalized title
    differs so read-side consumers can compact legacy content without losing the
    source wording.
    """
    normalized = dict(event or {})
    title = collapse_spaces(str(normalized.get("title") or ""))
    if not title:
        return normalized

    compact = normalize_event_title(title)
    if compact and compact != title:
        normalized.setdefault("raw_title", title)
        normalized["title"] = compact
    return normalized


def normalize_heading_key(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", normalized).strip()


def canonical_report_heading(name: str, heading: str) -> str | None:
    key = normalize_heading_key(heading)
    if not key:
        return None

    def has(*terms: str) -> bool:
        return all(term in key for term in terms)

    if name == "markets/state":
        if has("executive", "summary") or key == "summary" or has("what", "changed"):
            return "EXECUTIVE SUMMARY: What changed"
        if "macro" in key or has("rates", "inflation"):
            return "MACRO: Rates and inflation"
        if "crypto" in key or has("flows", "regulation"):
            return "CRYPTO: Flows and regulation"
        if "equities" in key or has("risk", "breadth"):
            return "EQUITIES: Risk and breadth"
        if "cross domain" in key or has("geo", "macro"):
            return "CROSS-DOMAIN: Geo to macro"
        if has("market", "implications") or has("position", "impact"):
            return "MARKET IMPLICATIONS: Position impact"
        if has("source", "credibility") or has("source", "audit"):
            return "SOURCE CREDIBILITY: Source audit"
        if key == "ach" or has("competing", "hypotheses"):
            return "ACH: Competing hypotheses"

    if name == "world_narrative":
        if has("executive", "summary") or key == "summary" or has("what", "changed"):
            return "EXECUTIVE SUMMARY: What changed"
        if "geopolitics" in key or has("core", "shifts"):
            return "GEOPOLITICS: Core shifts"
        if "politics" in key or has("policy", "moves"):
            return "POLITICS: Policy moves"
        if "finance" in key or has("market", "spillover"):
            return "FINANCE: Market spillover"
        if "sports" in key or has("event", "watch"):
            return "SPORTS: Event watch"
        if "crypto" in key or has("risk", "signals"):
            return "CRYPTO: Risk signals"
        if has("market", "implications") or has("position", "impact"):
            return "MARKET IMPLICATIONS: Position impact"
        if has("source", "credibility") or has("source", "audit"):
            return "SOURCE CREDIBILITY: Source audit"
        if key == "ach" or has("competing", "hypotheses"):
            return "ACH: Competing hypotheses"
        if "diversity" in key or has("coverage", "balance"):
            return "DIVERSITY: Coverage balance"

    return None


def normalize_memory_content(type: str, name: str, content: str) -> str:
    if type not in {"knowledge", "reference"} or name not in {"markets/state", "world_narrative"} or not content:
        return content

    normalized_lines: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^(#{2,6})\s*(.+?)\s*$", line)
        if not match:
            normalized_lines.append(line)
            continue
        hashes, heading = match.groups()
        canonical = canonical_report_heading(name, heading)
        normalized_lines.append(f"{hashes} {canonical}" if canonical else line)
    return "\n".join(normalized_lines)
