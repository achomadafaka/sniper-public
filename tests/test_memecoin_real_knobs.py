"""WS4 (2026-07-15l audit): the live real-money risk knobs must be VISIBLE on the Real cockpit,
read from settings at request time — never hard-coded in the UI. The operator could not see, from
the dashboard, that AUTO_TEMPLATES was on (so priced >=$30M coins now OPEN as scalps), that the
impact cap is Solana-only (the EVM leg has none), or what the demote / unquotable / gas-reserve
thresholds actually were.

`/api/memecoin/real/status` now carries a `knobs` block sourced from `cfg` on each request, so a
`.env` change + runner restart is reflected the moment the web server also restarts.
"""
import sqlite3
from pathlib import Path

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.settings import memecoin as cfg

APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


def test_real_header_renders_the_knobs_strip_from_settings():
    # the Real cockpit must read + show the knobs block, not hard-code the numbers
    assert "knobs" in APP_JS
    assert "impact_cap_scope" in APP_JS      # Solana-only truth is shown
    assert "auto_templates" in APP_JS        # big-caps-open-as-scalp truth is shown


def test_hardcoded_unquotable_24h_label_is_replaced():
    # the "24h" was hard-coded; it must come from unquotable_exit_s now (retunable in .env)
    assert "unquotable-24h guard" not in APP_JS


def test_real_armed_badge_says_per_chain_and_per_event_gates_apply():
    # WS5: the armed badge must NOT read as "every buy opens a real position" — per-event gates
    # (venue, slots, safety, impact, mcap) still apply. As of 2026-08-03 the badge moved from
    # Curated (which had no chain and so could not state one) to the per-chain Armed board, so
    # the claim it makes is now scoped to a chain as well as to the wallet.
    assert "armed AND effective" in APP_JS
    assert "eligible to open REAL positions" in APP_JS
    assert "per-event gates" in APP_JS


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope_sol.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(tmp_path / "nope_evm.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453"})
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def _knobs(monkeypatch, tmp_path):
    cl, _ = _client(monkeypatch, tmp_path)
    return cl.get("/api/memecoin/real/status").get_json()["knobs"]


def test_knobs_echo_settings_at_request_time(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 12_345_678.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 41.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DEMOTE_LOSS_USD", 99.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DEMOTE_CONSEC", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_UNQUOTABLE_EXIT_S", 3600.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_IMPACT_CAP_PCT", 7.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_IMPACT_CAP_TIERS", "1000000:11,5000000:9")
    monkeypatch.setattr(cfg, "MEMECOIN_AUTO_TEMPLATES", "on")
    k = _knobs(monkeypatch, tmp_path)
    assert k["mcap_ceiling_usd"] == 12_345_678.0
    assert k["chase_max_pct"] == 41.0
    assert k["demote_loss_usd"] == 99.0 and k["demote_consec"] == 5
    assert k["unquotable_exit_s"] == 3600.0
    assert k["impact_cap_base_pct"] == 7.0
    assert k["impact_cap_tiers"] == "1000000:11,5000000:9"
    assert k["auto_templates"] == "on"


def test_knobs_impact_cap_is_flagged_solana_only(monkeypatch, tmp_path):
    # the EVM leg has NO impact gate — the UI must not imply the cap protects every chain
    k = _knobs(monkeypatch, tmp_path)
    assert k["impact_cap_scope"] == "solana_only"


def test_knobs_auto_templates_on_exposes_scalp_open_of_bigcaps(monkeypatch, tmp_path):
    """When AUTO_TEMPLATES=on, the mcap ceiling no longer refuses >=SCALP_MCAP coins — they OPEN as
    scalps. The strip must carry the scalp params so 'priced >=$30M coins open now' is legible."""
    monkeypatch.setattr(cfg, "MEMECOIN_AUTO_TEMPLATES", "on")
    monkeypatch.setattr(cfg, "MEMECOIN_SCALP_MCAP_USD", 30_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SCALP_TP_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SCALP_STOP_PCT", 25.0)
    k = _knobs(monkeypatch, tmp_path)
    assert k["scalp_mcap_usd"] == 30_000_000.0
    assert k["scalp_tp_pct"] == 25.0 and k["scalp_stop_pct"] == 25.0


def test_knobs_gas_reserve_carries_formula_and_components(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_ENTRY_SOL", 0.003)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_EXIT_SOL", 0.004)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 3.0)
    g = _knobs(monkeypatch, tmp_path)["gas_reserve"]
    assert g["entry_sol"] == 0.003 and g["exit_sol"] == 0.004 and g["retry_factor"] == 3.0
    assert "GAS_EXIT_SOL" in g["formula"] and "GAS_RETRY_FACTOR" in g["formula"]


def test_knobs_sizing_and_staleness_present(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION", 0.13)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENTRY_MAX_AGE_S", 120.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2)
    k = _knobs(monkeypatch, tmp_path)
    assert k["sizing"] == "bankroll" and k["bankroll_fraction"] == 0.13
    assert k["min_trade_usd"] == 50.0
    assert k["entry_max_age_s"] == 120.0
    assert k["stop_confirm_sweeps"] == 2
