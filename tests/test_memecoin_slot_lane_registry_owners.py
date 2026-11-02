"""The slot lane watches the registry's verified addresses of ARMED Solana profiles (registry step 2,
docs/superpowers/specs/2026-09-26-solana-address-registry-design.md).

Before: owners_from_roster joined data/solana_roster_owners.jsonl to wallets BY HANDLE. On the
2026-09-26 pre-cut roster (45 armed on solana) that gave 37 owners; the registry, joined through
each receipt's J7 event by source_user_id, verifies 39, gaining ser_albert and lingardsol, whose
JSONL handle did not match, and losing none. The confirmed watcher reads the same function, so the
two lanes cannot disagree about who is watched.
"""
import contextlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_slot_lane as lane

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-27T03:10:00+00:00"
A = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
B = "DNsh1UfJdxmze6T6GV9QK5SoFm7HsM5TRNxVuwVgo8Zj"
C = "Cqu5uKfKh6mSfqasYKGDYghGc7J54FAp9WCus9QnQSsR"
D = "B9FJomxFnA5WauXkJdh4GBke3NAaGnC8nwEEW6sEGQ7y"
P = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    for uid, handle, approved, chains in (("u-a", "Alice", 1, "solana"), ("u-b", "Bob", 1, "solana"),
                                          ("u-c", "carol", 0, None), ("u-d", "dave", 1, "4663"),
                                          ("pump:" + P, "PumpGuy", 1, "solana")):
        c.execute("INSERT INTO memecoin_wallets (source_user_id, handle, paused, excluded, "
                  "real_approved, real_chains) VALUES (?,?,0,0,?,?)", (uid, handle, approved, chains))
    for uid, addr, status in (("u-a", A, "verified"), ("u-b", B, "candidate"), ("u-b", C, "refuted"),
                              ("u-c", C, "verified"), ("u-d", D, "verified")):
        m.record_address_evidence(c, source_user_id=uid, network_id="solana", address=addr,
                                  status=status, evidence_kind="t", now_iso=NOW, actor="t")
    c.commit()
    with contextlib.closing(c):
        yield c


def _jsonl(tmp_path, text=""):
    p = tmp_path / "owners.jsonl"
    p.write_text(text)
    return str(p)


def test_the_lane_watches_verified_addresses_of_armed_profiles_and_pump_ids(conn, tmp_path):
    # The JSONL names Bob with an owner; the registry does not verify it, so it is not watched.
    owners, armed = lane.owners_from_roster(
        conn, _jsonl(tmp_path, '{"handle": "bob", "owner": "%s", "error": null}\n' % B))
    assert owners == {A: ("u-a", "Alice"), P: ("pump:" + P, "PumpGuy")}
    assert armed == frozenset({"u-a", "u-b", "pump:" + P})


def test_a_profile_is_joined_by_id_even_when_its_handle_changed(conn, tmp_path):
    """ser_albert / lingardsol: verified by their receipt's J7 event, invisible to a handle join."""
    conn.execute("UPDATE memecoin_wallets SET handle = 'Alice_Renamed' WHERE source_user_id = 'u-a'")
    owners, _ = lane.owners_from_roster(conn, _jsonl(tmp_path))
    assert owners[A] == ("u-a", "Alice_Renamed")


def test_without_the_registry_table_the_legacy_jsonl_path_is_unchanged(conn, tmp_path):
    conn.execute("DROP TABLE memecoin_address_registry_events")
    conn.execute("DROP TABLE memecoin_address_registry")
    owners, _ = lane.owners_from_roster(
        conn, _jsonl(tmp_path, '{"handle": "bob", "owner": "%s", "error": null}\n' % B))
    assert owners == {B: ("u-b", "bob"), P: ("pump:" + P, "pumpguy")}


def test_a_failed_refresh_keeps_the_last_good_owner_set(conn, tmp_path):
    roster = {"owners": {"Old": ("u-old", "old")}, "armed": frozenset({"u-old"})}
    status = {}

    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error")

    assert lane._refresh_roster(roster, status, Broken(), _jsonl(tmp_path)) is False
    assert roster["owners"] == {"Old": ("u-old", "old")} and status == {}
    assert lane._refresh_roster(roster, status, conn, _jsonl(tmp_path)) is True
    assert set(roster["owners"]) == {A, P} and status == {"owners": 2, "armed": 3}


def test_the_watcher_and_the_lane_watch_the_same_addresses(conn, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "oww_same_set", ROOT / "scripts/research/onchain_wallet_watch.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    w.KEY, w.HTTP_URL, w.WS_URL = "fake", "https://x.invalid/?api-key=fake", "wss://x.invalid/"
    owners, _ = lane.owners_from_roster(conn, _jsonl(tmp_path))
    assert set(w.registry_watchlist(conn)) == set(owners)


def test_a_refuted_pump_id_is_not_watched(conn, tmp_path):
    m.record_address_evidence(conn, source_user_id="pump:" + P, network_id="solana", address=P,
                              status="refuted", evidence_kind="t", now_iso=NOW, actor="t")
    owners, _ = lane.owners_from_roster(conn, _jsonl(tmp_path))
    assert P not in owners
