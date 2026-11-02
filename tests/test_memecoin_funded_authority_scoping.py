"""Funded-authority defects found reviewing #287 (2026-08-03).

Arming became PER CHAIN on 2026-07-30, but the gate that grants it, the chain vocabulary it
accepts, and the board that renders it were all still speaking the older global language.
Each test here is one reproduced defect.
"""
import fcntl
import os
import sqlite3
import subprocess
import tempfile

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services.memecoin_chains import resolve_chain
from _web_csrf import csrf_client


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _client(conn, **ctx):
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn, **ctx}))
    return csrf_client(app)


def _alpha_b_close(conn, uid, network_id, pnl, i):
    cur = conn.execute(
        "INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,is_open,"
        "realized_pnl,opened_at,closed_at,strategy,origin_event_id) "
        "VALUES (?,?,?,'paper',0,?,?,?,'alpha_b',?)",
        (uid, f"mint-{network_id}-{i}", network_id, pnl,
         "2026-08-01T00:00:00+00:00", "2026-08-01T01:00:00+00:00", f"ev-{network_id}-{i}"))
    pid = cur.lastrowid
    m.insert_fill(conn, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="route_quote", fraction=1.0,
                  created_at="2026-08-01T00:00:00+00:00")
    m.insert_fill(conn, position_id=pid, kind="mirror_exit", price=1.0 + pnl / 10.0,
                  qty=10.0, notional=10.0 + pnl, fill_source="route_quote", fraction=1.0,
                  created_at="2026-08-01T01:00:00+00:00")
    conn.commit()


# --- 1. the arming gate must read the chain being armed, never a pooled total ---------------

def test_arming_a_chain_does_not_qualify_on_another_chains_evidence():
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    for i in range(5):                       # 5 closes / +$50 — but all on SOLANA
        _alpha_b_close(c, "u1", "solana", 10.0, i)

    refused = m.set_wallet_chain_armed(c, "u1", "4663", True)

    assert refused["ok"] is False and refused["action"] == "approve_refused"
    assert refused["stats"] == {"n_closes": 0, "pnl": 0.0}
    assert "on robin" in refused["reason"]
    assert m.wallet_armed_chains(c, "u1") == []
    # The chain it DOES have evidence on still arms.
    assert m.set_wallet_chain_armed(c, "u1", "solana", True)["ok"] is True
    assert m.wallet_armed_chains(c, "u1") == ["solana"]


def test_chain_scoped_gate_counts_every_spelling_of_the_chain():
    """`robin` positions are stored as both '4663' and 'robin'; a single-id match misses half."""
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    for i in range(3):
        _alpha_b_close(c, "u1", "4663", 10.0, i)
    for i in range(2):
        _alpha_b_close(c, "u1", "robin", 10.0, 100 + i)

    assert m.alpha_b_copy_stats(c, "u1", chain="robin") == {"n_closes": 5, "pnl": 50.0}
    assert m.set_wallet_chain_armed(c, "u1", "4663", True)["ok"] is True


def test_alpha_b_copy_stats_rejects_an_unknown_chain():
    c = _conn()
    with pytest.raises(ValueError):
        m.alpha_b_copy_stats(c, "u1", chain="nope")


# --- 2. only funded chains carry funded authority ------------------------------------------

@pytest.mark.parametrize("chain", ["56", "1", "evm-unknown"])
def test_research_and_unresolved_chains_cannot_be_armed(chain):
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    for i in range(5):                       # evidence on the research chain itself
        _alpha_b_close(c, "u1", chain, 10.0, i)

    r = m.set_wallet_chain_armed(c, "u1", chain, True)

    assert r["ok"] is False and r["action"] == "chain_not_funded"
    assert m.wallet_armed_chains(c, "u1") == []


def test_a_legacy_non_funded_chain_can_still_be_disarmed():
    """The guard blocks granting authority, never cleaning up authority already stored."""
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    c.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='56' "
              "WHERE source_user_id='u1'")
    c.commit()

    assert m.set_wallet_chain_armed(c, "u1", "56", False)["ok"] is True
    assert m.wallet_armed_chains(c, "u1") == []


@pytest.mark.parametrize("chain", ["bsc", "eth", "unresolved"])
def test_armed_board_refuses_non_funded_chains(chain):
    c = _conn()
    r = _client(c).get("/api/memecoin/armed?chain=" + chain)
    assert r.status_code == 400 and r.get_json()["error"] == "chain_not_funded"


@pytest.mark.parametrize("chain", ["solana", "base", "4663"])
def test_armed_board_serves_every_funded_chain(chain):
    c = _conn()
    assert _client(c).get("/api/memecoin/armed?chain=" + chain).status_code == 200


# --- 4. disarming the last chain must not remove the wallet from the board ------------------

def test_wallet_stays_on_the_board_after_its_last_chain_is_disarmed():
    c = _conn()
    cl = _client(c)
    m.add_wallet(c, "u1", "alice")
    for i in range(5):
        _alpha_b_close(c, "u1", "solana", 10.0, i)
    m.set_wallet_chain_armed(c, "u1", "solana", True)
    m.set_wallet_chain_armed(c, "u1", "solana", False)   # clears real_approved

    body = cl.get("/api/memecoin/armed?chain=solana").get_json()

    assert c.execute("SELECT real_approved FROM memecoin_wallets "
                     "WHERE source_user_id='u1'").fetchone()[0] == 0
    row = next(r for r in body["rows"] if r["source_user_id"] == "u1")
    assert row["real_approved"] is False and row["armed_here"] is False
    # It must remain re-armable from this board: still listed, still shown as relevant
    # (its chain-scoped evidence passes), and the gate number is visible.
    assert row["relevant"] is True and row["gate"] == {"n_closes": 5, "pnl": 50.0}


def test_excluded_wallets_never_appear_on_the_funded_board():
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    c.execute("UPDATE memecoin_wallets SET excluded=1 WHERE source_user_id='u1'")
    c.commit()
    body = _client(c).get("/api/memecoin/armed?chain=solana").get_json()
    assert body["rows"] == [] and body["roster_count"] == 0


# --- 5. pause removes configured and effective authority -----------------------------------

def test_paused_wallet_reports_no_configured_or_effective_authority():
    c = _conn()
    cl = _client(c)
    m.add_wallet(c, "u1", "alice")
    for i in range(5):
        _alpha_b_close(c, "u1", "solana", 10.0, i)
    m.set_wallet_chain_armed(c, "u1", "solana", True)
    m.set_wallet_paused(c, "u1", True)

    body = cl.get("/api/memecoin/armed?chain=solana").get_json()
    row = body["rows"][0]

    assert row["armed_here"] is False and row["armed_chains"] == []
    assert row["effective"] is False and row["paused"] is True
    assert body["armed_count"] == 0 and body["effective_count"] == 0


# --- 6. the board's per-chain columns must not read another chain ---------------------------

def test_last_signal_is_scoped_to_the_requested_chain():
    c = _conn()
    cl = _client(c)
    m.add_wallet(c, "u1", "alice")
    for network_id, ts in (("solana", "2026-08-01T00:00:00Z"), ("4663", "2026-08-02T00:00:00Z")):
        c.execute("INSERT INTO memecoin_events(source_user_id,mint,network_id,action,side,"
                  "created_at) VALUES ('u1','mx',?,'candidate','buy',?)", (network_id, ts))
    c.commit()

    sol = cl.get("/api/memecoin/armed?chain=solana").get_json()["rows"][0]
    rob = cl.get("/api/memecoin/armed?chain=4663").get_json()["rows"][0]

    assert sol["last_signal"] == "2026-08-01T00:00:00Z"
    assert rob["last_signal"] == "2026-08-02T00:00:00Z"


@pytest.mark.parametrize("chain,stored", [("4663", "4663"), ("base", "8453"), ("solana", "solana")])
def test_armed_here_compares_stored_network_ids_not_canonical_keys(chain, stored):
    """`robin` is stored as '4663'. Comparing the canonical KEY against the stored ID matches
    only Solana, so every Robin/Base arm rendered as unarmed and the board offered to arm a
    wallet that already was."""
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    m.set_wallet_chain_armed(c, "u1", chain, True, force=True)
    assert m.wallet_armed_chains(c, "u1") == [stored]

    body = _client(c).get("/api/memecoin/armed?chain=" + chain).get_json()
    row = body["rows"][0]

    assert row["armed_here"] is True and body["armed_count"] == 1
    # Keys are exposed alongside the stored ids so the UI can label them.
    assert row["armed_chain_keys"] == [resolve_chain(stored)]


def test_board_reports_the_gate_thresholds_it_is_measured_against():
    c = _conn()
    gate = _client(c).get("/api/memecoin/armed?chain=solana").get_json()["gate"]
    assert gate["min_closes"] >= 1 and gate["min_pnl"] >= 0


# --- 7 + 8. screen status must not claim success, or report a stranger's run as ours --------

class _Exited:
    def __init__(self, code=0):
        self._code = code

    def poll(self):
        return self._code


def _screen_client(tmp_path, conn):
    return _client(conn, screen_lock_path=str(tmp_path / "screen.lock"),
                   screen_log_path=str(tmp_path / "screen.log"))


def _seed_candidate(conn, uid="u1"):
    conn.execute(
        "INSERT INTO memecoin_candidates(source_user_id,handle,sighting_count,total_buy_usd,"
        "max_buy_usd,first_seen,last_seen,status) VALUES (?,?,1,5000,5000,?,?,'new')",
        (uid, "alice", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    conn.commit()


def test_finished_screen_that_wrote_nothing_is_not_reported_as_success(monkeypatch, tmp_path):
    c = _conn()
    _seed_candidate(c)
    cl = _screen_client(tmp_path, c)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Exited(0))
    cl.post("/api/memecoin/screen", json={"mode": "candidates", "n": 20, "min_usd": 1000})

    body = cl.get("/api/memecoin/screen").get_json()

    assert body["running"] is False and body["targets"] == 1
    assert body["updated"] == 0            # nothing advanced -> the UI must not claim it did
    assert body["exit_code"] == 0


def test_screen_counts_only_its_own_targets_advancing_after_it_started(monkeypatch, tmp_path):
    c = _conn()
    _seed_candidate(c, "u1")
    cl = _screen_client(tmp_path, c)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Exited(0))
    cl.post("/api/memecoin/screen", json={"mode": "candidates", "n": 20, "min_usd": 1000})

    # An UNRELATED trader advancing, and one of OUR targets stamped BEFORE the run began.
    # Neither is evidence that this run did anything.
    m.upsert_trader_analytics(c, {"source_user_id": "other",
                                  "computed_at": "2099-01-01T00:00:00+00:00"})
    m.upsert_trader_analytics(c, {"source_user_id": "u1",
                                  "computed_at": "2000-01-01T00:00:00+00:00"})
    assert cl.get("/api/memecoin/screen").get_json()["updated"] == 0

    m.upsert_trader_analytics(c, {"source_user_id": "u1",
                                  "computed_at": "2099-01-01T00:00:00+00:00"})
    assert cl.get("/api/memecoin/screen").get_json()["updated"] == 1


def test_nonzero_exit_is_reported(monkeypatch, tmp_path):
    c = _conn()
    _seed_candidate(c)
    cl = _screen_client(tmp_path, c)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Exited(2))
    cl.post("/api/memecoin/screen", json={"mode": "candidates", "n": 20, "min_usd": 1000})
    assert cl.get("/api/memecoin/screen").get_json()["exit_code"] == 2


def test_external_run_does_not_inherit_our_finished_runs_metadata(monkeypatch, tmp_path):
    c = _conn()
    _seed_candidate(c)
    cl = _screen_client(tmp_path, c)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Exited(0))
    cl.post("/api/memecoin/screen", json={"mode": "candidates", "n": 20, "min_usd": 1000})

    held = open(tmp_path / "screen.lock", "a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        body = cl.get("/api/memecoin/screen").get_json()
    finally:
        held.close()

    assert body["running"] is True and body["owner"] == "external"
    # Nothing is known about someone else's run — report that, not our last run's numbers.
    assert body["mode"] is None and body["targets"] is None and body["started_at"] is None
