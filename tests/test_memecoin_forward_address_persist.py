"""Persisting chain-proven Solana owner addresses so the watcher can subscribe to them.

The regression this guards: the 2026-09-22 run proved 18 addresses and wrote them nowhere, so
`onchain_wallet_watch.py` covered 1 armed wallet of 37 and saw 2.7% of the roster's J7 Solana buys.
"""
import importlib.util
import os
import sqlite3

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "research", "memecoin_forward_address_verify.py")
_SPEC = importlib.util.spec_from_file_location("fwd_verify", _PATH)
fwd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fwd)

NOW = "2026-09-23T09:00:00+00:00"


def _ledger(tmp_path, handles):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute("CREATE TABLE memecoin_wallets (source_user_id TEXT PRIMARY KEY, handle TEXT, "
                 "real_approved INTEGER)")
    conn.execute("CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, "
                 "handle TEXT, network_id TEXT NOT NULL, address TEXT NOT NULL, "
                 "resolved_at TEXT NOT NULL, method TEXT, coins_held INTEGER, "
                 "coins_checked INTEGER, verify_status TEXT, verify_detail TEXT)")
    for i, h in enumerate(handles):
        conn.execute("INSERT INTO memecoin_wallets VALUES (?,?,1)", ("uuid-%d" % i, h))
    conn.commit()
    return conn


def _results(n_proven, n_unproven):
    out = [{"handle": "proven%d" % i, "address": "Addr%d" % i, "verdict": "VERIFIED",
            "hits": 10, "tested": 10, "cross_source": None} for i in range(n_proven)]
    # A cross-source promotion with ZERO chain hits: VERIFIED, but NOT chain-proven.
    out += [{"handle": "weak%d" % i, "address": "Weak%d" % i, "verdict": "VERIFIED",
             "hits": 0, "tested": 10, "cross_source": True} for i in range(n_unproven)]
    return out


def test_eighteen_proven_in_eighteen_rows_out(tmp_path):
    handles = ["proven%d" % i for i in range(18)] + ["weak%d" % i for i in range(5)]
    conn = _ledger(tmp_path, handles)
    cands = fwd.persist_candidates(conn, _results(18, 5))
    assert len(cands) == 18, "only hits >= 3 may be written"
    planned, written = fwd.persist_rows(conn, cands, now_iso=NOW, confirm=True)
    assert (len(planned), written) == (18, 18)
    rows = conn.execute("SELECT COUNT(*) FROM memecoin_wallet_addresses").fetchone()[0]
    assert rows == 18
    # every written row must be one the watcher actually subscribes to
    subscribed = conn.execute(
        "SELECT COUNT(*) FROM memecoin_wallet_addresses WHERE network_id='solana' "
        "AND verify_status IN ('fomoscan_confirmed','live_confirmed')").fetchone()[0]
    assert subscribed == 18
    conn.close()


def test_the_unproven_are_never_written(tmp_path):
    conn = _ledger(tmp_path, ["weak0", "weak1"])
    cands = fwd.persist_candidates(conn, _results(0, 2))
    assert cands == []
    planned, written = fwd.persist_rows(conn, cands, now_iso=NOW, confirm=True)
    assert (planned, written) == ([], 0)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallet_addresses").fetchone()[0] == 0
    conn.close()


def test_a_second_run_writes_nothing(tmp_path):
    conn = _ledger(tmp_path, ["proven%d" % i for i in range(18)])
    cands = fwd.persist_candidates(conn, _results(18, 0))
    fwd.persist_rows(conn, cands, now_iso=NOW, confirm=True)
    planned, written = fwd.persist_rows(conn, cands, now_iso=NOW, confirm=True)
    assert (len(planned), written) == (0, 0), "idempotent: the same proof must not rewrite"
    conn.close()


def test_a_dry_run_writes_nothing(tmp_path):
    conn = _ledger(tmp_path, ["proven0"])
    cands = fwd.persist_candidates(conn, _results(1, 0))
    planned, written = fwd.persist_rows(conn, cands, now_iso=NOW, confirm=False)
    assert len(planned) == 1 and written == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallet_addresses").fetchone()[0] == 0
    conn.close()


def test_a_new_address_supersedes_a_refuted_one_and_keeps_the_old_value(tmp_path):
    """A REFUTED row must not block better evidence, but it must not vanish silently either."""
    conn = _ledger(tmp_path, ["proven0"])
    conn.execute("INSERT INTO memecoin_wallet_addresses VALUES "
                 "('uuid-0','proven0','solana','OldAddr','2026-08-20','holder_intersection',"
                 "8,8,'REFUTED','older')")
    conn.commit()
    cands = fwd.persist_candidates(conn, _results(1, 0))
    planned, written = fwd.persist_rows(conn, cands, now_iso=NOW, confirm=True)
    assert written == 1 and "supersedes REFUTED" in planned[0]["note"]
    row = conn.execute("SELECT address, verify_status, verify_detail FROM "
                       "memecoin_wallet_addresses WHERE source_user_id='uuid-0'").fetchone()
    assert row[0] == "Addr0" and row[1] == fwd.WRITE_STATUS
    assert "OldAddr" in row[2], "the superseded row must survive in verify_detail"
    conn.close()


def test_a_handle_with_no_roster_row_is_skipped(tmp_path):
    conn = _ledger(tmp_path, [])
    assert fwd.persist_candidates(conn, _results(3, 0)) == []
    conn.close()
