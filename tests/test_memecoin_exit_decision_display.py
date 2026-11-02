"""An exit shows the exact executable PnL that decided it; a market mid next to it says it is stale.

2026-09-27 loss audit: every funded Solana b_stop that night was confirmed on an exact whole-bag
route quote (65922: exact -35.4%), but the audit row's display_price, the chart and the cards
carried the DexScreener mid (65922: -2.0%), which lags 25-55 s. The operator read "-2% then sold at
-35%" as an insane exit. Display only: nothing here feeds a decision.
"""
import json
import sqlite3

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m

NOW = "2026-09-27T02:06:23.810000+00:00"
ENTRY = 7.923e-06


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def _position(c, *, exit_reason="b_stop", pnl=-52.91):
    pid = m.open_position(c, source_user_id="u", source_handle="untaxxable", mint="MINT",
                          ticker="COIN", entry_price=ENTRY, entry_notional=148.0,
                          entry_fill_source="chain", entry_lag_s=0, their_size_usd=500,
                          opened_at="2026-09-27T02:04:57+00:00", execution_mode="real",
                          network_id="solana", entry_mcap=6000)
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, exit_reason=?, realized_pnl=? "
              "WHERE id=?", ("2026-09-27T02:06:25.620000+00:00", exit_reason, pnl, pid))
    c.commit()
    return pid


def _audit(c, pid, reason, *, display_price, detail, checked_at=NOW, lane="fastlane_poll",
           allowed=True):
    m.record_exit_trigger_audit(c, position_id=pid, reason=reason, display_price=display_price,
                                preview_usdc=95.87, allowed=allowed,
                                detail=detail if isinstance(detail, str) else json.dumps(detail),
                                checked_at=checked_at, lane=lane)


DECIDED = {"code": "threshold_confirmed", "executable_unit_pnl_pct": -35.42645621,
           "decision_basis": "route", "display_basis": "market", "display_source": "fastlane_mid",
           "display_changed_at": "2026-09-27T02:05:42.810000+00:00",
           "display_checked_at": "2026-09-27T02:06:22.810000+00:00",
           "display_age_s": 41.0, "display_pnl_pct": -2.0}


# --- the ledger reader ---------------------------------------------------------------------------

def test_the_decision_is_the_exact_quote_and_the_mid_is_labelled_display(conn):
    pid = _position(conn)
    _audit(conn, pid, "b_stop", display_price=ENTRY * 0.98, detail=DECIDED)
    _audit(conn, pid, "b_stop_execution", display_price=None, lane="execution",
           checked_at="2026-09-27T02:06:24.516000+00:00",
           detail={"code": "threshold_confirmed", "executable_unit_pnl_pct": -35.5})
    d = m.exit_decisions(conn, [pid])[pid]
    assert d["rail"] == "b_stop" and d["lane"] == "fastlane_poll" and d["decided_at"] == NOW
    assert d["decision_basis"] == "route"
    assert d["decision_pnl_pct"] == pytest.approx(-35.43, abs=0.01)
    assert d["execution_pnl_pct"] == pytest.approx(-35.5)
    assert d["display_basis"] == "market" and d["display_source"] == "fastlane_mid"
    assert d["display_pnl_pct"] == pytest.approx(-2.0, abs=0.01)
    assert d["display_age_s"] == pytest.approx(41.0)
    assert d["display_checked_at"] == "2026-09-27T02:06:22.810000+00:00"


def test_a_row_written_before_the_labels_says_the_mid_is_unlabelled(conn):
    """65922's real row: the decision is readable, the mid's source and age were never recorded."""
    pid = _position(conn)
    _audit(conn, pid, "b_stop", display_price=ENTRY * 0.98,
           detail={"code": "threshold_confirmed", "executable_unit_pnl_pct": -35.42645621})
    d = m.exit_decisions(conn, [pid])[pid]
    assert d["decision_basis"] == "route" and d["decision_pnl_pct"] == pytest.approx(-35.43, abs=0.01)
    assert d["display_pnl_pct"] == pytest.approx(-2.0, abs=0.01)
    assert d["display_basis"] is None and d["display_age_s"] is None


def test_a_vol_death_exit_names_its_on_chain_proof_not_a_price(conn):
    pid = _position(conn, exit_reason="vol_death", pnl=-14.97)
    _audit(conn, pid, "vol_death", display_price=ENTRY * 0.88,
           detail="measured_onchain_still silence=30.0s")
    d = m.exit_decisions(conn, [pid])[pid]
    assert d["decision_basis"] == "onchain" and d["decision_pnl_pct"] is None
    assert d["decision_detail"] == "measured_onchain_still silence=30.0s"


def test_a_refused_row_or_another_rail_is_not_the_decision(conn):
    pid = _position(conn)
    _audit(conn, pid, "b_stop", display_price=ENTRY * 0.7, allowed=False,
           checked_at="2026-09-27T02:05:23+00:00",
           detail={"code": "debounce_pending", "executable_unit_pnl_pct": -30.0})
    _audit(conn, pid, "vol_death", display_price=ENTRY * 0.7, allowed=False,
           detail="exact_quote_moving silence=-1.0s")
    assert pid not in m.exit_decisions(conn, [pid])


# --- the runner records what its display price was -------------------------------------------

def test_the_runner_labels_a_dexscreener_mid_with_its_ages():
    from sniper.services.memecoin_real_runner import _display_evidence
    ev = _display_evidence({"price": ENTRY * 0.98, "source": "fastlane_mid",
                            "observed_at": "2026-09-27T02:05:42.810000+00:00",
                            "fetched_at": "2026-09-27T02:06:22.810000+00:00"},
                           ENTRY, ENTRY * 0.98, NOW)
    assert ev == {"display_basis": "market", "display_source": "fastlane_mid",
                  "display_changed_at": "2026-09-27T02:05:42.810000+00:00",
                  "display_checked_at": "2026-09-27T02:06:22.810000+00:00",
                  "display_age_s": pytest.approx(41.0), "display_pnl_pct": pytest.approx(-2.0)}


def test_the_runner_labels_an_exact_mark_as_route_and_never_raises():
    from sniper.services.memecoin_real_runner import _display_evidence
    assert _display_evidence({"price": 1.0, "source": "fastlane_rpc"}, 2.0, 1.0, NOW)[
        "display_basis"] == "route"
    assert _display_evidence({}, 2.0, 1.0, NOW)["display_basis"] == "route"   # exact fallback
    assert _display_evidence({"price": 1.0, "source": "fastlane_mid", "observed_at": "garbage"},
                             None, None, "not a time")["display_age_s"] is None
    assert _display_evidence(object(), 1, 1, NOW) == {}


def test_the_executable_audit_write_carries_the_display_evidence():
    import ast
    import inspect
    from sniper.services import memecoin_real_runner as R
    tree = ast.parse(inspect.getsource(R.real_exit_sweep))
    names = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_display_evidence" in names


def test_a_fastlane_mid_mark_says_when_it_was_fetched():
    from sniper.services import memecoin_fastlane as fl

    class Obs:
        def observe(self, key, mark, now):
            return "2026-09-27T02:05:42Z"

    marks, _ = fl._batched_mid_snapshot(
        [{"id": 1, "mint": "M1", "network_id": "solana"}],
        batch_fn=lambda keys: {("solana", "M1"): {"price": 1.0}},
        observation_state=Obs(), now_iso_fn=lambda: "2026-09-27T02:06:22Z")
    mark = marks[("solana", "M1")]
    assert mark["observed_at"] == "2026-09-27T02:05:42Z" and mark["fetched_at"] == "2026-09-27T02:06:22Z"


# --- Telegram ---------------------------------------------------------------------------------

def _exit_view(decision):
    return {"message_kind": "exit", "status": "sold", "position_id": 7, "network_id": "solana",
            "mint": "MINT", "ticker": "COIN", "source_handle": "untaxxable",
            "filled_notional": 95.87, "filled_price": 5.1e-06, "reason": "b_stop",
            "realized_pnl": -52.91, "entry_notional": 148.0, "position_closed": True,
            "exit_decision": decision}


def test_the_telegram_exit_line_leads_with_the_exact_decision():
    from sniper.services.memecoin_telegram import render_entry_notification
    text = render_entry_notification(_exit_view({
        "rail": "b_stop", "decision_basis": "route", "decision_pnl_pct": -35.43,
        "display_basis": "market", "display_source": "fastlane_mid", "display_pnl_pct": -2.0,
        "display_age_s": 41.0, "display_checked_at": "2026-09-27T02:06:22.810000+00:00",
        "decided_at": NOW}))
    assert "Decided on exact route quote -35.4%" in text
    assert "market -2.0% (DexScreener, changed 41s, checked 1s before; display only)" in text


def test_an_exit_without_a_decision_row_renders_as_before():
    from sniper.services.memecoin_telegram import render_entry_notification
    assert "Decided on" not in render_entry_notification(_exit_view(None))


def test_the_exit_view_reads_the_decision(conn):
    from sniper.persistence.db_memecoin_notifications import notification_exit_view
    pid = _position(conn)
    _audit(conn, pid, "b_stop", display_price=ENTRY * 0.98, detail=DECIDED)
    fill = m.insert_fill(conn, position_id=pid, kind="bracket_exit", price=5.1e-06, qty=1, notional=95.87,
                         fill_source="chain", fraction=1, created_at="2026-09-27T02:06:25Z")
    conn.commit()
    view = notification_exit_view(conn, fill)
    assert view["exit_decision"]["decision_pnl_pct"] == pytest.approx(-35.43, abs=0.01)


# --- the dashboard API ------------------------------------------------------------------------

def test_the_closed_list_and_the_position_detail_carry_the_decision(conn, monkeypatch):
    from sniper.routes import api_memecoin as api
    monkeypatch.setattr(api, "_now_iso", lambda: "2026-09-27T03:00:00+00:00")
    pid = _position(conn)
    _audit(conn, pid, "b_stop", display_price=ENTRY * 0.98, detail=DECIDED)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": conn}))
    client = app.test_client()
    closed = client.get("/api/memecoin/real/closed").get_json()["closed"]
    assert closed[0]["exit_decision"]["decision_pnl_pct"] == pytest.approx(-35.43, abs=0.01)
    detail = client.get("/api/memecoin/real/positions/%d" % pid).get_json()["position"]
    assert detail["exit_decision"]["display_basis"] == "market"
