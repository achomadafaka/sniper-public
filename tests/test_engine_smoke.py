"""WO-11 — the deployment smoke matrix must go green on a clean, synthetic state root.

This guards the gate itself: if a future refactor breaks the worker connect functions, the flag
gate, the kill switch, or the materialize->snapshot->decision plumbing, the operator's pre-arm gate
must fail loudly rather than pass a broken deploy. Runs the deterministic checks only (no
--state-root), which never touch production and spend zero RPC.
"""
import subprocess
import sys


def test_engine_smoke_synthetic_matrix_is_not_a_deployment_pass():
    r = subprocess.run(
        [sys.executable, "scripts/engine_smoke.py", "--synthetic"],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"smoke matrix FAILED:\n{r.stdout}\n{r.stderr}"
    assert "SYNTHETIC VERDICT: PASS" in r.stdout, r.stdout
    assert "DEPLOYMENT VERDICT: PASS" not in r.stdout
    # every deterministic check must be present and passing
    for check in ("db_modes:ticks_readonly", "flag_gate:on_picked_up_live",
                  "kill_switch:blocks_when_present", "worker:off_by_default",
                  "worker:process_started", "worker:fd_modes",
                  "materialize:snapshot_written", "decision:evidence_chain"):
        assert f"PASS] {check}" in r.stdout, f"missing/failed check {check}:\n{r.stdout}"


def test_engine_smoke_refuses_implicit_skip_all_mode():
    r = subprocess.run(
        [sys.executable, "scripts/engine_smoke.py"], capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "--synthetic" in (r.stdout + r.stderr)
    assert "--release-dir" in (r.stdout + r.stderr)


def test_engine_smoke_deployment_requires_both_paths(tmp_path):
    r = subprocess.run(
        [sys.executable, "scripts/engine_smoke.py", "--release-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "--state-root" in (r.stdout + r.stderr)


def test_deployment_kill_switch_check_requires_held_file(tmp_path):
    from scripts.engine_smoke import FAIL, PASS, Results, check_live_kill_switch_held
    (tmp_path / "data").mkdir()
    res = Results(); check_live_kill_switch_held(res, tmp_path)
    assert res.rows[-1][1] == FAIL
    (tmp_path / "data" / "memecoin_kill_switch").write_text("hold\n", encoding="utf-8")
    check_live_kill_switch_held(res, tmp_path)
    assert res.rows[-1][1] == PASS


def test_deployment_tape_freshness_rejects_future_timestamp(tmp_path):
    from datetime import datetime, timezone
    from scripts.engine_smoke import FAIL, Results, check_live_tape_fresh
    from sniper.persistence import db_ticks as t
    from sniper.util.clock import real_utc_now

    data = tmp_path / "data"
    data.mkdir()
    for name, chain in (("memecoin_ticks.db", "4663"), ("memecoin_sol_tape.db", "solana")):
        path = str(data / name)
        c = t.connect_ticks(path)
        observed = datetime.fromtimestamp(real_utc_now() + 3600, tz=timezone.utc).isoformat()
        t.insert_ticks(c, [{"chain_id": chain, "pool": "P", "kind": "sol_swap",
                            "block_no": 1, "tx_sig": "s", "log_index": 0, "maker": None,
                            "amount0": "1", "amount1": "-1", "observed_at": observed}])
        c.close()
    res = Results()
    check_live_tape_fresh(res, tmp_path, 300)
    assert [status for name, status, _ in res.rows if name.startswith("live:tape_fresh:")] == [FAIL, FAIL]
