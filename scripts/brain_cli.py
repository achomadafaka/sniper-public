#!/usr/bin/env python3
"""The brain CLI (docs/superpowers/specs/2026-09-28-brain-knowledge-graph-design.md).

Named brain_cli.py because the repo root already has a legacy `brain.py` module, and tests import
both (plan review finding 6).

    brain_cli.py boot                  role-free boot (what SessionStart can show)
    brain_cli.py boot alpha            role boot: role, live state, ranked facts, verify quota
    brain_cli.py live                  derived live state only
    brain_cli.py write --role alpha --kind rule --entity tool:git --section crew \\
                       --evidence path:CLAUDE.md "Never `git stash` in the shared checkout."
    brain_cli.py verify <id> verified --role beta --output-file /path/rerun.txt
    brain_cli.py retract <id> --role alpha --reason-kind misfiled --reason "duplicate task"
    brain_cli.py handover alpha        derive live state, record it, print the restart note
Exit codes: 0 ok, 2 refused, 3 needs judgement, 4 brain unavailable (a write is queued).

State (.env, data/brain_snapshot.json, data/brain_outbox.jsonl) lives in the STATE ROOT, meaning
SNIPER_STATE_ROOT or the main checkout, even when this runs from a worktree. .env and .fomo.env
are loaded by the runner's own loader (sniper.util.envfiles: first occurrence wins, quotes
stripped) inside main(), never at import, so importing this module (a test does) leaves
os.environ alone. The store refuses to start without the service key, because an anon key reads
an empty brain."""
import argparse
import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.brain_graph import live  # noqa: E402

STATE = live.state_root(ROOT)

from bot.brain_graph import api, views  # noqa: E402
from bot.brain_graph.crew_filter import CrewFilter  # noqa: E402
from bot.brain_graph.evidence import fetch_once  # noqa: E402
from bot.brain_graph.gate import secret_values_from_env, secret_values_from_mapping  # noqa: E402
from bot.brain_graph.store import BrainRefused, BrainUnavailable, RestStore, VersionConflict  # noqa: E402

SNAPSHOT = STATE / "data" / "brain_snapshot.json"
OUTBOX = STATE / "data" / "brain_outbox.jsonl"
EXIT = {"written": 0, "noop": 0, "refused": 2, "needs_judgement": 3, "queued": 4}
# Not the hooks' 3 s cap (spec 3.1): a session runs this by hand and can wait. On 2026-10-02 08:03:47Z
# Supabase answered every call in 0.5-1.9 s and a 3 s CLI budget printed the snapshot instead.
BOOT_BUDGET_S = 10


class BootTimeout(BaseException):
    """The boot budget ran out. A BaseException on purpose: live.guard and the store catch Exception,
    so an Exception raised by the alarm inside them was swallowed and boot ran on (review V2)."""


def _load_env():
    from sniper.util.envfiles import load_env_files
    load_env_files(str(STATE))


def build_parser():
    p = argparse.ArgumentParser(prog="brain_cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("boot", help="boot (role-free without ROLE)")
    b.add_argument("role", nargs="?")
    sub.add_parser("live", help="derived live state only")
    a = sub.add_parser("ask", help="search facts")
    a.add_argument("q")
    e = sub.add_parser("entity", help="facts on one entity")
    e.add_argument("type")
    e.add_argument("name")
    w = sub.add_parser("write", help="write one fact through the gate")
    w.add_argument("--role", required=True)
    w.add_argument("--kind", required=True)
    w.add_argument("--operator-order", action="store_true", help="link a decision/settled fact to actor:operator; authority remains unverified")
    w.add_argument("--entity", action="append", default=[], help="TYPE:NAME")
    w.add_argument("--section", help="section slug for any NEW entity (required when one is new)")
    w.add_argument("--evidence", action="append", default=[],
                   help="TYPE:REF, TYPE one of sha, path, knob, column, doc, query, episode; a path that "
                        "exists only on an unmerged branch passes when the same fact cites that sha")
    w.add_argument("--episode", action="append", default=[])
    w.add_argument("--detail")
    w.add_argument("--claim-scope", choices=["operational", "actual_execution", "relationship", "normative"],
                   help="what the claim is about; an operator decision or a config fact is operational, which "
                        "the memecoin performance-claim gate does not apply to (a result claim still needs a study)")
    w.add_argument("--judge", action="append", default=[],
                   help="ID=duplicate|refines|supersedes|contradicts|unrelated")
    w.add_argument("statement")
    v = sub.add_parser("verify", help="peer-verify a fact")
    v.add_argument("id")
    v.add_argument("verdict", choices=["verified", "verified_partial", "refuted"])
    v.add_argument("--role", required=True)
    v.add_argument("--output-file")
    v.add_argument("--note", default="")
    rt = sub.add_parser("retract", help="withdraw a misfiled or obsolete fact (finished tasks use complete)")
    rt.add_argument("id")
    rt.add_argument("--role", required=True)
    rt.add_argument("--reason", required=True)
    rt.add_argument("--reason-kind", choices=("obsolete", "misfiled"), required=True)
    j = sub.add_parser("judge", help="atomically resolve a candidate pair (newer, older)")
    j.add_argument("newer")
    j.add_argument("older")
    j.add_argument("verdict", choices=["duplicate", "refines", "supersedes", "contradicts", "unrelated"])
    j.add_argument("--role", required=True)
    q = sub.add_parser("queue", help="facts to verify or judge")
    q.add_argument("--role", required=True)
    q.add_argument("--author", help="only this author's unverified facts")
    ep = sub.add_parser("episode", help="append a provenance episode")
    ep.add_argument("--role", required=True)
    ep.add_argument("--kind", required=True)
    ep.add_argument("--occurred-at", help="ISO time the event happened; omit when unknown")
    ep.add_argument("body")
    h = sub.add_parser("handover", help="record and print the restart note")
    h.add_argument("role")
    h.add_argument("--inventory", help="host inventory JSON; CLI fact checks do not authenticate or seal it")
    rw = sub.add_parser("record-work", help="record a durable finding for one delivered unit")
    rw.add_argument("--role", required=True)
    choice = rw.add_mutually_exclusive_group(required=True)
    choice.add_argument("--merge")
    choice.add_argument("--no-integration", action="store_true")
    rw.add_argument("--artifact", help="episode:UUID or relative/path@SHA for no-integration delivery")
    rw.add_argument("--task")
    rw.add_argument("--kind", choices=("finding", "decision"), default="finding")
    rw.add_argument("--validation", required=True)
    rw.add_argument("--judge", action="append", default=[])
    rw.add_argument("statement")
    done = sub.add_parser("complete", help="close a task only after a durable completion fact")
    done.add_argument("id")
    done.add_argument("--role", required=True)
    done.add_argument("--completion-fact", required=True)
    done.add_argument("--reason", required=True)
    done.add_argument("--no-integration", action="store_true")
    coverage = sub.add_parser("work-coverage", help="bounded first-parent integration-unit coverage")
    coverage.add_argument("--base", required=True)
    coverage.add_argument("--head", required=True)
    audit = sub.add_parser("closure-audit", help="bounded diagnostic typed task-closure counts")
    audit.add_argument("--since", required=True)
    audit.add_argument("--until", required=True)
    sub.add_parser("flush", help="replay the offline outbox")
    return p


def _pair(s, sep):
    k, _, v = s.partition(sep)
    if not k.strip() or not v.strip():
        raise SystemExit(f"bad value {s!r}; expected X{sep}Y")
    return k.strip(), v.strip()


def draft_from_args(ns):
    return {"kind": ns.kind, "statement": ns.statement, "author": ns.role, "detail": ns.detail,
            "section": ns.section, "claim_scope": ns.claim_scope, "operator_order": getattr(ns, "operator_order", False),
            "entities": [dict(zip(("type", "name"), _pair(x, ":"))) for x in ns.entity],
            "evidence": [dict(zip(("type", "ref"), _pair(x, ":"))) for x in ns.evidence],
            "episode_ids": list(ns.episode), "author_session": (os.getenv("CLAUDE_SESSION_ID") or os.getenv("CODEX_THREAD_ID")),
            "judgements": dict(_pair(x, "=") for x in ns.judge)}


def _alarm(seconds):
    def raise_timeout(*_):
        raise BootTimeout("boot budget %ss" % seconds)
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.alarm(seconds)


def main(argv=None, *, store=None, outbox=None, snapshot=None, fetch=None):
    ns = build_parser().parse_args(argv)
    do_fetch = (store is None) if fetch is None else fetch
    outbox, snapshot = outbox or OUTBOX, snapshot or SNAPSHOT
    if ns.cmd == "live":
        print(live.render(live.derive(ROOT)))
        return 0
    if store is None:
        _load_env()
    try:
        if ns.cmd == "boot":
            _alarm(BOOT_BUDGET_S)
            try:
                store = store or RestStore()
                print(views.boot(store, ns.role, ROOT, snapshot=snapshot) if ns.role
                      else views.boot_role_free(store, ROOT, snapshot=snapshot))
            except (BrainUnavailable, BootTimeout) as exc:
                print(views._from_snapshot(snapshot, ns.role or "role-free", str(exc), views.BOOT_LIMIT))
            finally:
                signal.alarm(0)
            return 0
        store = store or RestStore()
        secrets = (secret_values_from_env(STATE / ".env")
                   | secret_values_from_env(STATE / ".fomo.env")
                   | secret_values_from_mapping(os.environ))
        if ns.cmd in ("record-work", "complete", "work-coverage", "closure-audit"):
            from bot.brain_graph import work_records as work
            if ns.cmd == "record-work":
                task_id = (store.resolve_fact_id(ns.task) or ns.task) if ns.task else None
                result = work.record_work(store, role=ns.role, statement=ns.statement, validation=ns.validation,
                    repo=ROOT, merge=ns.merge, no_integration=ns.no_integration, artifact=ns.artifact,
                    task_id=task_id, kind=ns.kind, fetched=fetch_once(ROOT) if do_fetch else False,
                    secret_values=secrets, outbox=outbox, judgements=dict(_pair(x, "=") for x in ns.judge))
                print(json.dumps(result, default=str))
                return EXIT.get(result["status"], 2)
            if ns.cmd == "complete":
                fid = store.resolve_fact_id(ns.completion_fact) or ns.completion_fact
                if ns.no_integration:
                    fact = store.get("brain_facts", fid)
                    if not fact or work._detail(fact).get("work", {}).get("completion_kind") != "no_integration":
                        raise BrainRefused("expected no-integration completion fact")
                result = work.complete_work(store, store.resolve_fact_id(ns.id) or ns.id, role=ns.role,
                    completion_fact_id=fid, reason=ns.reason, repo=ROOT, secret_values=secrets, outbox=outbox)
                print(json.dumps(result))
                return 0 if result["status"] == "written" else 2
            result = (work.work_coverage(store, repo=ROOT, base=ns.base, head=ns.head)
                      if ns.cmd == "work-coverage" else work.closure_audit(store, repo=ROOT, since=ns.since, until=ns.until))
            print(json.dumps(result))
            return 0 if result["complete"] and not result.get("missing") else 2
        if ns.cmd == "write":
            fetched = fetch_once(ROOT) if do_fetch else True
            d = draft_from_args(ns)
            r = api.write_fact(store, d, repo=ROOT, judgements=d.pop("judgements"), secret_values=secrets,
                               outbox=outbox, fetched=fetched)
            print(json.dumps({k: v for k, v in r.items() if k != "fact"}, indent=1))
            if r["status"] == "written":
                print(f"written {r['fact']['id']} [{r['fact']['status']}]")
            if r["status"] == "needs_judgement":
                print("Judge each candidate, then re-run with --judge ID=duplicate|refines|supersedes|contradicts|unrelated")
            return EXIT[r["status"]]
        if ns.cmd == "judge":
            from bot.brain_graph import VERIFIERS
            if ns.role not in VERIFIERS:
                raise BrainRefused("only a named reviewer or operator may judge")
            ns.newer = store.resolve_fact_id(ns.newer) or ns.newer
            ns.older = store.resolve_fact_id(ns.older) or ns.older
            newer = store.get("brain_facts", ns.newer)
            older = store.get("brain_facts", ns.older)
            if not newer or not older:
                raise BrainRefused("no such fact")
            print(json.dumps(store._rpc("brain_judge_pair", {
                "p_newer": ns.newer, "p_older": ns.older,
                "p_newer_version": newer["version"], "p_older_version": older["version"],
                "p_verdict": ns.verdict, "p_author": ns.role})))
            return 0
        if ns.cmd == "verify":
            ns.id = store.resolve_fact_id(ns.id) or ns.id
            if store.get("brain_facts", ns.id) is None:
                print("no such fact")
                return 2
            out = Path(ns.output_file).read_text() if ns.output_file else ""
            r = api.verify_fact(store, ns.id, ns.verdict, role=ns.role, output=out, note=ns.note,
                                secret_values=secrets)
            print(f"{r['id'][:8]} -> {r['status']}")
            return 0
        if ns.cmd == "retract":
            ns.id = store.resolve_fact_id(ns.id) or ns.id
            if store.get("brain_facts", ns.id) is None:
                print("no such fact")
                return 2
            r = api.retract_fact(store, ns.id, role=ns.role, reason=ns.reason, reason_kind=ns.reason_kind, secret_values=secrets)
            print(f"{r['id'][:8]} -> {r['status']}")
            return 0
        if ns.cmd == "ask":
            from bot.brain_graph.search import ask
            stats = {}
            for f in ask(store, ns.q, stats=stats):
                print(views.line(f, with_evidence=True))
            if stats.get("vector_only_cut"):
                print(f"(+{stats['vector_only_cut']} vector-only hits not shown; cap {stats['vector_only_cap']} of "
                      f"{stats['vector_only_seen']}; keyword and sha hits are never cut)", file=sys.stderr)
            return 0
        if ns.cmd == "entity":
            ent = store.find_entity(ns.type, ns.name)
            from bot.brain_graph.search import _shas
            by_sha = CrewFilter(store).facts(store.facts_by_sha(ns.name.lower())) if ns.type == "commit" and _shas(ns.name) else []
            if not ent and not by_sha:
                print("no such entity")
                return 2
            if ent:
                print(f"{ent['type']} {ent['name']} aliases={ent['aliases']}")
            seen = set()
            for f in (CrewFilter(store).facts(store.find_facts([ent["id"]])) if ent else [])[::-1] + by_sha:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    print(views.line(f, with_evidence=True))
            return 0
        if ns.cmd == "queue":
            secs = [s["id"] for s in store.sections()]
            rows = (store.unverified_queue(ns.role, secs, 50) if not ns.author else
                    [f for f in store.facts_in_sections(secs, None, queue_only=True, limit=500)
                     if f["author"] == ns.author])
            for f in CrewFilter(store).facts(rows):
                print(views.line(f, with_evidence=True))
            return 0
        if ns.cmd == "episode":
            eps = api.add_episode(store, ns.kind, ns.body, author=ns.role, occurred_at=ns.occurred_at,
                                  session_id=(os.getenv("CLAUDE_SESSION_ID") or os.getenv("CODEX_THREAD_ID")), secret_values=secrets)
            print("episode", eps[0]["id"], f"({len(eps)} part(s))")
            return 0
        if ns.cmd == "handover":
            inventory = None
            if ns.inventory:
                raw = Path(ns.inventory).read_bytes()
                if len(raw) > 128*1024:
                    raise BrainRefused("fact inventory too large")
                from bot.brain_graph.gate import validate_episode_body
                if validate_episode_body(raw.decode(), secret_values=secrets):
                    raise BrainRefused("fact inventory contains a secret")
                try:
                    inventory = json.loads(raw)
                except ValueError as exc:
                    raise BrainRefused("fact inventory invalid JSON") from exc
            print(views.handover(store, ns.role, ROOT, inventory=inventory, session_id=(os.getenv("CLAUDE_SESSION_ID") or os.getenv("CODEX_THREAD_ID"))))
            return 0
        if ns.cmd == "flush":
            print(api.flush_outbox(store, outbox, repo=ROOT, secret_values=secrets,
                                   fetched=fetch_once(ROOT) if do_fetch else True))
            return 0
    except (BrainRefused, VersionConflict) as exc:
        print(f"REFUSED: {exc}")
        return 2
    except BrainUnavailable as exc:
        print(f"BRAIN UNREACHABLE: {exc}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
