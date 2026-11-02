#!/usr/bin/env python3
"""Replay a candidate JUDGE playbook on recorded live sheets (read-only on the desk DB; one call at a time on the shared
subscription login; waits while a live JUDGE call is claimed or started < --gap-s ago). Sheet = the stored exact text
(result_json.sheet_zb64) when present, else re-rendered from the frozen inputs. Writes one JSONL row per sheet:
{call_id, ticket_id, live_verdict, replay_verdict, replay_text, sheet_source}.
    python3 scripts/research/desk_judge_playbook_replay.py --playbook <new.md> --desk-db data/memecoin_desk.db \
        --since 2026-10-23T00:00:00 --limit 50 --out <out.jsonl>"""
import argparse
import base64
import importlib.util
import json
import os
import sqlite3
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def verdict(text):
    try:
        return json.loads(text)["v"]
    except Exception:          # noqa: BLE001 - an unparseable answer is its own class
        return "void"


def sheet_of(result):
    if result.get("sheet_zb64"):
        return zlib.decompress(base64.b64decode(result["sheet_zb64"])).decode(), "stored"
    from sniper.services import memecoin_desk_contract as C
    sheet = C.render_judge_sheet(result["asof"]["candidate"], soft=result.get("soft"), structure=result.get("structure"),
                                 context=result.get("context"))
    return json.dumps(sheet, separators=(",", ":"), sort_keys=True), "rerendered"


def busy(db, gap_s):
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        if c.execute("SELECT count(*) FROM desk_judge_calls WHERE state='claimed'").fetchone()[0]:
            return True
        last = c.execute("SELECT max(call_started_at) FROM desk_judge_calls").fetchone()[0]
    finally:
        c.close()
    if not last:
        return False
    t = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() < gap_s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--playbook", required=True)
    ap.add_argument("--desk-db", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--gap-s", type=float, default=3.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        os.environ.pop(key, None)
    spec = importlib.util.spec_from_file_location("w", ROOT / "scripts/memecoin_desk_judge_worker.py")
    W = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(W)
    system, version = W.load_playbook(a.playbook)
    c = sqlite3.connect("file:%s?mode=ro" % a.desk_db, uri=True)
    rows = c.execute("SELECT call_id, ticket_id, result_json FROM desk_judge_calls WHERE state='responded' AND "
                     "call_started_at >= ? ORDER BY call_started_at DESC", (a.since,)).fetchall()
    c.close()
    done = {json.loads(x)["call_id"] for x in open(a.out)} if Path(a.out).exists() else set()
    n = len(done)
    with open(a.out, "a") as f:
        for call_id, ticket_id, rj in rows:
            if n >= a.limit:
                break
            r = json.loads(rj or "{}")
            if call_id in done or not r.get("text") or r.get("call_error") or not (r.get("asof") or r.get("sheet_zb64")):
                continue
            try:
                user, source = sheet_of(r)
            except Exception as exc:     # noqa: BLE001 - recorded, not fatal
                f.write(json.dumps({"call_id": call_id, "render_error": type(exc).__name__}) + "\n")
                continue
            while busy(a.desk_db, a.gap_s):
                time.sleep(1)
            out = W.sdk_call_model(system, user, persist_session=False)
            row = {"call_id": call_id, "ticket_id": ticket_id, "playbook": version, "sheet_source": source,
                   "live_verdict": verdict(r["text"]), "replay_verdict": verdict(out.get("text") or ""),
                   "replay_text": out.get("text"), "call_error": out.get("call_error")}
            f.write(json.dumps(row) + "\n")
            f.flush()
            n += 1
            print(n, row["live_verdict"], "->", row["replay_verdict"], flush=True)


if __name__ == "__main__":
    main()
