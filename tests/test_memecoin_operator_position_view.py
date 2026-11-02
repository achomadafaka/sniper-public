"""Full and stream responses use the same price and value observations."""
import sqlite3

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes import api_memecoin as api

NOW = "2026-09-11T12:00:00+00:00"


@pytest.fixture
def book(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    monkeypatch.setattr(api, "_now_iso", lambda: NOW)
    pid = m.open_position(c, source_user_id="u", source_handle="alice", mint="MINT", ticker="COIN",
                          entry_price=1, entry_notional=100, entry_fill_source="chain",
                          entry_lag_s=0, their_size_usd=500, opened_at=NOW,
                          execution_mode="real", network_id="solana", entry_mcap=1000)
    m.insert_fill(c, position_id=pid, kind="entry", price=1, qty=100, notional=100,
                  fill_source="chain", fraction=1, created_at=NOW)
    c.execute("UPDATE memecoin_positions SET last_quote_price=1.8,last_quote_ts=?, "
              "last_executable_price=1.8,last_executable_at=? WHERE id=?", (NOW, NOW, pid))
    c.execute("INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,mcap,liquidity,source,quality) "
              "VALUES ('solana','MINT',?,2,2000,100000,'web','live')", ('2026-09-11T11:59:59+00:00',))
    c.execute("INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) "
              "VALUES ('solana','MINT',?,1.8,'fastlane_rpc','exact_route')", (NOW,))
    c.commit()
    yield c, pid
    c.close()


def test_full_and_ticker_share_complete_price_fields(book):
    c, _ = book
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    client = app.test_client()
    full = client.get("/api/memecoin/real/positions").get_json()["positions"][0]
    tick = client.get("/api/memecoin/real/ticker").get_json()["positions"][0]
    fields = ("runner_decision_price", "runner_decision_at", "selected_exit_price", "selected_exit_at",
              "selected_exit_basis", "market_mid_price", "market_mid_at", "last_quote_price", "last_quote_ts",
              "remaining_value_usd", "recovered_usd", "total_pnl_usd", "total_pnl_pct")
    for field in fields:
        assert field in full and field in tick
        assert full[field] == tick[field]
    # Since 2026-09-27 the `web` provider mark (DexScreener, mid 2, mcap 2000) is not a Solana market
    # price; the fresh exact route (1.8) is shown instead. An entry-ratio supply is not measured.
    assert tick["market_cap"] is None
    assert tick["target_supply"] == 1000


def test_open_card_carries_the_latest_source_sell_decision(book):
    """The card reads the runner's latest mirror decision for the position, and only that lane."""
    import json
    c, pid = book
    for at, lane, detail in (
            ("2026-09-11T11:50:00+00:00", "mirror_on_sell", {"code": "size_hold", "sold_fraction": 0.1}),
            ("2026-09-11T11:55:00+00:00", "mirror_on_sell", {"code": "size_hold", "sold_fraction": 0.18,
                                                              "min_fraction": 0.3}),
            ("2026-09-11T11:58:00+00:00", "sweep", {"code": "size_mirror", "sold_fraction": 0.9})):
        m.record_exit_trigger_audit(c, position_id=pid, reason="mirror", display_price=None,
                                    preview_usdc=None, allowed=False, detail=json.dumps(detail),
                                    checked_at=at, lane=lane)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    row = app.test_client().get("/api/memecoin/real/positions").get_json()["positions"][0]
    assert row["source_sell"] == {"code": "size_hold", "sold_fraction": 0.18, "min_fraction": 0.3,
                                  "allowed": False, "checked_at": "2026-09-11T11:55:00+00:00"}


def test_remaining_value_excludes_recovered_cash(book):
    c, pid = book
    m.insert_fill(c, position_id=pid, kind="derisk", price=2, qty=50, notional=100,
                  fill_source="chain", fraction=.5, created_at=NOW)
    row = api._real_ticker_payload(c, NOW)["positions"][0]
    assert row["remaining_value_usd"] == 90
    assert row["recovered_usd"] == 100
    assert row["total_pnl_usd"] == 90
    assert row["total_pnl_pct"] == 90


def test_missing_price_keeps_recovered_cash_but_total_unknown(book):
    c, pid = book
    c.execute("DELETE FROM memecoin_position_marks")
    c.execute("UPDATE memecoin_positions SET last_quote_price=NULL,last_quote_ts=NULL, "
              "last_executable_price=NULL,last_executable_at=NULL WHERE id=?", (pid,))
    row = api._real_ticker_payload(c, NOW)["positions"][0]
    assert row["remaining_value_usd"] is None
    assert row["total_pnl_usd"] is None
    assert row["recovered_usd"] == 0


@pytest.mark.parametrize("chain", ["56", "4663", "solana"])
@pytest.mark.parametrize("observed_at", [
    "2026-09-11T11:54:59+00:00", None, "invalid", "2026-09-11T12:00:01+00:00",
])
def test_unverifiable_sell_quote_is_history_not_current_pnl(book, chain, observed_at):
    c, pid = book
    c.execute("DELETE FROM memecoin_position_marks")
    c.execute("UPDATE memecoin_positions SET network_id=?,last_quote_price=.6,last_quote_ts=?, "
              "last_executable_price=.6,last_executable_at=? WHERE id=?",
              (chain, observed_at, observed_at, pid))
    m.insert_fill(c, position_id=pid, kind="derisk", price=1, qty=20, notional=20,
                  fill_source="chain", fraction=.2, created_at=NOW)
    row = api._real_ticker_payload(c, NOW)["positions"][0]
    assert row["selected_exit_price"] == .6  # keep the historical route evidence
    assert row["selected_exit_at"] == observed_at
    assert row["recovered_usd"] == 20
    for field in ("remaining_value_usd", "total_pnl_usd", "total_pnl_pct", "pnl_pct"):
        assert row[field] is None, f"{field} must not imply a current value"


def test_sell_quote_at_freshness_boundary_still_values_the_position(book):
    c, pid = book
    c.execute("DELETE FROM memecoin_position_marks")
    at = "2026-09-11T11:55:00+00:00"
    c.execute("UPDATE memecoin_positions SET last_quote_ts=?,last_executable_at=? WHERE id=?",
              (at, at, pid))
    row = api._real_ticker_payload(c, NOW)["positions"][0]
    assert row["remaining_value_usd"] == 180
    assert row["total_pnl_usd"] == 80


def test_missing_entry_fill_keeps_quantity_and_value_unknown(book):
    c, pid = book
    c.execute("DELETE FROM memecoin_fills WHERE position_id=?", (pid,))
    row = api._real_ticker_payload(c, NOW)["positions"][0]
    assert row["remaining_value_usd"] is None
    assert row["open_qty"] is None


def test_effective_chart_levels_follow_operator_override(book):
    c, pid = book
    m.set_position_exit_override(c, pid, "operator", sl_pct=25, trail_pct=10, derisk_pct=0,
                                 now_iso=NOW, actor="operator")
    c.execute("UPDATE memecoin_positions SET executable_peak_price=3 WHERE id=?", (pid,))
    row = api._real_ticker_payload(c, NOW)["positions"][0]
    assert row["effective_stop_price"] == .75
    assert row["effective_trail_price"] == 2.7


def test_closed_position_detail_is_exact_and_has_no_execution_bytes(book):
    c, pid = book
    m.insert_fill(c, position_id=pid, kind="manual_close", price=2, qty=100, notional=200,
                  fill_source="chain", fraction=1, created_at=NOW, tx_sig="public-signature")
    c.execute("UPDATE memecoin_positions SET is_open=0,realized_pnl=100,closed_at=? WHERE id=?", (NOW, pid))
    c.commit()
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    response = app.test_client().get(f"/api/memecoin/real/positions/{pid}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["as_of"] == NOW
    row = payload["position"]
    assert row["id"] == pid and row["is_open"] == 0
    assert row["remaining_value_usd"] == 0
    assert row["recovered_usd"] == 200
    assert row["total_pnl_usd"] == 100
    assert row["links"]["position"]
    assert "signed_payload_b64" not in response.get_data(as_text=True)
    assert app.test_client().get("/api/memecoin/real/positions/99999").status_code == 404
    c.execute("UPDATE memecoin_positions SET execution_mode='paper' WHERE id=?", (pid,))
    assert app.test_client().get(f"/api/memecoin/real/positions/{pid}").status_code == 404


def test_ticker_holds_one_short_snapshot_for_quantity_and_proceeds(book, monkeypatch):
    c, _ = book
    real_value = m.position_value_view
    def value(conn, *args, **kwargs):
        assert conn.in_transaction, "quantity and proceeds require one snapshot"
        return real_value(conn, *args, **kwargs)
    monkeypatch.setattr(m, "position_value_view", value)
    api._real_ticker_payload(c, NOW)
    assert not c.in_transaction


def test_full_payload_keeps_snapshot_through_enrichment(book, monkeypatch):
    c, _ = book
    real_override = m.effective_exit_override
    def override(conn, *args, **kwargs):
        assert conn.in_transaction, "full enrichment must share the price snapshot"
        return real_override(conn, *args, **kwargs)
    monkeypatch.setattr(m, "effective_exit_override", override)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    assert app.test_client().get("/api/memecoin/real/positions").status_code == 200
    assert not c.in_transaction


def test_closed_detail_retains_frozen_sizing_and_legacy_stays_unknown(book):
    from test_memecoin_funded_policy import _classification
    from sniper.services.memecoin_funded_policy import build_funded_contract, canonical_funded_contract, resolve_funded_policy
    c, pid = book
    contract = build_funded_contract(classification=_classification(), base_clip_usd=100., approved_clip_usd=75.,
        policy=resolve_funded_policy(coin_band='small', network_id='solana'), origin_event_id=None)
    payload, digest = canonical_funded_contract(contract)
    c.execute('UPDATE memecoin_positions SET is_open=0,closed_at=?,funded_contract_version=1, '
              'funded_contract_hash=?,funded_contract_json=? WHERE id=?', (NOW, digest, payload, pid))
    c.commit()
    app = Flask(__name__); app.register_blueprint(api.create_api_memecoin_blueprint({'conn': c}))
    client = app.test_client()
    row = client.get(f'/api/memecoin/real/positions/{pid}').get_json()['position']
    assert row['policy_source'] == 'funded_contract'
    assert row['entry_contract']['sizing'] == {'base_clip_usd': 100., 'risk_scale': .75, 'approved_clip_usd': 75.}
    c.execute('UPDATE memecoin_positions SET funded_contract_version=NULL,funded_contract_hash=NULL, '
              'funded_contract_json=NULL WHERE id=?', (pid,))
    c.commit()
    row = client.get(f'/api/memecoin/real/positions/{pid}').get_json()['position']
    assert row['policy_source'] == 'legacy' and row['entry_contract'] is None
