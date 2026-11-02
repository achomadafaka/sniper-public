"""Immutable analyzer reads. A re-analysis APPENDS a new row pointing at the prior latest
(supersedes); the same data snapshot is idempotent; a read is never mutated; the latest view
returns the most recent read per wallet."""
from __future__ import annotations

import json
import sqlite3

from sniper.persistence import db_wallet_reads as DB
from sniper.analysis.winning_wallet_thesis.synthesis import WalletRead


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    DB.ensure_wallet_reads_tables(c)
    return c


def _read(*, verdict="established_directional_edge", route="strategy_candidate", av=1, rv=1, da=100):
    return WalletRead(
        primary_verdict=verdict, operational_route=route, headline="h", reasons=["r"], warnings=["w"],
        edge_location={"domains": ["sports"], "loss_centers": [], "entry_shape": {}},
        evidence_profile={"history_depth": "deep",
                          "fresh": {"ex_ante_level": "none", "ex_post_level": "none",
                                    "coordination_level": "none"},
                          "recent_anomaly_level": "none", "archetype": "directional_discretionary",
                          "intent": "thesis_specialist"},
        provenance={"analyzer_version": av, "rule_version": rv, "data_as_of": da,
                    "source_snapshot_hash": "hash"})


def test_append_sets_supersedes_and_latest():
    c = _conn()
    id1 = DB.append_read(c, "0xW", _read(verdict="emerging_directional_evidence", da=100), now_ts=1000)
    id2 = DB.append_read(c, "0xW", _read(verdict="established_directional_edge", da=200), now_ts=2000)
    assert id2 != id1
    row2 = c.execute("SELECT supersedes_read_id FROM wallet_analysis_reads WHERE read_id=?", (id2,)).fetchone()
    assert row2["supersedes_read_id"] == id1
    latest = DB.latest_read(c, "0xW")
    assert latest["primary_verdict"] == "established_directional_edge"
    assert latest["read_id"] == id2


def test_idempotent_on_same_snapshot():
    c = _conn()
    id1 = DB.append_read(c, "0xW", _read(av=1, da=100), now_ts=1000)
    id2 = DB.append_read(c, "0xW", _read(av=1, da=100), now_ts=1001)  # same (wallet, av, data_as_of)
    assert id1 == id2
    n = c.execute("SELECT COUNT(*) FROM wallet_analysis_reads WHERE wallet='0xw'").fetchone()[0]
    assert n == 1


def test_reads_are_append_only():
    c = _conn()
    DB.append_read(c, "0xW", _read(av=1, da=100), now_ts=1000)
    DB.append_read(c, "0xW", _read(av=1, da=200), now_ts=2000)
    n = c.execute("SELECT COUNT(*) FROM wallet_analysis_reads WHERE wallet='0xw'").fetchone()[0]
    assert n == 2


def test_rule_version_bump_appends_new_read():
    # CR2: same data snapshot but a new rule_version must NOT be idempotently skipped.
    c = _conn()
    id1 = DB.append_read(c, "0xW", _read(av=1, rv=1, da=100), now_ts=1000)
    id2 = DB.append_read(c, "0xW", _read(av=1, rv=2, da=100), now_ts=2000)
    assert id2 != id1
    assert DB.latest_read(c, "0xW")["rule_version"] == 2


def test_latest_none_for_unknown_wallet():
    assert DB.latest_read(_conn(), "0xNOPE") is None


def test_evidence_roundtrips_json():
    c = _conn()
    DB.append_read(c, "0xW", _read(av=1, da=100), now_ts=1000)
    latest = DB.latest_read(c, "0xW")
    ev = json.loads(latest["evidence_json"])
    assert ev["history_depth"] == "deep"
    assert json.loads(latest["edge_location_json"])["domains"] == ["sports"]


def test_wallet_keys_normalized_for_reads_and_latest_map():
    c = _conn()
    DB.append_read(c, "0xAbC", _read(av=1, da=100), now_ts=1000)
    assert DB.latest_read(c, "0xabc") is not None
    assert DB.latest_read(c, "0xABC") is not None
    assert list(DB.all_latest(c).keys()) == ["0xabc"]


def test_legacy_mixed_case_read_rows_still_resolve():
    c = _conn()
    rid = DB.append_read(c, "0xabc", _read(av=1, da=100), now_ts=1000)
    c.execute("UPDATE wallet_analysis_reads SET wallet='0xABC' WHERE read_id=?", (rid,))
    c.commit()
    assert DB.latest_read(c, "0xabc")["read_id"] == rid
    assert list(DB.all_latest(c).keys()) == ["0xabc"]
