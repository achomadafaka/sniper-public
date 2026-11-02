"""A real partial sell crosses to the runner as an INTENT (2026-09-05); the route validates the
fraction, writes the row, answers 202 queued, and never touches an executor. Executor semantics
(derisked / unknown / no_partial) are the drain's and are tested in
tests/test_memecoin_manual_exit_intents.py."""
import sqlite3
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_manual_exit_intents as xi
from _web_csrf import csrf_client

T0 = "2026-07-18T00:00:00+00:00"


def _client(intent_fn=None):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="MintP", ticker="P",
                          entry_price=0.01, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
                          their_size_usd=0.0, opened_at=T0, strategy="alpha_b", execution_mode="real",
                          network_id="solana")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.01, qty=1000.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at=T0, tx_sig="E")
    ctx = {"conn": c}
    if intent_fn is not None:
        ctx["real_exit_intent_fn"] = intent_fn
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return csrf_client(app), c, pid


def test_real_partial_sell_queues_an_intent_with_its_fraction():
    cli, c, pid = _client(xi.make_enqueue_exit_fn(lambda: T0))
    r = cli.post(f"/api/memecoin/positions/{pid}/sell", json={"fraction": 0.5})
    j = r.get_json()
    assert r.status_code == 202 and j["ok"] is False and j["status"] == "queued"
    row = xi.read_status(c, j["intent_id"])
    assert row["action"] == "partial" and row["fraction"] == 0.5 and row["position_id"] == pid
    assert m.get_position(c, pid)["is_open"] == 1


def test_real_partial_rejects_full_zero_or_negative_fraction():
    cli, c, pid = _client(xi.make_enqueue_exit_fn(lambda: T0))
    for f in (1.0, 0.0, -0.2, 1.5):
        r = cli.post(f"/api/memecoin/positions/{pid}/sell", json={"fraction": f})
        assert r.status_code == 400, f
    assert xi.pending_count(c) == 0


def test_real_partial_503_when_execution_not_configured():
    cli, c, pid = _client(None)                            # disarmed — no intent fn bound
    r = cli.post(f"/api/memecoin/positions/{pid}/sell", json={"fraction": 0.5})
    assert r.status_code == 503 and r.get_json()["ok"] is False
    assert xi.pending_count(c) == 0
