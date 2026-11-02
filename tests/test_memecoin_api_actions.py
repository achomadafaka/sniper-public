"""P1/P2 route surface: partial-sell, manual-buy, traders/performance. Price/fill
fns injected via ctx (no network). Style mirrors tests/test_memecoin_api.py."""
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from _web_csrf import csrf_client


def _client(ctx_extra=None):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    ctx = {"conn": c}
    if ctx_extra:
        ctx.update(ctx_extra)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return csrf_client(app), c


def _open(c, entry=0.01, notional=100.0, uid="u1", mint="MINT", handle="alice"):
    pid = m.open_position(c, source_user_id=uid, source_handle=handle, mint=mint, ticker="T",
                          entry_price=entry, entry_notional=notional, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1000.0, opened_at="2026-07-05T10:00:00+00:00")
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=notional / entry,
                  notional=notional, fill_source="jupiter", fraction=1.0,
                  created_at="2026-07-05T10:00:00+00:00")
    return pid


# ── partial-sell endpoint ───────────────────────────────────────────────────
def test_sell_half_keeps_open():
    cl, c = _client({"price_fn": lambda mint: {"price": 0.02, "liquidity": 1e15}})
    pid = _open(c)                       # qty 10000
    body = cl.post(f"/api/memecoin/positions/{pid}/sell", json={"fraction": 0.5}).get_json()
    assert body["ok"] and body["closed"] is False
    assert abs(body["realized_delta"] - 50.0) < 1e-6
    assert m.get_position(c, pid)["is_open"] == 1


def test_sell_full_closes():
    cl, c = _client({"price_fn": lambda mint: {"price": 0.02, "liquidity": 1e15}})
    pid = _open(c)
    body = cl.post(f"/api/memecoin/positions/{pid}/sell", json={"fraction": 1.0}).get_json()
    assert body["ok"] and body["closed"] is True
    assert m.get_position(c, pid)["is_open"] == 0


def test_sell_not_open_404():
    cl, c = _client({"price_fn": lambda mint: {"price": 0.02, "liquidity": 1e15}})
    assert cl.post("/api/memecoin/positions/999/sell", json={"fraction": 0.5}).status_code == 404


def test_manual_close_uses_remaining_after_partial():
    # sell 50% then full manual close → realized reflects both slices, not double-counted
    cl, c = _client({"price_fn": lambda mint: {"price": 0.02, "liquidity": 1e15}})
    pid = _open(c)                       # qty 10000 @0.01
    cl.post(f"/api/memecoin/positions/{pid}/sell", json={"fraction": 0.5})  # +50 on 5000
    # manual close uses live dexscreener; stub it via the module the route imports
    import sniper.execution.solana_quote as sq
    orig = sq.dexscreener_stats
    sq.dexscreener_stats = lambda mint, http=None: {"price": 0.02, "liquidity": 1e15}
    try:
        assert cl.post(f"/api/memecoin/positions/{pid}/close").get_json()["ok"]
    finally:
        sq.dexscreener_stats = orig
    pos = m.get_position(c, pid)
    assert pos["is_open"] == 0
    # both slices at 0.02: (0.02-0.01)*10000 = 100 total, not 150 (no double count)
    assert abs(pos["realized_pnl"] - 100.0) < 1e-6


# ── manual-buy endpoint ─────────────────────────────────────────────────────
def test_manual_buy_opens():
    cl, c = _client({"fill_fn": lambda *a, **k: (0.005, "jupiter")})
    body = cl.post("/api/memecoin/manual-buy",
                   json={"source_user_id": "u9", "mint": "MINT2", "ticker": "X"}).get_json()
    assert body["ok"] and body["position_id"]
    pos = m.get_open_position(c, "u9", "MINT2")
    assert pos is not None and pos["experiment_status"] == "manual"


def test_manual_close_excludes_paper_position_from_experiment(monkeypatch):
    cl, c = _client()
    pid = _open(c)
    monkeypatch.setattr(
        "sniper.execution.solana_quote.dexscreener_stats",
        lambda *_a, **_k: {"price": 0.02, "liquidity": 1e15},
    )
    assert cl.post(f"/api/memecoin/positions/{pid}/close").status_code == 200
    assert m.get_position(c, pid)["experiment_status"] == "operator_modified"


def test_manual_buy_requires_fields():
    cl, c = _client({"fill_fn": lambda *a, **k: (0.005, "jupiter")})
    assert cl.post("/api/memecoin/manual-buy", json={"mint": "M"}).status_code == 400
    assert cl.post("/api/memecoin/manual-buy", json={"source_user_id": "u"}).status_code == 400


def test_manual_buy_duplicate_400():
    cl, c = _client({"fill_fn": lambda *a, **k: (0.005, "jupiter")})
    cl.post("/api/memecoin/manual-buy", json={"source_user_id": "u9", "mint": "M2", "ticker": "X"})
    assert cl.post("/api/memecoin/manual-buy",
                   json={"source_user_id": "u9", "mint": "M2", "ticker": "X"}).status_code == 400


# ── traders/performance ─────────────────────────────────────────────────────
def test_traders_performance_rollup():
    cl, c = _client()
    # trader alice: one closed win, one open; trader bob: one closed loss
    p1 = _open(c, uid="u1", mint="A", handle="alice")
    m.close_position(c, p1, "2026-07-05T10:05:00+00:00", realized_pnl=40.0)
    _open(c, uid="u1", mint="B", handle="alice")            # open
    p3 = _open(c, uid="u2", mint="C", handle="bob")
    m.close_position(c, p3, "2026-07-05T10:02:00+00:00", realized_pnl=-10.0)
    traders = {t["source_user_id"]: t for t in
               cl.get("/api/memecoin/traders/performance").get_json()["traders"]}
    a = traders["u1"]
    assert a["entries"] == 2 and a["closed"] == 1 and a["win_rate"] == 1.0
    assert abs(a["closed_pnl"] - 40.0) < 1e-6
    assert a["median_hold_s"] == 300.0                      # 5 min
    b = traders["u2"]
    assert b["closed"] == 1 and b["win_rate"] == 0.0 and abs(b["closed_pnl"] + 10.0) < 1e-6
    # sorted by closed_pnl desc → alice first
    order = [t["source_user_id"] for t in
             cl.get("/api/memecoin/traders/performance").get_json()["traders"]]
    assert order[0] == "u1"


# ── partial_exit atomicity (review C2) ──────────────────────────────────────
def test_partial_exit_atomic_vs_concurrent_close():
    from sniper.services.memecoin_copy_trader import partial_exit
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; m.ensure_memecoin_tables(c)
    pid = _open(c)
    # simulate a bracket/mirror close winning the claim first (is_open 1->0)
    assert m.claim_close(c, pid) is True
    # a manual partial now must NOT book against the claimed position (would double-sell)
    r = partial_exit(c, pid, 0.5, "2026-07-05T10:05:00+00:00",
                     price_fn=lambda mint: {"price": 0.02, "liquidity": 1e15})
    assert r is None
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE kind='partial_exit'").fetchone()[0] == 0


def test_partial_then_full_no_double_count():
    from sniper.services.memecoin_copy_trader import partial_exit
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; m.ensure_memecoin_tables(c)
    pid = _open(c)                     # qty 10000
    px = lambda mint: {"price": 0.02, "liquidity": 1e15}
    partial_exit(c, pid, 0.5, "t1", price_fn=px)      # sell 5000, reopened
    assert m.get_position(c, pid)["is_open"] == 1 and abs(m.open_qty(c, pid) - 5000) < 1e-6
    partial_exit(c, pid, 1.0, "t2", price_fn=px)      # sell the rest, closes
    assert m.get_position(c, pid)["is_open"] == 0
    # total exited qty == entry qty (no double count / negative qty)
    assert abs(m.open_qty(c, pid)) < 1e-6
