"""Every mutating memecoin web route requires the CSRF token (2026-09-27, Alpha GO).

21 of 31 mutating /api/memecoin routes checked no token, including arm/disarm, the kill switch and
manual close, on a web bound to 0.0.0.0 behind Basic Auth only. The guard is blueprint-wide, so these
tests walk the app's url_map: a route added later is covered without editing this file.
"""
import logging
import sqlite3

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes import memecoin_csrf
from sniper.routes.api_memecoin import CSRF_EXEMPT, create_api_memecoin_blueprint
from sniper.routes.api_memecoin_x import create_api_memecoin_x_blueprint

FORGED = {"Content-Type": "text/plain", "Origin": "https://untrusted.invalid",
          "Sec-Fetch-Site": "cross-site"}
BLUEPRINTS = ("api_memecoin.", "api_memecoin_x.")


def _app(tmp_path, **ctx):
    c = sqlite3.connect(str(tmp_path / "ledger.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c, **ctx}))
    app.register_blueprint(create_api_memecoin_x_blueprint(
        {"db_path": str(tmp_path / "ledger.db"), **ctx}))
    app.config["TESTING"] = True
    return app


def _mutations(app):
    out = []
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith(BLUEPRINTS):
            continue
        path = rule.rule
        for arg in rule.arguments:
            path = path.replace("<int:%s>" % arg, "1").replace("<path:%s>" % arg, "x") \
                       .replace("<%s>" % arg, "x")
        for method in sorted(rule.methods & memecoin_csrf.MUTATING):
            out.append((rule.endpoint, method, path))
    return out


def _sentinel(app):
    """Replace every handler: a request that reaches one is recorded instead of mutating."""
    reached = []
    for endpoint in list(app.view_functions):
        if endpoint.startswith(BLUEPRINTS):
            app.view_functions[endpoint] = (lambda ep: lambda **kw: (reached.append(ep), ("ok", 200))[1])(endpoint)
    return reached


def test_every_mutation_without_a_token_is_refused_before_its_handler(tmp_path):
    app = _app(tmp_path)
    muts = _mutations(app)
    assert len(muts) >= 32, muts            # 31 on api_memecoin + the x-analysis pair
    reached = _sentinel(app)
    client = app.test_client()
    refused = set()
    for endpoint, method, path in muts:
        r = client.open(path, method=method, data='{"x": 1}', headers=FORGED)
        if endpoint in CSRF_EXEMPT:
            continue
        assert r.status_code == 403 and r.get_json() == {"ok": False, "error": "csrf rejected"}, \
            (endpoint, method, r.status_code)
        refused.add(endpoint)
    assert not [ep for ep in reached if ep not in CSRF_EXEMPT], reached
    for must in ("api_memecoin.wallet_real", "api_memecoin.real_kill", "api_memecoin.manual_close",
                 "api_memecoin.partial_sell", "api_memecoin.set_real_position_exit",
                 "api_memecoin.manual_recover", "api_memecoin.remove", "api_memecoin_x.queue_analysis"):
        assert must in refused, must


def test_a_valid_same_origin_token_reaches_every_handler(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    token = client.get("/api/memecoin/csrf-token").get_json()["csrf_token"]
    assert client.get("/api/memecoin/csrf-token").headers["Cache-Control"] == "no-store"
    reached = _sentinel(app)
    muts = _mutations(app)
    for endpoint, method, path in muts:
        r = client.open(path, method=method, data="{}", headers={
            "Content-Type": "application/json", "X-CSRF-Token": token})
        assert r.status_code == 200, (endpoint, method, r.status_code)
    assert sorted(set(reached)) == sorted({ep for ep, _m, _p in muts})


def test_buy_token_is_the_only_exemption(tmp_path):
    assert CSRF_EXEMPT == frozenset({"api_memecoin.real_buy_token"})
    app = _app(tmp_path)
    reached = _sentinel(app)
    r = app.test_client().post("/api/memecoin/real/buy_token", data="{}", headers=FORGED)
    assert r.status_code == 200 and reached == ["api_memecoin.real_buy_token"]


def test_the_off_switch_drops_the_blanket_guard_loudly(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="sniper.routes.memecoin_csrf"):
        app = _app(tmp_path, csrf_enforce_all=False)
    assert sum("MEMECOIN_WEB_CSRF_ENFORCE_ALL=0" in rec.getMessage() for rec in caplog.records) == 2
    reached = _sentinel(app)
    r = app.test_client().post("/api/memecoin/real/kill", data="{}", headers=FORGED)
    assert r.status_code == 200 and reached == ["api_memecoin.real_kill"]


def test_the_switch_defaults_on_and_nothing_on_the_web_can_write_it():
    import pathlib
    import sniper.settings.web as web_settings
    assert web_settings.MEMECOIN_WEB_CSRF_ENFORCE_ALL is True       # no env in the test process
    root = pathlib.Path(__file__).resolve().parents[1]
    readers = sorted(str(p.relative_to(root)) for p in (root / "sniper").rglob("*.py")
                     if "MEMECOIN_WEB_CSRF_ENFORCE_ALL" in p.read_text())
    assert readers == ["sniper/routes/memecoin_csrf.py", "sniper/settings/web.py"], readers
