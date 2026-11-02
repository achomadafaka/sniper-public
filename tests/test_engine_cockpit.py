"""WO-12 — the operator cockpit reader + the acted-engine-decision Telegram line."""
import json
import sqlite3

from sniper.persistence import db_exit_state as xs
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_engine_cockpit import engine_cockpit


def _ledger():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _exit():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    xs.ensure_exit_state_tables(c)
    return c


def _mark_engine(ledger, pid, catastrophe_pct=40.0):
    ledger.execute(
        "UPDATE memecoin_positions SET funded_contract_json=? WHERE id=?",
        (json.dumps({"policy": {"policy_id": "engine-envelope-v1",
                                "catastrophe_pct": catastrophe_pct}}), pid))
    ledger.commit()


def test_cockpit_assembles_state_decisions_and_distance():
    ledger = _ledger()
    pid = m.open_position(ledger, source_user_id="u", source_handle="h", mint="MINT", ticker="CK",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-25T00:00:00+00:00",
                          strategy="alpha_b_real", execution_mode="real", network_id="solana")
    ex = _exit()
    _mark_engine(ledger, pid, catastrophe_pct=40.0)
    ex.execute("INSERT INTO memecoin_exit_market_state(id, position_id, chain_id, mint, "
               "engine_version, feature_version, computed_at, state, nominal_price, "
               "executable_price, executable_peak, drawdown) "
               "VALUES(1,?,?,?,?,?,?,?,?,?,?,?)",
               (pid, "solana", "MINT", "exit-brain-v3", "fv1", "2026-07-25T00:05:00+00:00",
                "trending", 1.3, 1.25, 2.0, 0.375))
    ex.commit()
    for i, action in enumerate(("RIDE", "DERISK", "CUT")):
        xs.write_candidate_decision(ex, {
            "position_id": pid, "decided_at": f"2026-07-25T00:0{i}:00+00:00",
            "engine_version": "exit-brain-v3", "feature_version": "fv1",
            "candidate_action": action, "reason_codes": ["break_nominated"],
            "outcome": "closed" if action == "CUT" else None})

    cockpit = engine_cockpit(ex, ledger)
    assert len(cockpit) == 1
    row = cockpit[0]
    assert row["position_id"] == pid and row["state"] == "trending"
    assert row["snapshot"]["executable_price"] == 1.25 and row["snapshot"]["drawdown"] == 0.375
    assert abs(row["distance_to_catastrophe"] - 0.65) < 1e-9  # +25% is 65 points above -40%
    assert row["distance_status"] == "exact" and row["catastrophe_pct"] == 40.0
    assert len(row["recent_decisions"]) == 3
    assert row["recent_decisions"][0]["candidate_action"] == "CUT"   # newest first
    assert row["recent_decisions"][0]["reason_codes"] == ["break_nominated"]


def test_cockpit_empty_without_exit_state():
    ledger = _ledger()
    pid = m.open_position(ledger, source_user_id="u", source_handle="h", mint="M2", ticker="X",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-25T00:00:00+00:00",
                    strategy="alpha_b_real", execution_mode="real", network_id="solana")
    _mark_engine(ledger, pid)
    cockpit = engine_cockpit(None, ledger)               # exit-state DB absent (pre-arm)
    assert len(cockpit) == 1 and cockpit[0]["state"] is None and cockpit[0]["recent_decisions"] == []


def test_cockpit_filters_non_engine_positions_and_never_substitutes_nominal_for_exact():
    ledger = _ledger()
    engine = m.open_position(
        ledger, source_user_id="u", source_handle="h", mint="ENGINE", ticker="E",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1,
        their_size_usd=0, opened_at="2026-07-25T00:00:00+00:00", strategy="alpha_b_real",
        execution_mode="real", network_id="solana")
    m.open_position(
        ledger, source_user_id="u", source_handle="h", mint="LEGACY", ticker="L",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1,
        their_size_usd=0, opened_at="2026-07-25T00:00:00+00:00", strategy="alpha_b_real",
        execution_mode="real", network_id="solana")
    _mark_engine(ledger, engine)
    ex = _exit()
    ex.execute("INSERT INTO memecoin_exit_market_state(position_id,chain_id,mint,engine_version,"
               "feature_version,computed_at,state,nominal_price,executable_price) "
               "VALUES(?,?,?,?,?,?,?,?,NULL)",
               (engine, "solana", "ENGINE", "v", "fv", "2026-07-25T00:01:00Z", "trending", 9.0))
    ex.commit()
    rows = engine_cockpit(ex, ledger)
    assert [r["position_id"] for r in rows] == [engine]
    assert rows[0]["distance_to_catastrophe"] is None
    assert rows[0]["distance_status"] == "exact_unavailable"


def test_dashboard_fetches_and_renders_engine_cockpit():
    src = open("static/app.js", encoding="utf-8").read()
    assert "/api/memecoin/engine-cockpit" in src
    assert "recent_decisions" in src and "distance_status" in src


def test_engine_decision_alert_names_action_and_codes(monkeypatch):
    sent = []
    import sniper.services.memecoin_telegram as tg
    monkeypatch.setattr(tg, "enqueue_telegram", lambda text, **meta: sent.append((text, meta)))
    audit = {"candidate_action": "DERISK", "reason_codes": ["weakening", "flow_negative"],
             "proposed_fraction": 0.5}
    R._alert_engine_decision({"id": 7, "ticker": "PEPE"}, audit, {"status": "derisked"})
    assert len(sent) == 1
    assert "ENGINE DERISK" in sent[0][0] and "PEPE" in sent[0][0]
    assert "weakening,flow_negative" in sent[0][0] and "status=derisked" in sent[0][0]
    assert sent[0][1] == {"kind": "operational:ENGINE", "source_id": "position:7"}
