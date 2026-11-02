"""Step 1 of the Solana address registry (docs/superpowers/specs/2026-09-26-solana-address-registry-
design.md): reconcile the three existing sources, keep history, report coverage. Read-only effect."""
import json
import sqlite3

import pytest
from solders.keypair import Keypair

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_address_registry as R

NOW = "2026-09-26T23:30:00+00:00"
OLD_TABLE = """CREATE TABLE memecoin_wallet_addresses (
  source_user_id TEXT PRIMARY KEY, handle TEXT, network_id TEXT NOT NULL, address TEXT NOT NULL,
  resolved_at TEXT NOT NULL, method TEXT, coins_held INTEGER, coins_checked INTEGER,
  verify_status TEXT, verify_detail TEXT)"""


def _addr():
    return str(Keypair().pubkey())


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute(OLD_TABLE)
    c.commit()
    return c


def _wallet(c, uid, handle, *, armed=True):
    c.execute("INSERT INTO memecoin_wallets (source_user_id, handle, real_approved, real_chains, "
              "paused, excluded) VALUES (?,?,?,?,0,0)",
              (uid, handle, 1 if armed else 0, "solana" if armed else None))


def _j7_event(c, sig, trader, uid, handle):
    c.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, source_handle, mint, "
              "side, created_at) VALUES (?,?,?,?,?,?)",
              ("swap:raybot:%s:%s:mintx" % (sig, trader.lower()), uid, handle, "MintX", "buy", NOW))


def _jsonl(tmp_path, rows):
    p = tmp_path / "owners.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(p)


def _run(c, path):
    c.commit()                               # the fixture inserts leave an implicit transaction
    c.execute("BEGIN IMMEDIATE")
    seen = R.reconcile(c, roster_jsonl_path=path, now_iso=NOW)
    c.commit()
    return seen


def _status(c, uid, address):
    r = c.execute("SELECT status, evidence_kind, conflict_at FROM memecoin_address_registry "
                  "WHERE source_user_id=? AND network_id='solana' AND address=?",
                  (uid, address)).fetchone()
    return tuple(r) if r else None


def test_each_source_earns_its_status_keyed_by_profile_not_handle(conn, tmp_path):
    pump_addr, alice, bob, carol = _addr(), _addr(), _addr(), _addr()
    _wallet(conn, "pump:" + pump_addr, "pumper")
    _wallet(conn, "u-alice", "Alice")
    _wallet(conn, "u-bob", "Bob")
    _wallet(conn, "u-carol", "Carol")
    _j7_event(conn, "SIG1", alice, "u-alice", "Alice")
    _j7_event(conn, "SIG2", bob, "u-bob", "Bobby")          # J7 now calls u-bob "Bobby"
    path = _jsonl(tmp_path, [
        {"trader_lower": alice.lower(), "owner": alice, "handle": "alice", "signature": "SIG1"},
        {"trader_lower": bob.lower(), "owner": bob, "handle": "Bob", "signature": "SIG2"},
        {"trader_lower": carol.lower(), "owner": carol, "handle": "carol", "signature": "NOEVENT"},
    ])
    _run(conn, path)
    assert _status(conn, "pump:" + pump_addr, pump_addr)[:2] == ("verified", "pump_profile_id")
    assert _status(conn, "u-alice", alice)[:2] == ("verified", "j7_trader_receipt")
    assert _status(conn, "u-bob", bob)[:2] == ("candidate", "j7_trader_receipt")   # handle mismatch
    assert _status(conn, "u-carol", carol)[:2] == ("candidate", "jsonl_handle_only")


def test_a_refutation_belongs_to_one_address_and_survives(conn, tmp_path):
    old, new = _addr(), _addr()
    _wallet(conn, "u-change", "change")
    conn.execute("INSERT INTO memecoin_wallet_addresses VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("u-change", "change", "solana", old, NOW, "holder_intersection", 0, 0,
                  "REFUTED", "shared router"))
    _j7_event(conn, "SIG9", new, "u-change", "change")
    _run(conn, _jsonl(tmp_path, [{"trader_lower": new.lower(), "owner": new,
                                  "handle": "change", "signature": "SIG9"}]))
    assert _status(conn, "u-change", old)[0] == "refuted"
    assert _status(conn, "u-change", new)[0] == "verified"        # refuting A never refutes B
    cov = {w["source_user_id"]: w for w in R.solana_coverage(conn)["wallets"]}
    assert cov["u-change"]["label"] == "on-chain only"


def test_verified_evidence_for_a_refuted_address_is_a_conflict_not_an_unrefute(conn, tmp_path):
    x = _addr()
    _wallet(conn, "u-c", "cee")
    conn.execute("INSERT INTO memecoin_wallet_addresses VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("u-c", "cee", "solana", x, NOW, "fomo_profile_solana", 0, 0, "REFUTED", ""))
    _j7_event(conn, "SIGX", x, "u-c", "cee")
    _run(conn, _jsonl(tmp_path, [{"trader_lower": x.lower(), "owner": x, "handle": "cee",
                                  "signature": "SIGX"}]))
    status, _kind, conflict_at = _status(conn, "u-c", x)
    assert status == "refuted" and conflict_at == NOW
    cov = {w["source_user_id"]: w for w in R.solana_coverage(conn)["wallets"]}
    assert cov["u-c"]["label"] == "conflict"


def test_a_second_run_writes_nothing(conn, tmp_path):
    a = _addr()
    _wallet(conn, "u-a", "aa")
    _j7_event(conn, "SIGA", a, "u-a", "aa")
    path = _jsonl(tmp_path, [{"trader_lower": a.lower(), "owner": a, "handle": "aa",
                              "signature": "SIGA"}])
    _run(conn, path)
    events = conn.execute("SELECT COUNT(*) FROM memecoin_address_registry_events").fetchone()[0]
    seen = _run(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_address_registry_events").fetchone()[0] \
        == events
    assert all(k.endswith(":unchanged") for k in seen if ":" in k), seen


def test_weaker_evidence_never_downgrades(conn):
    a = _addr()
    common = dict(source_user_id="u", network_id="solana", address=a, now_iso=NOW, actor="t")
    assert m.record_address_evidence(conn, status="verified", evidence_kind="k1", **common)
    assert m.record_address_evidence(conn, status="candidate", evidence_kind="k2", **common) is None
    assert _status(conn, "u", a)[0] == "verified"
    assert m.record_address_evidence(conn, status="refuted", evidence_kind="k3", **common) \
        == "refuted"
    assert [tuple(r) for r in conn.execute(
        "SELECT from_status, to_status FROM memecoin_address_registry_events ORDER BY id")] == [
        (None, "verified"), ("verified", "refuted")]


def test_invalid_addresses_are_counted_not_stored(conn, tmp_path):
    _wallet(conn, "u-bad", "bad")
    conn.execute("INSERT INTO memecoin_wallet_addresses VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("u-bad", "bad", "solana", "not-a-pubkey", NOW, "x", 0, 0, "live_confirmed", ""))
    seen = _run(conn, _jsonl(tmp_path, []))
    assert seen["invalid_address"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_address_registry").fetchone()[0] == 0


def test_coverage_names_j7_only_and_legacy_watcher(conn, tmp_path):
    w = _addr()
    _wallet(conn, "u-j7", "onlyj7")
    _wallet(conn, "u-w", "watched")
    _wallet(conn, "u-off", "notarmed", armed=False)
    conn.execute("INSERT INTO j7_tracked_wallets (account_index, kind, source_user_id, seen_at) "
                 "VALUES (0, 'fomo', 'u-j7', ?)", (NOW,))
    conn.execute("INSERT INTO memecoin_wallet_addresses VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("u-w", "watched", "solana", w, NOW, "forward_signal_verify", 3, 3,
                  "live_confirmed", ""))
    _run(conn, _jsonl(tmp_path, []))
    cov = R.solana_coverage(conn)
    by = {x["source_user_id"]: x for x in cov["wallets"]}
    assert set(by) == {"u-j7", "u-w"}                              # unarmed wallets excluded
    assert by["u-j7"]["label"] == "J7 only" and "onlyj7" in cov["j7_only"]
    assert by["u-w"]["label"] == "on-chain only" and by["u-w"]["watcher_legacy"] is True


def test_a_missing_old_table_is_no_evidence_not_a_failure(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t2.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    seen = _run(c, _jsonl(tmp_path, []))
    assert seen["wallet_addresses_missing"] == 1
    assert R.solana_coverage(c)["armed"] == 0


@pytest.mark.parametrize("first,second", [("verified", "refuted"), ("refuted", "verified")])
def test_a_conflict_is_flagged_in_either_order_and_refuted_stands(conn, first, second):
    a = _addr()
    common = dict(source_user_id="u", network_id="solana", address=a, now_iso=NOW, actor="t")
    m.record_address_evidence(conn, status=first, evidence_kind="k1", **common)
    m.record_address_evidence(conn, status=second, evidence_kind="k2", **common)
    status, _kind, conflict_at = _status(conn, "u", a)
    assert status == "refuted" and conflict_at == NOW


def test_heartbeat_whitelists_counts_and_the_j7_only_names(tmp_path):
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), address_coverage={
        "armed": 3, "labels": {"J7 only": 1}, "j7_only": ["x"], "wallets": [{"big": 1}]})
    got = json.loads(path.read_text())["address_coverage"]
    assert got == {"armed": 3, "labels": {"J7 only": 1}, "j7_only": ["x"]}


def test_heartbeat_shows_how_many_owners_the_slot_lane_watches(tmp_path):
    """2026-09-27: the watcher showed 39 owners and nothing showed the lane's count, so a newly
    armed owner the lane had not picked up (sunnydog06) was invisible."""
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), slot_lane_status={
        "mode": "live", "owners": 41, "armed": 44, "handled": 3, "roster": {"big": 1},
        "signal": {"subscriptions": 41, "connected": True, "events": 9, "sells": 2,
                   "decode_errors": 0, "reconnects": 1, "head_slot": 5}})
    got = json.loads(path.read_text())["slot_lane"]
    assert isinstance(got.pop("exotic_cu_blind"), dict)   # process-global counter, own test
    assert got == {"mode": "live", "owners": 41, "armed": 44, "handled": 3, "subscriptions": 41,
                   "connected": True, "events": 9, "sells": 2, "decode_errors": 0,
                   "reconnects": 1}
