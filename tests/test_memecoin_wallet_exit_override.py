"""Per-wallet exit override: the operator sets a take-profit (and optionally a stop) per wallet and
chain in the UI (operator order 2026-09-27: "make it so i can set different tp strategies for
different users through the UI").

The override is ledger state, read ONCE at contract build, frozen into the NEW contract's policy
numbers, with its provenance in contract['source'] (the block a pinned prior release ignores). Open
positions keep the contract they were frozen under.
"""
import dataclasses
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.persistence import db_memecoin as m  # noqa: E402
from sniper.services import memecoin_funded_policy as fp  # noqa: E402
from sniper.services import memecoin_risk as risk  # noqa: E402
from sniper.services.memecoin_market_state import EntryClassification  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402

NOW = "2026-09-27T14:45:00+00:00"


@pytest.fixture(autouse=True)
def _live_shape(monkeypatch):
    """Production resolves every wallet to follow-30-50 (TP 25 / stop 35); pin that shape so the
    test does not depend on the code defaults of a process with no .env."""
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_WALLETS", ())
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_TP_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_STOP_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BAND_POLICY_BANDS", ())
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_ONLY_WALLETS", ())


def _conn(uid="u", handle="w1"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.start_entry_attempt(conn, attempt_id="attempt-1",
                          decision_started_at="2026-09-27T14:40:00+00:00",
                          network_id="solana", mint="MINT")
    conn.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES (?,?)", (uid, handle))
    conn.commit()
    return conn


def _classification(coin_band="nano", effective_mcap=30_000.0):
    return EntryClassification(
        classification_version=1, network_id="solana", mint="MINT",
        signal_source="follow_activity", decision_at="2026-09-27T14:40:00+00:00",
        coin_band=coin_band, lifecycle="young", liquidity_class="unknown",
        momentum_phase="climbing", market_regime="hot_narrow", data_quality="partial",
        risk_scale=1.0, reason_codes=(f"coin_band:{coin_band}",),
        facts={"network_id": "solana", "mint": "MINT", "effective_mcap": effective_mcap,
               "liquidity": 5_000.0, "base_clip_usd": None})


_ATTEMPTS = iter(range(2, 10_000))


def _contract(conn, uid="u"):
    """A NEW reservation each call: a frozen contract is immutable once written."""
    attempt = "attempt-%d" % next(_ATTEMPTS)
    m.start_entry_attempt(conn, attempt_id=attempt, decision_started_at="2026-09-27T14:40:00+00:00",
                          network_id="solana", mint="MINT")
    frozen = risk.prepare_funded_contract(
        conn, classification=_classification(), network_id="solana", mint="MINT",
        attempt_id=attempt, origin_event_id="event-1", base_clip_usd=100.0,
        approved_clip_usd=100.0, source_user_id=uid)
    return json.loads(frozen["funded_contract_json"])


def _set(conn, uid="u", network="solana", tp=40.0, stop=None):
    return m.set_wallet_exit_override(conn, uid, network_id=network, tp_pct=tp, stop_pct=stop,
                                      actor="test", reason="unit", now_iso=NOW)


# ---- storage ----------------------------------------------------------------------------------

def test_set_read_reset_round_trip_with_an_audit_row_each():
    conn = _conn()
    _set(conn, tp=40.0, stop=20.0)
    got = m.wallet_exit_override(conn, "u", network_id="solana")
    assert (got["tp_pct"], got["stop_pct"], got["actor"]) == (40.0, 20.0, "test")
    assert m.reset_wallet_exit_override(conn, "u", network_id="solana", actor="test",
                                        reason="unit", now_iso=NOW) == 1
    assert m.wallet_exit_override(conn, "u", network_id="solana") is None
    actions = [r["action"] for r in m.wallet_exit_override_audit(conn, "u")]
    assert actions == ["reset", "set"]            # newest first


def test_both_chain_spellings_are_one_row():
    conn = _conn()
    _set(conn, network="robin", tp=30.0)
    assert m.wallet_exit_override(conn, "u", network_id="4663")["tp_pct"] == 30.0
    _set(conn, network="4663", tp=35.0)
    assert m.wallet_exit_override(conn, "u", network_id="robin")["tp_pct"] == 35.0
    assert m.wallet_exit_override(conn, "u", network_id="solana") is None


@pytest.mark.parametrize("tp,stop", [
    (0.0, None), (4.9, None), (500.1, None), (float("nan"), None), (float("inf"), None),
    (None, 0.0), (None, 9.9), (None, 80.1), (None, None), ("abc", None),
])
def test_out_of_bounds_values_are_refused(tp, stop):
    conn = _conn()
    with pytest.raises(ValueError):
        _set(conn, tp=tp, stop=stop)
    assert m.wallet_exit_override(conn, "u", network_id="solana") is None


def test_an_unknown_wallet_is_refused():
    with pytest.raises(ValueError):
        _set(_conn(), uid="nobody")


def test_a_hand_written_bad_value_is_ignored_on_read():
    conn = _conn()
    _set(conn, tp=40.0, stop=20.0)
    conn.execute("UPDATE memecoin_wallet_exit_overrides SET tp_pct=0")
    got = m.wallet_exit_override(conn, "u", network_id="solana")
    assert got["tp_pct"] is None and got["stop_pct"] == 20.0


def test_excluding_the_wallet_clears_its_overrides():
    conn = _conn()
    _set(conn, tp=40.0)
    m.set_wallet_excluded(conn, "u", True, actor="test", now_iso=NOW)
    assert m.wallet_exit_override(conn, "u", network_id="solana") is None
    assert m.wallet_exit_override_audit(conn, "u")[0]["action"] == "cleared_on_exclusion"


# ---- resolution -------------------------------------------------------------------------------

def test_an_override_changes_a_new_contract_and_records_its_provenance():
    conn = _conn()
    before = _contract(conn)
    assert (before["policy"]["tp_pct"], before["policy"]["stop_pct"]) == (25.0, 35.0)
    assert "exit_override" not in before["source"]
    _set(conn, tp=40.0)
    after = _contract(conn)
    assert (after["policy"]["tp_pct"], after["policy"]["stop_pct"]) == (40.0, 35.0)
    assert after["policy"]["policy_id"] == before["policy"]["policy_id"]
    prov = after["source"]["exit_override"]
    assert (prov["tp_pct"], prov["stop_pct"], prov["base_tp_pct"], prov["base_stop_pct"]) \
        == (40.0, None, 25.0, 35.0)
    # The frozen policy still parses under the strict reader a pinned prior release uses.
    fp._validated_policy_data(after["policy"])


def test_an_open_position_keeps_the_contract_it_was_frozen_under():
    conn = _conn()
    frozen = json.dumps(_contract(conn), sort_keys=True)
    _set(conn, tp=40.0, stop=20.0)
    policy = fp._validated_policy_data(json.loads(frozen)["policy"])
    assert (policy.tp_pct, policy.stop_pct) == (25.0, 35.0)


def test_the_override_wins_over_the_env_tp_only_list(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_ONLY_WALLETS", ("w1:solana",))
    conn = _conn()
    assert _contract(conn)["policy"]["policy_id"].startswith("tp-only")
    _set(conn, tp=60.0)
    assert _contract(conn)["policy"]["tp_pct"] == 60.0


def test_the_engine_envelope_is_never_overridden():
    conn = _conn()
    _set(conn, tp=40.0, stop=20.0)
    base = fp._follow3050_policy(coin_band="nano")
    envelope = dataclasses.replace(base, policy_id=fp.ENGINE_ENVELOPE_POLICY_ID)
    got, prov = risk.apply_wallet_exit_override(conn, envelope, source_user_id="u",
                                                network_id="solana")
    assert got is envelope and prov is None


def test_a_read_failure_falls_back_to_base_rails(caplog):
    conn = _conn()
    _set(conn, tp=40.0)
    conn.execute("DROP TABLE memecoin_wallet_exit_overrides")
    contract = _contract(conn)
    assert contract["policy"]["tp_pct"] == 25.0 and "exit_override" not in contract["source"]


def test_the_funded_clone_paper_arm_sees_the_override():
    from sniper.services.memecoin_policy import resolve_paper_arm_policy
    conn = _conn()
    _set(conn, tp=40.0)
    _id, _ver, payload = resolve_paper_arm_policy(
        "funded_clone", entry_mcap=30_000.0, network_id="solana", conn=conn,
        source_user_id="u")
    assert json.loads(payload)["exit_policy"]["tp_pct"] == 40.0
    assert json.loads(payload)["exit_policy"] == _contract(conn)["policy"]


# ---- web --------------------------------------------------------------------------------------

@pytest.fixture()
def client():
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    c = _conn()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    app.config["TESTING"] = True
    cl = app.test_client()
    cl._ledger = c
    return cl


def _token(client):
    r = client.get("/api/memecoin/wallets/u/exit-rails?chain=solana")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["csrf_token"]


RAILS = "/api/memecoin/wallets/u/exit-rails"


def test_the_route_refuses_a_write_without_the_token(client):
    r = client.post(RAILS, json={"chain": "solana", "tp_pct": 40})
    assert r.status_code == 403 and r.get_json()["error"] == "csrf rejected"
    assert m.wallet_exit_override(client._ledger, "u", network_id="solana") is None


def test_the_route_sets_reports_and_resets(client):
    token = {"X-CSRF-Token": _token(client)}
    view = client.get(RAILS + "?chain=solana").get_json()
    assert view["source"] == "default" and view["effective"]["tp_pct"] == 25.0
    r = client.post(RAILS, json={"chain": "solana", "tp_pct": 40, "reason": "ui"}, headers=token)
    assert r.status_code == 200, r.get_data(as_text=True)
    view = client.get(RAILS + "?chain=solana").get_json()
    assert view["source"] == "override"
    assert (view["effective"]["tp_pct"], view["effective"]["stop_pct"]) == (40.0, 35.0)
    assert (view["default"]["tp_pct"], view["default"]["stop_pct"]) == (25.0, 35.0)
    bad = client.post(RAILS, json={"chain": "solana", "tp_pct": 0}, headers=token)
    assert bad.status_code == 400
    r = client.post(RAILS + "/reset", json={"chain": "solana"}, headers=token)
    assert r.status_code == 200
    assert client.get(RAILS + "?chain=solana").get_json()["source"] == "default"


def test_the_roster_shows_the_override_on_a_disarmed_row(client, monkeypatch):
    monkeypatch.setattr("sniper.services.memecoin_chains.funded_chain_keys",
                        lambda *a, **k: ("solana",))
    _set(client._ledger, tp=40.0)
    r = client.get("/api/memecoin/armed?chain=solana")
    assert r.status_code == 200, r.get_data(as_text=True)
    row = next(x for x in r.get_json()["rows"] if x["source_user_id"] == "u")
    assert row["armed_here"] is False
    assert row["exit_override"]["tp_pct"] == 40.0


def test_the_panel_sends_the_token():
    app = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "app.js")).read()
    panel = app[app.index("async _walletExitRails(ds)"):]
    panel = panel[:panel.index("async _realBuy(")]
    assert "const headers = {'X-CSRF-Token':" in panel
    posts = panel.split("method: 'POST'")[1:]
    assert len(posts) == 2, "set and reset"
    assert all(p.lstrip(", ").startswith("headers") for p in posts)
    assert "base + '/reset'" in panel
