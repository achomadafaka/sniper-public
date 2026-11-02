"""RH receipt admission retains verified initiation and complete split economics offline."""
import pytest

from sniper.services import memecoin_rh_fast_lane as lane
from sniper.services.memecoin_source_fill import validate_source_fill
from sniper.settings import memecoin as cfg
from test_memecoin_rh_fast_entry import _attempts, _go, _payload, entry  # noqa: F401


def _relay_payload(version="relay-origin-paid-v1", *, deposit=False):
    p = _payload()
    proof = p["source_fill"]
    initiation = dict(
        schema_version=1, proof_version=version, network_id="4663",
        tx_hash=p["tx_hash"], block_hash=proof["block_hash"], block_number=p["seq"],
        mint=p["mint"], beneficiary=p["wallet"], payer="0x" + "f7" * 20,
        source_user_id=p["source_user_id"], acquired_atomic=proof["acquired_atomic"],
        origin_network_id="4663", origin_wallet=p["wallet"], origin_tx="0x" + "bb" * 32,
        origin_slot=900, origin_amount_atomic="100000000", order_id="0x" + "cc" * 32,
        origin_identity_user_id=p["source_user_id"],
    )
    if deposit:
        initiation.update(origin_network_id="solana", origin_wallet="1" * 32,
                          origin_tx="5" * 88)
    p["source_fill"] = dict(proof, receipt_proof_version="relay-envelope-linked-v1",
                            payment_basis="relay_receipt_outflow", source_initiation=initiation)
    return p


def _split_payload(*, relay=False):
    p = _relay_payload() if relay else _payload()
    p.update(route_kind="split_shared", pool=None, post_ratio=None)
    p["source_fill"]["source_post"] = dict(
        reason="split_route", venue="v4", pools=["0x" + "a1" * 32, "0x" + "a2" * 32],
        pool_output_atomic=p["source_fill"]["acquired_atomic"], receipt_routes_complete=True,
    )
    return p


def _queue_shared(entry, monkeypatch):
    built, _, _, ledger = entry
    live = built["live"]
    live.routes = frozenset({"v4_shared"})
    worker = lane.SharedRouteWorker(live, connect=lambda: ledger, db_path=built["db_path"])
    # Keep the real nonblocking queue boundary; its executor is deliberately not started.
    monkeypatch.setattr(worker.thread, "is_alive", lambda: True)
    live.shared_worker = worker
    return worker


@pytest.mark.parametrize("version,deposit", [
    ("relay-origin-paid-v1", False), ("relay-origin-bound-v1", False),
    ("relay-deposit-processed-v1", True),
])
def test_verified_non_direct_receipt_reaches_the_native_funded_hook(entry, monkeypatch, version, deposit):
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_BOUND_PROOF_ACCEPT", True)
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ("4663", "56"))
    p = _relay_payload(version, deposit=deposit)
    assert validate_source_fill(p["source_fill"], p) is not None
    built, sender, net, ledger = entry
    result = _go(built, p)
    assert result["reason"].startswith("rejected:"), result
    assert len(sender.raws) == 1
    assert [call for call in net.calls if not call[1]] == []
    assert [row["signal_source"] for row in _attempts(ledger)] == ["rh_fast_lane"]


@pytest.mark.parametrize("relay", [False, True])
def test_complete_split_receipt_queues_shared_without_inventing_one_pool_price(entry, monkeypatch, relay):
    worker = _queue_shared(entry, monkeypatch)
    p = _split_payload(relay=relay)
    assert validate_source_fill(p["source_fill"], p) is not None
    built, sender, net, ledger = entry
    result = _go(built, p)
    assert result["reason"] == "shared_queued", result
    queued, _ = worker.jobs.get_nowait()
    assert queued == p and queued["pool"] is None and queued["post_ratio"] is None
    assert worker.live._pilot_pending == 1
    assert sender.raws == [] and net.calls == [] and _attempts(ledger) == []


@pytest.mark.parametrize("field,value", [
    ("beneficiary", "0x" + "aa" * 20), ("source_user_id", "other"),
    ("origin_identity_user_id", "other"), ("origin_wallet", "0x" + "aa" * 20),
    ("network_id", "56"), ("tx_hash", "0x" + "aa" * 32),
    ("block_hash", "0x" + "aa" * 32), ("block_number", 1001),
    ("acquired_atomic", "1"), ("origin_tx", None), ("order_id", None),
    ("proof_version", "relay-unsupported-v1"),
])
def test_invalid_relay_initiation_stops_before_attempt_or_provider(entry, field, value):
    p = _relay_payload()
    p["source_fill"]["source_initiation"][field] = value
    built, sender, net, ledger = entry
    result = _go(built, p)
    assert result["reason"] == "source_fill_unproved", result
    assert sender.raws == [] and net.calls == [] and _attempts(ledger) == []
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_bound_origin_proof_stays_refused_when_its_current_flag_is_off(entry, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_BOUND_PROOF_ACCEPT", False)
    built, sender, net, ledger = entry
    result = _go(built, _relay_payload("relay-origin-bound-v1"))
    assert result["reason"] == "source_fill_unproved"
    assert sender.raws == [] and net.calls == [] and _attempts(ledger) == []


@pytest.mark.parametrize("receipt_version", [
    "relay-envelope-linked-v1", "relay-envelope-other-target-v1", "relay-kyber-linked-v1",
])
def test_beneficiary_only_relay_receipt_stays_refused_before_attempt(entry, receipt_version):
    p = _relay_payload()
    p["source_fill"].pop("source_initiation")
    p["source_fill"]["receipt_proof_version"] = receipt_version
    built, sender, net, ledger = entry
    result = _go(built, p)
    assert result["reason"] == "relay_origin_unproved"
    assert sender.raws == [] and net.calls == [] and _attempts(ledger) == []


@pytest.mark.parametrize("mutation", [
    "missing_pools", "one_pool", "duplicate_pool", "unknown_keys", "wrong_venue",
    "missing_output", "below_wallet_net", "over_90pct_bound", "bad_pool",
])
def test_incomplete_split_receipt_never_queues_or_attempts(entry, monkeypatch, mutation):
    worker = _queue_shared(entry, monkeypatch)
    p = _split_payload()
    post = p["source_fill"]["source_post"]
    if mutation == "missing_pools": post.pop("pools")
    elif mutation == "one_pool": post["pools"] = post["pools"][:1]
    elif mutation == "duplicate_pool": post["pools"] = [post["pools"][0]] * 2
    elif mutation == "unknown_keys": post["receipt_routes_complete"] = False
    elif mutation == "wrong_venue": post["venue"] = "v3"
    elif mutation == "missing_output": post.pop("pool_output_atomic")
    elif mutation == "below_wallet_net": post["pool_output_atomic"] = str(int(p["src_out"]) - 1)
    elif mutation == "over_90pct_bound": post["pool_output_atomic"] = str(int(p["src_out"]) * 10 // 9 + 1)
    elif mutation == "bad_pool": post["pools"][0] = "0x1234"
    built, sender, net, ledger = entry
    result = _go(built, p)
    assert result["reason"] in {"source_fill_unproved", "source_post_unbound"}, result
    assert worker.jobs.empty() and worker.live._pilot_pending == 0
    assert sender.raws == [] and net.calls == [] and _attempts(ledger) == []


def test_direct_legacy_receipt_without_explicit_initiation_keeps_the_native_path(entry):
    built, sender, net, ledger = entry
    p = _payload()
    assert "source_initiation" not in p["source_fill"]
    result = _go(built, p)
    assert result["reason"].startswith("rejected:")
    assert len(sender.raws) == 1 and len(_attempts(ledger)) == 1
    assert [call for call in net.calls if not call[1]] == []
