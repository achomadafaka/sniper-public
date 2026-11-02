"""The control API distinguishes effective rules from generic values."""
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_entry_knobs as knobs
from sniper.settings import memecoin as cfg


def test_api_resolves_chain_and_wallet_waivers_without_mutating_settings(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setenv("MEMECOIN_GATE_EXEMPT_FAMILIES_SOLANA", "sell_share,chase_first_seen")
    monkeypatch.setenv("MEMECOIN_REAL_SOLANA_NANO_INBAND_MAX_TURNOVER_M5", "0")
    monkeypatch.setenv("MEMECOIN_REAL_4663_NANO_INBAND_MAX_TURNOVER_M5", "2")
    monkeypatch.setenv("MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5_4663", "0.02")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 2000000)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", .01)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 3000)
    monkeypatch.setattr(knobs, "_DEFAULTS", {})
    m.set_entry_knob_override(conn, "MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5", .03,
                              active=True, actor="test", reason="test", now_iso="now")
    m.set_wallet_gate_exemption(conn, "u", "winner_reentry_price", active=True,
                                actor="test", reason="test", now_iso="now")
    before = cfg.MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn}))
    data = app.test_client().get("/api/memecoin/entry-knobs?source_user_id=u").get_json()
    view = data["effective_controls"]
    assert view["basis"] == "web_configuration"
    assert view["runner_acknowledged"] is False
    contexts = {(x["network_id"], x["coin_band"]): x for x in view["contexts"]}
    sol = {r["key"]: r for r in contexts[("solana", "nano")]["controls"]}
    rh = {r["key"]: r for r in contexts[("4663", "nano")]["controls"]}
    assert sol["sell_share"]["state"] == "waived"
    assert sol["winner_reentry_price"]["state"] == "waived"
    assert sol["m5_ceiling"]["state"] == "disabled"
    assert rh["m5_ceiling"]["value"] == 2
    assert rh["m5_floor"]["value"] == .02
    assert sol["m5_txns"]["state"] == "out_of_scope"
    assert sol["h1_absolute"]["state"] == "fallback"
    assert sol["h1_ratio"]["missing_input"] == "passes"
    assert cfg.MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5 == before
    conn.close()
