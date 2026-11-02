"""Item 1 — the manual UI routes look a position up by id (bypassing the mode-scoped sweep), so a
real (on-chain) position must be guarded: its close routes through the real exit executor (chain
PnL), NEVER a modeled DexScreener fill, and a partial sell is rejected outright. Without a bound
real exit fn a real close fails LOUDLY and leaves the position open — it never fabricates a fill."""
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


def _open_real(c, mint="So1Mint", uid="u1"):
    pid = m.open_position(c, source_user_id=uid, source_handle="h", mint=mint, ticker="T",
                          entry_price=0.01, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="solana")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.01, qty=1000.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="2026-07-09T00:00:00Z", tx_sig="E")
    return pid


def test_real_close_without_exit_fn_fails_loud_and_stays_open():
    cl, c = _client()                                   # no real_exit_fn bound
    pid = _open_real(c)
    r = cl.post(f"/api/memecoin/positions/{pid}/close")
    assert r.status_code == 503
    assert r.get_json()["ok"] is False
    assert m.get_position(c, pid)["is_open"] == 1       # NOT fake-closed
    # crucially: no modeled fill was written against the on-chain position
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='manual_close'",
                     (pid,)).fetchone()[0] == 0


def test_real_close_queues_an_intent_and_never_sells_in_process():
    # 2026-09-05: the web writes an INTENT (202, still open); the runner sells with its executors.
    from sniper.services import memecoin_manual_exit_intents as xi
    executed = []
    cl, c = _client({"real_exit_fn": lambda *a: executed.append(a) or {"status": "closed"},
                     "real_exit_intent_fn": xi.make_enqueue_exit_fn(lambda: "2026-07-09T00:00:01Z")})
    pid = _open_real(c)
    r = cl.post(f"/api/memecoin/positions/{pid}/close")
    j = r.get_json()
    assert r.status_code == 202 and j["ok"] is False and j["status"] == "queued"
    assert executed == [] and m.get_position(c, pid)["is_open"] == 1
    assert xi.read_status(c, j["intent_id"])["action"] == "close"


def test_the_venue_router_sends_an_evm_position_to_the_evm_executor():
    # `make_venue_exit_fn` is what the RUNNER's drain uses; it must route by network, never reject.
    from sniper.services.memecoin_real_runner import make_venue_exit_fn
    seen = {}

    def evm_exit(conn, pos, reason, now):
        seen["chain"] = pos["network_id"]
        return {"status": "closed", "realized": 3.0, "tx_hash": "0xh"}

    exit_fn = make_venue_exit_fn({"solana": {"exit_fn": lambda *a: pytest_fail_sol()},
                                  "8453": {"exit_fn": evm_exit}})
    res = exit_fn(None, {"id": 1, "network_id": "8453", "mint": "0xevm"}, "manual", "t")
    assert res["status"] == "closed" and seen["chain"] == "8453"
    assert exit_fn(None, {"id": 2, "network_id": "56", "mint": "0xbsc"}, "manual", "t")["status"] == "no_venue"


def pytest_fail_sol():
    raise AssertionError("EVM position must not route to the Solana exit_fn")


def test_partial_sell_on_real_position_never_modeled_needs_real_executor():
    # A real bag must NEVER take a modeled partial fill. Since 2026-09-05 a real partial crosses to
    # the runner as an intent: with no real_exit_intent_fn bound (disarmed web ctx here) it returns
    # 503 — never a fabricated partial_exit fill. The queued path is covered in
    # tests/test_memecoin_partial_sell.py.
    cl, c = _client({"price_fn": lambda mint: {"price": 0.02, "liquidity": 1e15}})
    pid = _open_real(c)
    r = cl.post(f"/api/memecoin/positions/{pid}/sell", json={"fraction": 0.5})
    assert r.status_code == 503 and r.get_json()["ok"] is False
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='partial_exit'",
                     (pid,)).fetchone()[0] == 0
