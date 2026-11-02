#!/usr/bin/env python3
"""Frozen-sheet subscription replay. Research artifacts only; desk DB always read-only.

freeze --out DIR --cohort PREFIX; run --out DIR [--limit-cases N]; summary --out DIR.
One fresh preconnected client per call; query wall excludes connect (reported separately).
Live JUDGE claims/recent calls take priority. No settings, ledger or service mutations.
"""
import argparse
import asyncio
import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ARMS = {
    "haiku_live": {"model": "claude-haiku-5-5", "thinking": None},
    "haiku_off": {"model": "claude-haiku-5-5", "thinking": {"type": "disabled"}},
    "opus_live": {"model": "claude-opus-5-5", "thinking": None},
    "opus_off": {"model": "claude-opus-5-5", "thinking": {"type": "disabled"}},
}


@contextmanager
def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    try:
        c.execute("PRAGMA query_only=ON")
        yield c
    finally:
        c.close()


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def exact_sheet(result):
    if not result.get("sheet_zb64"):
        raise ValueError("no_exact_sheet")
    text = zlib.decompress(base64.b64decode(result["sheet_zb64"])).decode()
    if digest(text) != result.get("sheet_sha256"):
        raise ValueError("sheet_hash_mismatch")
    return text


def letter(text):
    try:
        value = json.loads(text)["v"]
        return value if value in ("E", "S", "W") else "VOID"
    except (ValueError, TypeError, KeyError):
        return "VOID"


def arm_order(i):
    names = tuple(ARMS)
    return names[i % 4:] + names[:i % 4]


def freeze(db, prefix, out):
    if (out / "manifest.json").exists():
        raise ValueError("manifest_exists")
    with ro(db) as c:
        cohorts = c.execute("SELECT cohort_id,prompt_version,policy_json FROM desk_cohorts WHERE cohort_id LIKE ?",
                            (f"%{prefix}%",)).fetchall()
        if len(cohorts) != 1:
            raise ValueError("cohort_not_unique")
        cid, version, policy = cohorts[0]
        prompt_path = "docs/superpowers/specs/2026-10-21-desk-judge-playbook.md"
        prompt_file = subprocess.check_output(["git", "show", f"76bcca838:{prompt_path}"], cwd=ROOT).decode()
        if f"prompt_version: `{version}`" not in prompt_file:
            raise ValueError("historical_prompt_version_mismatch")
        system = prompt_file.split("\n---\n", 1)[1]
        calls = c.execute("SELECT j.call_id,j.ticket_id,j.call_started_at,j.elapsed_ms,j.result_json,"
                          "t.mint,t.verdict,t.opportunity_id FROM desk_judge_calls j JOIN desk_tickets t "
                          "ON t.ticket_id=j.ticket_id AND t.stage='vet' WHERE t.cohort_id=? ORDER BY j.call_id", (cid,)).fetchall()
        vets = c.execute("SELECT count(*) FROM desk_tickets WHERE cohort_id=? AND stage='vet'", (cid,)).fetchone()[0]
    cases, excluded = [], []
    for call, ticket, started, elapsed, raw, mint, vet, oid in calls:
        result = json.loads(raw or "{}")
        try:
            sheet = exact_sheet(result)
            if result.get("call_error") or letter(result.get("text")) == "VOID":
                raise ValueError("live_call_error_or_void")
        except (ValueError, KeyError, zlib.error) as exc:
            excluded.append({"call_id": call, "reason": str(exc)[:80]})
            continue
        cases.append({"call_id": call, "ticket_id": ticket, "opportunity_id": oid, "mint": mint,
                      "call_started_at": started, "sheet": sheet, "sheet_sha256": digest(sheet),
                      "live_text": result["text"], "live_letter": letter(result["text"]), "live_vet": vet,
                      "live_elapsed_ms": elapsed, "deadline_s": result.get("deadline_s", 12.0),
                      "system_sha256_recorded": result.get("system_sha256")})
    cases.sort(key=lambda r: digest(r["call_id"]))
    manifest = {"cohort_id": cid, "prompt_version": version, "system": system, "system_sha256": digest(system),
                "system_provenance": "git76bcca838; v18 matches cohort, historical calls have no independent prompt hash",
                "policy": json.loads(policy), "vet_rows": vets, "call_rows": len(calls), "cases": cases,
                "excluded": excluded, "arms": ARMS, "effort": "low", "output_cap": None,
                "order": "SHA256(call_id); rotate four arms by case index", "max_calls": 4 * len(cases),
                "cli_path": shutil.which("claude"), "created_at": datetime.now(timezone.utc).isoformat(),
                "limits": ["default thinking is live control, not independently proven enabled",
                           "64 sheets cannot meet prior >=150-sheet/three-day promotion bar",
                           "no funded authority; no guaranteed hard network cancellation"]}
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    print(json.dumps({k: manifest[k] for k in ("cohort_id", "vet_rows", "call_rows", "max_calls")}), flush=True)


def busy(db, gap_s=3.0):
    with ro(db) as c:
        if c.execute("SELECT 1 FROM desk_judge_calls WHERE state='claimed' LIMIT 1").fetchone():
            return True
        last = c.execute("SELECT max(call_started_at) FROM desk_judge_calls").fetchone()[0]
    if not last:
        return False
    dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return time.time() - dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp() < gap_s


async def invoke(W, manifest, case, arm):
    from claude_agent_sdk import ClaudeSDKClient, AssistantMessage, ResultMessage, ThinkingBlock, ToolUseBlock
    config = manifest["arms"][arm]
    options = W._sdk_options(manifest["system"], model=config["model"], effort="low",
                             thinking=config["thinking"], persist_session=False, cli_path=manifest["cli_path"])
    client = ClaudeSDKClient(options)
    row = {"arm": arm, "call_id": case["call_id"], "sheet_sha256": case["sheet_sha256"],
           "thinking_blocks": 0, "tool_blocks": 0, "model_echo": None, "call_error": None,
           "text": None, "wall_ms": None, "connect_ms": None}
    result, assistant_error = None, None
    began = time.perf_counter()
    query_start = None
    try:
        await asyncio.wait_for(client.connect(), 20)
        row["connect_ms"] = round((time.perf_counter() - began) * 1000, 1)
        query_start = time.perf_counter()
        async def query():
            nonlocal result, assistant_error
            await client.query(case["sheet"])
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    row["model_echo"] = message.model
                    assistant_error = message.error or assistant_error
                    row["thinking_blocks"] += sum(isinstance(b, ThinkingBlock) for b in message.content)
                    row["tool_blocks"] += sum(isinstance(b, ToolUseBlock) for b in message.content)
                    if row["tool_blocks"]:
                        raise ValueError("tool_event")
                elif isinstance(message, ResultMessage):
                    result = message
        await asyncio.wait_for(query(), case["deadline_s"])
        row["call_error"] = W.classify(result, assistant_error, None)
        if result:
            row.update(text=result.result if isinstance(result.result, str) else None,
                       is_error=result.is_error, duration_ms=result.duration_ms,
                       duration_api_ms=result.duration_api_ms, usage=dict(result.usage or {}))
            if result.is_error:
                row["call_error"] = row["call_error"] or "model_result_error"
    except Exception as exc:
        row["call_error"] = W.classify(result, assistant_error, exc)
        row["exception_class"] = type(exc).__name__
    finally:
        if query_start is not None:
            row["wall_ms"] = round((time.perf_counter() - query_start) * 1000, 1)
        try:
            await asyncio.wait_for(client.disconnect(), 5)
        except Exception:
            pass
    row["letter"] = letter(row["text"]) if not row["call_error"] else "VOID"
    if row["model_echo"] != config["model"]:
        row["call_error"], row["letter"] = "model_echo_mismatch", "VOID"
    row["ts"] = datetime.now(timezone.utc).isoformat()
    return row


def summarize(cases, rows):
    latest = {(r["call_id"], r["arm"]): r for r in rows}
    report = {}
    for arm in ARMS:
        pairs = [(c, latest[(c["call_id"], arm)]) for c in cases if (c["call_id"], arm) in latest]
        good = [r["wall_ms"] for _, r in pairs if r.get("letter") != "VOID" and r.get("wall_ms") is not None]
        live_e = sum(c["live_letter"] == "E" for c, _ in pairs)
        retention = sum(c["live_letter"] == "E" and r["letter"] == "E" for c, r in pairs)
        report[arm] = {"attempted": len(pairs), "missing": len(cases) - len(pairs),
                       "void": sum(r["letter"] == "VOID" for _, r in pairs),
                       "p50_ms": statistics.median(good) if good else None,
                       "p90_ms": sorted(good)[min(len(good) - 1, int(.9 * len(good)))] if good else None,
                       "confusion": dict(Counter(c["live_letter"] + "->" + r["letter"] for c, r in pairs)),
                       "live_enter_retention": retention / live_e if live_e else None,
                       "added_enters": [c["call_id"] for c, r in pairs if c["live_letter"] != "E" and r["letter"] == "E"],
                       "thinking_blocks": sum(r.get("thinking_blocks", 0) for _, r in pairs)}
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("freeze", "run", "summary"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--desk-db", type=Path, default=ROOT / "data/memecoin_desk.db")
    p.add_argument("--cohort", default="1d7891fd")
    p.add_argument("--limit-cases", type=int, default=0)
    p.add_argument("--arms", nargs="+", choices=tuple(ARMS), default=list(ARMS))
    a = p.parse_args()
    if a.action == "run" and (a.out / "complete.json").exists():
        p.error("completed study is closed; no further model calls")
    if a.action == "freeze":
        return freeze(a.desk_db, a.cohort, a.out)
    manifest = json.loads((a.out / "manifest.json").read_text())
    if digest(manifest["system"]) != manifest["system_sha256"] or any(
            digest(c["sheet"]) != c["sheet_sha256"] for c in manifest["cases"]):
        raise ValueError("frozen_input_hash_mismatch")
    path = a.out / "replay.jsonl"
    rows = [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
    if a.action == "summary":
        result = summarize(manifest["cases"], rows)
        (a.out / "summary.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        return
    with open(a.out / "run.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (a.out / "complete.json").exists():
            p.error("completed study is closed; no further model calls")
        rows = [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            os.environ.pop(key, None)                # this research subprocess only, before any SDK thread
        spec = importlib.util.spec_from_file_location("speed_worker", ROOT / "scripts/memecoin_desk_judge_worker.py")
        W = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(W)
        done, failures = {(r["call_id"], r["arm"]) for r in rows}, 0
        with path.open("a") as f:
            for i, case in enumerate(manifest["cases"]):
                if a.limit_cases and i >= a.limit_cases:
                    break
                for arm in arm_order(i):
                    if arm not in a.arms:
                        continue
                    if (case["call_id"], arm) in done:
                        continue
                    while busy(a.desk_db):
                        time.sleep(1)
                    row = asyncio.run(invoke(W, manifest, case, arm))
                    f.write(json.dumps(row, sort_keys=True) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                    done.add((case["call_id"], arm))
                    print(json.dumps({"completed": len(done), "case": i, "arm": arm, "letter": row["letter"],
                                      "wall_ms": row["wall_ms"], "error": row["call_error"],
                                      "thinking_blocks": row["thinking_blocks"]}), flush=True)
                    failures = failures + 1 if row["call_error"] else 0
                    if row["call_error"] in ("auth_expired", "rate_limited", "model_echo_mismatch") or failures >= 3:
                        raise SystemExit("replay stopped on quota/auth/model/three consecutive failures")


if __name__ == "__main__":
    main()
