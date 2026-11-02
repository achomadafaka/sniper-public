"""Recent and closed trades show the coin's name, the trader we copied, and both buy prices.

Operator, 2026-09-28: the slot lane and the on-chain watcher open positions with no ticker, so
those trades showed as mint codes, and neither list said who we copied or what each side paid.
The ledger names most of those coins; the rest are named by one DexScreener token batch that runs
off the request (names only), so no poll waits on an external call.
"""
import sqlite3
import threading
import time

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes import api_memecoin as api
from sniper.services import memecoin_coin_names as coin_names
from sniper.settings import memecoin as cfg
from _web_csrf import csrf_client

MINT = "FSJPGs5c8FYvNamelessMint1111111111111111pump"


@pytest.fixture(autouse=True)
def _fresh_name_cache(monkeypatch):
    monkeypatch.setattr(coin_names, "_NAMES", {})
    monkeypatch.setattr(coin_names, "_ASKED", {})
    monkeypatch.setattr(coin_names, "_BUSY", [False])


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope_sol.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(tmp_path / "nope_evm.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    return csrf_client(app), c


def _closed(c, *, mint=MINT, ticker=None, their_price=0.5, entry_price=0.6, handle="alice",
            signal_source="slot_lane"):
    pid = m.open_position(c, source_user_id="u1", source_handle=handle, mint=mint, ticker=ticker,
                          entry_price=entry_price, entry_notional=100.0, entry_fill_source="chain",
                          entry_lag_s=0.5, their_size_usd=500.0, opened_at="2026-09-28T09:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="solana",
                          signal_source=signal_source)
    c.execute("UPDATE memecoin_positions SET their_price=? WHERE id=?", (their_price, pid))
    c.commit()
    m.close_position(c, pid, "2026-09-28T09:05:00Z", realized_pnl=7.0)
    return pid


def _event(c, *, mint=MINT, ticker="FUND"):
    m.insert_event(c, {"fomo_event_id": "e-" + ticker, "source_user_id": "u2", "source_handle": "bob",
                       "mint": mint, "ticker": ticker, "side": "buy", "their_price": 0.5,
                       "their_market_cap": 1e6, "their_size_usd": 100.0,
                       "created_at": "2026-09-28T08:59:00Z", "received_at": "2026-09-28T08:59:00Z",
                       "lag_s": 0.1, "network_id": "solana"})


def _no_dex(*_a, **_k):
    raise AssertionError("DexScreener must not be asked for a coin the ledger names")


def test_the_closed_ledger_carries_the_trader_and_both_prices(monkeypatch, tmp_path):
    monkeypatch.setattr(coin_names, "_default_batch", _no_dex)
    cl, c = _client(monkeypatch, tmp_path)
    _closed(c, ticker="SYRUP")
    row = cl.get("/api/memecoin/real/closed").get_json()["closed"][0]
    assert (row["ticker"], row["source_handle"], row["their_price"], row["entry_price"]) == (
        "SYRUP", "alice", 0.5, 0.6)
    assert row["signal_source"] == "slot_lane"


def test_a_zero_source_price_is_unknown_not_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(coin_names, "_default_batch", _no_dex)
    cl, c = _client(monkeypatch, tmp_path)
    _closed(c, ticker="SYRUP", their_price=0.0)
    assert cl.get("/api/memecoin/real/closed").get_json()["closed"][0]["their_price"] is None


def test_an_unnamed_coin_takes_its_name_from_the_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(coin_names, "_default_batch", _no_dex)
    cl, c = _client(monkeypatch, tmp_path)
    _closed(c)
    _event(c)
    assert cl.get("/api/memecoin/real/closed").get_json()["closed"][0]["ticker"] == "FUND"


def test_a_coin_the_ledger_cannot_name_is_looked_up_off_the_request_once(monkeypatch, tmp_path):
    calls, release = [], threading.Event()

    def fake_batch(keys, *a, **k):
        calls.append(list(keys))
        release.wait(5)                                   # the request must not wait for this
        return {("solana", MINT): {"symbol": "VBUCKS"}}
    monkeypatch.setattr(coin_names, "_default_batch", fake_batch)
    cl, c = _client(monkeypatch, tmp_path)
    _closed(c)
    began = time.monotonic()
    first = cl.get("/api/memecoin/real/closed").get_json()["closed"][0]
    assert time.monotonic() - began < 2.0, "the request waited for the external lookup"
    assert first["ticker"] is None
    release.set()
    for t in threading.enumerate():
        if t.name == "coin-name-lookup":
            t.join(5)
    assert cl.get("/api/memecoin/real/closed").get_json()["closed"][0]["ticker"] == "VBUCKS"
    cl.get("/api/memecoin/real/closed")
    assert calls == [[("solana", MINT)]], "a known name was asked again"


def test_a_miss_is_asked_again_only_after_ten_minutes(monkeypatch, tmp_path):
    calls = []

    def fake_batch(keys, *a, **k):
        calls.append(list(keys))
        return {}
    monkeypatch.setattr(coin_names, "_default_batch", fake_batch)
    _cl, c = _client(monkeypatch, tmp_path)
    keys = [("solana", MINT)]

    def ask(now):
        names = api._coin_names(c, keys, now=now)
        for t in threading.enumerate():
            if t.name == "coin-name-lookup":
                t.join(5)
        return names
    assert ask(1000.0) == {}
    assert ask(1000.0 + 599.0) == {}
    assert len(calls) == 1
    ask(1000.0 + 601.0)
    assert len(calls) == 2


def test_a_failed_lookup_never_breaks_the_ledger(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(coin_names, "_default_batch", boom)
    cl, c = _client(monkeypatch, tmp_path)
    _closed(c)
    r = cl.get("/api/memecoin/real/closed")
    for t in threading.enumerate():
        if t.name == "coin-name-lookup":
            t.join(5)
    assert r.status_code == 200 and r.get_json()["closed"][0]["ticker"] is None
    assert coin_names._BUSY == [False], "a failed lookup must free the next one"


def test_open_positions_are_named_the_same_way(monkeypatch, tmp_path):
    monkeypatch.setattr(coin_names, "_default_batch", _no_dex)
    cl, c = _client(monkeypatch, tmp_path)
    m.open_position(c, source_user_id="u1", source_handle="alice", mint=MINT, ticker=None,
                    entry_price=0.6, entry_notional=100.0, entry_fill_source="chain",
                    entry_lag_s=0.5, their_size_usd=500.0, opened_at="2026-09-28T09:00:00Z",
                    strategy="alpha_b", execution_mode="real", network_id="solana",
                    signal_source="slot_lane")
    _event(c)
    rows = cl.get("/api/memecoin/real/positions").get_json()["positions"]
    assert [(r["ticker"], r["source_handle"]) for r in rows] == [("FUND", "alice")]


def test_the_ledger_name_read_skips_the_slow_feed_scan():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, network_id, "
              "mint, ticker, observed_at, raw_json) VALUES ('u1', 'swap', 'solana', ?, 'FEED', "
              "'2026-09-28T09:00:00Z', '{}')", (MINT,))
    assert m.resolve_mint_ticker(c, MINT, network_id="solana") == "FEED"
    assert m.resolve_mint_ticker(c, MINT, network_id="solana", include_feed=False) is None


# --- Telegram (operator, 2026-09-28: "also fix the telegram alerts to show the real name") -------

def test_the_telegram_card_names_a_coin_the_row_left_unnamed(monkeypatch):
    from types import SimpleNamespace
    from sniper.services.memecoin_telegram import _NotificationWorker
    calls = []

    def fake_batch(keys, *a, **k):
        calls.append(list(keys))
        return {("solana", MINT): {"symbol": "VBUCKS"}}
    monkeypatch.setattr(coin_names, "_default_batch", fake_batch)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = _closed(c)
    worker = SimpleNamespace(read_conn=c, write_conn=c, _source_intent=lambda view: None)
    view = {"message_kind": "exit", "status": "sold", "mint": MINT, "ticker": None,
            "network_id": "solana", "source_handle": "probablyinsider", "realized_pnl": 7.0,
            "entry_notional": 100.0, "filled_notional": 107.0, "reason": "real_tp",
            "position_id": pid}
    text = _NotificationWorker._render(worker, view)
    assert "VBUCKS" in text.splitlines()[0], text
    assert m.get_position(c, pid)["ticker"] == "VBUCKS", "the name must stick to the position"
    assert calls == [[("solana", MINT)]], "the card must wait for the name on its own thread"
    _NotificationWorker._render(worker, view)
    assert len(calls) == 1, "a known name was asked again"


def test_the_telegram_card_prefers_the_ledger_and_never_raises(monkeypatch):
    from types import SimpleNamespace
    from sniper.services.memecoin_telegram import _NotificationWorker
    monkeypatch.setattr(coin_names, "_default_batch", _no_dex)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    _event(c)
    worker = SimpleNamespace(read_conn=c, write_conn=c, _source_intent=lambda view: None)
    view = {"message_kind": "exit", "status": "sold", "mint": MINT, "ticker": None,
            "network_id": "solana", "reason": "real_tp"}
    assert "FUND" in _NotificationWorker._render(worker, view).splitlines()[0]
    # The trading thread's text alerts read only the cache, so a ledger name must land there too.
    assert coin_names.cached_for_mint(MINT) == "FUND"
    broken = SimpleNamespace(read_conn=None, write_conn=None, _source_intent=lambda view: None)
    text = _NotificationWorker._render(broken, dict(view, mint="Zzzz" + MINT[4:]))
    assert text.splitlines()[0].startswith("💰 SOLD · Zzzz…pump"), text   # the mint, as before


def test_text_alerts_use_a_name_the_process_already_knows():
    from sniper.services import memecoin_alerts as alerts
    sent = []
    send = lambda text, **kw: sent.append(text) or (True, 0.0)  # noqa: E731
    alerts.alert_failed_tx(MINT, "SIG12345", "slippage", send_fn=send)
    assert "VBUCKS" not in sent[-1]
    coin_names._NAMES[("solana", MINT)] = "VBUCKS"
    alerts.alert_failed_tx(MINT, "SIG12345", "slippage", send_fn=send)
    alerts.alert_stuck_bag(MINT, 7, send_fn=send)
    alerts.alert_derisk(MINT, 12.0, "SIG12345", handle="alice", send_fn=send)
    assert all("VBUCKS" in text for text in sent[1:]), sent
