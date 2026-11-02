"""Desk cluster check (plumbing spec rev 5 §6.1 #4; operator decision 821a42b0: the cluster gate is
desk-only). The desk hard filter needs `checks.cluster` to PASS, and unknown never passes, so without
this nothing on the desk can trade.

For each new producer-U ticket in data/desk/candidate_emissions/*.jsonl this runs the existing
shared-origin ClusterAdmissionService (funding origin of the top holders) on the RESEARCH Helius key
(MEMECOIN_RESEARCH_HELIUS_RPC_URLS via pick_research_url; never the spent singular key, never a
funded or watcher key), behind the shared research pacer, with its own store, and appends one row
per ticket to data/desk/cluster_checks/<date>.jsonl:

    {"schema": "desk-cluster/v1", "ticket_id", "network_id", "mint", "pool", "snapshot_hash",
     "known_at", "check": {"status": "pass"|"kill"|"unknown", "reason"}, "decision_id",
     "policy_id", "metrics", "rpc_calls", "wall_ms"}

Read-only on the producer's files, one writer for the cluster rows, never on an entry path. Caps:
candidates per minute (MEMECOIN_DESK_CLUSTER_PER_MIN), research calls per candidate / minute / UTC
day (MEMECOIN_DESK_CLUSTER_CALLS_PER_{CANDIDATE,MIN,DAY}); a trace that would spend past its budget
ends cluster_unknown_budget. A book under four eligible owners takes the thin rule (operator
2026-10-21, b): trace the owners that exist, block only on a funder shared by two or more, and an
allow is the distinct code pass_thin_owners. A final block is kill; anything unresolved is unknown,
which the hard filter refuses.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

SCHEMA = "desk-cluster/v1"
# Outcomes that can return while a trace thread is still mid-read (decide_cluster returns on a
# deadline, a quota or a trace error without waiting for the rest; a raising decide leaves the same
# doubt). The stop flag ends a trace at its next page, but a read already on the wire still lands,
# so such a row is charged its whole reservation, the metered opener's hard ceiling (Codex,
# 669b0b0a4: a deadline row charged 2 while the released trace made a 3rd read).
IN_FLIGHT_REASONS = frozenset({"cluster_unknown_deadline", "cluster_unknown_quota",
                               "cluster_unknown_trace_error", "cluster_unknown_worker_error"})
KILL_REASONS = frozenset({"cluster_share_high", "cluster_top_funder_high", "cluster_no_holders",
                          "cluster_too_few_owners"})


def check_from_decision(decision) -> dict:
    """The hard filter's checks.cluster row: allowed is pass, a final block is kill, else unknown."""
    if decision.allowed:
        return {"status": "pass",
                "reason": decision.reason if decision.reason in ("pass_thin_owners", "allowed_bound", "pass_thin_bound")
                else "cluster_clear"}
    if decision.reason in KILL_REASONS:
        return {"status": "kill", "reason": decision.reason}
    return {"status": "unknown", "reason": decision.reason or "cluster_unknown"}


def _rows(path: Path):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue                 # a torn last line while the producer appends; read it next pass
        if isinstance(row, dict):
            yield row


def done_tickets(out_dir: Path) -> set:
    return {row.get("ticket_id") for path in sorted(out_dir.glob("*.jsonl")) for row in _rows(path)
            if isinstance(row.get("ticket_id"), str)}


def pending_candidates(emissions_dir: Path, done: set, *, now_epoch, max_age_s, refused=None, intake=False) -> list:
    """New, genuine, fresh U emissions, oldest first. Identity is Lambda's verify_u_emission (schema,
    ticket_id re-derived, pool = bonding curve, snapshot hash recomputed, age window): a forged,
    altered or stale emission never reaches the RPC and writes nothing; it is counted by reason."""
    from sniper.services.memecoin_desk_contract import validate_candidate
    from sniper.services.memecoin_desk_safety import verify_intake_emission, verify_u_emission
    out, seen = [], set()
    for path in sorted(emissions_dir.glob("*.jsonl")):
        for row in _rows(path):
            ticket = row.get("ticket_id")
            if not isinstance(ticket, str) or not ticket:
                # an unhashable or empty id (ticket_id=[]) raised TypeError here and stopped the
                # pass before every later valid ticket (Codex, 85f4fdcbb)
                if refused is not None and row.get("producer") == "U":
                    refused["ticket_id_invalid"] = refused.get("ticket_id_invalid", 0) + 1
                continue
            if row.get("producer") != "U" or ticket in done or ticket in seen:
                continue
            try:
                if intake:                       # order B: desk intake PumpSwap emissions (their own directory)
                    verify_intake_emission(row, now=now_epoch, max_age_s=max_age_s)
                elif row.get("venue", "pumpfun") != "pumpfun" or row.get("schema") == "desk-candidate/pumpswap-v1":
                    raise ValueError("emission_venue")
                else:
                    verify_u_emission(row, now=now_epoch, max_age_s=max_age_s)
                # the full candidate contract too: a self-consistent rehashed candidate with an invalid
                # body (wrong missing list, a string mcap, an extra envelope key) passes the digest
                # and identity checks but must never spend a trace or bind a row (Codex P2, 71edac009)
                validate_candidate(row)
            except ValueError as exc:
                if refused is not None:
                    reason = str(exc.args[0]) if exc.args else "emission_invalid"
                    refused[reason] = refused.get(reason, 0) + 1
                continue
            seen.add(ticket)
            out.append(row)
    return out


class BudgetExceeded(RuntimeError):
    """This candidate's research-call budget is spent: the trace stops, the row is unknown."""


def _transient(exc) -> bool:
    """A read that may succeed on one more try: a timeout, a dropped connection, an HTTP 5xx. A 429 is
    never retried (it pauses the key for every reader); any other 4xx is the request's own fault."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return 500 <= code < 600
    return isinstance(exc, (TimeoutError, OSError))      # URLError, ConnectionError and socket.timeout are OSErrors


def metered(http, budget, *, pacer=None):
    """An HTTP opener behind the shared research pacer that counts its calls and refuses past
    `budget`. A 429 pauses every reader of the key (the pacer's contract). A transient failure
    (see _transient) is retried ONCE, as a second paced and counted call, so the budget stays the
    hard ceiling; the last failure type is kept on the meter ("last_error") so an unknown row can
    name its cause. Thread-safe: the desk trace runs several workers on one meter."""
    box = {"calls": 0, "over": False}
    lock = threading.Lock()

    def one(args, kwargs):
        with lock:
            if box["calls"] >= budget:
                box["over"] = True
                raise BudgetExceeded("desk cluster call budget")
            box["calls"] += 1
        if pacer is not None:
            pacer.acquire()
        try:
            return http(*args, **kwargs)
        except Exception as exc:
            box["last_error"] = "http_%s" % exc.code if isinstance(getattr(exc, "code", None), int) else type(exc).__name__
            if pacer is not None and getattr(exc, "code", None) == 429:
                pacer.pause(60.0)
            raise

    def opener(*args, **kwargs):
        try:
            return one(args, kwargs)
        except BudgetExceeded:
            raise
        except Exception as exc:
            if not _transient(exc):
                raise
        return one(args, kwargs)
    return box, opener


def calls_today(out_dir: Path, day: str) -> int:
    """Research calls already spent today, from this worker's own rows: survives restarts."""
    return sum(int(r.get("rpc_calls") or 0) for r in _rows(out_dir / (day + ".jsonl")))


def _claims_dir(out_dir: Path) -> Path:
    # A subdirectory, so readers of out_dir/*.jsonl (the hard filter, done_tickets) never see claims.
    return out_dir / "claims"


def _append_durable(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _spent(out_dir: Path, now_iso: str):
    """(candidates this minute, calls this minute, calls today) from this worker's own rows, so a
    restart, or a second worker, never resets a cap (Codex, d87abea8c). A ticket is charged its
    whole call budget from the claim written BEFORE its first provider read, and its real count
    once its result row lands: a process that dies mid-trace has still spent (Codex, 85f4fdcbb)."""
    minute, day = str(now_iso)[:16], str(now_iso)[:10]
    results = {r.get("ticket_id"): r for r in _rows(out_dir / (day + ".jsonl"))}
    charged = {t: (str(r.get("written_at") or ""), int(r.get("rpc_calls") or 0))
               for t, r in results.items()}
    for claim in _rows(_claims_dir(out_dir) / (day + ".jsonl")):
        ticket = claim.get("ticket_id")
        if ticket not in results:
            charged[ticket] = (str(claim.get("written_at") or ""), int(claim.get("call_budget") or 0))
    this_minute = [calls for at, calls in charged.values() if at[:16] == minute]
    return len(this_minute), sum(this_minute), sum(calls for _at, calls in charged.values())


def settle_interrupted(out_dir: Path, now_fn) -> int:
    """A claim with no result row is a trace whose process died after spending. Write its one row,
    unknown, charged the full budget (the real count died with the process), dated at the claim so
    the caps stay where the calls were spent; the ticket is never traced again."""
    done = done_tickets(out_dir)
    settled = 0
    for path in sorted(_claims_dir(out_dir).glob("*.jsonl")):
        for claim in _rows(path):
            ticket = claim.get("ticket_id")
            if not isinstance(ticket, str) or ticket in done:
                continue
            record = {key: claim.get(key) for key in
                      ("ticket_id", "network_id", "mint", "pool", "snapshot_hash", "call_budget",
                       "written_at")}
            record.update(schema=SCHEMA, known_at=now_fn(),
                          check={"status": "unknown", "reason": "cluster_unknown_interrupted"},
                          decision_id=None, policy_id=None, metrics={"error": "interrupted"},
                          rpc_calls=int(claim.get("call_budget") or 0), wall_ms=None)
            _append_durable(out_dir / path.name, record)
            done.add(ticket)
            settled += 1
    return settled


def run_pass(service_factory, emissions_dir, out_dir, *, per_min, calls_per_candidate, calls_per_min,
             calls_per_day, now_fn, max_age_s=60.0, epoch_fn=time.time, clock=time.monotonic, intake_dir=None):
    """Decide new tickets within the minute's candidate and call caps and the day's call cap, all
    counted from persisted rows; append one row each. `service_factory(call_budget) -> (meter,
    service)`. Returns stats."""
    emissions_dir, out_dir = Path(emissions_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # One pass at a time per out_dir (Gamma, 0b041ec26): settle_interrupted reads every claim with
    # no result as dead, so a second worker (a manual --once beside --loop, a deploy overlap) would
    # settle the first one's in-flight claim and the ticket would get two rows. The lock spans the
    # whole pass, so no claim is in flight outside it; the OS drops it if the process dies.
    with open(out_dir / "worker.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"pending": 0, "decided": 0, "capped": 0, "stop": "locked", "refused": {},
                    "interrupted": 0}
        return _locked_pass(service_factory, emissions_dir, out_dir, per_min=per_min,
                            calls_per_candidate=calls_per_candidate, calls_per_min=calls_per_min,
                            calls_per_day=calls_per_day, now_fn=now_fn, max_age_s=max_age_s,
                            epoch_fn=epoch_fn, clock=clock, intake_dir=intake_dir)


def _locked_pass(service_factory, emissions_dir, out_dir, *, per_min, calls_per_candidate,
                 calls_per_min, calls_per_day, now_fn, max_age_s, epoch_fn, clock, intake_dir=None):
    interrupted = settle_interrupted(out_dir, now_fn)
    refused = {}
    done = done_tickets(out_dir)
    todo = pending_candidates(emissions_dir, done, now_epoch=epoch_fn(), max_age_s=max_age_s, refused=refused)
    if intake_dir is not None:            # order B: intake tickets share the same per-minute and per-day caps
        todo += pending_candidates(Path(intake_dir), done, now_epoch=epoch_fn(), max_age_s=max_age_s, refused=refused,
                                   intake=True)
    stats = {"pending": len(todo), "decided": 0, "capped": 0, "stop": None, "refused": refused,
             "interrupted": interrupted}
    for row in todo:
        now_iso = now_fn()
        used, minute_calls, day_calls = _spent(out_dir, now_iso)
        allowance = min(int(calls_per_candidate), int(calls_per_min) - minute_calls,
                        int(calls_per_day) - day_calls)
        stop = ("candidates_per_min" if used >= int(per_min) else
                "calls_per_day" if int(calls_per_day) - day_calls <= 0 else
                "calls_per_min" if allowance < min(int(calls_per_candidate), 5) else None)
        if stop:
            stats.update(capped=len(todo) - stats["decided"], stop=stop)
            break
        identity = {"ticket_id": row["ticket_id"], "network_id": row["network_id"],
                    "mint": row["mint"], "pool": row["pool"], "snapshot_hash": row["snapshot"]["hash"]}
        _append_durable(_claims_dir(out_dir) / (str(now_iso)[:10] + ".jsonl"),
                        {**identity, "call_budget": allowance, "written_at": now_iso})
        meter, service = service_factory(allowance)
        started = clock()
        try:
            decision = service.decide(row["mint"], source_path="desk", requested_at=now_iso)
            check, known_at = check_from_decision(decision), decision.completed_at
            ids = {"decision_id": decision.decision_id, "policy_id": decision.policy_id,
                   "metrics": decision.metrics}
        except Exception as exc:   # one row, once: a raising ticket never re-spends every pass
            check, known_at = {"status": "unknown", "reason": "cluster_unknown_worker_error"}, now_fn()
            ids = {"decision_id": None, "policy_id": None, "metrics": {"error": type(exc).__name__}}
        if meter["over"] and check["status"] != "kill":
            check = {"status": "unknown", "reason": "cluster_unknown_budget"}
        elif check["status"] == "unknown" and meter.get("last_error") and "error" not in ids["metrics"]:
            ids["metrics"] = {**(ids["metrics"] or {}), "last_error": meter["last_error"]}   # the cause, not a bare unknown
        charged = allowance if check["reason"] in IN_FLIGHT_REASONS else meter["calls"]
        extra = meter["extra"]() if callable(meter.get("extra")) else {}     # the desk parser version, page cap and deep owners
        record = {"schema": SCHEMA, **identity, **extra,
                  "known_at": known_at, "check": check, **ids, "rpc_calls": charged,
                  "rpc_calls_at_write": meter["calls"], "call_budget": allowance,
                  "written_at": now_iso,
                  "wall_ms": round((clock() - started) * 1000.0, 1)}
        _append_durable(out_dir / (str(now_iso)[:10] + ".jsonl"), record)
        stats["decided"] += 1
        if extra.get("fatal"):                                  # a provider auth/plan error: the pass stops, nothing retries into it
            stats.update(stop=extra["fatal"], capped=len(todo) - stats["decided"])
            break
    return stats
