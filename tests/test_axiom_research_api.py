"""Axiom account actions remain separate from funded wallet authority."""
import sqlite3

import pytest

from test_memecoin_api import _client
from sniper.persistence.db_axiom import ensure_axiom_tables, upsert_directory

SOL = "5PTsE6UM92yZNRMPKfn7c6BSPYtZBeptop1vMkiwpump"
OTHER = "HpYdUftXwEaG7jekkjFvFLWeJ5dfqaFvtDHoRC6jd99s"
AT = "2026-09-16T12:00:00+00:00"


def client():
    cl, db = _client()
    ensure_axiom_tables(db)
    upsert_directory(db, [dict(x_handle="outside_fomo", address=SOL, chain="solana", pnl_24h=123),
                          dict(x_handle="other", address=OTHER, chain="solana", pnl_24h=9)], AT)
    db.commit()
    return cl, db


def test_research_list_does_not_create_fomo_identity_or_authority():
    cl, db = client()
    response = cl.post("/api/memecoin/axiom/accounts", json={"chain": "solana", "address": SOL})
    assert response.status_code == 200
    body = cl.get("/api/memecoin/axiom").get_json()
    assert body["accounts"][0]["address"] == SOL
    assert body["directory"][0]["x_handle"] == "outside_fomo"
    assert db.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM axiom_verified_addresses").fetchone()[0] == 0


def test_link_requires_existing_tracked_id_and_remains_a_request():
    cl, db = client()
    payload = {"chain": "solana", "address": SOL, "requested_source_user_id": "missing"}
    assert cl.post("/api/memecoin/axiom/accounts", json=payload).status_code == 400
    cl.post("/api/memecoin/wallets", json={"source_user_id": "known", "handle": "outside_fomo"})
    payload["requested_source_user_id"] = "known"
    assert cl.post("/api/memecoin/axiom/accounts", json=payload).status_code == 200
    assert cl.get("/api/memecoin/axiom").get_json()["accounts"][0]["requested_source_user_id"] == "known"
    assert db.execute("SELECT real_approved FROM memecoin_wallets").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM axiom_verified_addresses").fetchone()[0] == 0


def test_research_swap_is_atomic_and_remove_clears_link_request():
    cl, db = client()
    cl.post("/api/memecoin/axiom/accounts", json={"chain": "solana", "address": SOL})
    payload = {"old_chain": "solana", "old_address": SOL, "new_chain": "solana", "new_address": "invalid"}
    assert cl.post("/api/memecoin/axiom/accounts/swap", json=payload).status_code == 400
    assert cl.get("/api/memecoin/axiom").get_json()["accounts"][0]["active"] == 1
    payload["new_address"] = OTHER
    assert cl.post("/api/memecoin/axiom/accounts/swap", json=payload).status_code == 200
    rows = cl.get("/api/memecoin/axiom").get_json()["accounts"]
    assert {r["address"] for r in rows if r["active"]} == {OTHER}
    assert cl.post("/api/memecoin/axiom/accounts", json={"chain": "solana", "address": OTHER, "active": False}).status_code == 200
    assert not any(r["active"] for r in cl.get("/api/memecoin/axiom").get_json()["accounts"])


@pytest.mark.parametrize("body", [[], {"chain": "solana", "address": SOL, "active": "false"},
                                 {"chain": "unsupported", "address": SOL}])
def test_invalid_research_action_is_rejected(body):
    cl, _ = client()
    assert cl.post("/api/memecoin/axiom/accounts", json=body).status_code == 400


def test_manual_address_is_research_only_and_has_explicit_origin():
    cl, db = client()
    manual = "0x" + "a" * 40
    assert cl.post("/api/memecoin/axiom/accounts", json={"chain": "evm", "address": manual}).status_code == 200
    row = next(r for r in cl.get("/api/memecoin/axiom").get_json()["accounts"] if r["address"] == manual)
    assert row["origin"] == "manual"
    assert row["handle"] is None
    assert db.execute("SELECT COUNT(*) FROM axiom_verified_addresses").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM axiom_trader_directory WHERE address=?", (manual,)).fetchone()[0] == 0


def test_manual_research_account_does_not_need_capture_schema():
    cl, db = _client()
    assert cl.post("/api/memecoin/axiom/accounts", json={"chain": "solana", "address": SOL}).status_code == 200
    assert cl.get("/api/memecoin/axiom").get_json()["accounts"][0]["origin"] == "manual"
