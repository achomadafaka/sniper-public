"""Heartbeat writes remain best-effort and failures name malformed sections."""
import inspect
import json
import logging

from sniper.analysis.memecoin_quote_failures import QuoteFailure
from sniper.services import memecoin_runner as run


def _real_producers():
    from sniper.execution.solana_real_seams import jupiter_load_snapshot
    from sniper.services import memecoin_exit_tape, memecoin_route_series
    from sniper.services.memecoin_addon_guard import RING
    return {"jupiter_load": jupiter_load_snapshot(), "route_series": memecoin_route_series.snapshot(),
            "exit_tape": memecoin_exit_tape.snapshot() or {"appended": 0},
            "rails_observability": {"addon_ring": dict(RING.stats),
                                    "last_failure": QuoteFailure.ROUTE_NOT_INDEXED.value}}


def test_every_heartbeat_keyword_populated_writes(tmp_path):
    params = [p for p in inspect.signature(run.write_heartbeat).parameters
              if p not in ("result", "auth_ok", "path", "ts", "bracket_ts", "started_at", "_base_payload")]
    kwargs = {name: {"probe": 1} for name in params}
    kwargs.update(submit_audit={"/x/ledger.db": {"queued": 0}}, lifi_status={"state": "ok"},
                  evm_rpc_pool_status={"56": ["host-a"]}, onchain_volume_feed={"4663": {"ok": 1}},
                  research_rpc_pool={"helius": [{"id": "k1"}]}, entry_retry_workers={"solana": {"queued": 0}},
                  slot_lane_status={"mode": "live", "exotic_warm": {"refreshed": 0}},
                  quote_price_warmers={"56": {"ok": 1}}, **_real_producers())
    path = tmp_path / "heartbeat.json"
    assert run.write_heartbeat({"events": 1}, auth_ok=True, path=str(path), ts=1.0, bracket_ts=1.0,
                               started_at="2026-10-17T00:00:00+00:00", **kwargs) is True
    assert json.loads(path.read_text())["ts"] == 1.0


def test_an_unserializable_section_is_logged_by_name_and_never_raised(tmp_path, caplog):
    from sniper.util import log_sample
    log_sample._last.pop(("heartbeat_write_failed",), None)
    path = tmp_path / "heartbeat.json"
    with caplog.at_level(logging.ERROR):
        ok = run.write_heartbeat({}, auth_ok=True, path=str(path),
                                 rails_observability={"fine": 1, "bad": object()})
    assert ok is False and not path.exists()
    lines = [r.getMessage() for r in caplog.records if "heartbeat write FAILED" in r.getMessage()]
    assert lines and "rails_observability(TypeError at bad:object)" in lines[0], lines


def test_diagnostics_failure_cannot_escape_the_heartbeat_writer(tmp_path, monkeypatch):
    from sniper.util import log_sample
    def unavailable(*args, **kwargs):
        raise RuntimeError("logging unavailable")
    monkeypatch.setattr(log_sample, "log_sampled", unavailable)
    assert run.write_heartbeat({}, auth_ok=True, path=str(tmp_path / "beat"),
                               rails_observability={"bad": object()}) is False


def test_heartbeat_reports_the_running_modules_execution_flags(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    path = tmp_path / 'beat'
    for enabled in (True, False):
        monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_NATIVE_EXIT', enabled)
        monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP', enabled)
        assert run.write_heartbeat({}, auth_ok=True, path=str(path))
        assert json.loads(path.read_text())['solana_execution'] == {
            'native_exit_enabled': enabled, 'exotic_round_trip_enabled': enabled}
