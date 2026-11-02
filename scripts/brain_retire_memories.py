#!/usr/bin/env python3
"""Retire imported session/* mirrors with a durable rollback manifest; never delete rows.

--dry-run lists counts; --apply writes a new manifest before conditional PATCHes;
--rollback MANIFEST restores only rows still pointing to that manifest's replacement.
Direct structured/domain records remain active: existing get_memory/domain_query
readers still use them. Importing a direct record does not authorize its retirement.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _key(ref):
    ref = str(ref or "").split("#", 1)[0]
    if ref.startswith("memory/") and ref.endswith(".md"):
        return "session/" + ref[len("memory/"):-3]
    if ref.startswith("supabase:memories/"):
        return ref[len("supabase:memories/"):]
    return None


def plan_retire(memories, episodes, facts):
    names = {e["id"]: _key(e.get("source_ref")) for e in episodes}
    first = {}
    for fact in sorted(facts, key=lambda f: (f.get("recorded_at") or "", f["id"])):
        for episode in fact.get("episode_ids") or []:
            name = names.get(episode)
            if name:
                first.setdefault(name, fact["id"])
    return [{"memory_id": row["id"], "name": row["name"], "prior_status": row["status"],
             "prior_superseded_by": row.get("superseded_by"), "fact_id": first[row["name"]]}
            for row in memories if row.get("status") == "active" and row.get("type") != "user"
            and str(row.get("name", "")).startswith("session/")
            and not str(row.get("name", "")).startswith("session/HANDOFF")
            and row.get("name") in first]


def _filter(value):
    return "is.null" if value is None else "eq." + quote(str(value), safe="")


def apply(request, rows, manifest_path):
    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves the original rollback state across retries or two operators.
    with path.open("x") as handle:
        os.chmod(path, 0o600)
        json.dump({"written_at": datetime.now(timezone.utc).isoformat(), "rows": rows}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    patched = 0
    for row in rows:
        result = request("PATCH", "memories?id=" + _filter(row["memory_id"])
                         + "&status=" + _filter(row["prior_status"])
                         + "&superseded_by=" + _filter(row.get("prior_superseded_by")),
                         {"status": "superseded", "superseded_by": row["fact_id"]},
                         headers={"Prefer": "return=representation"})
        patched += len(result or [])
    return {"patched": patched, "skipped": len(rows) - patched}


def rollback(request, manifest_path):
    rows = json.loads(Path(manifest_path).read_text())["rows"]
    restored = 0
    for row in rows:
        result = request("PATCH", "memories?id=" + _filter(row["memory_id"])
                         + "&status=eq.superseded&superseded_by=" + _filter(row["fact_id"]),
                         {"status": row["prior_status"],
                          "superseded_by": row.get("prior_superseded_by")},
                         headers={"Prefer": "return=representation"})
        restored += len(result or [])
    return {"restored": restored, "skipped": len(rows) - restored}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    group.add_argument("--rollback", metavar="MANIFEST")
    parser.add_argument("--manifest", type=Path, help="new rollback manifest path for --apply")
    args = parser.parse_args(argv)
    from bot.brain_graph import live
    from sniper.util.envfiles import load_env_files
    state = live.state_root(ROOT)
    load_env_files(str(state))
    from bot.brain_graph.store import RestStore
    store = RestStore(timeout=10)  # refuses anonymous or absent service credentials
    request = store._request
    if args.rollback:
        print(json.dumps(rollback(request, args.rollback)))
        return 0

    def pages(path):
        result, offset = [], 0
        while True:
            rows = request("GET", f"{path}&limit=500&offset={offset}", timeout=10) or []
            result.extend(rows)
            if len(rows) < 500:
                return result
            offset += 500

    memories = pages("memories?select=id,name,type,status,superseded_by&order=id.asc")
    episodes = pages("brain_episodes?author=eq.import&select=id,source_ref&order=id.asc")
    facts = pages("brain_facts?author=in.(import,operator)&select=id,episode_ids,recorded_at&order=id.asc")
    if not facts:
        raise SystemExit("Graph has no imported facts; refusing cutover retirement")
    rows = plan_retire(memories, episodes, facts)
    print(json.dumps({"scope": "imported_session_mirrors",
                      "memories": len(memories), "episodes": len(episodes), "facts": len(facts),
                      "retire": len(rows), "remaining": len(memories) - len(rows)}))
    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        manifest = args.manifest or state / "data" / f"brain_retire_manifest_{stamp}.json"
        print(json.dumps({"manifest": str(manifest), **apply(request, rows, manifest)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
