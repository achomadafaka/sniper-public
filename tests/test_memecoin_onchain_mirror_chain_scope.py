"""MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS accepts "<chain>:*" (Alpha GO 2026-10-17).

J7 went silent on every chain at 2026-10-16T17:52Z, and the on-chain sell mirror acted for one listed
56 wallet only: every other funded EVM position had no source-sell exit. "<chain>:*" adds every wallet
armed on that chain (real_approved_user_ids: approved, real_chains lists it, not paused or excluded)
when the mirror reads it, beside the listed uids, with the same mirror rules per sale. Pins: "1:*"
matches an armed chain-1 wallet and not an unarmed one; a uid entry still works; a malformed entry is
ignored and counted; the lane's sweep and the runner's gate read the same scope.
"""
import logging
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_lane as lane
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

ARMED_1 = "evm:0x" + "a1" * 20
ARMED_56 = "evm:0x" + "b5" * 20
UNARMED = "evm:0x" + "c0" * 20
PAUSED_1 = "evm:0x" + "d1" * 20
LISTED = "evm:0x" + "e2" * 20
COIN = "0x" + "4b" * 20
AT = "2026-10-17T05:00:00+00:00"
KNOB = "MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS"


@pytest.fixture
def db(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    for uid, approved, chains, paused in ((ARMED_1, 1, "1", 0), (ARMED_56, 1, "56", 0),
                                          (UNARMED, 0, "1", 0), (PAUSED_1, 1, "1", 1)):
        c.execute("INSERT INTO memecoin_wallets (source_user_id, handle, real_approved, real_chains, paused) "
                  "VALUES (?,?,?,?,?)", (uid, uid[-4:], approved, chains, paused))
    c.commit()
    monkeypatch.delenv(KNOB, raising=False)
    getattr(lane, "_MIRROR_MALFORMED_LOGGED", set()).clear()
    return c


def _sale(c, uid, *, network="1", tx="0x" + "aa" * 32):
    m.record_onchain_evm_sell_shadow(
        c, network_id=network, tx_hash=tx, address=uid[4:], source_user_id=uid, handle=uid[-4:],
        mint=COIN, sold_atomic=40, pre_atomic=100, post_atomic=60, proof="b_relay",
        proceeds_token=None, proceeds_atomic=None, block_number=100, detected_at=AT, decoded_at=AT,
        created_at=AT)
    return dict(c.execute("SELECT * FROM onchain_evm_sell_shadow ORDER BY id DESC LIMIT 1").fetchone())


def test_a_chain_wildcard_matches_an_armed_wallet_and_not_an_unarmed_one(db, monkeypatch):
    monkeypatch.setenv(KNOB, "1:*")
    assert lane.mirror_wallets(db, "1") == {ARMED_1}          # not UNARMED, PAUSED_1 or ARMED_56
    assert lane.mirror_wallets(db, "56") == frozenset()
    db.execute("UPDATE memecoin_wallets SET real_approved=1 WHERE source_user_id=?", (UNARMED,))
    assert lane.mirror_wallets(db, "1") == {ARMED_1, UNARMED}  # read per pass: arming applies at once


def test_uid_entries_still_work_and_add_to_the_wildcard(db, monkeypatch):
    monkeypatch.setenv(KNOB, "56:%s,1:%s" % (ARMED_56, LISTED.upper().replace("EVM:", "evm:")))
    assert lane.mirror_wallets(db, "56") == {ARMED_56}
    assert lane.mirror_wallets(db, "1") == {LISTED}
    monkeypatch.setenv(KNOB, "1:*,1:%s" % LISTED)
    assert lane.mirror_wallets(db, "1") == {ARMED_1, LISTED}
    assert cfg.onchain_sell_mirror_wallets("1") == {LISTED}    # the listed uids alone, as before


def test_a_malformed_entry_is_ignored_and_counted(db, monkeypatch, caplog):
    monkeypatch.setenv(KNOB, "1:*,bogus,:%s,56:, ,56:%s" % (ARMED_56, ARMED_56))
    scope = cfg.onchain_sell_mirror_scope("1")
    assert scope["malformed"] == 3 and scope["all_armed"] and scope["uids"] == frozenset()
    with caplog.at_level(logging.WARNING):
        assert lane.mirror_wallets(db, "1") == {ARMED_1}
        assert lane.mirror_wallets(db, "56") == {ARMED_56}
    assert [r.getMessage() for r in caplog.records].count(
        "onchain sell mirror: 3 malformed MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS entries ignored "
        "(want chain:uid or chain:*)") == 1                    # once per value, not per sweep


def test_the_sweep_and_the_runner_gate_read_the_same_scope(db, monkeypatch):
    monkeypatch.setenv(KNOB, "1:*")
    armed, unarmed = _sale(db, ARMED_1), _sale(db, UNARMED, tx="0x" + "bb" * 32)
    asked = []
    lane.sweep_onchain_sells(db, lambda c, row: asked.append(row["source_user_id"]) or {"status": "ok"},
                             cursor=0, network_id="1", wallets=lane.mirror_wallets(db, "1"))
    assert asked == [ARMED_1]
    looked = []
    monkeypatch.setattr(m, "get_open_position", lambda conn, uid, *a, **k: looked.append(uid))
    monkeypatch.setattr(R, "_entry_pending", lambda *a, **k: False)
    assert R.real_onchain_sell_mirror(db, unarmed, {}, now_fn=lambda: AT) is None and looked == []
    assert R.real_onchain_sell_mirror(db, armed, {}, now_fn=lambda: AT) is None and looked == [ARMED_1]


def test_the_sell_worker_resolves_the_wildcard_on_its_own_connection(db, monkeypatch, tmp_path):
    import threading
    path = tmp_path / "ledger.db"
    disk = sqlite3.connect(path)
    disk.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(disk)
    disk.execute("INSERT INTO memecoin_wallets (source_user_id, handle, real_approved, real_chains, paused) "
                 "VALUES (?,?,1,'1',0)", (ARMED_1, "a1"))
    disk.commit()
    _sale(disk, ARMED_1)
    disk.close()
    monkeypatch.setenv(KNOB, "1:*")
    monkeypatch.setattr(lane, "sell_startup_cursor", lambda conn, network, wallets: 0)
    stop, seen = threading.Event(), []

    def connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    def hook(conn, row):
        seen.append(row["source_user_id"])
        stop.set()
        return {"status": "ok"}
    _wakes, _status, threads = lane.start_network_workers(
        ["1"], conn_factory=connect, real_hook=lambda *a, **k: None, now_fn=lambda: AT, max_age_s=60,
        poll_s=0.05, stop=stop, sell_hook=hook, sell_wallets_fn=lambda network: frozenset())
    assert stop.wait(5.0) and seen == [ARMED_1]
    for thread in threads:
        thread.join(2.0)


def test_a_wallet_disarmed_while_we_hold_its_coin_stays_mirrored_until_the_position_closes(db, monkeypatch):
    """Gamma LOW-1 (Alpha 2026-10-17): the mirror follows the source of a position we hold."""
    monkeypatch.setenv(KNOB, "1:*")
    pid = m.open_position(db, source_user_id=UNARMED, source_handle="c0c0", mint=COIN, ticker="X",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=1.0,
                          their_size_usd=300.0, opened_at=AT, strategy=cfg.MEMECOIN_REAL_POLICY,
                          execution_mode="real", network_id="1", signal_source="onchain_watch",
                          opened_recorded_at=AT)
    assert lane.mirror_wallets(db, "1") == {ARMED_1, UNARMED}
    assert lane.mirror_wallets(db, "56") == frozenset()           # the chain's own positions only
    db.execute("UPDATE memecoin_positions SET is_open=0, closed_at=? WHERE id=?", (AT, pid))
    assert lane.mirror_wallets(db, "1") == {ARMED_1}
    monkeypatch.setenv(KNOB, "1:%s" % LISTED)
    db.execute("UPDATE memecoin_positions SET is_open=1, closed_at=NULL WHERE id=?", (pid,))
    assert lane.mirror_wallets(db, "1") == {LISTED}                 # no "*": listed uids only, as before
