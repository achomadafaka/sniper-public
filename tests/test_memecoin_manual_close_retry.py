"""The manual-close retry ladder (2/5/10/30 s on an unsigned stuck result; a signed result is never
retried) moved out of the web request on 2026-09-05. It now lives in
`memecoin_manual_exit_intents.drain` as re-queues across runner passes, and is tested in
tests/test_memecoin_manual_exit_intents.py. This file pins that the route no longer sleeps or
retries in-process: one POST, one row, no executor call.
"""
import sqlite3
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_manual_exit_intents as xi
from _web_csrf import csrf_client


def test_the_route_neither_sleeps_nor_retries(monkeypatch):
    slept = []
    monkeypatch.setattr("sniper.routes.api_memecoin._time.sleep", slept.append)
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="MintB", ticker="BURQA",
                          entry_price=0.0001, entry_notional=40.0, entry_fill_source="chain", entry_lag_s=-1.0,
                          their_size_usd=0.0, opened_at="2026-08-14T18:30:00+00:00",
                          strategy="alpha_b", execution_mode="real", network_id="solana")
    executed = []
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({
        "conn": c, "real_exit_fn": lambda *a: executed.append(a) or {"status": "closed"},
        "real_exit_intent_fn": xi.make_enqueue_exit_fn(lambda: "2026-09-05T09:00:00+00:00")}))
    r = csrf_client(app).post(f"/api/memecoin/positions/{pid}/close")
    assert r.status_code == 202 and r.get_json()["status"] == "queued"
    assert slept == [] and executed == []
    assert xi.pending_count(c) == 1
