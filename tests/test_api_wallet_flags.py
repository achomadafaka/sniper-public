"""Favorite / delete wallet endpoints. Favorite pins + re-engages (restores) + boosted re-analyze
enqueue; delete is a reversible soft-delete. Invalid wallets rejected."""
from __future__ import annotations

import sqlite3

from flask import Flask

from sniper.persistence import db_wallet_flags as F
from sniper.persistence import db_wallet_analysis_queue as Q
from sniper.routes.api_wallet_flags import create_api_wallet_flags_blueprint

A = "0x" + "a" * 40


def _client(*, ensure=True):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    if ensure:
        F.ensure_wallet_flags_tables(c)
        Q.ensure_queue_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_wallet_flags_blueprint({"conn": c}))
    return app.test_client(), c


def test_favorite_sets_flag_and_enqueues():
    cl, c = _client()
    r = cl.post("/api/wallet-analyzer/favorite", json={"wallet": A})
    body = r.get_json()
    assert r.status_code == 200 and body["favorite"] is True
    assert "enrolled" not in body and "enroll_error" not in body
    assert F.is_favorite(c, A)
    row = c.execute("SELECT source, priority FROM analyzer_read_queue WHERE wallet=?", (A,)).fetchone()
    assert row and row["source"] == "favorite" and row["priority"] < 100  # boosted


def test_unfavorite_clears_flag():
    cl, c = _client()
    cl.post("/api/wallet-analyzer/favorite", json={"wallet": A})
    r = cl.delete(f"/api/wallet-analyzer/favorite/{A}")
    assert r.status_code == 200 and r.get_json()["favorite"] is False
    assert not F.is_favorite(c, A)


def test_delete_soft_deletes():
    cl, c = _client()
    r = cl.delete(f"/api/wallet-analyzer/wallet/{A}")
    assert r.status_code == 200 and r.get_json()["deleted"] is True
    assert F.is_deleted(c, A)


def test_favorite_restores_a_deleted_wallet():
    cl, c = _client()
    F.soft_delete(c, A, now=1)
    cl.post("/api/wallet-analyzer/favorite", json={"wallet": A})
    assert F.is_favorite(c, A) and not F.is_deleted(c, A)  # re-engaging un-deletes


def test_invalid_wallet_rejected():
    cl, _ = _client()
    assert cl.post("/api/wallet-analyzer/favorite", json={"wallet": "nope"}).status_code == 400
    assert cl.delete("/api/wallet-analyzer/wallet/nope").status_code == 400


def test_restore_endpoint_undeletes():
    cl, c = _client()
    F.soft_delete(c, A, now=1)
    r = cl.post(f"/api/wallet-analyzer/restore/{A}")
    assert r.status_code == 200 and r.get_json()["deleted"] is False
    assert not F.is_deleted(c, A)


def test_favorite_lazily_creates_flags_table():
    cl, c = _client(ensure=False)
    r = cl.post("/api/wallet-analyzer/favorite", json={"wallet": A})
    assert r.status_code == 200
    assert F.is_favorite(c, A)
