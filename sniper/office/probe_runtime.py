"""T15 step 2 probe as a supervisor runtime factory (Alpha option a, 2026-10-29):

    office_supervisor.py ... --runtime-factory sniper.office.probe_runtime:build --once

It runs inside the pinned office-supervisor unit, so the launcher's validate_peer authenticates it unchanged. It
wraps sniper.office.runtime.build (all pins, guard, caps, clock, identity and transports) and replaces the agent
task list: no agent turns run. Each --once pass advances the probe one phase, recorded in
data/office/probe/report.jsonl:
  - no trader record: sheet -> 2 blind judges -> aggregate -> Risk -> Trader (paper), from data/office/probe/input.json
  - trader recorded, order open: nothing (the always-on paper watcher exits it)
  - order closed/censored or no order: probe grade (economic_outcome), then done
A stop record or a grade record is terminal; delete the report to retry. Model calls need input.json
"allow_model_calls": true and go through the runtime's Scheduler (provider caps and the host guard apply)."""
import contextlib
import json
import time
from pathlib import Path

from sniper.office import ledger as L, pilot_models, probe as P, runtime as RT
from sniper.office.host_auth import approved_path
from sniper.office.supervisor import StaleGeneration

PROBE_DIR = "data/office/probe"
INPUT_KEYS = {"decision_id", "candidate", "hard_filter_row", "desk_policy", "strategy_epoch", "allow_model_calls",
              "bound_usd_per_fill"}


def check_inside_commit(ledger_conn, check):
    """A write fence from a lease check: the check runs inside the ledger write transaction after the writes and
    before the commit, so a takeover during the writes rolls them back. Any failure is ProbeRetry('lease_lost')."""
    @contextlib.contextmanager
    def fence():
        try:
            with L._write(ledger_conn):
                yield
                check()
        except P.ProbeRetry:
            raise
        except Exception:
            raise P.ProbeRetry("lease_lost") from None
    return fence


def read_report(path):
    rows = []
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            rows.append(json.loads(line))
    return rows


def probe_pass(*, probe_dir, store_conn, ledger_conn, book_policy_path, desk_dir, clock_file, scheduler, transports,
               manifest, prompts, deadline_s, now_host=time.time, ledger_reader=None, fx_reader=None, clock_reader=None,
               write_fence=None, lease_left=None, fence=None):
    """One phase. Returns the phase name: done | front | waiting_exit | graded | no_order | stopped | retry.
    deadline_s bounds the WHOLE phase (every judge call gets only what remains). write_fence() wraps every mutation and
    commits only while the supervisor lease is ours (compare-and-write). lease_left() -> seconds of lease left; a model
    call starts only if the lease outlives the call's budget."""
    if write_fence is None and fence is not None:
        write_fence = check_inside_commit(ledger_conn, fence)
    write_fence = write_fence or contextlib.nullcontext
    probe_dir = Path(probe_dir)
    report_path = probe_dir / "report.jsonl"
    rows = read_report(report_path)
    steps = {r["step"]: r["payload"] for r in rows}
    if "grade" in steps or "stop" in steps:
        return "done"
    doc = json.loads((probe_dir / "input.json").read_text())
    if set(doc) != INPUT_KEYS:
        raise P.ProbeStop("input_keys")

    def report(step, payload):
        with report_path.open("a") as f:
            f.write(json.dumps({"label": P.PROBE_LABEL, "epoch_id": P.PROBE_EPOCH, "decision_id": doc["decision_id"],
                                "step": step, "payload": payload}, sort_keys=True, default=repr) + "\n")
            f.flush()

    host = now_host()
    offset, sampled_at = clock_reader(clock_file, now=host)
    if offset is None:
        return "clock_unknown"                          # not terminal: the next pass retries
    corrected = host - offset
    deadline_at = host + deadline_s
    try:
        with write_fence():
            policy = P.bootstrap_probe_book(ledger_conn, book_policy_path, now=corrected)
        if "trader" not in steps:
            def scheduled(slot, user, schema):      # caps + host guard: same Scheduler as the agent turns
                if doc["allow_model_calls"] is not True:
                    raise P.ProbeStop("model_calls_not_allowed")
                remaining = deadline_at - now_host()
                if remaining <= 0:
                    raise P.ProbeRetry("phase_deadline")
                if lease_left is not None and not lease_left() > remaining:
                    raise P.ProbeRetry("lease_margin")      # never start a call the lease cannot outlive
                call = P.judge_call_over(manifest, prompts, transports, lambda: now_host() - offset,
                                         decision_id=doc["decision_id"], deadline_s=remaining,
                                         allow_model_calls=doc["allow_model_calls"])
                return scheduler.submit(manifest["seats"][slot]["provider"], lambda: call(slot, user, schema),
                                        timeout_s=remaining, priority="live")
            fx, fx_at = fx_reader(desk_dir, now_host=host, offset=offset)
            P.front(decision_id=doc["decision_id"], store_conn=store_conn, ledger_conn=ledger_conn, policy=policy,
                    sheet_kwargs=dict(candidate=doc["candidate"], desk_dir=desk_dir, policy=doc["desk_policy"],
                                      clock_sample={"offset_s": offset, "sampled_at": sampled_at},
                                      hard_filter_row=doc["hard_filter_row"], now=host, buyer_ledger=ledger_reader(),
                                      strategy_epoch=doc["strategy_epoch"]),
                    judge_call=scheduled, now=corrected, sol_usd=fx, sol_at=fx_at, report=report, prior=steps,
                    write_fence=write_fence, deadline_at=deadline_at, clock=now_host)
            return "front"
        return P.back(ledger_conn, steps["trader"], look_at=corrected, bound_usd_per_fill=doc["bound_usd_per_fill"],
                      report=report)
    except P.ProbeRetry as exc:
        print(json.dumps({"state": "probe", "phase": "retry", "reason": str(exc)}), flush=True)
        return "retry"
    except P.ProbeStop as exc:
        report("stop", {"reason": str(exc)})
        return "stopped"


def _buyer_ledger():
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.persistence.db_memecoin import desk_buyer_ledger
    try:
        conn = get_readonly_connection()
        try:
            return desk_buyer_ledger(conn)
        finally:
            conn.close()
    except Exception as exc:                            # typed unknown in the sheet, same as the judge worker
        return exc


def build(*, store_path, ledger_path, config_path, guard_path):
    from scripts.office_paper_watcher import read_fx
    from sniper.services.memecoin_desk_intake import clock_sample
    base = RT.build(store_path=store_path, ledger_path=ledger_path, config_path=config_path, guard_path=guard_path)
    config, _ = RT._read_config(config_path)
    state = approved_path(config["state_root"])
    manifest = pilot_models.load(config["model_manifest"]["path"], config["model_manifest"]["sha256"])
    prompts = config["pilot_seed_launcher_config"]["prompts"]
    ledger = L.open_ledger(ledger_path)
    base_close = base["close"]
    lease = base["supervisor"].lease
    held = {}

    def initialize(gen):
        if "initialize" in base:
            base["initialize"](gen)
        held["gen"] = gen

    @contextlib.contextmanager
    def write_fence():
        """Compare-and-write: the Office write lock (blocks any takeover) plus the ledger write transaction; the lease
        is checked again INSIDE the ledger transaction, so the commit fails if the generation changed or it expired."""
        if "gen" not in held:
            raise P.ProbeRetry("lease_unknown")
        gen = held["gen"]
        try:
            with lease.guard(gen):
                with L._write(ledger):
                    yield
                    lease.check(gen)
        except StaleGeneration:
            raise P.ProbeRetry("lease_lost") from None

    def lease_left():
        row = base["supervisor"].conn.execute("SELECT expires_at FROM supervisor_lease WHERE id=1").fetchone()
        now = lease.clock()
        if row is None or type(now) not in (int, float):
            return -1.0                                   # unknown lease or clock: no model call starts
        return row[0] - now

    def tasks():
        base["tasks"]()                                  # readiness only (raises when unready); agent turns are dropped
        phase = probe_pass(probe_dir=state / PROBE_DIR, store_conn=base["supervisor"].conn, ledger_conn=ledger,
                           book_policy_path=Path(config["config_root"]) / "book_policy_pilot.json",
                           desk_dir=state / "data/desk", clock_file=config["clock_file"], scheduler=base["scheduler"],
                           transports=base["transports"], manifest=manifest, prompts=prompts,
                           deadline_s=config["turn_deadline_s"], ledger_reader=_buyer_ledger, fx_reader=read_fx,
                           clock_reader=clock_sample, write_fence=write_fence, lease_left=lease_left)
        print(json.dumps({"state": "probe", "phase": phase}), flush=True)
        return []

    def close():
        ledger.close()
        base_close()
    return dict(base, tasks=tasks, close=close, initialize=initialize)
