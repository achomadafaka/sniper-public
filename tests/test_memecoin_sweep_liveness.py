"""Item 8 — exit-sweep liveness: a dead/failing bracket sweep (SL/TP/timecap) must be visible
even when the poll loop is fresh and auth is fine, so real stop-losses can't fail silently under
a green heartbeat. runner_health flags a stale exit sweep as restart-worthy."""
from sniper.services import memecoin_runner as run


def test_runner_health_flags_stale_exit_sweep():
    hb = {"ts": 1000.0, "auth_ok": True, "bracket_ts": 1000.0 - 200}   # poll fresh, sweep 200s stale
    r = run.runner_health(hb, now_epoch=1000.0, max_age_s=120, bracket_max_age_s=90)
    assert r["healthy"] is False and r["reason"] == "sweep_stale"


def test_runner_health_ok_when_sweep_fresh():
    hb = {"ts": 1000.0, "auth_ok": True, "bracket_ts": 1000.0 - 10}
    r = run.runner_health(hb, now_epoch=1000.0, max_age_s=120, bracket_max_age_s=90)
    assert r["healthy"] is True and r["reason"] == "ok"


def test_runner_health_ignores_missing_bracket_ts():
    # backward compat: a heartbeat without bracket_ts (pre-item8) is not flagged for the sweep
    hb = {"ts": 1000.0, "auth_ok": True}
    r = run.runner_health(hb, now_epoch=1000.0, max_age_s=120, bracket_max_age_s=90)
    assert r["healthy"] is True


def test_write_heartbeat_carries_bracket_ts(tmp_path):
    p = str(tmp_path / "hb.json")
    run.write_heartbeat({"events": 1, "opened": 0}, auth_ok=True, path=p, ts=1000.0, bracket_ts=999.0)
    hb = run.read_heartbeat(p)
    assert hb["bracket_ts"] == 999.0 and hb["ts"] == 1000.0


def test_write_heartbeat_carries_runner_start_time(tmp_path):
    p = str(tmp_path / "hb.json")
    started_at = "2026-08-19T11:00:00+00:00"
    run.write_heartbeat(
        {"events": 1, "opened": 0}, auth_ok=True, path=p, ts=1000.0,
        started_at=started_at,
    )
    assert run.read_heartbeat(p)["started_at"] == started_at


def test_write_heartbeat_carries_sanitized_tpu_status(tmp_path):
    p = str(tmp_path / "hb.json")
    status = {
        "configured": True, "ready": False, "running": True,
        "restart_count": 2, "leader_count": None,
        "last_health_age_s": 4.5, "reason": "socket_unavailable",
    }

    run.write_heartbeat(
        {"events": 0, "opened": 0}, auth_ok=True, path=p, ts=1000.0,
        bracket_ts=999.0, tpu_status=status)

    assert run.read_heartbeat(p)["tpu"] == status


def test_write_heartbeat_carries_sanitized_rpc_pool_status(tmp_path):
    p = str(tmp_path / "hb.json")
    status = {
        "active_index": 1, "endpoint_count": 4,
        "exhausted_count": 1, "fallback_active": False,
    }

    run.write_heartbeat(
        {"events": 0, "opened": 0}, auth_ok=True, path=p, ts=1000.0,
        bracket_ts=999.0, rpc_pool_status=status)

    assert run.read_heartbeat(p)["solana_rpc_pool"] == status


def test_write_heartbeat_carries_sanitized_fastlane_status(tmp_path):
    p = str(tmp_path / "hb.json")
    status = {
        "status": "healthy", "thread_alive": True, "restart_count": 0,
        "last_success_age_s": 0.2, "last_failure_age_s": None,
        "failure": None, "fallback_active": False,
    }

    run.write_heartbeat(
        {"events": 0, "opened": 0}, auth_ok=True, path=p, ts=1000.0,
        bracket_ts=999.0, fastlane_status=status)

    assert run.read_heartbeat(p)["fastlane"] == status


def test_write_heartbeat_carries_fomo_activity_websocket_status(tmp_path):
    p = str(tmp_path / "hb.json")
    status = {
        "status": "subscribed",
        "connected_at": "2026-08-31T19:14:58+00:00",
        "subscribed_at": "2026-08-31T19:14:59+00:00",
        "last_payload_at": "2026-08-31T19:15:01+00:00",
        "last_error_at": None,
        "reconnect_count": 0,
        "raw_error": "must-not-leak",
    }

    run.write_heartbeat(
        {"events": 0, "opened": 0}, auth_ok=True, path=p, ts=1000.0,
        fomo_activity_ws_status=status,
    )

    saved = run.read_heartbeat(p)["fomo_activity_ws"]
    assert saved == {key: value for key, value in status.items() if key != "raw_error"}


def test_write_heartbeat_carries_only_sanitized_lifi_status(tmp_path):
    p = str(tmp_path / "hb")
    status = {
        "corrected_at": "2026-08-30T20:00:00+00:00",
        "runner_heartbeat_at": "2026-08-30T20:00:00+00:00",
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
        "chain_id": "4663", "custody_wallet": "0x" + "1" * 40,
        "mode": "disabled", "authenticated": False, "effective_limit": None,
        "remaining": None, "reset_s": None, "in_flight": 0, "exit_reserve": 0,
        "slots_used": 0, "slot_limit": 0, "entry_admitted": False,
        "disable_reasons": ["disabled"], "unresolved_approvals": 0,
        "unresolved_swaps": 0, "unresolved_revokes": 0, "unresolved_finality": 0,
        "api_key": "must-not-leak", "raw_calldata": "0xdead",
    }

    run.write_heartbeat({}, True, path=p, ts=1, lifi_status=status)

    saved = run.read_heartbeat(p)["lifi"]
    assert saved["mode"] == "disabled"
    assert "api_key" not in saved and "raw_calldata" not in saved


def test_launch_health_requires_heartbeat_and_sweep_from_the_new_process():
    before_launch = {"ts": 999.0, "auth_ok": True, "bracket_ts": 999.0}
    r = run.runner_launch_health(
        before_launch, launched_at=1000.0, now_epoch=1001.0,
        max_age_s=120, bracket_max_age_s=90,
    )
    assert r["healthy"] is False and r["reason"] == "launch_heartbeat_missing"

    no_sweep = {"ts": 1001.0, "auth_ok": True}
    r = run.runner_launch_health(
        no_sweep, launched_at=1000.0, now_epoch=1001.0,
        max_age_s=120, bracket_max_age_s=90,
    )
    assert r["healthy"] is False and r["reason"] == "launch_sweep_missing"

    old_sweep = {"ts": 1002.0, "auth_ok": True, "bracket_ts": 999.0}
    r = run.runner_launch_health(
        old_sweep, launched_at=1000.0, now_epoch=1002.0,
        max_age_s=120, bracket_max_age_s=90,
    )
    assert r["healthy"] is False and r["reason"] == "launch_sweep_missing"

    live = {"ts": 1002.0, "auth_ok": True, "bracket_ts": 1001.0}
    r = run.runner_launch_health(
        live, launched_at=1000.0, now_epoch=1002.0,
        max_age_s=120, bracket_max_age_s=90,
    )
    assert r["healthy"] is True and r["reason"] == "ok"


def test_write_heartbeat_carries_evm_rpc_pool_by_host(tmp_path):
    """2026-09-03: 4663 provider health was invisible in the heartbeat; a dead custody endpoint read
    as a total outage. Hosts only -- a custody url may carry an API key."""
    p = str(tmp_path / "hb.json")
    status = {"4663": [{"host": "robinhood-mainnet.g.alchemy.com", "ewma_ms": 116.0, "ok": 10,
                        "fail": 0, "lag_blocks": 0, "quarantined_s": 0.0, "identity": True}]}
    run.write_heartbeat({"events": 0, "opened": 0}, auth_ok=True, path=p, ts=1000.0,
                        evm_rpc_pool_status=status)
    got = run.read_heartbeat(p)["evm_rpc_pool"]
    assert got == status
    assert "http" not in __import__("json").dumps(got)
