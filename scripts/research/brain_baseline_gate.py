#!/usr/bin/env python3
"""Brain staleness baseline under the GATE's own evidence checks (plan Task 12, spec R5). Read-only.

The dry run (brain_migration_dryrun.py, 8b59a6fd) measured 9.1% of checkable active memories failing
a looser check. This re-measures the same memories with bot.brain_graph.evidence.check, the check a
new fact's evidence must pass, so the baseline and the gate agree on what "broken" means.

References are extracted by the dry run's own extractor and filters (entities + check_memory, loaded
by file path), then each reference goes to evidence.check as a typed item:
  sha    hex tokens that are commit objects in this clone. A hex token that is no commit object
         (an attempt id, a key hash, a commit never fetched) cannot be told apart from text; it is
         counted as `no_commit` and never fed to the gate, which would call it broken.
  path   repo paths, minus the dry run's placeholders (`<`, `*`, `...`).
  knob   ENV-style names whose two-segment prefix is a known .env family.
Columns are not extracted (the dry run has no column extractor), so no column row is measured.

    python3 scripts/research/brain_baseline_gate.py                 # live read of memories
    python3 scripts/research/brain_baseline_gate.py --snapshot m.json
    python3 scripts/research/brain_baseline_gate.py --selftest
"""
import argparse
import collections
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from bot.brain_graph import evidence, live  # noqa: E402

TYPES = ("sha", "path", "knob")
VERDICTS = ("ok", "unchecked", "pending_push", "broken")


def _dryrun():
    spec = importlib.util.spec_from_file_location("brain_migration_dryrun",
                                                  REPO / "scripts/research/brain_migration_dryrun.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DRY = _dryrun()
EMPTY_SETS = dict(commits={}, main_commits=set(), tree=set(), tree_names_archive=set(),
                  disk_exists=lambda p: False, code_tokens=set(), env_names=set(), env_prefixes=set())


def refs(text, sets):
    """{type: sorted tokens} the dry run's extractor and filters find in one memory's text."""
    res = DRY.check_memory(DRY.entities(text, ()), **sets)
    return {t: sorted({tok for tok, _ in res.get(t, ())}) for t in TYPES}


def measure(memories, sets, repo, *, check=evidence.check, fetched=True):
    """Counts of gate verdicts per evidence type (one per memory x reference), plus the memories that
    carry at least one gate-checkable reference and those with at least one `broken` one."""
    cache, commit = {}, {}

    def is_commit(tok):
        if tok not in commit:
            commit[tok] = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{tok}^{{commit}}"],
                                         capture_output=True).returncode == 0
        return commit[tok]

    per = {t: collections.Counter() for t in TYPES}
    groups = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    mem_type = {t: collections.Counter() for t in TYPES}     # memories per type: with refs, with broken
    for m in memories:
        text = (m.get("description") or "") + "\n" + (m.get("content") or "")
        group = "session_mirror" if (m.get("name") or "").startswith("session/") else "direct"
        verdicts = []
        for t, toks in refs(text, sets).items():
            mine = []
            for tok in toks:
                if t == "sha" and not is_commit(tok):
                    per[t]["no_commit"] += 1
                    continue
                if (t, tok) not in cache:
                    cache[(t, tok)] = check({"type": t, "ref": tok}, repo, fetched=fetched)
                v, why = cache[(t, tok)]
                per[t][v] += 1
                mine.append(v)
                if v == "broken" and len(examples[t]) < 6:
                    examples[t].append(f"{m.get('name')}: {why}")
            mem_type[t]["with_refs"] += bool(mine)
            mem_type[t]["broken"] += "broken" in mine
            verdicts += mine
        groups[group]["active"] += 1
        groups[group]["checkable"] += bool(verdicts)
        groups[group]["broken"] += "broken" in verdicts
    total = collections.Counter()
    for c in groups.values():
        total.update(c)
    unique = {t: collections.Counter(v for (tt, _), (v, _) in cache.items() if tt == t) for t in TYPES}
    return {"per_type": per, "groups": dict(groups), "total": total, "examples": dict(examples),
            "unique_refs": len(cache), "mem_type": mem_type, "unique": unique}


def render(r):
    lines = ["| type | " + " | ".join(VERDICTS) + " | no_commit |", "|---|" + "---:|" * (len(VERDICTS) + 1)]
    for t in TYPES:
        c = r["per_type"][t]
        lines.append(f"| {t} | " + " | ".join(str(c[v]) for v in VERDICTS)
                     + f" | {c['no_commit'] if t == 'sha' else '-'} |")
    lines.append("")
    for t in TYPES:
        mt, u = r["mem_type"][t], r["unique"][t]
        lines.append(f"  {t}: memories with gate-checkable refs {mt['with_refs']}, with >=1 broken {mt['broken']}; "
                     "unique refs " + ", ".join(f"{v} {u[v]}" for v in VERDICTS))
    tot = r["total"]
    lines.append("")
    lines.append(f"active {tot['active']}; gate-checkable {tot['checkable']}; with >=1 broken {tot['broken']} "
                 f"({100.0 * tot['broken'] / max(tot['checkable'], 1):.1f}% of checkable, "
                 f"{100.0 * tot['broken'] / max(tot['active'], 1):.1f}% of active); unique refs checked "
                 f"{r['unique_refs']}")
    for g, c in sorted(r["groups"].items()):
        lines.append(f"  {g:15s} active {c['active']:4d} checkable {c['checkable']:4d} broken {c['broken']:4d}")
    for t, ex in sorted(r["examples"].items()):
        lines += [f"  e.g. {t}: {e[:160]}" for e in ex]
    return "\n".join(lines)


def selftest():
    mems = [{"name": "a", "content": "Merged as b70ddfb0 on main."},
            {"name": "b", "content": "See scripts/no_such_selftest_script.py for the old path."},
            {"name": "c", "content": "Capture went to data/no_such_selftest_capture.json."},
            {"name": "session/d", "content": "Attempt a1b2c3d4e5f6 refused."}]      # hex, no commit object
    r = measure(mems, EMPTY_SETS, live.state_root(REPO))
    got = {v: sum(r["per_type"][t][v] for t in TYPES) for v in VERDICTS}
    assert got == {"ok": 1, "unchecked": 1, "pending_push": 0, "broken": 1}, got
    assert r["per_type"]["sha"]["no_commit"] == 1, r["per_type"]["sha"]
    assert r["total"]["checkable"] == 3 and r["total"]["broken"] == 1, r["total"]
    assert r["groups"]["session_mirror"] == {"active": 1, "checkable": 0, "broken": 0}, r["groups"]
    assert r["mem_type"]["path"] == {"with_refs": 2, "broken": 1}, r["mem_type"]
    assert r["unique"]["sha"] == {"ok": 1}, r["unique"]
    print("selftest ok")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", help="memories JSON from an earlier fetch")
    ap.add_argument("--save", help="write the fetched memories JSON here (for --snapshot reruns)")
    ap.add_argument("--selftest", action="store_true")
    ns = ap.parse_args(argv)
    if ns.selftest:
        return selftest()
    os.nice(19)
    state = live.state_root(REPO)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(state))
    DRY.REPO = state                      # the dry run's git, .env names and disk checks read the main checkout
    fetched = evidence.fetch_once(state)
    memories = json.load(open(ns.snapshot)) if ns.snapshot else DRY.fetch_memories()
    if ns.save:
        Path(ns.save).write_text(json.dumps(memories))
    active = [m for m in memories if m.get("status") == "active"]
    print(f"memories fetched {len(memories)}, active {len(active)}; fetch_once {fetched}")
    print(render(measure(active, DRY.repo_sets(), state, fetched=fetched)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
