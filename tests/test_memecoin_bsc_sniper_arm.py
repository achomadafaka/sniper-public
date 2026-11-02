"""china_sniper_1 (operator 2026-10-07): a self-signing BSC launch sniper, 0xd1bee21a..., to be armed
on 56 at a $35 trial clip in the nano band, with his sells mirrored.

B1  an `evm:<0x lowercase address>` roster id: its own address is its watched address on every
    allowed EVM chain (as pump: names a Solana address); proof is our own detection.
X1  MEMECOIN_REAL_MIN_COIN_AGE_S_BY_WALLET: a per-wallet coin-age floor over the chain's 120 s.
X2  trial start --evidence-ack: a wallet we could not watch before arming has no detection to show.
B4  MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS: the watcher's decoded sale of a listed wallet exits
    our position when it sold >= MIRROR_SIZE_MIN_FRACTION of its holding; one decision per
    (position, sell tx)."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import axiom_address_verify as A
from sniper.services import memecoin_onchain_lane as lane
from sniper.services import memecoin_real_runner as R
from sniper.services import memecoin_trial_ops as ops
from sniper.services.memecoin_risk import coin_age_refusal
from sniper.settings import memecoin as cfg

ADDR = "0xd1bee21a7898232c337e46bc399488be065bae4f"
UID = "evm:" + ADDR
COIN = "0x" + "4b" * 20
NOW = "2026-10-07T16:00:00+00:00"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56", "8453"}, raising=False)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    from sniper.persistence.db_axiom import ensure_axiom_tables
    ensure_axiom_tables(c)                      # axiom_verified_addresses (the proof rows)
    c.execute("CREATE TABLE IF NOT EXISTS memecoin_wallet_addresses (source_user_id TEXT, handle TEXT, "
              "network_id TEXT, address TEXT, verify_status TEXT)")   # the live table's read columns
    return c


# --- B1 ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("uid, ok", [
    (UID, True), ("evm:" + ADDR.upper().replace("0X", "0x"), False), ("evm:0x1234", False),
    ("evm:" + ADDR + "00", False), ("0123abcd-0000-4000-8000-0123456789ab", True),
    ("pump:" + "A" * 43, True), ("china_sniper_1", False)])
def test_the_roster_takes_an_evm_address_id_and_refuses_typos(uid, ok):
    assert ops.valid_roster_uid(uid) is ok


def test_an_evm_id_names_its_own_address_on_every_allowed_evm_chain(db):
    m.add_wallet(db, UID, "china_sniper_1")
    owners = A.watcher_address_owners(db)
    assert owners[("56", ADDR)] == (UID, "china_sniper_1")
    assert owners[("8453", ADDR)] == (UID, "china_sniper_1")
    assert ("1", ADDR) not in owners, "only chains in MEMECOIN_EVM_ALLOWLIST"
    assert ops.evm_addresses(db, UID, "56") == {ADDR}


def test_the_watcher_subscribes_it_only_where_it_is_armed(db):
    m.add_wallet(db, UID, "china_sniper_1")
    assert ADDR not in A.evm_watched_owners(db, "56")
    assert m.set_wallet_chain_armed(db, UID, "56", True, force=True)["ok"]
    assert A.evm_watched_owners(db, "56")[ADDR] == (UID, "china_sniper_1")
    assert ADDR not in A.evm_watched_owners(db, "8453")


def test_a_proven_owner_of_the_same_address_keeps_it(db, monkeypatch):
    m.add_wallet(db, UID, "china_sniper_1")
    monkeypatch.setattr(A, "_current_proofs", lambda conn: iter([("56", ADDR, "fomo-uid", "fomo", None)]))
    assert A.watcher_address_owners(db)[("56", ADDR)] == ("fomo-uid", "fomo")


# --- X1 ---------------------------------------------------------------------------------------------

def test_a_per_wallet_coin_age_floor_overrides_the_chain_floor_for_that_wallet_only(monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_MIN_COIN_AGE_S", "120")
    monkeypatch.setenv("MEMECOIN_REAL_MIN_COIN_AGE_S_BY_WALLET", "56:%s=0,56:bad=x,=5" % UID)
    assert cfg.min_coin_age_s_for("56", UID) == 0.0
    assert cfg.min_coin_age_s_for("56", "someone-else") == 120.0
    assert cfg.min_coin_age_s_for("8453", UID) == 120.0, "per chain"
    young = SimpleNamespace(facts={"pair_age_s": 5.0, "mint": COIN})
    assert coin_age_refusal(young, "56", UID) is None
    assert coin_age_refusal(young, "56", "someone-else").startswith("coin_too_young")
    assert coin_age_refusal(young, "56").startswith("coin_too_young"), "no wallet, chain floor"


# --- X2 ---------------------------------------------------------------------------------------------

def test_a_trial_without_detections_needs_the_operators_evidence_ack(db, monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_CLIP_USD_56", "35")
    m.add_wallet(db, UID, "china_sniper_1")
    refused = ops.start(db, "56", uid=UID, confirm=False, now_iso=NOW)
    assert not refused["ok"] and "no on-chain buy" in refused["reason"]
    dry = ops.start(db, "56", uid=UID, confirm=False, now_iso=NOW, evidence_ack="operator 10-07")
    assert dry["ok"] and not dry["written"] and dry["clip_usd"] == 35.0
    done = ops.start(db, "56", uid=UID, confirm=True, now_iso=NOW, evidence_ack="operator 10-07")
    assert done["written"] and "56" in m.wallet_armed_chains(db, UID)
    assert m.open_trial(db, UID, "56")["clip_usd"] == 35.0


def test_the_ack_never_replaces_a_proven_address(db, monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_CLIP_USD_56", "35")
    m.add_wallet(db, "0123abcd-0000-4000-8000-0123456789ab", "fomo")
    out = ops.start(db, "56", uid="0123abcd-0000-4000-8000-0123456789ab", confirm=False,
                    now_iso=NOW, evidence_ack="operator")
    assert not out["ok"] and "no proven 56 address" in out["reason"]


# --- B4 ---------------------------------------------------------------------------------------------

SOURCE_TX = "0x" + "5b" * 32


def _copied_buy(c, block):
    """The attempt our position copied: its origin names his buy, our detection holds its block."""
    c.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, network_id, mint, "
              "decision_started_at) VALUES ('att1', ?, '56', ?, ?)",
              ("onchain:56:%s:%s" % (SOURCE_TX, COIN), COIN, "2026-10-07T14:59:58+00:00"))
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, "
              "mint, detected_at, created_at, source_slot) VALUES (?,?,?,?,?,?,?,?)",
              ("56:%s:%s" % (SOURCE_TX, COIN), UID, ADDR, "56", COIN, "2026-10-07T14:59:57+00:00",
               "2026-10-07T14:59:57+00:00", block))
    c.commit()


def _position(c, *, opened="2026-10-07T15:00:00+00:00", mirror_disabled=False, attempt_id=None):
    pid = m.open_position(c, source_user_id=UID, source_handle="china_sniper_1", mint=COIN, attempt_id=attempt_id,
                          ticker="LAUNCH", entry_price=1.0, entry_notional=35.0,
                          entry_fill_source="chain", entry_lag_s=2.0, their_size_usd=300.0,
                          opened_at=opened, strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                          network_id="56", signal_source="onchain_watch", opened_recorded_at=opened)
    if mirror_disabled:
        c.execute("UPDATE memecoin_positions SET mirror_disabled=1 WHERE id=?", (pid,))
        c.commit()
    return pid


def _sale(c, *, tx="0x" + "aa" * 32, sold=40, pre=100, at="2026-10-07T15:10:00+00:00", uid=UID, block=100):
    m.record_onchain_evm_sell_shadow(
        c, network_id="56", tx_hash=tx, address=ADDR, source_user_id=uid, handle="china_sniper_1",
        mint=COIN, sold_atomic=sold, pre_atomic=pre, post_atomic=None if pre is None else pre - sold,
        proof="a_native", proceeds_token="native", proceeds_atomic=1, block_number=block,
        detected_at=at, decoded_at=at, created_at=at)
    return dict(c.execute("SELECT * FROM onchain_evm_sell_shadow ORDER BY id DESC LIMIT 1").fetchone())


@pytest.fixture
def mirror(db, monkeypatch):
    monkeypatch.setenv("MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS", "56:%s" % UID)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30, raising=False)
    monkeypatch.setattr(R, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(R, "check_wallet_demotion", lambda *a, **k: None)
    exits = []

    def exit_fn(conn, pos, reason, at):
        exits.append((pos["id"], reason))
        return {"status": "closed"}
    contexts = {"56": {"exit_fn": exit_fn}}

    def run(row):
        return R.real_onchain_sell_mirror(db, row, contexts, now_fn=lambda: NOW)
    return SimpleNamespace(db=db, exits=exits, run=run, mp=monkeypatch, exit_fn=exit_fn)


def test_a_sale_of_30_percent_or_more_after_our_fill_exits_once(mirror):
    pid = _position(mirror.db)
    row = _sale(mirror.db, sold=40, pre=100)
    assert mirror.run(row)["status"] == "closed"
    assert mirror.exits == [(pid, "mirror")]
    assert mirror.run(row)["status"] == "already_decided", "one decision per (position, sell tx)"
    assert mirror.exits == [(pid, "mirror")]
    audit = mirror.db.execute("SELECT lane, allowed, detail FROM memecoin_exit_trigger_audit").fetchone()
    assert audit["lane"] == "onchain_sell_mirror" and audit["allowed"] == 1
    assert json.loads(audit["detail"])["sold_fraction"] == 0.4


@pytest.mark.parametrize("kwargs, code", [
    ({"sold": 20, "pre": 100}, "size_hold"),
    ({"sold": 20, "pre": None}, "size_unknown"),
    ({"sold": 90, "pre": 100, "at": "2026-10-07T14:59:00+00:00"}, "sold_before_fill")])
def test_a_small_unknown_or_earlier_sale_holds_on_the_rails(mirror, kwargs, code):
    _position(mirror.db)
    out = mirror.run(_sale(mirror.db, **kwargs))
    assert out["status"] == "mirror_held" and out["reason"] == code and mirror.exits == []


def test_the_operators_per_position_hold_wins(mirror):
    _position(mirror.db, mirror_disabled=True)
    assert mirror.run(_sale(mirror.db, sold=100, pre=100))["reason"] == "mirror_disabled"
    assert mirror.exits == []


def test_an_unlisted_wallet_or_an_empty_list_does_nothing(mirror):
    _position(mirror.db)
    mirror.mp.setenv("MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS", "")
    assert mirror.run(_sale(mirror.db, sold=100, pre=100)) is None
    assert mirror.exits == []


def test_the_lane_sweeps_only_listed_wallets_and_starts_before_the_first_sale_after_the_open(db):
    _sale(db, tx="0x" + "01" * 32, at="2026-10-07T14:00:00+00:00")          # before any open position
    _position(db, opened="2026-10-07T15:00:00+00:00")
    first = _sale(db, tx="0x" + "02" * 32, at="2026-10-07T15:05:00+00:00")
    _sale(db, tx="0x" + "03" * 32, at="2026-10-07T15:06:00+00:00", uid="someone-else")
    cursor = lane.sell_startup_cursor(db, "56", {UID})
    assert cursor == first["id"] - 1
    seen = []
    new_cursor, hits = lane.sweep_onchain_sells(
        db, lambda c, row: seen.append(row["tx_hash"]) or {"status": "x"},
        cursor=cursor, network_id="56", wallets={UID})
    assert seen == ["0x" + "02" * 32] and new_cursor == first["id"] + 1


def test_a_failing_hook_still_advances_the_cursor(db):
    _sale(db)

    def boom(c, row):
        raise RuntimeError("exit failed")
    new_cursor, hits = lane.sweep_onchain_sells(db, boom, cursor=0, network_id="56", wallets={UID})
    assert new_cursor == 1 and hits[0]["result"]["status"] == "error"


def test_with_no_open_position_the_sell_cursor_starts_at_the_end(db):
    _sale(db)
    assert lane.sell_startup_cursor(db, "56", {UID}) == 1


def test_a_slow_mirror_exit_never_holds_the_chains_buy_sweep(tmp_path, monkeypatch):
    """Speed rule: the exit runs on its own thread and connection, woken after each buy sweep."""
    import threading
    import time
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56"}, raising=False)
    path = str(tmp_path / "ledger.db")

    def connect():
        c = sqlite3.connect(path, timeout=5, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c
    setup = connect()
    m.ensure_memecoin_tables(setup)
    _position(setup)
    _sale(setup)
    setup.close()
    release, entered, sweeps = threading.Event(), threading.Event(), []

    def sell_hook(conn, row):
        entered.set()
        entered.thread = threading.current_thread().name
        release.wait(5)
        return {"status": "closed"}

    def real_hook(conn, ev):
        return None
    stop = threading.Event()
    ticks = []

    def now_fn():
        ticks.append(threading.current_thread().name)
        return NOW
    wakes, status, _threads = lane.start_network_workers(
        ["56"], conn_factory=connect, real_hook=real_hook, now_fn=now_fn, max_age_s=60,
        poll_s=0.25, stop=stop, sell_hook=sell_hook, sell_wallets_fn=lambda network: {UID})
    try:
        assert entered.wait(5), "the sell thread ran the hook"
        assert entered.thread == "onchain-sells-56"
        before = ticks.count("onchain-lane-56")
        deadline = time.time() + 5
        while ticks.count("onchain-lane-56") < before + 4 and time.time() < deadline:
            wakes["56"].set()
            time.sleep(0.05)
        assert ticks.count("onchain-lane-56") >= before + 4, "buy sweeps kept running during the exit"
    finally:
        release.set()
        stop.set()



# --- Gamma MEDIUM on 92756ef1d: the reference is HIS buy; a sale during our entry waits for the fill -

def test_a_fast_flip_between_his_buy_and_our_fill_mirrors_after_we_fill(mirror):
    _copied_buy(mirror.db, block=1000)
    pid = _position(mirror.db, opened="2026-10-07T15:00:00+00:00", attempt_id="att1")
    row = _sale(mirror.db, sold=50, pre=100, at="2026-10-07T14:59:59+00:00", block=1003)
    assert mirror.run(row)["status"] == "closed", "sold after his buy: mirrors though before our fill"
    assert mirror.exits == [(pid, "mirror")]


def test_a_sale_before_his_copied_buy_holds(mirror):
    _copied_buy(mirror.db, block=1000)
    _position(mirror.db, attempt_id="att1")
    out = mirror.run(_sale(mirror.db, sold=100, pre=100, at="2026-10-07T15:30:00+00:00", block=999))
    assert out["reason"] == "sold_before_source_buy" and mirror.exits == []


def test_a_sale_during_our_entry_is_held_then_mirrored_once_we_fill(mirror):
    m.reserve_pending(mirror.db, mint=COIN, source_user_id=UID, strategy="s", notional=35.0,
                      reserved_at="2026-10-07T14:59:59+00:00", network_id="56")
    _copied_buy(mirror.db, block=1000)
    row = _sale(mirror.db, sold=50, pre=100, at="2026-10-07T15:00:01+00:00", block=1002)
    hook = lambda c, r: R.real_onchain_sell_mirror(c, r, {"56": {"exit_fn": mirror.exit_fn}},
                                                     now_fn=lambda: NOW)
    held, t = {}, [0.0]
    cursor, hits = lane.sweep_onchain_sells(mirror.db, hook, cursor=0, network_id="56",
                                            wallets={UID}, held=held, clock=lambda: t[0])
    assert row["id"] in held and hits == [] and cursor == row["id"], "held, cursor still advances"
    mirror.db.execute("UPDATE memecoin_pending_entries SET status='confirmed'")
    pid = _position(mirror.db, attempt_id="att1")
    t[0] = 5.0
    lane.sweep_onchain_sells(mirror.db, hook, cursor=cursor, network_id="56", wallets={UID},
                             held=held, clock=lambda: t[0])
    assert mirror.exits == [(pid, "mirror")] and not held


def test_a_held_sale_expires_when_our_entry_never_resolves(mirror):
    m.reserve_pending(mirror.db, mint=COIN, source_user_id=UID, strategy="s", notional=35.0,
                      reserved_at="2026-10-07T14:59:59+00:00", network_id="56")
    _sale(mirror.db)
    hook = lambda c, r: R.real_onchain_sell_mirror(c, r, {}, now_fn=lambda: NOW)
    held, t = {}, [0.0]
    lane.sweep_onchain_sells(mirror.db, hook, cursor=0, network_id="56", wallets={UID}, held=held,
                             clock=lambda: t[0])
    t[0] = lane.SELL_PENDING_HOLD_S + 1
    lane.sweep_onchain_sells(mirror.db, hook, cursor=1, network_id="56", wallets={UID}, held=held,
                             clock=lambda: t[0])
    assert not held


# --- X3: per-wallet absolute source-buy floor ------------------------------------------------------

def test_a_per_wallet_source_buy_floor_replaces_the_absolute_floor_for_that_wallet_only(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 200.0, raising=False)
    monkeypatch.setenv("MEMECOIN_REAL_MIN_SOURCE_BUY_USD_BY_WALLET", "56:%s=50,56:x=nan,bad" % UID)
    assert cfg.min_source_buy_usd_for("56", UID) == 50.0
    assert cfg.min_source_buy_usd_for("56", "someone-else") == 200.0
    assert cfg.min_source_buy_usd_for("8453", UID) == 200.0, "per chain"
    assert cfg.min_source_buy_usd_for("56") == 200.0
    monkeypatch.delenv("MEMECOIN_REAL_MIN_SOURCE_BUY_USD_BY_WALLET")
    assert cfg.min_source_buy_usd_for("56", UID) == 200.0, "empty = today"


@pytest.mark.parametrize("path", [
    "sniper/services/memecoin_real_runner.py", "sniper/services/memecoin_rh_fast_lane.py",
    "sniper/services/memecoin_slot_lane.py", "sniper/services/memecoin_promotion_lane.py"])
def test_every_funded_source_floor_reader_goes_through_the_per_wallet_lookup(path):
    """A reader of the bare knob would refuse china_sniper_1's $77-154 buys on that lane only."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath(path).read_text()
    assert "min_source_buy_usd_for(" in src or "source_buy_floor_override(" in src
