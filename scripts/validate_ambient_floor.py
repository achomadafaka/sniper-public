#!/usr/bin/env python3
"""Validate the ambient-brain relevance FLOOR against REAL first-prompts.

The on/off-topic similarity gap is thin (~0.04), so the floor default in
brain_ambient.py must be checked against real prompts, not clean synthetic ones.
This reads first-user-prompts from Claude Code transcripts (on-topic for this
project) and scores them alongside deliberately off-topic controls.

Usage: HF_HUB_OFFLINE=1 python3 scripts/validate_ambient_floor.py
"""
import glob
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        _k, _, _v = _l.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())

import bot.supabase_helper as sb  # noqa: E402

FLOOR = float(os.getenv("BRAIN_AMBIENT_FLOOR", "0.80"))
TRANSCRIPTS = str(Path.home() / ".claude" / "projects" / "-home-bigpapi-Desktop-Sniper" / "*.jsonl")

OFF_TOPIC = [
    "what is a good recipe for high altitude sourdough bread",
    "best hiking trails in the swiss alps this autumn",
    "how do i change a car tire safely on a highway",
]

FALLBACK_ONTOPIC = [
    "check on all of the work done so far on the brain",
    "can you look at what fable found and fix it",
    "keep going on the crypto5m maker engine",
    "how is the memecoin copy thing going",
    "review the wallet analyzer changes and tell me if they hold up",
    "spec out the ambient brain context feature and make sure it makes sense",
    "did the hyperliquid cascade detector ever fire",
    "sweep the dead config skills and decide keep vs delete",
    "what's the status of the second brain retrieval work",
    "look at the supabase advisors and confirm zero warnings",
]


def _first_user_prompt(path: str) -> str | None:
    try:
        for line in open(path):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "user":
                continue
            c = o.get("message", {}).get("content")
            text = c if isinstance(c, str) else ""
            text = text.strip()
            # genuine typed prompt: long enough, not a paste/system/command block
            if len(text) >= 40 and not text.startswith(("<", "#", "Caveat:", "[Request")):
                return text[:300]
    except Exception:
        pass
    return None


def _load_ontopic() -> list[str]:
    prompts, seen = [], set()
    for path in sorted(glob.glob(TRANSCRIPTS)):
        p = _first_user_prompt(path)
        if p and p[:60] not in seen:
            seen.add(p[:60])
            prompts.append(p)
    return prompts[:12] if prompts else FALLBACK_ONTOPIC


def _top(prompt: str) -> float | None:
    rows = sb.semantic_search_memories(prompt, limit=1)
    for r in rows:
        s = r.get("similarity")
        if isinstance(s, (int, float)):
            return float(s)
    return None


def main() -> None:
    on = [(p, _top(p)) for p in _load_ontopic()]
    off = [(p, _top(p)) for p in OFF_TOPIC]

    print(f"FLOOR under test = {FLOOR}\n")
    print("ON-TOPIC (real first-prompts):")
    for p, s in on:
        mark = "hit " if (s is not None and s >= FLOOR) else "MISS"
        print(f"  {s if s is None else round(s,3)}  [{mark}] {p[:70]}")
    print("\nOFF-TOPIC (controls — should be BELOW floor):")
    for p, s in off:
        mark = "leak" if (s is not None and s >= FLOOR) else "ok  "
        print(f"  {s if s is None else round(s,3)}  [{mark}] {p[:70]}")

    on_s = [s for _, s in on if s is not None]
    off_s = [s for _, s in off if s is not None]
    on_min = min(on_s) if on_s else None
    off_max = max(off_s) if off_s else None
    hit = sum(1 for s in on_s if s >= FLOOR)
    print("\n--- summary ---")
    print(f"on-topic:  min={on_min}  median={round(statistics.median(on_s),3) if on_s else None}  hit_rate@{FLOOR}={hit}/{len(on_s)}")
    print(f"off-topic: max={off_max}  leaks@{FLOOR}={sum(1 for s in off_s if s >= FLOOR)}/{len(off_s)}")
    if on_min is not None and off_max is not None:
        if on_min > off_max:
            mid = round((on_min + off_max) / 2, 3)
            print(f"SEPARABLE: on_min {on_min} > off_max {off_max} -> clean floor ≈ {mid}")
        else:
            print(f"OVERLAP: on_min {on_min} <= off_max {off_max} -> no clean floor; "
                  f"pick to favor precision (>= {round(off_max+0.005,3)}, accept some on-topic misses) "
                  f"or recall (<= {on_min}, accept some off-topic leaks)")


if __name__ == "__main__":
    main()
