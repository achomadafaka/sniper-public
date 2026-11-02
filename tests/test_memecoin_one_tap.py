"""One-tap entry tokens (Phase 7): signature, TTL, tamper, single-use, endpoint refusals."""
import sqlite3

import pytest

from sniper.services import memecoin_one_tap as ot


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONE_TAP_SECRET", "test-secret")


def _mint(now="2026-07-23T12:00:00+00:00", **kw):
    args = dict(mint="0xabc", network_id="4663", source_user_id="uid-1", ticker="TKN",
                now_iso=now)
    args.update(kw)
    return ot.mint_token(**args)


def test_roundtrip_within_ttl():
    tok = _mint()
    payload, err = ot.verify_token(tok, "2026-07-23T12:01:00+00:00")
    assert err is None
    assert payload["mint"] == "0xabc" and payload["network_id"] == "4663"
    assert payload["source_user_id"] == "uid-1"


def test_expired_and_future_tokens_refuse():
    tok = _mint()
    assert ot.verify_token(tok, "2026-07-23T12:02:01+00:00")[1] == "expired"   # ttl 120s
    assert ot.verify_token(tok, "2026-07-23T11:59:00+00:00")[1] == "expired"   # issued in future


def test_parameter_tamper_breaks_signature():
    import base64, json
    tok = _mint()
    body, sig = tok.rsplit(".", 1)
    blob = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    blob["mint"] = "0xEVIL"
    forged = base64.urlsafe_b64encode(
        json.dumps(blob, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=") + "." + sig
    assert ot.verify_token(forged, "2026-07-23T12:00:30+00:00")[1] == "bad_signature"
    assert ot.verify_token("garbage", "2026-07-23T12:00:30+00:00")[1] == "malformed"


def test_wrong_secret_refuses(monkeypatch):
    tok = _mint()
    monkeypatch.setenv("MEMECOIN_ONE_TAP_SECRET", "other-secret")
    assert ot.verify_token(tok, "2026-07-23T12:00:30+00:00")[1] == "bad_signature"


def test_single_use_replay_refuses():
    c = sqlite3.connect(":memory:")
    payload, err = ot.verify_token(_mint(), "2026-07-23T12:00:30+00:00")
    assert err is None
    assert ot.consume_token(c, payload, "2026-07-23T12:00:31+00:00") is True
    assert ot.consume_token(c, payload, "2026-07-23T12:00:32+00:00") is False   # replay


def test_endpoint_dry_run_preview_and_refusals(monkeypatch):
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    monkeypatch.setenv("MEMECOIN_ONE_TAP_ENABLED", "1")
    monkeypatch.delenv("MEMECOIN_ONE_TAP_ARMED", raising=False)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    calls = {"buys": 0}

    def fake_buy(conn, mint, uid, **kw):
        calls["buys"] += 1
        return {"status": "filled"}

    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c, "real_buy_fn": fake_buy}))
    client = app.test_client()
    tok = _mint(now="2026-07-23T12:00:00+00:00")
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso",
                        lambda: "2026-07-23T12:00:30+00:00", raising=False)

    # dry-run: valid token -> PREVIEW, no buy call, token consumed
    r = client.post("/api/memecoin/real/buy_token", json={"token": tok})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["mode"] == "dry_run"
    assert j["preview"]["mint"] == "0xabc"
    assert calls["buys"] == 0

    # replay refuses
    r2 = client.post("/api/memecoin/real/buy_token", json={"token": tok})
    assert r2.status_code == 409

    # tampered/garbage refuses
    r3 = client.post("/api/memecoin/real/buy_token", json={"token": "junk"})
    assert r3.status_code == 400

    # GET confirm page is idempotent (renders, consumes nothing)
    tok2 = _mint(now="2026-07-23T12:00:10+00:00")
    g = client.get(f"/memecoin/one-tap/{tok2}")
    assert g.status_code == 200
    assert b"buy_token" in g.data                 # page carries the POST action, not a fill
    r4 = client.post("/api/memecoin/real/buy_token", json={"token": tok2})
    assert r4.status_code == 200                  # GET did not consume it


def test_endpoint_runner_dead_refuses(monkeypatch):
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    monkeypatch.setenv("MEMECOIN_ONE_TAP_ENABLED", "1")
    monkeypatch.setenv("MEMECOIN_ONE_TAP_ARMED", "1")
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))  # runner dead
    client = app.test_client()
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso",
                        lambda: "2026-07-23T12:00:30+00:00", raising=False)
    r = client.post("/api/memecoin/real/buy_token", json={"token": _mint()})
    assert r.status_code == 503


def test_alert_message_gains_tap_link_only_when_configured(monkeypatch):
    from sniper.services.memecoin_telegram import build_alert_message

    class _Ev:
        mint = "0xabc"; network_id = "4663"; source_user_id = "uid-1"
        source_handle = "trader"; their_size_usd = 100.0; created_at = "t"; lag_s = 1.0
        ticker = "TKN"

    monkeypatch.setenv("MEMECOIN_ONE_TAP_ENABLED", "1")
    monkeypatch.setenv("MEMECOIN_ONE_TAP_BASE_URL", "http://dash:5000")
    msg = build_alert_message(_Ev())
    assert "⚡ tap: http://dash:5000/memecoin/one-tap/" in msg
    # disabled or no base URL -> no line, alert still composes
    monkeypatch.delenv("MEMECOIN_ONE_TAP_BASE_URL")
    assert "⚡ tap:" not in build_alert_message(_Ev())
    monkeypatch.setenv("MEMECOIN_ONE_TAP_ENABLED", "0")
    assert "⚡ tap:" not in build_alert_message(_Ev())


def test_one_tap_confirm_page_escapes_reflected_ticker(monkeypatch):
    """2026-07-23 review P1: a memecoin ticker is attacker-controlled and rides inside the signed
    token, so signing does not sanitize it. The confirm page inserted ticker/mint/network into
    HTML unescaped -> reflected XSS. Every reflected field must be escaped."""
    import sqlite3 as _sqlite3
    from flask import Flask
    from sniper.persistence import db_memecoin as m
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    from sniper.util.clock import real_utc_now_iso
    monkeypatch.setenv("MEMECOIN_ONE_TAP_SECRET", "test-secret")
    monkeypatch.setenv("MEMECOIN_ONE_TAP_ENABLED", "1")

    c = _sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = _sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__); app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    client = app.test_client()

    tok = ot.mint_token(mint="0xabc", network_id="4663",
                        ticker="<img src=x onerror=alert(1)>",
                        source_user_id="u1", now_iso=real_utc_now_iso())
    body = client.get(f"/memecoin/one-tap/{tok}").get_data(as_text=True)
    assert "one-tap token refused" not in body            # token is valid (fresh)
    assert "<img src=x onerror=" not in body              # raw payload NOT reflected
    assert "&lt;img src=x onerror=alert(1)&gt;" in body   # escaped instead


def test_action_tokens_table_registered_in_bootstrap():
    """The single-use ledger is created via ensure_memecoin_tables, not lazily per-request."""
    import sqlite3 as _sqlite3
    from sniper.persistence import db_memecoin as m
    c = _sqlite3.connect(":memory:"); c.row_factory = _sqlite3.Row
    m.ensure_memecoin_tables(c)
    got = c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='memecoin_action_tokens'").fetchone()
    assert got is not None
