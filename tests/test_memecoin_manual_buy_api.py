import pytest
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services.memecoin_manual_intents import make_enqueue_buy_fn


NOW = "2026-08-29T12:00:00+00:00"


def _client(buy_fn):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    app = Flask(__name__)
    app.secret_key = "manual-buy-test"
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn, "real_buy_fn": buy_fn}))
    return app.test_client(), conn


def _csrf(client):
    token = client.get("/api/memecoin/real/status").get_json()["csrf_token"]
    return {"X-CSRF-Token": token}


def test_manual_buy_requires_csrf_before_it_calls_the_money_seam():
    calls = []
    client, _ = _client(lambda *a, **k: calls.append(1) or {"status": "queued"})

    response = client.post("/api/memecoin/real/buy", json={"mint": "So1Mint", "size_usd": 20})

    assert response.status_code == 403
    assert calls == []


def test_manual_buy_accepts_https_origin_from_loopback_proxy():
    calls = []
    client, _ = _client(lambda *a, **k: calls.append(1) or {"status": "queued"})
    headers = {
        **_csrf(client),
        "Host": "public.trycloudflare.com",
        "Origin": "https://public.trycloudflare.com",
        "X-Forwarded-Proto": "https",
    }

    response = client.post(
        "/api/memecoin/real/buy",
        json={"mint": "So1Mint", "size_usd": 20},
        headers=headers,
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert calls == [1]


def test_manual_buy_rejects_forwarded_origin_from_untrusted_client():
    calls = []
    client, _ = _client(lambda *a, **k: calls.append(1) or {"status": "queued"})
    headers = {
        **_csrf(client),
        "Host": "public.trycloudflare.com",
        "Origin": "https://public.trycloudflare.com",
        "X-Forwarded-Proto": "https",
    }

    response = client.post(
        "/api/memecoin/real/buy",
        json={"mint": "So1Mint", "size_usd": 20},
        headers=headers,
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert response.status_code == 403
    assert calls == []


def test_manual_buy_accepts_one_percent_size_input():
    seen = {}

    def buy_fn(conn, mint, uid, **kw):
        seen.update(mint=mint, uid=uid, **kw)
        return {"status": "queued", "intent_id": "intent-13"}

    client, _ = _client(buy_fn)
    response = client.post(
        "/api/memecoin/real/buy",
        json={"mint": "So1Mint", "size_pct_equity": 0.13, "intent_id": "intent-13"},
        headers=_csrf(client),
    )

    assert response.status_code == 200
    assert seen["size_pct_equity"] == 0.13
    assert seen["intent_id"] == "intent-13"
    assert "size_usd" not in seen


def test_manual_buy_threads_override_to_the_buy_seam():
    seen = {}

    def buy_fn(conn, mint, uid, **kw):
        seen.update(mint=mint, uid=uid, **kw)
        return {"status": "queued", "intent_id": "intent-override"}

    client, _ = _client(buy_fn)
    response = client.post(
        "/api/memecoin/real/buy",
        json={"mint": "0xMint", "network_id": "56", "size_usd": 100000,
              "override": True, "intent_id": "intent-override"},
        headers=_csrf(client),
    )

    assert response.status_code == 200
    assert seen["override"] is True


@pytest.mark.parametrize("value", ["false", "0", "no", 1, "true", {"x": 1}])
def test_manual_buy_override_accepts_only_a_json_true(value):
    """Codex review 2026-09-23 A2: `bool("false")` is True, so any nonempty string authorised
    the risk-gate bypass. Only the JSON boolean `true` carries the operator's intent; anything
    else is a malformed request and is refused before the money seam."""
    seen = {}

    def buy_fn(conn, mint, uid, **kw):
        seen.update(kw)
        return {"status": "queued", "intent_id": "intent-x"}

    client, _ = _client(buy_fn)
    response = client.post(
        "/api/memecoin/real/buy",
        json={"mint": "0xMint", "network_id": "56", "size_usd": 20, "override": value,
              "intent_id": "intent-x"},
        headers=_csrf(client),
    )
    assert response.status_code == 400
    assert "override" not in seen


def test_manual_buy_omits_override_when_not_requested():
    """Backward compatibility: a plain manual buy must not silently carry override=False into a
    buy_fn that predates the parameter."""
    seen = {}

    def buy_fn(conn, mint, uid, **kw):
        seen.update(mint=mint, uid=uid, **kw)
        return {"status": "queued", "intent_id": "intent-plain"}

    client, _ = _client(buy_fn)
    response = client.post(
        "/api/memecoin/real/buy",
        json={"mint": "So1Mint", "size_usd": 20, "intent_id": "intent-plain"},
        headers=_csrf(client),
    )

    assert response.status_code == 200
    assert "override" not in seen


def test_manual_buy_rejects_two_size_inputs():
    client, _ = _client(lambda *a, **k: {"status": "queued"})
    response = client.post(
        "/api/memecoin/real/buy",
        json={"mint": "So1Mint", "size_usd": 20, "size_pct_equity": 0.13},
        headers=_csrf(client),
    )
    assert response.status_code == 400
    assert "one size" in response.get_json()["error"]


def test_manual_buy_rejects_an_invalid_percent():
    client, _ = _client(lambda *a, **k: {"status": "queued"})
    for value in (0, -0.1, 1.1, "bad"):
        response = client.post(
            "/api/memecoin/real/buy",
            json={"mint": "So1Mint", "size_pct_equity": value},
            headers=_csrf(client),
        )
        assert response.status_code == 400, value


def test_manual_buy_rejects_a_boolean_percent():
    calls = []
    client, _ = _client(lambda *a, **k: calls.append(1) or {"status": "queued"})
    response = client.post(
        "/api/memecoin/real/buy", json={"mint": "So1Bool", "size_pct_equity": True},
        headers=_csrf(client),
    )
    assert response.status_code == 400
    assert calls == []


def test_manual_buy_rejects_a_nonfinite_dollar_size():
    calls = []
    client, _ = _client(lambda *a, **k: calls.append(1) or {"status": "queued"})
    response = client.post(
        "/api/memecoin/real/buy", json={"mint": "So1Inf", "size_usd": "Infinity"},
        headers=_csrf(client),
    )
    assert response.status_code == 400
    assert calls == []


def test_repeated_intent_id_returns_the_first_queue_row():
    client, conn = _client(make_enqueue_buy_fn(lambda: NOW))
    payload = {"mint": "So1Mint", "size_pct_equity": 0.26, "intent_id": "same-intent"}
    headers = _csrf(client)

    first = client.post("/api/memecoin/real/buy", json=payload, headers=headers).get_json()
    second = client.post("/api/memecoin/real/buy", json=payload, headers=headers).get_json()

    assert first["status"] == second["status"] == "queued"
    assert first["intent_id"] == second["intent_id"] == "same-intent"
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_manual_intents WHERE intent_id='same-intent'"
    ).fetchone()[0] == 1
