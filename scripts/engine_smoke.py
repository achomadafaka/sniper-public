#!/usr/bin/env python3
"""WO-11 — engine deployment smoke harness. The activation gate.

Codex's final word before arming: "activation/deployment smoke testing, intentionally out of
scope." This is that gate. The operator runs it AFTER `deploy_memecoin_runtime.sh <sha>` and BEFORE
deleting the kill switch. It proves the plumbing the engine needs is wired correctly, in DRY mode,
spending zero live RPC and never mutating production.

Two explicit modes:
  * Deterministic checks (`--synthetic`) run entirely inside a throwaway temp STATE root with synthetic
    data — so this is runnable anywhere (CI, a laptop, a staging box) and proves the wiring:
    DB modes, the flag gate's live .env re-read, the kill switch, the worker's OFF-by-default gate,
    the materialize->snapshot->decision evidence chain.
  * Deployment checks (`--release-dir <pinned> --state-root <real>`) run the synthetic proof from
    the pinned release, then READ the real deployment READ-ONLY: the funded
    tick tape must be advancing on both chains, and the real .env flag must be OFF pre-arm. These
    never write the real state root.

Output: PASS / FAIL / SKIP per check, one page. Deployment mode fails on any FAIL or SKIP.

Nothing here signs, broadcasts, or enables anything. `MEMECOIN_EXIT_STATE_ENABLED` and
`MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS` stay whatever they were; the operator flips them, not this.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)



def _open_positions_on(chain, state_root):
    """Open REAL positions on `chain` from the state-root ledger; -1 if unreadable (keeps SKIP)."""
    try:
        import sqlite3
        c = sqlite3.connect(os.path.join(str(state_root), "sniper_tracker.db"), timeout=5)
        n = c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1 "
                      "AND execution_mode='real' AND network_id=?", (chain,)).fetchone()[0]
        c.close()
        return int(n)
    except Exception:
        return -1

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Results:
    def __init__(self):
        self.rows = []

    def add(self, name, status, detail=""):
        self.rows.append((name, status, detail))
        return status

    def render(self, *, mode, fail_on_skip=False):
        width = max(len(n) for n, _, _ in self.rows)
        print("\n" + "=" * 72)
        print("  ENGINE DEPLOYMENT SMOKE MATRIX")
        print("=" * 72)
        icon = {PASS: "✓", FAIL: "✗", SKIP: "—"}
        for name, status, detail in self.rows:
            print(f"  [{icon[status]} {status:4}] {name.ljust(width)}  {detail}")
        print("=" * 72)
        failed = [n for n, s, _ in self.rows if s == FAIL]
        skipped = [n for n, s, _ in self.rows if s == SKIP]
        verdict = "FAIL" if failed or (fail_on_skip and skipped) else "PASS"
        print(f"  {mode} VERDICT: {verdict}   ({len(self.rows)} checks, "
              f"{len(failed)} failed, {len(skipped)} skipped)")
        if failed:
            print("  DO NOT delete the kill switch. Failed: " + ", ".join(failed))
        print("=" * 72 + "\n")
        return 0 if verdict == "PASS" else 1


# ── synthetic ledger / ticks builders (temp root only) ───────────────────────

def _inject_synthetic_position(ledger_path):
    """A synthetic OPEN REAL Solana position in a throwaway ledger — never production."""
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(ledger_path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="smoke", source_handle="smoke", mint="SMOKEMINT1111111111111111111111",
        ticker="SMOKE", entry_price=0.0004, entry_notional=50.0, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-25T00:00:00+00:00",
        strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="solana")
    c.commit()
    c.close()
    return pid


def _init_empty_ticks(path):
    """Create the ticks file + tables so the read-only handle can open it (funded runner owns it)."""
    from sniper.persistence import db_ticks as t
    c = t.connect_ticks(path)
    c.close()


def _init_empty_ledger(path):
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.close()


# ── checks ───────────────────────────────────────────────────────────────────

def check_db_modes(res, root):
    """The own-DB split: the worker's ticks handle must be structurally read-only; its own exit-state
    and sol-tape handles must open read-write. A deploy that got the modes wrong re-creates the
    two-writer WAL-lock incident that disabled the worker."""
    from sniper.persistence import db_ticks as t
    from sniper.persistence import db_exit_state as xs
    ticks = str(root / "data" / "memecoin_ticks.db")
    _init_empty_ticks(ticks)
    try:
        ro = t.connect_ticks_readonly(ticks)
        try:
            ro.execute("INSERT INTO pool_ticks(chain_id,pool,kind,observed_at) VALUES('x','p','v2_swap','t')")
            ro.commit()
            res.add("db_modes:ticks_readonly", FAIL, "ticks handle accepted a WRITE")
        except sqlite3.OperationalError:
            res.add("db_modes:ticks_readonly", PASS, "write rejected (mode=ro)")
        finally:
            ro.close()
    except Exception as e:
        res.add("db_modes:ticks_readonly", FAIL, f"could not open ticks ro: {e}")

    for label, conn_fn in (("exit_state", xs.connect_exit_state), ("sol_tape", t.connect_sol_tape)):
        try:
            c = conn_fn()   # anchored at the temp state root via env override
            c.execute("PRAGMA user_version")
            c.close()
            res.add(f"db_modes:{label}_rw", PASS, "opened read-write in state root")
        except Exception as e:
            res.add(f"db_modes:{label}_rw", FAIL, f"{e}")


def check_flag_gate(res, root):
    """The engine authority flag is read LIVE from the state-root .env every decision boundary (a
    pinned release's process env is a stale import-time snapshot). OFF => zero eligible ids; ON =>
    the position's chain becomes eligible next sweep; malformed => fail OFF."""
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID
    from sniper.services import memecoin_real_runner as real_runner
    from sniper.services.memecoin_real_runner import _engine_authoritative_ids
    from unittest.mock import patch
    env_path = root / ".env"
    pos = {"id": 1, "network_id": "solana", "mint": "SMOKE"}
    frozen = {1: type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID})()}

    env_path.write_text("MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS=\n", encoding="utf-8")
    off = cfg.engine_authoritative_chains()
    with patch.object(real_runner, "engine_policy_runtime_capable", return_value=True):
        off_ids = _engine_authoritative_ids([pos], frozen, limit=5)
    ok_off = (off == set() and off_ids == set())
    res.add("flag_gate:off_zero_eligible", PASS if ok_off else FAIL,
            f"chains={off or '{}'} eligible={off_ids or '{}'}")

    env_path.write_text("MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS=solana\n", encoding="utf-8")
    on = cfg.engine_authoritative_chains()
    with patch.object(real_runner, "engine_policy_runtime_capable", return_value=True):
        on_ids = _engine_authoritative_ids([pos], frozen, limit=5)
    ok_on = ("solana" in on and on_ids == {1})
    res.add("flag_gate:on_picked_up_live", PASS if ok_on else FAIL,
            f"chains={on} eligible={on_ids}")

    env_path.write_text('MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS="solana\n', encoding="utf-8")  # unbalanced quote
    ok_bad = (cfg.engine_authoritative_chains() == set())
    res.add("flag_gate:malformed_fails_off", PASS if ok_bad else FAIL,
            "malformed quote -> {}" if ok_bad else "malformed quote was NOT rejected")
    env_path.unlink(missing_ok=True)


def check_kill_switch(res, root):
    """Kill switch present => entries fail-closed; absent => not tripped. Fail-closed on any read error."""
    from sniper.services.memecoin_arming import is_kill_tripped
    kill = root / "data" / "memecoin_kill_switch"
    kill.unlink(missing_ok=True)
    absent = is_kill_tripped(str(kill))
    kill.write_text("tripped by smoke test\n", encoding="utf-8")
    present = is_kill_tripped(str(kill))
    kill.unlink(missing_ok=True)
    ok = (absent is False and present is True)
    res.add("kill_switch:blocks_when_present", PASS if ok else FAIL,
            f"absent={absent} present={present}")


def check_exit_state_flag_off(res):
    """The materializer worker is OFF by default — no RPC spend, no connection — until the operator
    sets MEMECOIN_EXIT_STATE_ENABLED. Enabling it is itself an operator STOP condition."""
    import scripts.memecoin_exit_state_worker as w
    prev = os.environ.pop("MEMECOIN_EXIT_STATE_ENABLED", None)
    try:
        off = w._enabled()
        os.environ["MEMECOIN_EXIT_STATE_ENABLED"] = "1"
        on = w._enabled()
    finally:
        os.environ.pop("MEMECOIN_EXIT_STATE_ENABLED", None)
        if prev is not None:
            os.environ["MEMECOIN_EXIT_STATE_ENABLED"] = prev
    ok = (off is False and on is True)
    res.add("worker:off_by_default", PASS if ok else FAIL, f"default={off} when_set={on}")
    import scripts.memecoin_sol_tape_worker as sw
    sol_prev = os.environ.pop("MEMECOIN_SOL_TAPE_ENABLED", None)
    try:
        sol_off = sw._enabled()
        os.environ["MEMECOIN_SOL_TAPE_ENABLED"] = "1"
        sol_on = sw._enabled()
    finally:
        os.environ.pop("MEMECOIN_SOL_TAPE_ENABLED", None)
        if sol_prev is not None:
            os.environ["MEMECOIN_SOL_TAPE_ENABLED"] = sol_prev
    res.add("sol_worker:off_by_default", PASS if not sol_off and sol_on else FAIL,
            f"default={sol_off} when_set={sol_on}")


def check_worker_process(res, root):
    """Launch the actual worker entrypoint, then inspect its live database descriptors.

    Import-only smoke missed the two-writer failure.  This check proves the deployed process can
    start with zero RPC and that the funded tick tape is O_RDONLY while worker-owned stores are RW.
    """
    ticks = root / "data" / "memecoin_ticks.db"
    ledger = root / "sniper_tracker.db"
    _init_empty_ticks(str(ticks))
    _init_empty_ledger(str(ledger))
    from sniper.persistence import db_ticks as _db_ticks
    seed_sol = _db_ticks.connect_sol_tape(str(root / "data" / "memecoin_sol_tape.db"))
    seed_sol.close()
    ready = root / "worker-smoke-ready.json"
    cmd = [sys.executable, str(Path(_REPO_ROOT) / "scripts" / "memecoin_exit_state_worker.py"),
           "--smoke-no-rpc", "--smoke-ready-file", str(ready), "--smoke-hold-s", "10"]
    proc = subprocess.Popen(cmd, cwd=_REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={**os.environ, "PYTHONPATH": _REPO_ROOT})
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and proc.poll() is None and not ready.exists():
            time.sleep(0.05)
        if proc.poll() is not None or not ready.exists():
            out, err = proc.communicate(timeout=2)
            res.add("worker:process_started", FAIL,
                    f"rc={proc.returncode} stdout={out[-200:]} stderr={err[-400:]}")
            res.add("worker:fd_modes", FAIL, "worker was not available for /proc inspection")
            return
        payload = json.loads(ready.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        res.add("worker:process_started", PASS, f"pid={pid} no-RPC pass completed")

        expected = {os.path.realpath(v): k for k, v in payload["paths"].items()
                    if k in {"ticks", "exit_state", "sol_tape"}}
        found = {}
        for fd_name in os.listdir(f"/proc/{pid}/fd"):
            fd_path = f"/proc/{pid}/fd/{fd_name}"
            try:
                target = os.path.realpath(os.readlink(fd_path))
            except (FileNotFoundError, PermissionError):
                continue
            label = expected.get(target)
            if not label:
                continue
            try:
                info = Path(f"/proc/{pid}/fdinfo/{fd_name}").read_text(encoding="utf-8")
                flags_line = next(line for line in info.splitlines() if line.startswith("flags:"))
                flags = int(flags_line.split()[1], 8)
                found[label] = flags & os.O_ACCMODE
            except (OSError, StopIteration, ValueError):
                found[label] = None
        ok = (found.get("ticks") == os.O_RDONLY
              and found.get("exit_state") == os.O_RDWR
              and found.get("sol_tape") == os.O_RDONLY)
        res.add("worker:fd_modes", PASS if ok else FAIL,
                f"ticks={found.get('ticks')} exit_state={found.get('exit_state')} "
                f"sol_tape={found.get('sol_tape')} (0=RO,2=RW)")
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=3)


def check_materialize_and_decision(res, root):
    """End-to-end plumbing on synthetic data, zero RPC: a synthetic open real position materializes a
    market-state snapshot, and a decision row lands carrying the evidence-chain fields
    (state_id + outcome + executable_confirmed). This is the wiring the engine's authority depends on."""
    import scripts.memecoin_exit_state_worker as w
    from sniper.persistence import db_exit_state as xs
    from sniper.analysis.exit_state_types import market_state_from_dict
    from sniper.services import memecoin_real_runner as runner
    from sniper.services.memecoin_exit_brain import DEFER, CandidateDecision, exit_engine
    from sniper.settings import memecoin as cfg

    ledger = str(root / "sniper_tracker.db")
    pid = _inject_synthetic_position(ledger)
    _init_empty_ticks(str(root / "data" / "memecoin_ticks.db"))

    from sniper.persistence import db_ticks as _db_ticks
    seed_sol = _db_ticks.connect_sol_tape()
    seed_sol.close()
    mconn = w._connect_ledger()
    tc = w._connect_ticks()
    xc = w._connect_exit_state()
    sol_tc = w._connect_sol_tape_readonly()
    try:
        # get_block_fn=None -> no network at all; a no-pool position still gets an
        # honest SOURCE_UNTRUSTED snapshot, which is all this plumbing check needs to observe.
        w.run_pass(mconn, tc, xc, 1_800_000_000, get_block_fn=None, sol_tc=sol_tc)
        snap = xs.latest_state(xc, pid)
        if not snap:
            res.add("materialize:snapshot_written", FAIL, "no snapshot for synthetic position")
            return
        res.add("materialize:snapshot_written", PASS,
                f"state={snap.get('state')} id={snap.get('id')}")

        decided_at = "2026-07-25T00:00:05+00:00"
        state = market_state_from_dict(json.loads(snap["state_json"]))
        decision = CandidateDecision(action=DEFER, reason_codes=("smoke",),
                                     no_action_reason="synthetic smoke")
        engine_version, _proposer = exit_engine(cfg.MEMECOIN_EXIT_ENGINE_VERSION)
        record = runner._engine_audit_record(
            {"id": pid}, state, decision, decided_at, state_id=snap.get("id"),
            engine_version=engine_version)
        # Match the funded path's deferred-flush contract: outcome and exact-confirmation are final
        # before the single INSERT.  The smoke never patches the row after it lands.
        record["executable_confirmed"] = True
        record["outcome"] = "smoke_ok"
        runner._flush_engine_decisions(xc, [record])
        row = xc.execute(
            "SELECT state_id, outcome, executable_confirmed FROM memecoin_exit_candidate_decisions "
            "WHERE position_id=? AND decided_at=?", (pid, decided_at)).fetchone()
        version = xc.execute(
            "SELECT engine_version FROM memecoin_exit_candidate_decisions "
            "WHERE position_id=? AND decided_at=?", (pid, decided_at)).fetchone()
        ok = (row and version and version[0] == engine_version
              and row["state_id"] == snap.get("id") and row["outcome"] == "smoke_ok"
              and row["executable_confirmed"] == 1)
        res.add("decision:evidence_chain", PASS if ok else FAIL,
                f"state_id={row['state_id']} outcome={row['outcome']} "
                f"confirmed={row['executable_confirmed']}" if row else "no decision row")
    finally:
        for c in (mconn, tc, xc, sol_tc):
            try:
                c.close()
            except Exception:
                pass


def check_live_tape_fresh(res, real_root, max_age_s):
    """READ-ONLY liveness against a real deployment: the funded tick tape must be advancing.

    Own-DB split (2026-07-24): EVM (4663) ticks live in the shared ticks DB, which the funded runner
    writes continuously; Solana ticks live in their OWN sol-tape DB, populated only once the exit-state
    worker is enabled. So Solana freshness is only meaningful post-enable — absent sol-tape DB => SKIP
    (pre-arm expected), present-but-stale => FAIL. Reads each DB with mode=ro (never a write)."""
    from sniper.persistence import db_ticks as t
    from sniper.util.clock import real_utc_now
    from datetime import datetime
    # REAL deployment's learned clock offset (this host runs ~+7h fast; without this every age is
    # inflated by the skew).
    now = real_utc_now(str(Path(real_root) / "data" / "clock_offset.json"))

    def _fresh(label, db_path, chain, skip_if_absent):
        if not os.path.exists(db_path):
            res.add(f"live:tape_fresh:{label}", SKIP if skip_if_absent else FAIL,
                    f"no DB at {db_path}"
                    + (" (worker not yet enabled)" if skip_if_absent else ""))
            return
        conn = t.connect_ticks_readonly(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(observed_at) FROM pool_ticks WHERE chain_id=?", (chain,)).fetchone()
            latest = row[0] if row else None
        finally:
            conn.close()
        if not latest:
            if skip_if_absent and _open_positions_on(chain, real_root) == 0:
                # The sol tape is PER-POSITION by design: zero open positions -> zero ticks is
                # the correct state, not an untestable skip. Without this, the pre-switch smoke
                # can never fully pass (bootstrap circularity: tape needs a position, positions
                # need the switch, the switch waits on the smoke).
                res.add(f"live:tape_fresh:{label}", PASS,
                        "vacuous: no open positions on this chain; per-position tape empty by design")
                return
            res.add(f"live:tape_fresh:{label}",
                    SKIP if skip_if_absent else FAIL,
                    "no ticks recorded" + (" (worker not yet enabled)" if skip_if_absent else ""))
            return
        try:
            age = now - datetime.fromisoformat(str(latest).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            res.add(f"live:tape_fresh:{label}", FAIL, f"unparseable observed_at={latest}")
            return
        # A future timestamp is not fresh evidence; it usually means the corrected-clock anchor is
        # wrong. Allow only tiny scheduling/serialization jitter.
        res.add(f"live:tape_fresh:{label}", PASS if -5.0 <= age <= max_age_s else FAIL,
                f"latest tick {age:.0f}s ago (bound {max_age_s:.0f}s)")

    ticks_db = str(Path(real_root) / "data" / "memecoin_ticks.db")
    sol_db = str(Path(real_root) / "data" / "memecoin_sol_tape.db")
    _fresh("4663", ticks_db, "4663", skip_if_absent=False)          # funded runner writes this always
    _fresh("solana", sol_db, "solana", skip_if_absent=True)         # worker-populated; SKIP pre-enable


def check_live_flag_off(res, real_root):
    """Pre-arm safety: the real deployment's engine authority flag must be OFF before smoke passes —
    smoke runs BEFORE the operator flips it. Reads the real state-root .env only."""
    from sniper.settings import memecoin as cfg
    chains = cfg.engine_authoritative_chains(env={"SNIPER_STATE_ROOT": str(real_root)})
    ok = (chains == set())
    res.add("live:engine_flag_off", PASS if ok else FAIL,
            "OFF (pre-arm)" if ok else f"already ON: {chains} — expected OFF before smoke")


def check_live_kill_switch_held(res, real_root):
    """Pre-switch safety: deployment smoke runs while the entry kill switch is still held."""
    from sniper.services.memecoin_arming import is_kill_tripped
    path = Path(real_root) / "data" / "memecoin_kill_switch"
    held = is_kill_tripped(str(path))
    res.add("live:kill_switch_held", PASS if held else FAIL,
            "held (pre-switch)" if held else f"not held at {path}")


def main():
    ap = argparse.ArgumentParser(description="Engine deployment smoke matrix (DRY, no live RPC).")
    ap.add_argument("--synthetic", action="store_true",
                    help="run CI plumbing only; never emits a deployment PASS")
    ap.add_argument("--release-dir", help="pinned deployed release directory")
    ap.add_argument("--state-root", help="real deployment state root for READ-ONLY live checks")
    ap.add_argument("--max-tape-age-s", type=float, default=300.0,
                    help="live tape freshness bound (default 300s)")
    args = ap.parse_args()

    if args.synthetic and (args.release_dir or args.state_root):
        ap.error("--synthetic cannot be combined with --release-dir/--state-root")
    if not args.synthetic and not (args.release_dir and args.state_root):
        ap.error("choose --synthetic, or provide BOTH --release-dir and --state-root")

    if not args.synthetic:
        release = Path(args.release_dir).expanduser().resolve()
        release_smoke = release / "scripts" / "engine_smoke.py"
        if not release_smoke.is_file() or not (release / "sniper").is_dir():
            print(f"DEPLOYMENT VERDICT: FAIL — invalid release dir: {release}")
            return 1
        import subprocess
        child = subprocess.run(
            [sys.executable, str(release_smoke), "--synthetic"], cwd=str(release),
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(release)})
        res = Results()
        res.add("release:synthetic_plumbing", PASS if child.returncode == 0 else FAIL,
                f"release={release} rc={child.returncode}")
        check_live_tape_fresh(res, Path(args.state_root).expanduser().resolve(), args.max_tape_age_s)
        check_live_flag_off(res, Path(args.state_root).expanduser().resolve())
        check_live_kill_switch_held(res, Path(args.state_root).expanduser().resolve())
        return res.render(mode="DEPLOYMENT", fail_on_skip=True)

    res = Results()
    _saved_env = {k: os.environ.get(k) for k in (
        "SNIPER_STATE_ROOT", "DB_PATH", "MEMECOIN_TICKS_DB", "MEMECOIN_EXIT_STATE_DB",
        "MEMECOIN_SOL_TAPE_DB")}
    with tempfile.TemporaryDirectory(prefix="engine_smoke_") as td:
        root = Path(td)
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        # Every path resolver reads these at call time -> the whole run is anchored in the temp root.
        os.environ["SNIPER_STATE_ROOT"] = str(root)
        os.environ["DB_PATH"] = str(root / "sniper_tracker.db")
        os.environ["MEMECOIN_TICKS_DB"] = str(root / "data" / "memecoin_ticks.db")
        os.environ["MEMECOIN_EXIT_STATE_DB"] = str(root / "data" / "memecoin_exit_state.db")
        os.environ["MEMECOIN_SOL_TAPE_DB"] = str(root / "data" / "memecoin_sol_tape.db")

        check_db_modes(res, root)
        check_flag_gate(res, root)
        check_kill_switch(res, root)
        check_exit_state_flag_off(res)
        check_worker_process(res, root)
        check_materialize_and_decision(res, root)

    # Leaving the temp root deleted: restore env so the live checks (and clock offset) resolve
    # against the REAL deployment, not a dangling temp path.
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    return res.render(mode="SYNTHETIC")


if __name__ == "__main__":
    sys.exit(main())
