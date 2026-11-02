#!/usr/bin/env python3
"""Relay option A coverage: who signed the origin deposit of every on-chain-lane Relay buy that did
not verify (spec 2026-10-09 relay deposit proof on the watcher lane).

For every `relay origin` line in logs/onchain_evm_armed*.log since --since whose reason is not
`verified`, find the attempt (origin onchain:<chain>:<tx>:<mint>), ask Relay's PUBLIC GET
/requests/v2?hash=<destination tx> (no key, paced; hours later the request is indexed), and class the
origin deposit's depositor against memecoin_address_registry for that source user:

- solana_verified:   a Solana origin whose depositor is a verified, conflict-free address of the user,
                     verified before the deposit's block time (what option C proves from the frame);
- solana_verified_late / solana_conflict / solana_unverified: registered, but not provable;
- solana_unregistered: no registry row for the address;
- evm_beneficiary:   an EVM origin signed by the destination wallet itself (today's EVM rule);
- evm_other:         an EVM origin signed by any other address;
- not_found / ambiguous / unsettled: Relay still has no single settled request.

Read-only on the ledger; the only network calls are Relay's public endpoint, one per 6 s. Not on any fill path.

    python3 scripts/research/relay_deposit_coverage.py --since 2026-10-07 [--out FILE.json]
"""
import argparse
import collections
import glob
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOLANA_CHAIN = 792703809
LINE = re.compile(r"^(\S+) \[onchain-evm\] relay origin (0x[0-9a-f]+) reason=([A-Za-z_:]+) verify_ms=([0-9.]+)")
PACE_S = 6.0                      # one public Relay call per 6 s: 1.5 s drew 429s


def log_lines(since, logs_dir):
    out = []
    for path in sorted(glob.glob(str(Path(logs_dir) / "onchain_evm_armed*.log"))):
        if "observe-only" in path:
            continue
        with open(path, errors="replace") as f:
            for line in f:
                m = LINE.match(line)
                if m and m.group(1) >= since:
                    out.append(dict(at=m.group(1), prefix=m.group(2), reason=m.group(3).split(":")[0],
                                    verify_ms=float(m.group(4)), log=Path(path).name))
    return out


def attempts(ledger, since):
    rows = ledger.execute(
        "SELECT attempt_id, origin_event_id, network_id, source_user_id, source_handle, terminal_reason, "
        "decision_started_at, json_extract(trace_json,'$.source_evidence.source_initiation') "
        "FROM memecoin_entry_attempts WHERE (origin_event_id LIKE 'onchain:%' OR origin_event_id LIKE "
        "'swap:raybot:%') AND decision_started_at>=? AND network_id!='solana' ORDER BY "
        "origin_event_id LIKE 'onchain:%' DESC", (since,)).fetchall()
    by_tx = {}
    for aid, origin, net, uid, handle, reason, at, init in rows:
        tx = next((p.lower() for p in origin.split(":") if re.fullmatch(r"0x[0-9a-fA-F]{64}", p)), None)
        if tx is None:
            continue
        by_tx.setdefault(tx, dict(attempt_id=aid, network_id=net, tx=tx, origin=origin,
                                                source_user_id=uid, handle=handle, terminal=reason, at=at,
                                                init=json.loads(init) if init else {}))
    return by_tx


def relay(tx):
    """Relay's public answer for one destination tx. A 429 waits 120 s and asks once more; a second
    429 ends the run (the watchers share Relay with us)."""
    url = "https://api.relay.link/requests/v2?" + urllib.parse.urlencode({"hash": tx})
    req = urllib.request.Request(url, headers={"Accept": "application/json", "user-agent": "Mozilla/5.0 Sniper/1.0"})
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt:
                raise
            time.sleep(120)


def classify(ledger, att, payload):
    if not isinstance(payload, dict):
        raise ValueError("malformed Relay response: expected an object")
    reqs = payload.get("requests")
    if not isinstance(reqs, list) or any(not isinstance(q, dict) for q in reqs):
        raise ValueError("malformed Relay requests: expected a list of objects")
    if not reqs:
        return "not_found", {}
    if len(reqs) != 1:
        return "ambiguous", {}
    q = reqs[0]
    dep = ((q.get("protocol") or {}).get("deposit") or {}).get("origin") or {}
    ins = (q.get("data") or {}).get("inTxs") or []
    facts = dict(status=q.get("status"), origin_chain=dep.get("chainId"), depositor=dep.get("depositor"),
                 origin_tx=dep.get("transactionId"), currency=dep.get("currency"), amount=dep.get("amount"),
                 origin_ts=ins[0].get("timestamp") if len(ins) == 1 else None)
    if q.get("status") != "success" or not dep.get("depositor"):
        return "unsettled", facts
    if dep.get("chainId") == SOLANA_CHAIN:
        rows = ledger.execute("SELECT status, status_at, conflict_at FROM memecoin_address_registry "
                              "WHERE source_user_id=? AND network_id='solana' AND address=?",
                              (att["source_user_id"], dep["depositor"])).fetchall()
        if not rows:
            return "solana_unregistered", facts
        for status, status_at, conflict in rows:
            if status == "verified" and conflict is None:
                at = datetime.fromisoformat(status_at).timestamp()
                ok = facts["origin_ts"] is not None and at <= float(facts["origin_ts"])
                return ("solana_verified" if ok else "solana_verified_late"), facts
        return ("solana_conflict" if any(c for _s, _a, c in rows) else "solana_unverified"), facts
    beneficiary = str(att["init"].get("beneficiary") or "").lower()
    return ("evm_beneficiary" if str(dep["depositor"]).lower() == beneficiary else "evm_other"), facts


def run(ledger_path, since, logs_dir):
    ledger = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True, timeout=30)
    lines = log_lines(since, logs_dir)
    by_tx = attempts(ledger, since)
    outcomes = collections.Counter(l["reason"] for l in lines)
    rows = []
    stop_reason = None
    for line in lines:
        if line["reason"] == "verified":
            continue
        hits = [a for tx, a in by_tx.items() if tx.startswith(line["prefix"])]
        att = hits[0] if len(hits) == 1 else None
        row = dict(line, attempt_id=att and att["attempt_id"], network_id=att and att["network_id"],
                   handle=att and att["handle"], terminal=att and att["terminal"])
        if att is None:
            row["class"] = "attempt_unmatched"
        else:
            try:
                row["class"], row["relay"] = classify(ledger, att, relay(att["tx"]))
            except Exception as exc:                       # noqa: BLE001 - recorded per row
                row["class"], row["error"] = "relay_error", "%s: %s" % (type(exc).__name__, str(exc)[:120])
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    stop_reason = "relay_rate_limit"
            if stop_reason is None:
                time.sleep(PACE_S)
        rows.append(row)
        if stop_reason is not None:
            break
    table = collections.Counter((r["reason"], r["class"], str((r.get("relay") or {}).get("origin_chain")))
                                for r in rows)
    depositors = collections.Counter((r.get("handle"), (r.get("relay") or {}).get("depositor"), r["class"])
                                     for r in rows)
    eligible = len(lines) - outcomes["verified"]
    return dict(since=since, outcomes=dict(outcomes), status="stopped" if stop_reason else "complete",
                stop_reason=stop_reason, eligible_rows=eligible, processed_rows=len(rows),
                unprocessed_rows=eligible - len(rows),
                table=[dict(reason=k[0], cls=k[1], origin_chain=k[2], n=v) for k, v in sorted(table.items())],
                depositors=[dict(handle=k[0], depositor=k[1], cls=k[2], n=v) for k, v in depositors.most_common()],
                rows=rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ledger", default=str(REPO / "sniper_tracker.db"))
    p.add_argument("--logs", default=str(REPO / "logs"))
    p.add_argument("--since", required=True)
    p.add_argument("--out")
    a = p.parse_args()
    out = run(a.ledger, a.since, a.logs)
    text = json.dumps(out, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    json.dump({k: v for k, v in out.items() if k != "rows"}, sys.stdout, indent=1, default=str)
    print()


if __name__ == "__main__":
    main()
