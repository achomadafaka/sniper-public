import sqlite3
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from _web_csrf import csrf_client


def _client(ctx_extra=None):
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    ctx = {"conn": c}
    if ctx_extra:
        ctx.update(ctx_extra)
    app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return csrf_client(app), c


def test_resolve_handle_to_userid():
    def fake_fomo_get(auth, path):
        assert path == "/v2/users/userHandle/techquant"
        return 200, {"responseObject": {"id": "uuid-123", "displayName": "techquant"}}, None
    cl, c = _client({"fomo_get": fake_fomo_get})
    r = cl.get("/api/memecoin/resolve/techquant").get_json()
    assert r["found"] is True and r["source_user_id"] == "uuid-123"
    assert r["display_name"] == "techquant"


def test_resolve_handle_not_found():
    def fake_fomo_get(auth, path):
        return 200, {"responseObject": {}}, None
    cl, c = _client({"fomo_get": fake_fomo_get})
    assert cl.get("/api/memecoin/resolve/nobody").get_json()["found"] is False


def test_wallet_crud_endpoints():
    cl, c = _client()
    assert cl.post("/api/memecoin/wallets",
                   json={"source_user_id": "u1", "handle": "alice"}).get_json()["ok"]
    assert cl.get("/api/memecoin/wallets").get_json()["wallets"][0]["handle"] == "alice"
    assert cl.post("/api/memecoin/wallets/u1/pause", json={"paused": True}).get_json()["ok"]
    assert m.curated_user_ids(c) == set()
    assert cl.delete("/api/memecoin/wallets/u1").get_json()["ok"]
    retired = cl.get("/api/memecoin/wallets").get_json()["wallets"]
    assert len(retired) == 1
    assert retired[0]["excluded"] == 1
    assert retired[0]["paused"] == 1
    assert retired[0]["real_approved"] == 0


def test_open_positions_endpoint_shape():
    cl, c = _client()
    m.open_position(c, source_user_id="u1", source_handle="a", mint="MINT", ticker="T",
                    entry_price=0.005, entry_notional=100.0, entry_fill_source="jupiter",
                    entry_lag_s=0.3, their_size_usd=1000.0, opened_at="t")
    body = cl.get("/api/memecoin/positions/open").get_json()
    assert body["positions"][0]["mint"] == "MINT" and "marks" in body


def test_open_positions_uses_stored_mark_when_live_pricer_raises(monkeypatch):
    cl, c = _client()
    pid = m.open_position(
        c, source_user_id="u1", source_handle="a", mint="MINT", ticker="T",
        entry_price=1.0, entry_notional=100.0, entry_fill_source="dexscreener",
        entry_lag_s=0.3, their_size_usd=1000.0,
        opened_at="2026-07-28T00:00:00+00:00",
    )
    m.insert_fill(
        c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
        fill_source="dexscreener", fraction=1.0,
        created_at="2026-07-28T00:00:00+00:00",
    )
    m.update_position_last_quote(c, pid, 1.2, "2026-07-28T00:01:00+00:00")
    monkeypatch.setattr(
        "sniper.routes.api_memecoin.dexscreener_stats_batch_keyed",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network call")),
    )
    response = cl.get("/api/memecoin/positions/open")
    assert response.status_code == 200
    mark = response.get_json()["marks"][0]
    assert mark["current_price"] == 1.2 and mark["open_qty"] == 100.0
    assert "quote_age_s" in mark and "stale" in mark


def test_closed_stats_shape():
    cl, c = _client()
    pid = m.open_position(c, source_user_id="u1", source_handle="alice", mint="M", ticker="T",
                          entry_price=0.005, entry_notional=100.0, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1000.0, opened_at="t")
    m.close_position(c, pid, "t2", realized_pnl=25.0)
    stats = cl.get("/api/memecoin/positions/closed").get_json()["stats"]
    assert stats["total_pnl"] == 25.0 and stats["win_rate"] == 1.0
    assert stats["by_wallet"]["alice"] == 25.0


def test_blueprint_registers_on_app():
    cl, c = _client()
    # /api/memecoin/wallets reachable => blueprint registered
    assert cl.get("/api/memecoin/wallets").status_code == 200


def test_memecoin_page_redirects_to_tab():
    # Standalone _PAGE retired — /memecoin now redirects to the native dashboard tab.
    cl, c = _client()
    r = cl.get("/memecoin")
    assert r.status_code == 302 and "/?tab=memecoin" in r.headers["Location"]


def test_strategies_endpoint_surfaces_measurement_honesty_fields():
    cl, c = _client()
    pid = m.open_position(c, source_user_id="u1", source_handle="alice", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1000.0,
                          opened_at="2026-07-05T10:00:00+00:00", strategy="alpha_b")
    m.close_position(c, pid, "2026-07-05T10:05:00+00:00", realized_pnl=0.0,
                     exit_reason="b_timecap_noquote")
    body = cl.get("/api/memecoin/strategies").get_json()
    row = {r["strategy"]: r for r in body["rows"]}["alpha_b"]
    assert row["noquote_closes"] == 1
    assert row["stale_closes"] == 0
    assert row["noquote_shadow_pnl"] == -100.0
    assert row["conservative_total_pnl"] == -100.0
