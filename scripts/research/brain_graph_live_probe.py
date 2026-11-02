#!/usr/bin/env python3
"""Prove migration 032's invariants on the LIVE Supabase brain (plan Task 10, with review amendments).

Writes only: the section brain/selftest, one selftest entity, one fact per run (retracted at the end)
and one selftest episode. Every other write below is EXPECTED to be refused. Exit 0 only if every
refusal happened and every server default was set.

    python3 scripts/research/brain_graph_live_probe.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text().splitlines():          # BEFORE importing supabase_helper
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

from bot.brain_graph.store import BrainRefused, RestStore, VersionConflict  # noqa: E402


def refused(label, fn):
    try:
        fn()
    except (BrainRefused, VersionConflict) as exc:
        print(f"ok   {label}: refused ({str(exc)[:100]})")
        return True
    print(f"FAIL {label}: the database accepted it")
    return False


def anon(method, path, body=None):
    url, key = os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"]
    req = urllib.request.Request(f"{url}/rest/v1/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"apikey": key, "Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def main():
    s = RestStore(timeout=10)
    sec = (s.sections(["brain/selftest"]) or [None])[0] or \
        s.insert("brain_sections", {"slug": "brain/selftest", "title": "selftest"})
    ent = s.find_entity("selftest", "probe") or \
        s.insert("brain_entities", {"type": "selftest", "name": "probe", "section_id": sec["id"]})
    base = {"kind": "rule", "statement": "Selftest fact written by brain_graph_live_probe.",
            "entity_ids": [ent["id"]], "evidence": [{"type": "doc", "ref": "selftest", "check": "unchecked"}],
            "author": "alpha"}
    f = s.insert("brain_facts", base)
    ok = [bool(f.get("verify_by")) and f["version"] == 1 and bool(f.get("recorded_at"))]
    print(("ok  " if ok[0] else "FAIL") + " server set verify_by, version and recorded_at")
    ok.append(refused("fact without evidence", lambda: s.insert("brain_facts", dict(base, evidence=[]))))
    ok.append(refused("kind config", lambda: s.insert("brain_facts", dict(base, kind="config"))))
    ok.append(refused("author codex", lambda: s.insert("brain_facts", dict(base, author="codex"))))
    ok.append(refused("self-verify", lambda: s.patch_fact(f["id"], 1, {"status": "verified", "verified_by": "alpha"})))
    ok.append(refused("rewrite statement", lambda: s.patch_fact(f["id"], 1, {"statement": "changed text here"})))
    ok.append(refused("move valid_from", lambda: s.patch_fact(f["id"], 1, {"valid_from": "2020-01-01T00:00:00Z"})))
    s.patch_fact(f["id"], 1, {"verify_note": "a"})
    ok.append(refused("stale version", lambda: s.patch_fact(f["id"], 1, {"verify_note": "b"})))
    ok.append(refused("delete fact", lambda: s._call("DELETE", f"brain_facts?id=eq.{f['id']}")))
    ok.append(refused("entity without section", lambda: s.insert("brain_entities", {"type": "selftest",
                                                                                   "name": "nosection"})))
    ep = s.insert("brain_episodes", {"kind": "selftest", "body": "probe", "author": "alpha"})
    ok.append(ep.get("occurred_at") is None)
    print(("ok  " if ok[-1] else "FAIL") + " episode occurred_at stays NULL when unknown")
    ok.append(refused("rewrite episode", lambda: s._call("PATCH", f"brain_episodes?id=eq.{ep['id']}",
                                                         {"body": "changed"})))
    ok.append(refused("oversized episode part", lambda: s.insert("brain_episodes", {
        "kind": "selftest", "body": "x" * 1801, "author": "alpha"})))
    code, rows = anon("GET", "brain_facts?limit=1")
    ok.append(code == 200 and rows == [])
    print(("ok  " if ok[-1] else "FAIL") + f" anon SELECT reads nothing ({code}, {rows})")
    code, _ = anon("POST", "rpc/brain_queue_count", {"p_section_ids": []})
    ok.append(code in (401, 403))
    print(("ok  " if ok[-1] else "FAIL") + f" anon RPC refused ({code})")
    cur = s.get("brain_facts", f["id"])
    closed = s.patch_fact(f["id"], cur["version"], {"status": "retracted",
                                                    "valid_to": datetime.now(timezone.utc).isoformat()})
    ok.append(refused("revive a closed fact", lambda: s.patch_fact(f["id"], closed["version"], {"valid_to": None})))
    print("ALL OK" if all(ok) else "SOME INVARIANT FAILED")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
