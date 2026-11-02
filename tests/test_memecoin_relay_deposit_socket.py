"""Relay option A, the socket side (spec 2026-10-09 relay deposit proof on the on-chain lane, 3.1 and 3.3):
one deposit socket for every funded EVM chain in MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS (plus 4663 while
option C is on), writing the shared store; the fast lane claims through the same store, and the
confirmer confirms every claimed deposit, whichever lane claimed it."""
import importlib
import json
import sqlite3

import pytest

import test_memecoin_rh_deposit_proof as C
from sniper.services import memecoin_relay_deposit_store as st
from sniper.services import memecoin_rh_deposit_proof as dep

FILL = "0x" + "ab" * 32


def _ledger(tmp_path, armed):
    from sniper.persistence import db_memecoin as db
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    for uid, chains, address in armed:
        db.add_wallet(conn, uid, uid)
        db.set_wallet_real_approved(conn, uid, True, chains=chains, force=True)
        conn.execute("INSERT INTO memecoin_address_registry (source_user_id, network_id, address, status, "
                     "evidence_kind, status_at, conflict_at, first_seen_at) VALUES (?,?,?,?,?,?,?,?)",
                     (uid, "solana", address, "verified", "test", "2026-09-27T00:34:05+00:00", None,
                      "2026-09-27T00:34:05+00:00"))
    return conn


def test_the_roster_covers_every_listed_chain(tmp_path):
    conn = _ledger(tmp_path, [("rh", ["4663"], "A" * 44), ("base", ["8453"], "B" * 44),
                              ("bsc", ["56"], "C" * 44)])
    assert set(dep.deposit_owners(conn)) == {"A" * 44}                      # today's default: 4663
    assert set(dep.deposit_owners(conn, networks=("4663", "8453"))) == {"A" * 44, "B" * 44}
    assert dep.deposit_owners(conn, networks=()) == {}


@pytest.mark.parametrize("lane, chains, want", [
    (False, frozenset(), set()), (True, frozenset(), {"4663"}),
    (False, frozenset({"8453", "56"}), {"8453", "56"}), (True, frozenset({"8453"}), {"4663", "8453"})])
def test_the_socket_networks_are_the_lane_and_the_listed_chains(monkeypatch, lane, chains, want):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", lane)
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", chains)
    assert dep.deposit_networks() == want


@pytest.mark.parametrize("raw, want", [("", frozenset()), ("4663, 8453", frozenset({"4663", "8453"})),
                                       ("56,1,5042", frozenset({"56", "1", "5042"}))])
def test_the_chain_knob_parses(monkeypatch, raw, want):
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", raw)
    assert importlib.reload(cfg).MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS == want


@pytest.mark.parametrize("raw", ["solana", "4663,base", "137"])
def test_a_chain_knob_naming_a_non_funded_evm_chain_fails_loud(monkeypatch, raw):
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", raw)
    try:
        with pytest.raises(ValueError, match="MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS"):
            importlib.reload(cfg)
    finally:
        monkeypatch.delenv("MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS")
        importlib.reload(cfg)


def test_the_subscription_requires_the_depository_and_includes_the_roster():
    params = dep.subscribe_params(("A" * 44, "B" * 44))
    assert params[0]["accountRequired"] == [dep.SOLANA_DEPOSITORY]
    assert params[0]["accountInclude"] == ["A" * 44, "B" * 44]
    assert params[0]["failed"] is False and params[0]["vote"] is False
    assert params[1]["commitment"] == "processed" and params[1]["encoding"] == "jsonParsed"


def test_a_put_reaches_the_store_and_the_fast_lane_claims_through_it(tmp_path):
    store = st.DepositStore(str(tmp_path / "s.db"), clock=lambda: C.ARRIVED)
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: C.ARRIVED, store=store)
    proofs.put(C._proof())
    proof, status, _ms = proofs.take(dict(C._fill(), tx_hash=FILL), decimals={C.USDG: 6}, now=C.ARRIVED + 1)
    assert status == "matched" and proof["order_id"] == C.ORDER_ID
    # The watcher sees a DIFFERENT fill tx on the same order id: the claim is the fast lane's.
    got = store.take(order_id=C.ORDER_ID, fill_tx="0x" + "cd" * 32, source_user_id=C.UID, fill_usd=597.0,
                     now=C.ARRIVED + 1, registry_ok=lambda d, u: True)
    assert got == (None, "order_reused")


def test_a_fill_tx_the_watcher_already_claimed_is_reused_on_the_fast_lane(tmp_path):
    store = st.DepositStore(str(tmp_path / "s.db"), clock=lambda: C.ARRIVED)
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: C.ARRIVED, store=store)
    proofs.put(C._proof())
    assert store.take(order_id=C.ORDER_ID, fill_tx="0x" + "cd" * 32, source_user_id=C.UID, fill_usd=597.0,
                      now=C.ARRIVED + 1, registry_ok=lambda d, u: True)[1] == "matched"
    assert proofs.take(dict(C._fill(), tx_hash=FILL), decimals={C.USDG: 6}, now=C.ARRIVED + 1)[1] == "order_reused"


def test_the_fast_lane_refuses_a_shared_depositor(tmp_path):
    store = st.DepositStore(str(tmp_path / "s.db"), clock=lambda: C.ARRIVED)
    store.note_funder(C.DEPOSITOR, "user-b")
    store.note_funder(C.DEPOSITOR, "user-c")
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: C.ARRIVED, store=store)
    proofs.put(C._proof())
    assert proofs.take(dict(C._fill(), tx_hash=FILL), decimals={C.USDG: 6}, now=C.ARRIVED + 1)[1] == "shared_depositor"


@pytest.mark.parametrize("change", ["revoked", "conflicted"])
@pytest.mark.parametrize("use_store", [False, True])
def test_fast_deposit_claim_rechecks_registry_after_the_frame(tmp_path, change, use_store):
    conn = _ledger(tmp_path, [(C.UID, ["4663"], C.DEPOSITOR)])
    conn.commit()
    store = st.DepositStore(str(tmp_path / "s.db"), clock=lambda: C.ARRIVED)
    store.put(C._proof())
    registry = st.registry_reader(tmp_path / "l.db")
    proofs = dep.DepositProofs(clock=lambda: C.ARRIVED, store=store if use_store else None,
                              registry_ok=registry)
    proofs.put(C._proof())
    if change == "revoked":
        conn.execute("UPDATE memecoin_address_registry SET status='revoked'")
    else:
        conn.execute("UPDATE memecoin_address_registry SET conflict_at='2026-10-14T00:00:00+00:00'")
    conn.commit()
    try:
        assert store.take(order_id=C.ORDER_ID, fill_tx=FILL, source_user_id=C.UID, fill_usd=598,
                          now=C.ARRIVED + 1, registry_ok=registry) == (None, "registry_revoked")
        assert proofs.take(C._fill(), decimals={C.USDG: 6}, now=C.ARRIVED + 1)[:2] == (
            None, "registry_revoked")
        assert store.unconfirmed() == []
    finally:
        conn.close()


def test_fast_deposit_without_current_registry_check_has_no_proof():
    proofs = dep.DepositProofs(clock=lambda: C.ARRIVED)
    proofs.put(C._proof())
    assert proofs.take(C._fill(), decimals={C.USDG: 6}, now=C.ARRIVED + 1)[:2] == (
        None, "registry_unavailable")


def test_production_startup_binds_fast_deposit_check_to_the_current_ledger(monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    conn = _ledger(tmp_path, [(C.UID, ["4663"], C.DEPOSITOR)])
    conn.commit()
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True)
    monkeypatch.setattr(dep, "deposit_store", lambda: None)
    monkeypatch.setattr(dep, "key_refusal", lambda *a: None)
    monkeypatch.setattr(dep.threading.Thread, "start", lambda self: None)
    proofs, _confirmer, _counters, _threads = dep.start_deposit_proofs(
        ledger_path=tmp_path / "l.db", db_path=str(tmp_path / "d.db"), log=lambda _: None,
        env={"MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL": "https://offline/?api-key=test"})
    proofs.clock = lambda: C.ARRIVED
    try:
        proofs.put(C._proof())
        conn.execute("UPDATE memecoin_address_registry SET conflict_at='2026-10-14T00:00:00+00:00'")
        conn.commit()
        assert proofs.take(C._fill(), decimals={C.USDG: 6}, now=C.ARRIVED + 1)[:2] == (
            None, "registry_revoked")
    finally:
        conn.close()


def test_without_a_store_the_fast_lane_is_option_c_unchanged():
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: C.ARRIVED)
    proofs.put(C._proof())
    assert proofs.take(dict(C._fill(), tx_hash=FILL), decimals={C.USDG: 6}, now=C.ARRIVED + 1)[1] == "matched"


@pytest.mark.parametrize("status, finished, paged", [
    ({"confirmationStatus": "confirmed", "err": None}, "confirmed", False),
    ({"confirmationStatus": "confirmed", "err": {"InstructionError": [0, "Custom"]}}, "initiation_unconfirmed:failed", True)])
def test_the_confirmer_confirms_a_deposit_another_process_claimed(tmp_path, status, finished, paged):
    store = st.DepositStore(str(tmp_path / "s.db"), clock=lambda: C.ARRIVED)
    store.put(C._proof())
    assert store.take(order_id=C.ORDER_ID, fill_tx=FILL, source_user_id=C.UID, fill_usd=597.0,
                      now=C.ARRIVED + 1, registry_ok=lambda d, u: True,
                      fill_info={"chain": "8453", "wallet": C.WALLET, "mint": C.MINT})[1] == "matched"
    rows, pages = [], []
    confirmer = dep.DepositConfirmer(lambda m, p: {"value": [status]}, alert_fn=pages.append, sink=rows.append,
                                     store=store)
    confirmer.tick(C.ARRIVED + 3)
    assert [r["status"] for r in rows] == [finished] and rows[0]["fill_tx"] == FILL
    assert rows[0]["wallet"] == C.WALLET and rows[0]["mint"] == C.MINT
    assert bool(pages) == paged and (not paged or "8453" in pages[0])
    confirmer.tick(C.ARRIVED + 5)                    # finished once: the store row is marked
    assert len(rows) == 1


def test_the_shared_store_carries_its_page(monkeypatch, tmp_path):
    """Gamma L2: the store the socket process builds must page a 2-user pair, or nobody ever does."""
    from sniper.services import memecoin_alerts
    from sniper.settings import memecoin as cfg
    sent = []
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"8453"}))
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_STORE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setattr(memecoin_alerts, "send_alert", lambda kind, text, **k: sent.append((kind, text)))
    store = dep.deposit_store()
    store.note_funder("D" * 44, "user-a")
    assert store.shared("D" * 44, "user-b") is False
    assert [kind for kind, _text in sent] == ["relay_origin"] and "user-b" in sent[0][1]
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset())
    assert dep.deposit_store() is None


def test_a_late_claim_is_confirmed_from_its_claim_time_and_with_history(tmp_path):
    """Gamma L3: a claim 110 s after the deposit, first answered null, is not a false 'dropped'."""
    store = st.DepositStore(str(tmp_path / "s.db"), clock=lambda: C.ARRIVED)
    store.put(C._proof())
    assert store.take(order_id=C.ORDER_ID, fill_tx=FILL, source_user_id=C.UID, fill_usd=597.0,
                      now=C.ARRIVED + 110, registry_ok=lambda d, u: True,
                      fill_info={"chain": "8453"})[1] == "matched"
    calls, rows, pages = [], [], []

    def rpc(method, params):
        calls.append(params)
        return {"value": [None]}
    confirmer = dep.DepositConfirmer(rpc, alert_fn=pages.append, sink=rows.append, store=store)
    confirmer.tick(C.ARRIVED + 112)
    assert rows == [] and calls[0][1] == {"searchTransactionHistory": True}
    confirmer.tick(C.ARRIVED + 110 + dep.CONFIRM_GIVE_UP_S)
    assert [r["status"] for r in rows] == ["initiation_unconfirmed:dropped"] and len(pages) == 1


def test_two_lanes_proving_one_fill_reserve_the_coin_once(tmp_path):
    """Gamma's rule-3 question: with 4663 listed and option C on, rhfast:4663 and onchain:4663 can both
    prove the SAME fill tx within ms. The guard is the partial UNIQUE index idx_mc_pending_coin_uniq_v2
    on (network, mint) WHERE status='reserved': the second lane's reservation is None and refuses
    'one position per coin: lost reservation race'."""
    from sniper.persistence import db_memecoin as db
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    tx, mint = "0x" + "ab" * 32, "0x" + "3" * 40
    kw = dict(mint=mint, source_user_id=C.UID, strategy="s", notional=175.0, reserved_at="2026-10-09T00:00:00+00:00",
              network_id="4663")
    first = db.reserve_pending(conn, origin_event_id="rhfast:4663:%s:%s" % (tx, mint), **kw)
    second = db.reserve_pending(conn, origin_event_id="onchain:4663:%s:%s" % (tx, mint), **kw)
    assert first is not None and second is None
