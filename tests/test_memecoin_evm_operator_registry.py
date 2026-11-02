"""Explicit operator receipt attestations are chain-scoped background roster inputs."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.persistence.db_axiom import ensure_axiom_tables
from sniper.services import axiom_address_verify as av
from sniper.settings import memecoin as cfg

UID = "2251aa65-06e0-5845-bacb-75bdbae5dd31"
ADDRESS = "0xa3ad0dc6b280ee4373306200444e2d5d1133b42c"
NOW = "2026-10-20T10:00:00+00:00"
KIND = "operator_receipt_identity"
EVIDENCE = {"audit": "docs/audits/2026-10-20-tontheneko-identity.md",
            "receipts": [{"tx": "0x" + "12" * 32}]}


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663", "56"})
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    m.ensure_address_registry_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_wallets (source_user_id TEXT PRIMARY KEY, handle TEXT,
            real_approved INTEGER, real_chains TEXT, paused INTEGER DEFAULT 0,
            excluded INTEGER DEFAULT 0);
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY,
            handle TEXT, network_id TEXT, address TEXT, verify_status TEXT);
    """)
    c.execute("INSERT INTO memecoin_wallets VALUES (?, 'tontheneko', 1, '4663,56', 0, 0)",
              (UID,))
    yield c
    c.close()


def attest(c, *, uid=UID, address=ADDRESS, chain="4663", actor="operator:copy-setup",
           kind=KIND, status="verified", evidence=None):
    m.record_address_evidence(c, source_user_id=uid, network_id=chain, address=address,
        status=status, evidence_kind=kind, now_iso=NOW, actor=actor,
        evidence_json=json.dumps(EVIDENCE if evidence is None else evidence), handle="old-handle")


def watched(c, chain="4663"):
    return av.evm_watched_owners(c, chain, include_operator=True)


def test_operator_attestation_is_opt_in_and_never_cross_chain(conn):
    attest(conn, address=ADDRESS.upper().replace("0X", "0x"))
    assert av.evm_watched_owners(conn, "4663") == {}
    assert watched(conn) == {ADDRESS: (UID, "tontheneko")}
    assert watched(conn, "56") == {}
    assert av.watcher_address_owners(conn) == {}
    assert list(av._current_proofs(conn)) == []


@pytest.mark.parametrize("actor", ["ui", "operator", "operator:", "operator:  ", "operator:\t\n",
                                  "Operator:setup", "background:operator:setup"])
def test_only_explicit_operator_actor_can_attest(conn, actor):
    attest(conn, actor=actor)
    assert watched(conn) == {}


@pytest.mark.parametrize("mutation", ["candidate", "refuted", "conflict", "wrong_kind",
    "missing_audit_event", "event_uid", "event_chain", "event_address", "event_time",
    "event_kind", "event_status", "event_evidence"])
def test_current_row_must_match_its_exact_operator_verification_event(conn, mutation):
    attest(conn)
    if mutation in {"candidate", "refuted"}:
        conn.execute("UPDATE memecoin_address_registry SET status=?", (mutation,))
    elif mutation == "conflict":
        conn.execute("UPDATE memecoin_address_registry SET conflict_at=?", (NOW,))
    elif mutation == "wrong_kind":
        conn.execute("UPDATE memecoin_address_registry SET evidence_kind='fomo_buy_receipt_match'")
        conn.execute("UPDATE memecoin_address_registry_events SET evidence_kind='fomo_buy_receipt_match'")
    elif mutation == "missing_audit_event":
        conn.execute("DELETE FROM memecoin_address_registry_events")
    else:
        field, value = {
            "event_uid": ("source_user_id", "someone-else"),
            "event_chain": ("network_id", "56"),
            "event_address": ("address", "0x" + "33" * 20),
            "event_time": ("at", "2026-10-19T10:00:00+00:00"),
            "event_kind": ("evidence_kind", "some_other_method"),
            "event_status": ("to_status", "candidate"),
            "event_evidence": ("evidence_json", "{}"),
        }[mutation]
        conn.execute("UPDATE memecoin_address_registry_events SET " + field + "=?", (value,))
    assert watched(conn) == {}


@pytest.mark.parametrize("evidence", [{}, {"audit": "", "receipts": EVIDENCE["receipts"]},
    {"audit": EVIDENCE["audit"], "receipts": []},
    {"audit": EVIDENCE["audit"], "receipts": ["not-a-receipt"]},
    {"audit": EVIDENCE["audit"], "receipts": [{"tx": "not-a-hash"}]},
    {"audit": EVIDENCE["audit"], "receipts": [{"tx": "0x+" + "1" * 63}]}, []])
def test_attestation_retains_audit_reference_and_real_receipt_identities(conn, evidence):
    attest(conn, evidence=evidence)
    assert watched(conn) == {}


@pytest.mark.parametrize("chain,address", [("robinhood", ADDRESS), ("04663", ADDRESS),
    ("8453", ADDRESS), ("solana", "7zyowp3jJHuVTm5VBkht21EgZHmbFTZR3edTAeqcy8Da"),
    ("4663", "0x" + "00" * 20), ("4663", "0x" + "gg" * 20),
    ("4663", " " + ADDRESS), ("4663", "0x1234")])
def test_requires_canonical_configured_evm_chain_and_valid_address(conn, chain, address):
    attest(conn, chain=chain, address=address)
    assert watched(conn, chain) == {}
    assert watched(conn, "4663") == {}


@pytest.mark.parametrize("other_status", ["candidate", "verified", "refuted"])
def test_registry_foreign_owner_claim_never_becomes_an_operator_mapping(conn, other_status):
    attest(conn)
    attest(conn, uid="other-owner", status=other_status)
    assert watched(conn) == {}


@pytest.mark.parametrize("source", ["legacy_confirmed", "legacy_refuted", "axiom_claim",
                                   "axiom_refuted", "evm_roster"])
def test_operator_cannot_replace_existing_owner_or_refuted_identity(conn, source):
    attest(conn)
    if source.startswith("legacy"):
        uid = UID if source.endswith("refuted") else "other-owner"
        conn.execute("INSERT INTO memecoin_wallet_addresses VALUES (?, 'other', '4663', ?, ?)",
                     (uid, ADDRESS, "REFUTED" if source.endswith("refuted") else "fomoscan_confirmed"))
    elif source.startswith("axiom"):
        conn.execute("INSERT INTO axiom_verified_addresses(source_user_id,network_id,address,"
                     "verify_status,verified_at) VALUES (?, '4663', ?, ?, ?)",
                     (UID if source.endswith("refuted") else "other-owner", ADDRESS,
                      "REFUTED" if source.endswith("refuted") else "unverified", NOW))
    else:
        conn.execute("INSERT INTO memecoin_wallets VALUES (?, 'direct', 1, '4663', 0, 0)",
                     ("evm:" + ADDRESS,))
    assert UID not in {uid for uid, _handle in watched(conn).values()}


def test_existing_proofs_survive_and_foreign_chain_does_not_claim_this_chain(conn):
    attest(conn)
    other = "0x" + "44" * 20
    conn.execute("INSERT INTO memecoin_wallet_addresses VALUES ('existing', 'old', '4663', ?,"
                 "'fomoscan_confirmed')", (other,))
    conn.execute("INSERT INTO memecoin_wallets VALUES ('existing', 'current', 1, '4663', 0, 0)")
    conn.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,network_id,address,"
                 "verify_status,verified_at,detail) VALUES ('foreign', 'foreign', '56', ?,"
                 "'axiom_confirmed', ?, ?)",
                 (ADDRESS, NOW, json.dumps({"proof_version": av.PROOF_VERSION})))
    assert watched(conn) == {ADDRESS: (UID, "tontheneko"), other: ("existing", "current")}
    assert av.evm_watched_owners(conn, "4663") == {other: ("existing", "current")}


@pytest.mark.parametrize("assignment", ["real_approved=0", "paused=1", "excluded=1",
                                      "real_chains='56'"])
def test_operator_identity_cannot_grant_or_preserve_funded_authority(conn, assignment):
    attest(conn)
    conn.execute("UPDATE memecoin_wallets SET " + assignment)
    assert watched(conn) == {}


def test_missing_registry_keeps_legacy_behavior_but_other_read_errors_propagate(conn):
    conn.execute("DROP TABLE memecoin_address_registry_events")
    assert watched(conn) == {}
    conn.execute("CREATE TABLE memecoin_address_registry_events (bad_schema TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        watched(conn)


def test_read_is_memory_and_database_only_without_any_write(conn, monkeypatch):
    attest(conn)
    conn.commit()
    before = conn.total_changes
    monkeypatch.setattr(av, "_http_call", lambda *_a, **_k: pytest.fail("unexpected network"))
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
    conn.set_authorizer(lambda action, *_args: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
    assert watched(conn) == {ADDRESS: (UID, "tontheneko")}
    assert conn.total_changes == before


def test_default_entry_coverage_does_not_read_operator_registry(conn):
    attest(conn)
    reads = []
    conn.set_authorizer(lambda action, name, *_args:
        reads.append(name) or sqlite3.SQLITE_OK if action == sqlite3.SQLITE_READ else sqlite3.SQLITE_OK)
    assert av.evm_watched_owners(conn, "4663") == {}
    assert not {"memecoin_address_registry", "memecoin_address_registry_events"}.intersection(reads)


def test_operator_identity_never_proves_a_new_trade_was_initiated_by_the_wallet(conn):
    from types import SimpleNamespace
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    attest(conn)
    assert watched(conn) == {ADDRESS: (UID, "tontheneko")}
    event = SimpleNamespace(signal_source="onchain_watch", fomo_event_id="onchain:4663:trade",
                            source_wallet=ADDRESS,
                            source_initiation={**EVIDENCE, "proof_version": KIND})
    assert source_initiation_refusal(event, "4663") == "wallet_initiation_unproved"


def test_background_evm_watcher_explicitly_requests_operator_identities(monkeypatch):
    import scripts.research.onchain_evm_wallet_watch as watcher
    calls = []

    def read(c, chain, **kwargs):
        calls.append((c, chain, kwargs))
        return {ADDRESS: (UID, "tontheneko")}

    monkeypatch.setattr(watcher, "evm_watched_owners", read)
    assert watcher._verified_addresses("readonly-handle") == {ADDRESS: (UID, "tontheneko")}
    assert calls == [("readonly-handle", watcher.CHAIN, {"include_operator": True})]


def test_background_feed_refresh_requests_operator_identities_without_starting_io(monkeypatch):
    import queue
    import threading
    from types import SimpleNamespace
    from sniper.persistence import db_core
    from sniper.services import memecoin_rh_feed_shadow as shadow
    from sniper.services import memecoin_rh_relay_prebuy as relay

    class StopWriter(BaseException):
        pass

    class EmptyWork:
        def get(self, **_kwargs):
            raise StopWriter

    threads, calls = [], []

    class Thread:
        def __init__(self, *, target, **_kwargs):
            self.target = target
            threads.append(self)

        def start(self):
            pass

    c = SimpleNamespace(execute=lambda *_args: None, close=lambda: None)
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda _path: c)
    monkeypatch.setattr(shadow, "threading", SimpleNamespace(Thread=Thread, Lock=threading.Lock))
    monkeypatch.setattr(shadow, "queue", SimpleNamespace(Queue=lambda **_kwargs: EmptyWork(), Empty=queue.Empty))
    monkeypatch.setattr(cfg, "memecoin_rh_fast_decide_shadow", lambda: False)
    monkeypatch.setattr(relay, "relay_rosters", lambda read: {chain: read(chain) for chain in ("4663", "56")})

    def read(_conn, chain, **kwargs):
        calls.append((chain, kwargs))
        return {ADDRESS: (UID, "tontheneko")}

    monkeypatch.setattr(av, "evm_watched_owners", read)
    shadow.start_shadow(ledger_path="unused", log=lambda *_args: None)
    with pytest.raises(StopWriter):
        threads[0].target()
    assert calls == [("4663", {"include_operator": True}), ("56", {"include_operator": True})]
