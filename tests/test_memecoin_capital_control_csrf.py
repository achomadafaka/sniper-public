"""Capital controls reject a forged cross-origin write (audit finding B1, 2026-09-18).

Two mutating routes move money controls and neither checked CSRF:

* `POST /api/memecoin/entry-knobs/<name>` was unguarded while it moved selection thresholds.
  `92e40824` put the drawdown pause and the daily loss breaker behind it, so a forged request
  could disable a capital rail.
* `POST /api/memecoin/real/risk-reset` shipped in `653304c1` with the same hole.

Both parse with `force=True`, which accepts `text/plain` — exactly the body a simple cross-origin
form can send with no JSON preflight. These tests drive the real routes, because the defect was in
the route and a test that asserts the guard's own expression proves nothing about the caller.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KNOB = "/api/memecoin/entry-knobs/MEMECOIN_DAILY_LOSS_LIMIT_USD"
RESET = "/api/memecoin/real/risk-reset"
# A selection-gate waiver is a funded-admission control: it was unguarded until 2026-09-27.
EXEMPT = "/api/memecoin/wallets/W1/exemption"
FORGED = {"Content-Type": "text/plain", "Origin": "https://untrusted.invalid",
          "Sec-Fetch-Site": "cross-site"}


@pytest.fixture()
def client(tmp_path):
    """The real blueprint on an in-memory ledger — the routes under test, not a stand-in."""
    import sqlite3
    from flask import Flask
    from sniper.persistence import db_memecoin as m
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    app.config["TESTING"] = True
    cl = app.test_client()
    cl._ledger = c
    return cl


def _token(client):
    r = client.get("/api/memecoin/entry-knobs")
    if r.status_code != 200:
        pytest.skip("entry-knobs is unavailable in this environment")
    return (r.get_json() or {}).get("csrf_token")


@pytest.mark.parametrize("path,body", [
    (KNOB, '{"value": 0, "actor": "forged-operator"}'),
    (RESET, '{"reason": "forged"}'),
    (EXEMPT, '{"gate_family": "source_buy_floor", "active": true}'),
])
def test_a_forged_cross_origin_write_is_refused(client, path, body):
    r = client.post(path, data=body, headers=FORGED)
    assert r.status_code == 403, f"{path} accepted a cross-origin write"
    assert (r.get_json() or {}).get("error") == "csrf rejected"


@pytest.mark.parametrize("path,body", [
    (KNOB, {"value": 0, "actor": "ui"}),
    (RESET, {"reason": "test"}),
    (EXEMPT, {"gate_family": "source_buy_floor", "active": True}),
])
def test_a_write_without_any_token_is_refused(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 403
    assert (r.get_json() or {}).get("error") == "csrf rejected"


def test_a_wrong_token_is_refused(client):
    _token(client)
    r = client.post(KNOB, json={"value": 0}, headers={"X-CSRF-Token": "not-the-token"})
    assert r.status_code == 403


def test_the_guard_runs_before_the_override_is_written(client):
    """The refusal must precede the persistence call, not report after it."""
    from sniper.services.memecoin_entry_knobs import read_overrides
    before = dict(read_overrides(client._ledger))
    client.post(KNOB, data='{"value": 0}', headers=FORGED)
    assert dict(read_overrides(client._ledger)) == before, \
        "a refused request still moved a capital control"


def test_a_correctly_tokened_write_still_works(client):
    """The guard must not break the operator's own cockpit."""
    token = _token(client)
    r = client.post(KNOB, json={"value": 250, "active": False, "actor": "test",
                                "reason": "csrf regression"},
                    headers={"X-CSRF-Token": token})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert (r.get_json() or {}).get("ok") is True


def test_both_cockpit_callers_send_the_token():
    """A guarded route with an unguarded caller is an outage, not a fix."""
    app = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "app.js")).read()
    knob = app[app.index("_setEntryKnob(name, value, active)"):][:900]
    assert "X-CSRF-Token" in knob
    reset = app[app.index("async _riskReset()"):][:1200]
    assert "X-CSRF-Token" in reset


def _wallet(client, uid="W1"):
    client._ledger.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES(?,?)",
                           (uid, "w1"))
    client._ledger.commit()


def test_a_refused_exemption_writes_no_waiver(client):
    """The refusal must precede the persistence call, not report after it."""
    from sniper.persistence import db_memecoin as m
    _wallet(client)
    client.post(EXEMPT, data='{"gate_family": "source_buy_floor", "active": true}', headers=FORGED)
    assert not m.wallet_gate_exemptions(client._ledger, "W1"), "a forged request granted a waiver"


def test_a_correctly_tokened_exemption_still_works(client):
    from sniper.persistence import db_memecoin as m
    _wallet(client)
    r = client.post(EXEMPT, json={"gate_family": "source_buy_floor", "active": True},
                    headers={"X-CSRF-Token": _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "source_buy_floor" in m.wallet_gate_exemptions(client._ledger, "W1")


def test_the_exemption_panel_sends_the_token():
    app = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "app.js")).read()
    panel = app[app.index("async _walletExemptions(ds)"):][:3500]
    call = panel[panel.index("'/exemption'"):][:300]
    assert "X-CSRF-Token" in call
