"""Narrative guesser matcher (docs/superpowers/specs/2026-10-27-desk-narrative-guesser-design.md sections 3-5): pure functions, no I/O, frozen by VERSION_HASH.

A launch matches a guess only if the guess was written strictly before the launch was first seen and has not expired. A stale guesser means UNKNOWN (match_kind None),
never 'none': an analysis must treat None as censored, not as a control."""
import hashlib
import json
import re
import unicodedata

FILLER = ("coin", "inu", "ai", "token", "sol", "cto", "the")          # dropped only when another token remains
MIN_FUZZY_LEN = 5                                                    # edit distance 1 on a 4-letter key is not evidence
RANK = {"exact": 3, "fuzzy": 2, "theme_only": 1}
PRELOAD_KINDS = ("exact", "fuzzy")                                   # theme_only is recorded, never preloaded (Alpha 10-27)


def version_hash():
    return hashlib.sha256(json.dumps({"filler": list(FILLER), "min_fuzzy_len": MIN_FUZZY_LEN, "norm": "nfkc-lower-alnum-tokens-v1", "rank": RANK}, sort_keys=True).encode()).hexdigest()


VERSION_HASH = version_hash()


def normalise(s):
    if not s:
        return ""
    toks = [t for t in re.split(r"[^0-9a-z]+", unicodedata.normalize("NFKC", str(s)).lower()) if t]
    kept = [t for t in toks if t not in FILLER]
    return "".join(kept or toks)


def _edit_le1(a, b):
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    a, b = (a, b) if len(a) < len(b) else (b, a)
    return any(a == b[:i] + b[i + 1:] for i in range(len(b)))


def _kind(keys, guess, theme_id):
    """-> (kind, matched_key, edit_distance); the key is the launch-side string that matched."""
    gkeys = {k for k in (normalise(x) for x in list(guess.get("tickers") or []) + list(guess.get("names") or [])) if k}
    common = sorted(keys & gkeys)
    if common:
        return "exact", common[0], 0
    for a in sorted(keys):
        for b in sorted(gkeys):
            if len(a) >= MIN_FUZZY_LEN and len(b) >= MIN_FUZZY_LEN and _edit_le1(a, b):
                return "fuzzy", a, 1
    if theme_id and guess.get("theme_id") == theme_id:
        return "theme_only", None, None
    return None, None, None


def match(launch, guesses, *, guesser_state):
    """-> {'match_kind': exact|fuzzy|theme_only|none|None, 'guess_id': id|None}. None = unknown (guesser not ok)."""
    if guesser_state != "ok":
        return {"match_kind": None, "guess_id": None, "matched_key": None, "edit_distance": None}
    seen = float(launch["first_seen"])
    keys = {k for k in (normalise(launch.get("name")), normalise(launch.get("symbol"))) if k}
    best = None
    for g in guesses:
        if not (float(g["known_at"]) < seen < float(g["expires_at"])):
            continue
        kind, key, dist = _kind(keys, g, launch.get("theme_id"))
        if kind is None:
            continue
        rank = (-RANK[kind], float(g["known_at"]), str(g["id"]))
        if best is None or rank < best[0]:
            best = (rank, kind, g["id"], key, dist)
    if best is None:
        return {"match_kind": "none", "guess_id": None, "matched_key": None, "edit_distance": None}
    return {"match_kind": best[1], "guess_id": best[2], "matched_key": best[3], "edit_distance": best[4]}


def preload_budget(rows, *, now, cap_per_h):
    """Preloads left this hour; rows are dicts with match_kind and at (epoch s). Only exact/fuzzy count."""
    used = sum(1 for r in rows if r.get("match_kind") in PRELOAD_KINDS and float(r["at"]) > now - 3600.0)
    return max(0, int(cap_per_h) - used)
