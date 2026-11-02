"""Candidate stage of the contradiction pass. It proposes; a session judges (spec 2.1.3, R3).

Beta's dry run (c8d909bf): cosine cannot separate same-domain facts (574 of 588 same-key pairs at
cos >= 0.75; 0 of 10 graded pairs at >= 0.80 were reversals). Candidates are therefore facts that
share an entity AND a key within the same kind GROUP (rule, trap, decision and settled compare with
each other). Keys are backticked code, knob names, and canonical ACTION keys. Each action key has
several phrasings, because the verbatim memory that endorsed subagents says "general-purpose Agent"
and "Multi-agent parallel dispatch", while the rule that reversed it says "the Agent tool" and
"subagents". Neither side carries a backtick or a knob (plan review finding 20)."""
from __future__ import annotations

import re

from bot.brain_graph import GROUPS, group_of

# canonical key -> lower-case phrases that mean it
ACTIONS = {
    "git stash": ("git stash",),
    "git add -a": ("git add -a", "git add ."),
    "git commit -a": ("git commit -a",),
    "git amend": ("--amend",),
    "git rebase": ("git rebase",),
    "pkill -f": ("pkill -f",),
    "pgrep -f": ("pgrep -f",),
    "agent tool": ("agent tool", "general-purpose agent", "general-purpose", "subagent", "multi-agent",
                   "worker fleet"),
    "pytest -n": ("pytest -n", "-n auto", "xdist"),
    "geckoterminal": ("geckoterminal",),
    "mainnet-beta": ("mainnet-beta",),
    "--with-backup": ("--with-backup",),
    ":memory:": (":memory:",),
    "utcnow": ("utcnow",),
}
AMBIGUOUS = {"agents", ".env", "restart", "deploy", "the runner"}
_BACKTICK = re.compile(r"`([^`\n]{2,80})`")
_KNOB = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]{2,}\b")
_NEG = re.compile(r"^\W*(?:never|do not|don't|must not|no longer|stop|avoid|refuse|forbid)\b", re.I)
_WORD = re.compile(r"[a-z0-9_]{3,}")


def action_keys(text: str) -> set[str]:
    low = (text or "").lower()
    return {key for key, phrases in ACTIONS.items() if any(p in low for p in phrases)}


def keys(text: str) -> set[str]:
    text = text or ""
    out = {m.strip().lower() for m in _BACKTICK.findall(text)}
    out |= set(_KNOB.findall(text))
    out |= action_keys(text)
    return {k for k in out if k not in AMBIGUOUS}


def polarity(text: str) -> int:
    """-1 when the first clause opens with a negation ("Never ...", "Do not ..."), else +1. Stored on
    the fact; the judge, not this sign, decides whether two facts disagree."""
    first = re.split(r"[.;:]", text or "", maxsplit=1)[0]
    return -1 if _NEG.search(first) else 1


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def candidates(store, statement: str, kind: str, entity_ids: list[str]) -> list[dict]:
    """Live facts on any of entity_ids, in the same kind group, that share a key with the statement;
    with no key, the five most word-similar (Jaccard >= 0.3). find_facts returns EVERY fact, oldest
    first, so an old fact a new one reverses is never cut off by a page."""
    pool = store.find_facts(entity_ids, kinds=GROUPS[group_of(kind)], live_only=True)
    mine = keys(statement)
    if mine:
        return [f for f in pool if keys(f["statement"]) & mine]
    scored = sorted(((_jaccard(statement, f["statement"]), f) for f in pool), key=lambda x: -x[0])
    return [f for score, f in scored[:5] if score >= 0.3]
