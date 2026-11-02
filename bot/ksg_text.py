"""Shared text utilities for KSG (Knowledge Sync Gate) validators.

Two helpers, both pure (no IO, no DB, no external deps):

  strip_fenced_code(text)             — remove fenced/indented code blocks
                                        per CommonMark, preserving line count.
  phrase_has_preceding_negation(...)  — look-back token window check for
                                        negation context.

These are used by both `scripts/knowledge_sync.py` (memory entry
validation) and `scripts/validate_knowledge_sync.py` (advisory
file/PR-body checks). Keep behavior identical across both consumers by
importing from here, not by duplicating regexes.

Design references:
  * CommonMark §4.5 Fenced code blocks
    https://spec.commonmark.org/0.31.2/#fenced-code-blocks
  * The negation token window mirrors how lint tools like Vale handle
    "occurrence" rules with surrounding context.
"""
from __future__ import annotations

import re
from typing import Iterable


# ----------------------------------------------------------------------
# Fenced code block stripping (CommonMark-correct for common cases)
# ----------------------------------------------------------------------

# Matches 0–3 leading spaces, then 3+ backticks or 3+ tildes.
# Capture group 2 is the fence string itself (e.g. "```", "~~~~", "```python")
_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


def strip_fenced_code(text: str) -> str:
    """Remove fenced code blocks from `text`, preserving line count.

    Replaces fenced lines (including the fence delimiters themselves)
    with empty lines so that downstream regex / substring searches over
    the result do not match inside code blocks. Line numbers are
    preserved — useful when reporters need to cite line positions.

    CommonMark spec details we honor:
      * Opening fence: ≥3 backticks or tildes, optionally preceded by
        up to 3 spaces, optionally followed by an info string.
      * Closing fence: same character as opening, length ≥ opening's
        length. Cannot have content on the closing line after the
        fence (per CommonMark §4.5).
      * Mixed fence characters do not close each other (``` cannot
        close ~~~ and vice versa).

    What we deliberately do NOT handle (acceptable for KSG use case):
      * Indented (4-space) code blocks — rare in PR bodies; treating
        them as prose introduces fewer surprises than treating them
        as code, given that operators write Knowledge Sync blocks in
        prose-heavy markdown.
      * Nested fence-in-fence with different lengths inside content
        (we only track the outer fence's character + length).
      * HTML-style code (`<code>`) — not used in PR bodies.

    Args:
        text: raw markdown string.

    Returns:
        the same string with fenced code regions blanked out. Returns
        an empty string if `text` is empty.
    """
    if not text:
        return ""

    out: list[str] = []
    fence_char: str | None = None  # None when not in a fence
    fence_len = 0

    for line in text.split("\n"):
        m = _FENCE_OPEN.match(line)
        if m:
            fence_str = m.group(2)
            ch = fence_str[0]
            ln = len(fence_str)
            if fence_char is None:
                # Opening a new fence — blank the fence line and start
                # tracking.
                fence_char = ch
                fence_len = ln
                out.append("")
                continue
            elif ch == fence_char and ln >= fence_len:
                # Valid closing fence (same char, length ≥ opening).
                fence_char = None
                fence_len = 0
                out.append("")
                continue
            # else: fence-like line inside an open fence of different
            # character (e.g. ``` line inside a ~~~ fence). Treat as
            # content (and therefore blank because we're inside a fence).

        if fence_char is not None:
            out.append("")  # blank out content inside a fence
        else:
            out.append(line)

    return "\n".join(out)


# ----------------------------------------------------------------------
# Negation context detection
# ----------------------------------------------------------------------

# Single-token negation markers. Matched against the last N tokens before
# a phrase. Case-insensitive (input is lowercased before comparison).
# Stripped of common trailing punctuation before lookup.
NEGATION_TOKENS: frozenset[str] = frozenset({
    # English single-word negations
    "no", "not", "never", "neither", "nor", "none",
    # Contracted negations
    "don't", "doesn't", "didn't", "won't", "wouldn't",
    "shouldn't", "couldn't", "isn't", "aren't", "wasn't", "weren't",
    "cannot", "can't",
    # Deontic / proscriptive verbs in any common inflection
    "forbid", "forbids", "forbade", "forbidden",
    "avoid", "avoids", "avoided", "avoiding",
    "refuse", "refuses", "refused", "refusing",
    "reject", "rejects", "rejected", "rejecting",
    "disallow", "disallows", "disallowed", "disallowing",
    "prohibit", "prohibits", "prohibited", "prohibiting",
    # Sentinel prepositions / connectors that signal counterfactual
    "without",
})

# Multi-word negation phrases that may appear in the look-back window.
# Checked as substrings against the joined look-back text. Lowercase.
NEGATION_MULTIWORD: tuple[str, ...] = (
    "do not", "does not", "did not", "will not",
    "must not", "should not", "would not", "could not", "shall not",
    "is not", "are not", "was not", "were not", "has not", "have not",
    "no longer", "no copy", "no recommendation",
    "not a copy", "not a buy", "not a sell", "not a trigger",
)

# Token-window size — how many tokens before the matched phrase we scan
# for negation. 6 is large enough to catch "It is recommended to never
# copy this wallet" (5 tokens between "recommended" and "copy") and small
# enough that distant earlier sentences don't pollute. Operators who want
# to write longer disclaimers should put them in a comment rather than
# the entry's content.
NEGATION_WINDOW_TOKENS = 6

# Punctuation to strip when token-matching.
_PUNCT_STRIP = ".,;:!?\"'()[]{}<>"


# Regex used to find sentence boundaries. A sentence ends at `.`/`!`/`?`
# followed by whitespace, OR at a paragraph break (two newlines), OR at
# a markdown line break.
_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s)|\n\s*\n")


def phrase_has_preceding_negation(
    text_lower: str,
    match_start: int,
    *,
    extra_token_negations: Iterable[str] = (),
    extra_multiword_negations: Iterable[str] = (),
) -> bool:
    """Return True if the current sentence containing `match_start`
    carries a negation signal earlier than the matched phrase.

    The text MUST be already lowercased — callers are expected to lowercase
    once and pass the same string into multiple scans.

    Algorithm — sentence-scoped lookback:
      1. Find the start of the sentence containing `match_start`. A
         sentence boundary is `.`/`!`/`?` followed by whitespace, a
         paragraph break (two newlines), or the start of the text.
      2. Take the substring from sentence-start to match_start ("sentence
         prefix") and check it against:
           a. multi-word negation phrases (substring search);
           b. single-token negations on every word in the prefix.

    Why sentence-scoped rather than token-window-scoped:
      Doctrine prose often reads "Do not frame analyzer output as copy-
      trading advice, deployable triggers, expected PnL, sizing, ..." —
      the negation is 8-12 tokens before the matched phrase, far beyond
      any reasonable fixed token window. The sentence boundary correctly
      captures the entire scope a negation should propagate over.

    Why not paragraph-scoped:
      A paragraph can contain "Copy this wallet. Do not copy this wallet."
      — first sentence is a positive assertion, second is negated.
      Paragraph-scoped would incorrectly skip both.

    Args:
        text_lower: the source text, already lowercased.
        match_start: char offset where the phrase match begins.
        extra_token_negations: additional single-token negations.
        extra_multiword_negations: additional multi-word negations.

    Returns:
        True if a negation context was detected.
    """
    if match_start <= 0:
        return False

    # Walk forward through sentence boundaries up to match_start; the
    # last boundary BEFORE match_start gives us the current sentence's
    # start (boundary char index + length of the boundary match).
    sentence_start = 0
    for bm in _SENTENCE_BOUNDARY.finditer(text_lower, 0, match_start):
        sentence_start = bm.end()

    prefix = text_lower[sentence_start:match_start]
    if not prefix.strip():
        return False

    # Multi-word negation: substring search on the joined look-back.
    multiword_pool: list[str] = list(NEGATION_MULTIWORD) + list(extra_multiword_negations)
    for multi in multiword_pool:
        if multi in prefix:
            return True

    # Single-token negation: check every token in the sentence prefix.
    token_pool: set[str] = set(NEGATION_TOKENS) | set(extra_token_negations)
    for tok in prefix.split():
        cleaned = tok.strip(_PUNCT_STRIP)
        if cleaned in token_pool:
            return True

    return False


# ----------------------------------------------------------------------
# Combined: find forbidden phrases that are real assertions
# ----------------------------------------------------------------------

def find_forbidden_assertions(
    text: str,
    forbidden_phrases: Iterable[str],
    *,
    strip_fences: bool = True,
    check_negation: bool = True,
) -> list[dict]:
    """Find occurrences of any forbidden phrase that are positive
    assertions (not negated, not inside code fences).

    Returns a list of {phrase, position, context} dicts — one per real
    assertion. Empty list = no real assertions found.
    """
    if not text:
        return []

    scan_text = strip_fenced_code(text) if strip_fences else text
    scan_lower = scan_text.lower()
    results: list[dict] = []

    for phrase in forbidden_phrases:
        phrase_lower = phrase.lower()
        start = 0
        while True:
            idx = scan_lower.find(phrase_lower, start)
            if idx < 0:
                break
            if check_negation and phrase_has_preceding_negation(scan_lower, idx):
                start = idx + len(phrase_lower)
                continue
            # Surrounding context for the report (about 80 chars total)
            ctx_start = max(0, idx - 30)
            ctx_end = min(len(scan_text), idx + len(phrase) + 30)
            results.append({
                "phrase": phrase,
                "position": idx,
                "context": scan_text[ctx_start:ctx_end].replace("\n", " ").strip(),
            })
            start = idx + len(phrase_lower)

    return results
