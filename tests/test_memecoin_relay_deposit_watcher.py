"""Relay option A, the on-chain watcher side (spec 2026-10-09 relay deposit proof on the on-chain lane,
sections 3.2 and 4): a Relay row whose order id has a stored deposit from the user's own registry
identity is proved at detection with no Relay call; a deposit that lands later is taken on the next
poll; the shared-depositor rule binds the Relay path too; refusals name their class.

Real frames: the hooks fixture's 56 destination fill and its Solana deposit (DopamineFeenFr1, 600 USDC),
the same deposit the option C tests record."""
import copy
from types import SimpleNamespace

import pytest

import test_memecoin_relay_initiation as R
import test_memecoin_rh_deposit_proof as C
from sniper.services import memecoin_relay_deposit_store as st
from sniper.services import memecoin_relay_initiation as relay
from sniper.services.memecoin_source_initiation import (
    RELAY_DEPOSIT_PROOF_VERSION, relay_refusal_reason, source_initiation_refusal)

UID = R.UID
assert UID == C.UID


def _row():
    _req, _origin, proof, destination = R.inputs()
    row = {"tx_hash": proof["tx_hash"], "source_user_id": UID,
           "buy": {"source_initiation": proof, "source_size_usd": 597.0, "mint": proof["mint"]}}
    return row, destination


def _store(tmp_path, *, now=None, **kw):
    store = st.DepositStore(str(tmp_path / "s.db"), clock=lambda: C.ARRIVED if now is None else now, **kw)
    return store


def _registry(depositor, uid):
    return (depositor, uid) == (C.DEPOSITOR, UID)


def _ev(chain, proof, relay_origin_error=None):
    return SimpleNamespace(signal_source="onchain_watch", fomo_event_id="onchain:%s:%s:%s" % (
        chain, proof["tx_hash"], proof["mint"]), source_wallet=proof["beneficiary"],
        source_block_number=proof["block_number"], source_user_id=UID, mint=proof["mint"],
        source_initiation=proof, relay_origin_error=relay_origin_error)


def test_a_stored_deposit_proves_the_relay_row_with_no_relay_call(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    store = _store(tmp_path)
    store.put(C._proof())
    row, destination = _row()
    proved, status = st.prove_from_deposit(row, destination, chain="56", store=store, registry_ok=_registry,
                                           now=C.ARRIVED + 1)
    assert status == "matched"
    proof = proved["buy"]["source_initiation"]
    assert proof["proof_version"] == RELAY_DEPOSIT_PROOF_VERSION
    assert (proof["origin_network_id"], proof["origin_wallet"], proof["order_id"]) == (
        "solana", C.DEPOSITOR, C.ORDER_ID)
    assert proof["origin_tx"] == C.ORIGIN["transaction"]["signatures"][0]
    assert proof["block_hash"] == row["buy"]["source_initiation"]["block_hash"]   # the receipt's own facts
    assert row["buy"]["source_initiation"]["proof_version"] != RELAY_DEPOSIT_PROOF_VERSION  # a new dict
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"56"}))
    assert source_initiation_refusal(_ev("56", proof), "56") is None


def test_the_deposit_proof_is_refused_on_a_chain_not_listed(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    store = _store(tmp_path)
    store.put(C._proof())
    row, destination = _row()
    proof = st.prove_from_deposit(row, destination, chain="56", store=store, registry_ok=_registry,
                                  now=C.ARRIVED + 1)[0]["buy"]["source_initiation"]
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"4663"}))
    assert source_initiation_refusal(_ev("56", proof), "56") == "wallet_initiation_unproved"
    # ... and never on the fast lane's origin shape, nor without the receipt's block hash.
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"56"}))
    assert source_initiation_refusal(_ev("56", dict(proof, block_hash=None)), "56") == "receipt_identity_mismatch"
    assert source_initiation_refusal(_ev("56", dict(proof, acquired_atomic=None)), "56") == "receipt_identity_mismatch"


def test_a_row_without_a_stored_deposit_is_left_to_the_relay_path(tmp_path):
    row, destination = _row()
    assert st.prove_from_deposit(row, destination, chain="56", store=_store(tmp_path), registry_ok=_registry,
                                 now=C.ARRIVED + 1) == (None, "no_deposit")


def _verifier(tmp_path, store, fetch, **kw):
    return relay.WatcherVerifier(
        lambda _: R.FRAME["identity"], chains=R.FRAME["chains"], fetch=fetch, poll_s=0.3,
        deposit_fn=st.deposit_check(store, chain="56", registry_ok=_registry, now_fn=lambda: C.ARRIVED + 1),
        depositor_fn=store, clock=lambda: 0.0, sleep=lambda _s: None, **kw)


def _item():
    row, destination = _row()
    return {"row": row, "transaction": destination}


def test_a_deposit_already_stored_is_taken_before_any_relay_call(tmp_path):
    store = _store(tmp_path)
    store.put(C._proof())
    calls = []
    result = _verifier(tmp_path, store, lambda url, **k: calls.append(url) or {"requests": []})(_item())
    assert result["row"]["buy"]["source_initiation"]["proof_version"] == RELAY_DEPOSIT_PROOF_VERSION
    assert calls == [] and result["origin_attempts"][0].startswith("deposit/")


def test_a_deposit_that_lands_during_the_relay_wait_wins_the_next_poll(tmp_path):
    store = _store(tmp_path)
    calls = []

    def fetch(url, **k):
        calls.append(url)
        if len(calls) == 2:
            store.put(C._proof())                # lands between the second and third poll
        return {"requests": []}
    result = _verifier(tmp_path, store, fetch)(_item())
    assert result["row"]["buy"]["source_initiation"]["proof_version"] == RELAY_DEPOSIT_PROOF_VERSION
    assert len(calls) == 2


def test_a_registry_verified_shared_depositor_is_refused_on_the_relay_path(tmp_path):
    """THE CHECK LEFT BEHIND for the Relay path: Relay's settled answer names the user's own registry
    identity, but that depositor funds three source users."""
    store = _store(tmp_path)
    store.note_funder(C.DEPOSITOR, "user-b")
    store.note_funder(C.DEPOSITOR, "user-c")

    def fetch(url, **k):
        return R.FRAME["relay"] if "requests" in url else R.FRAME["origin-sol"]
    with pytest.raises(relay.ProofError, match="shared_depositor"):
        _verifier(tmp_path, store, fetch)(_item())


def test_at_two_users_the_relay_path_keeps_the_registry_owner_and_pages(tmp_path):
    pages = []
    store = _store(tmp_path, page_fn=pages.append)
    store.note_funder(C.DEPOSITOR, "user-b")

    def fetch(url, **k):
        return R.FRAME["relay"] if "requests" in url else R.FRAME["origin-sol"]
    result = _verifier(tmp_path, store, fetch)(_item())
    assert result["row"]["buy"]["source_initiation"]["proof_version"] == relay.PROOF_VERSION
    assert len(pages) == 1 and store.funder_count(C.DEPOSITOR) == 2


@pytest.mark.parametrize("funders, reason", [((), "foreign_depositor"), (("u1", "u2", "u3"), "shared_depositor")])
def test_a_settled_answer_from_an_unregistered_depositor_names_its_class(tmp_path, funders, reason):
    store = _store(tmp_path)
    for uid in funders:
        store.note_funder(C.DEPOSITOR, uid)

    def fetch(url, **k):
        return R.FRAME["relay"] if "requests" in url else R.FRAME["origin-sol"]
    verifier = relay.WatcherVerifier(lambda _: [], chains=R.FRAME["chains"], fetch=fetch, poll_s=0.3,
                                     depositor_fn=store, clock=lambda: 0.0, sleep=lambda _s: None)
    with pytest.raises(relay.ProofError, match=reason):
        verifier(_item())
    assert UID in store.funders(C.DEPOSITOR)          # the observation is kept either way


def test_without_the_store_the_verifier_is_todays():
    def fetch(url, **k):
        return R.FRAME["relay"] if "requests" in url else R.FRAME["origin-sol"]
    verifier = relay.WatcherVerifier(lambda _: [], chains=R.FRAME["chains"], fetch=fetch, poll_s=0.3,
                                     clock=lambda: 0.0, sleep=lambda _s: None)
    with pytest.raises(relay.ProofError, match="origin_wallet_unverified"):
        verifier(_item())


@pytest.mark.parametrize("error, reason", [
    ("relay_request_not_indexed", "relay_origin_not_indexed"),
    ("origin_wallet_unverified", "relay_origin_foreign_depositor"),
    ("foreign_depositor", "relay_origin_foreign_depositor"),
    ("shared_depositor", "relay_origin_shared_depositor"),
    ("origin_rate_limit:relay_requests", "relay_origin_rate_limit"),
    ("pregate:mcap $4,300,000 over ceiling $250,000 (over_ceiling)",
     "mcap $4,300,000 over ceiling $250,000 (over_ceiling) [relay origin unproved]"),
    (None, "wallet_initiation_unproved"),
    ("something_new", "wallet_initiation_unproved"),
])
def test_an_unproved_relay_row_refuses_with_its_class(error, reason):
    _req, _origin, proof, _dest = R.inputs()
    assert relay_refusal_reason(error) == reason
    assert source_initiation_refusal(_ev("56", proof, relay_origin_error=error), "56") == reason


# --- the watcher script: the deposit is checked before the row is ever queued ------------------------

from scripts.research import onchain_evm_wallet_watch as _watch_module  # noqa: E402
REAL_DEPOSIT_STORE = _watch_module._deposit_store

@pytest.fixture
def watch_56(tmp_path, monkeypatch):
    from scripts.research import onchain_evm_wallet_watch as watch
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(watch, "CHAIN", "56")
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"56"}))
    store = _store(tmp_path)
    monkeypatch.setattr(watch, "_deposit_store", lambda: store)
    monkeypatch.setattr(watch, "_deposit_registry_ok", lambda depositor, uid: _registry(depositor, uid))
    monkeypatch.setattr(watch.time, "time", lambda: C.ARRIVED + 1)
    lines = []
    monkeypatch.setattr(watch, "_say", lines.append)
    return watch, store, lines


class _Worker:
    def __init__(self):
        self.items = []

    def submit(self, item):
        self.items.append(item)
        return True


def test_a_stored_deposit_publishes_the_row_at_once_never_queued(watch_56):
    watch, store, lines = watch_56
    store.put(C._proof())
    row, destination = _row()
    row["valuation"] = {}
    worker = _Worker()
    assert watch._defer_relay_origin(worker, row, {"_transaction": destination}, 600.0, 5.0) is False
    assert worker.items == []                                   # no Relay wait at all
    assert row["buy"]["source_initiation"]["proof_version"] == RELAY_DEPOSIT_PROOF_VERSION
    assert row["valuation"]["relay_origin_path"] == "deposit" and row["valuation"]["relay_origin_error"] is None
    assert any("relay origin" in l and "reason=verified" in l and "path=deposit" in l for l in lines)


def test_without_a_deposit_the_row_waits_on_relay_as_today(watch_56):
    watch, _store_, _lines = watch_56
    row, destination = _row()
    row["valuation"] = {}
    worker = _Worker()
    assert watch._defer_relay_origin(worker, row, {"_transaction": destination}, 600.0, 5.0) is True
    assert len(worker.items) == 1
    assert row["buy"]["source_initiation"]["proof_version"] != RELAY_DEPOSIT_PROOF_VERSION


def test_a_chain_not_listed_never_reads_the_store(watch_56, monkeypatch, tmp_path):
    watch, store, _lines = watch_56
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(watch, "_deposit_store", REAL_DEPOSIT_STORE)        # the knob check itself
    monkeypatch.setattr(watch, "_DEPOSIT_STORE", [])
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_STORE_PATH", str(tmp_path / "never.db"))
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset({"4663"}))
    assert watch._deposit_store() is None
    store.put(C._proof())
    row, destination = _row()
    row["valuation"] = {}
    worker = _Worker()
    assert watch._defer_relay_origin(worker, row, {"_transaction": destination}, 600.0, 5.0) is True
    assert not (tmp_path / "never.db").exists()
    assert store.take(order_id=C.ORDER_ID, fill_tx=row["tx_hash"], source_user_id=UID, fill_usd=597.0,
                      now=C.ARRIVED + 1, registry_ok=_registry)[1] == "matched"   # still unclaimed


def test_the_onchain_event_reads_the_watchers_relay_error():
    import json
    from sniper.services.memecoin_onchain_lane import OnchainEv
    _req, _origin, proof, _dest = R.inputs()
    row = dict(mint=proof["mint"], source_user_id=UID, handle="DopamineFeenFr1", network_id="56",
               address=proof["beneficiary"], source_slot=proof["block_number"], source_price=1.0,
               source_size_usd=597.0, signature="56:%s:%s" % (proof["tx_hash"], proof["mint"]),
               detected_at="2026-10-09T00:00:00+00:00", source_valuation_json=json.dumps(
                   {"source_initiation": proof, "relay_origin_error": "relay_request_not_indexed"}))
    ev = OnchainEv(row)
    assert ev.relay_origin_error == "relay_request_not_indexed"
    assert source_initiation_refusal(ev, "56") == "relay_origin_not_indexed"


class _LockedStore:
    def note_funder(self, *a, **k):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")

    def shared(self, *a, **k):
        raise AssertionError("never reached")


def test_a_locked_store_keeps_a_relay_verified_buy_verified():
    """Gamma M1: the shared rule is new strictness, so a store error falls back to today's outcome."""
    def fetch(url, **k):
        return R.FRAME["relay"] if "requests" in url else R.FRAME["origin-sol"]
    verifier = relay.WatcherVerifier(lambda _: R.FRAME["identity"], chains=R.FRAME["chains"], fetch=fetch,
                                     poll_s=0.3, depositor_fn=_LockedStore(), clock=lambda: 0.0,
                                     sleep=lambda _s: None)
    result = verifier(_item())
    assert result["row"]["buy"]["source_initiation"]["proof_version"] == relay.PROOF_VERSION
    assert verifier.store_errors == 1


def test_a_locked_store_keeps_todays_unverified_refusal():
    def fetch(url, **k):
        return R.FRAME["relay"] if "requests" in url else R.FRAME["origin-sol"]
    verifier = relay.WatcherVerifier(lambda _: [], chains=R.FRAME["chains"], fetch=fetch, poll_s=0.3,
                                     depositor_fn=_LockedStore(), clock=lambda: 0.0, sleep=lambda _s: None)
    with pytest.raises(relay.ProofError, match="origin_wallet_unverified"):
        verifier(_item())
