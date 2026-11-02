"""Hybrid retrieval: full-text + vector, fused by reciprocal rank, then a one-hop expansion to the
newest facts sharing an entity with the top hit."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from bot.brain_graph.crew_filter import CrewFilter


def _embed(q):
    """The query embedding, or None. The model loads lazily, here only, at nice 19 with 4 threads
    (bot/embeddings.py), because fastembed defaults to every core of the funded host."""
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        from bot.embeddings import embed_query, is_available
        return embed_query(q) if is_available() else None
    except Exception:
        return None


def _chore(f):
    return f.get("kind") == "task" and f.get("author") == "import"


# Age = valid_from (the event date; imports keep 2026-03..), else recorded_at (mostly the 2026-09-29 import day).
# Vector search returns the same ~30 short generic rules at similarity 0.84-0.87 for ANY query (measured 2026-10-30), so a
# vector-only hit is a tie-breaker, not evidence: it must rank below every keyword hit. Newer facts get up to +30% so a
# decision made today is not buried one keyword rank behind a March finding; unknown age gets no boost.
W_KEYWORD, W_VECTOR = 1.0, 0.25
RECENCY_BOOST, RECENCY_HALF_LIFE_DAYS = 0.3, 30.0
VECTOR_ONLY_CAP = 5                            # a hit found ONLY by vector search: the 30 nearest are generic whatever the query, so show 5
W_SHA = 4.0                                    # a commit named in the query is the strongest key there is
_SHA = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,40}(?![0-9A-Za-z])")


def _shas(q):
    """Commit-like tokens: 7-40 hex chars with at least one digit AND one letter, so 'defaced' and 1793353688 are not shas."""
    out = []
    for t in _SHA.findall(q):
        t = t.lower()
        if any(c.isdigit() for c in t) and any(c in "abcdef" for c in t) and t not in out:
            out.append(t)
    return out[:3]


def _recency(f, now):
    try:
        at = datetime.fromisoformat(str(f.get("valid_from") or f["recorded_at"]).replace("Z", "+00:00"))
        if at.tzinfo is None:
            return 0.0
        return 0.5 ** (max(0.0, (now - at).total_seconds()) / 86400.0 / RECENCY_HALF_LIFE_DAYS)
    except (KeyError, TypeError, ValueError):
        return 0.0


def ask(store, q, *, k=60, limit=15, embed=True, now=None, stats=None):
    guard = CrewFilter(store)
    now = now or datetime.now(timezone.utc)
    ranked, lexical = {}, set()
    sources = [(W_SHA, store.facts_by_sha(t, 20)) for t in _shas(q)]
    for weight, hits in sources + [(W_KEYWORD, store.search_facts(q, 30)), (W_VECTOR, store.match_facts(_embed(q) if embed else None, 30))]:
        for rank, f in enumerate(guard.facts(hits)):
            ranked.setdefault(f["id"], [f, 0.0])
            ranked[f["id"]][1] += weight / (k + rank + 1)
            if weight != W_VECTOR:
                lexical.add(f["id"])
    vector_only = sorted((i for i in ranked if i not in lexical), key=lambda i: -ranked[i][1])
    excluded = set(vector_only[VECTOR_ONLY_CAP:])
    for i in excluded:
        del ranked[i]
    if stats is not None:
        stats.update(vector_only_seen=len(vector_only), vector_only_cut=max(0, len(vector_only) - VECTOR_ONLY_CAP),
                     vector_only_cap=VECTOR_ONLY_CAP)
    for entry in ranked.values():
        entry[1] *= 1.0 + RECENCY_BOOST * _recency(entry[0], now)
    # Import-judging chores ("Judge import batch ...") match every topic and crowd out the facts.
    top = [f for f, _ in sorted(ranked.values(), key=lambda x: -x[1]) if not _chore(f)]
    if top:
        seen = {f["id"] for f in top}
        near = guard.facts(store.find_facts(top[0]["entity_ids"]))[::-1]          # find_facts is oldest first
        top += [f for f in near if f["id"] not in seen and f["id"] not in excluded and not _chore(f)][:5]
    return top[:limit]
