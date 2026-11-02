"""Offline follow-up witnesses for the immutable 94125c44c learning-loop pin."""
import sqlite3
from types import SimpleNamespace

from test_desk_lessons import L, W, T0, swap, unit, _fixture


def test_partial_entry_horizon_is_not_a_complete_maximum():
    tape = L.Tape([swap(T0 - 1, 3e-8), swap(T0, 3e-8), swap(T0 + 100, 3e-8)])
    f, _ = L.facts_for(unit(), tape, launch_at=None, fill_latency=1.5)
    assert f["horizon_covered_s"] == 100
    assert f["best_R_hindsight"] is None, f["best_R_hindsight"]


def test_missing_internal_pool_interval_is_not_a_complete_exit_window():
    rows = [swap(T0 - 1, 3e-8), swap(T0, 3e-8),
            swap(T0 + 1, 3e-8, buy=False, sol=1),
            swap(T0 + 5, 3e-8, real_quote=85_000_000_000)]
    pool = [{"t": T0 + t, "q": 1_000_000_000, "b": 1_000_000_000_000, "fee": 0.0}
            for t in (6, 900)]
    tape = L.Tape(rows, pool, coverage_until=T0 + 1000)
    info = {"exit_ts": T0 + 1, "exit_sig": rows[2]["signature"],
            "realized_pnl_usd": 0.0, "entry_notional_usd": 25.0, "exit_reason": "test"}
    f, _ = L.facts_for(unit(cost_sol=1.0, qty=1_000_000), tape,
                        launch_at=None, fill_latency=1.5, exit_info=info)
    assert f["exit_window"]["known_s"] < 900
    assert f["exit_window"]["complete"] is False, f["exit_window"]
    assert f["exit_regret_R"] is None


def test_a_fixed_exit_cannot_slide_past_its_unobserved_execution_time():
    rows = [swap(T0 - 1, 3e-8), swap(T0, 3e-8),
            swap(T0 + 5, 3e-8, real_quote=85_000_000_000)]
    pool = [{"t": T0 + t, "q": int(q * 1e9), "b": 1_000_000_000_000, "fee": 0.0}
            for t, q in ((6, 30), (150, 60), (151, 60))]
    f, _ = L.facts_for(unit(), L.Tape(rows, pool), launch_at=None, fill_latency=1.5)
    result = f["alternatives"]["fixed_120s"]
    assert result["R"] is None and result["censored"], result


def test_raw_host_judge_response_is_not_offset_a_second_time(tmp_path):
    desk, ledger, deskdb = _fixture(tmp_path)
    units, _ = W.discover(ledger, deskdb, T0 + 6000, W.Clock(120.0), 100, set())
    refused = next(u for u in units if u["ticket_id"] == "desk:U:b1")
    assert refused["t_ref"] == T0 + 2000, refused


def test_retryable_units_consume_the_cpu_batch_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("MEMECOIN_DESK_LESSONS_ENABLED", raising=False)
    monkeypatch.setattr(W, "time", SimpleNamespace(time=lambda: T0 + 6000))
    pending = [{"kind": "counterfactual", "key": "tkt:%d" % i, "t_ref": T0} for i in range(10)]
    monkeypatch.setattr(W, "discover", lambda *a: (pending, 0))
    monkeypatch.setattr(W, "fill_latency", lambda *a: 1.5)
    attempted = []

    def outage(u, *args):
        attempted.append(u["key"])
        raise W.Retry("capture unavailable")

    monkeypatch.setattr(W, "compute", outage)
    W.main(["--out", str(tmp_path / "lessons.db"), "--max", "2"])
    assert len(attempted) <= 2, attempted


def test_one_mismatched_entry_event_is_not_accepted(tmp_path):
    desk, ledger, deskdb = _fixture(tmp_path)
    c = sqlite3.connect(ledger)
    try:
        sig = c.execute("SELECT tx_sig FROM memecoin_fills WHERE position_id=1").fetchone()[0]
    finally:
        c.close()
    result, status = W.entry_swap(desk, "MA", sig, 1.0, T0)
    assert result is None and status != "ok", (status, result)


def test_terminal_pre_enqueue_refusal_is_not_waiting_forever(tmp_path):
    desk, ledger, deskdb = _fixture(tmp_path)
    c = sqlite3.connect(deskdb)
    c.execute("DELETE FROM desk_tickets WHERE ticket_id='desk:U:b1' AND stage='fill'")
    c.execute("INSERT INTO desk_outbox VALUES('desk:U:b1','refused')")
    c.commit()
    c.close()
    units, _ = W.discover(ledger, deskdb, T0 + 6000, W.Clock(0), 100, set())
    assert any(u["ticket_id"] == "desk:U:b1" and u["kind"] == "counterfactual" for u in units)
