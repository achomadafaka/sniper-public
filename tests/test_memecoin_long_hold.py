"""Long Hold keeps funded ownership and manual exits while pausing automatic work."""
import sqlite3
from types import SimpleNamespace

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes import api_memecoin as api
from sniper.services import memecoin_real_runner as runner
from sniper.execution.exec_lock import serialize_executor


def _book(extra=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.open_position(
        conn, source_user_id="u", source_handle="alice", mint="MINT", ticker="COIN",
        entry_price=1, entry_notional=25, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=25, opened_at="2026-09-26T12:00:00+00:00",
        execution_mode="real", network_id="solana", strategy="alpha_b")
    m.insert_fill(conn, position_id=pid, kind="entry", price=1, qty=25, notional=25,
                  fill_source="chain", fraction=1, created_at="2026-09-26T12:00:00+00:00")
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({
        "conn": conn, "real_exit_intent_fn": lambda _c, _p, _r, actor=None:
            {"status": "queued", "intent_id": "manual-1"},
        **(extra or {}),
    }))
    return conn, app.test_client(), pid


def test_long_hold_is_visible_unpriced_and_refreshes_only_on_demand(monkeypatch):
    conn, client, pid = _book()
    csrf = client.get("/api/memecoin/real/status").get_json()["csrf_token"]
    paused = client.post(f"/api/memecoin/positions/{pid}/recover", json={"action": "pause"},
                         headers={"X-CSRF-Token": csrf})
    assert paused.status_code == 200
    assert m.get_position(conn, pid)["is_open"] == 1
    assert not m.open_positions_by_strategy(conn, "alpha_b", mode="real")

    calls = []
    monkeypatch.setattr(api, "dexscreener_stats_batch_keyed",
                        lambda keys, **kw: calls.append(keys) or {("solana", "MINT"): {"price": 2.0, "source": "dexscreener"}})
    book = client.get("/api/memecoin/real/positions").get_json()["positions"]
    hold = next(row for row in book if row["id"] == pid)
    assert hold["tracking_paused_at"]
    assert hold.get("market_mid_price") is None
    assert calls == []
    quote = client.get(f"/api/memecoin/real/positions/{pid}/refresh-price")
    assert quote.status_code == 200
    assert quote.get_json()["price"] == 2.0
    assert len(calls) == 1
    assert client.post(f"/api/memecoin/positions/{pid}/close", json={},
                       headers={"X-CSRF-Token": csrf}).status_code == 202


def test_long_hold_accounting_counts_inventory_without_quote_calls():
    calls = []
    conn, client, pid = _book({
        "acct_stats_fn": lambda mints: calls.append(("stats", mints)) or {},
        "real_mark_fn": lambda positions: calls.append(("mark", positions)) or {},
        "acct_balances_fn": lambda: {},
    })
    m.set_position_tracking_paused(conn, pid, "2026-09-26T12:01:00+00:00")
    response = client.get("/api/memecoin/real/accounting")
    assert response.status_code == 200
    assert calls == []
    assert response.get_json()["slots"]["used"] == 1


def test_observation_book_excludes_hold_but_exposure_keeps_it():
    conn, _client, pid = _book()
    assert m.set_position_tracking_paused(conn, pid, "2026-09-26T12:01:00+00:00")
    assert m.list_open_positions(conn, mode="real", include_paused=False) == []
    assert m.real_open_snapshot(conn)["notional"] == 25.0
    assert m.set_position_tracking_paused(conn, pid, None)
    assert [p["id"] for p in m.list_open_positions(conn, mode="real", include_paused=False)] == [pid]


def test_refresh_failure_is_unknown_and_pause_requires_csrf(monkeypatch):
    conn, client, pid = _book()
    assert client.post(f"/api/memecoin/positions/{pid}/recover",
                       json={"action": "pause"}).status_code == 403
    m.set_position_tracking_paused(conn, pid, "2026-09-26T12:01:00+00:00")
    monkeypatch.setattr(api, "dexscreener_stats_batch_keyed", lambda _keys: {})
    monkeypatch.setattr(api, "gt_stats_batch_keyed", lambda _keys, **_kw: {})
    response = client.get(f"/api/memecoin/real/positions/{pid}/refresh-price")
    assert response.status_code == 503
    assert "price" not in response.get_json()


def test_robinhood_refresh_uses_gt_without_dex(monkeypatch):
    conn, client, pid = _book()
    conn.execute("UPDATE memecoin_positions SET mint='0xabc', network_id='4663' WHERE id=?", (pid,))
    conn.commit()
    m.set_position_tracking_paused(conn, pid, "2026-09-26T12:01:00+00:00")
    monkeypatch.setattr(api, "dexscreener_stats_batch_keyed",
                        lambda _keys: (_ for _ in ()).throw(AssertionError("Dex must not be read")))
    monkeypatch.setattr(api, "gt_stats_batch_keyed",
                        lambda keys, **_kw: {keys[0]: {"price": 3.0, "source": "geckoterminal"}})
    quote = client.get(f"/api/memecoin/real/positions/{pid}/refresh-price")
    assert quote.status_code == 200
    assert quote.get_json()["price"] == 3.0


def test_long_hold_blocks_mirror_and_executor_but_allows_manual(tmp_path):
    conn, _client, pid = _book()
    m.set_position_tracking_paused(conn, pid, "2026-09-26T12:01:00+00:00")
    called = []
    contexts = {"solana": {"exit_fn": lambda *_args: called.append("mirror")}}
    event = SimpleNamespace(source_user_id="u", mint="MINT", network_id="solana", side="sell")
    result = runner.real_mirror_on_sell(conn, event, contexts, now_fn=lambda: "now")
    assert result["status"] == "tracking_paused"
    assert called == []

    wrapped = serialize_executor(
        lambda *_args: called.append("exit") or {"status": "sent"},
        "long-hold-test", lock_dir=str(tmp_path), release_unsigned_close_on_error=True)
    pos = m.get_position(conn, pid)
    assert wrapped(conn, pos, "stop", "now")["status"] == "tracking_paused"
    assert called == []
    assert wrapped(conn, pos, "manual", "now")["status"] == "sent"
    assert called == ["exit"]


def test_pause_and_automatic_claim_are_atomic():
    conn, client, pid = _book()
    assert m.set_position_tracking_paused(conn, pid, "2026-09-26T12:01:00+00:00")
    assert m.claim_position_close(conn, pid, "2026-09-26T12:02:00+00:00", "stop") is None
    assert m.claim_position_derisk(conn, pid, "2026-09-26T12:02:00+00:00") is None
    assert m.claim_position_close(conn, pid, "2026-09-26T12:02:00+00:00", "manual")
    assert not m.set_position_tracking_paused(conn, pid, "2026-09-26T12:03:00+00:00")
    csrf = client.get("/api/memecoin/real/status").get_json()["csrf_token"]
    response = client.post(f"/api/memecoin/positions/{pid}/recover",
                           json={"action": "pause"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 409
