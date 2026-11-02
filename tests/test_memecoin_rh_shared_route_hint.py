"""The rh lane's v4_shared signals hint the source's own V4 pool to the entry ladder (2026-10-15,
operator "fill fastest"; Alpha GO on (ii), MEMECOIN_EVM_LANE_SHARED_HINT_CHAINS).

A shared-kind LaneEvent's chase reference held only {"reason": "wallet_net_post_units_unproven"},
so the source rungs and the early quote's hint never ran: cf3672df (14:24:24Z) waited 1620 ms on the
provider multihop. The receipt-bound source pool now rides on that post as `hint_pool`.

Pins (Alpha): v4_shared only; the pool must equal the receipt decode's `source_post.pool` with venue
v4; the knob off leaves the post byte-identical; resolve() still lands on avg_fill with no ratio;
56's `_receipt_pool` and `seed_pairs` ignore `hint_pool`."""
import pytest

from sniper.execution import evm_source_route as src
from sniper.services import memecoin_evm_post_trade as pt
from sniper.services.memecoin_rh_fast_lane import LaneEvent
from rh_source_fill_fixture import proof_for
from test_memecoin_rh_fast_decide import SIG

CHAIN = "4663"
POOL = "0x" + "ab" * 32
V3_POOL = "0x" + "11" * 20
BASE = dict(SIG, tx_hash="0x" + "cd" * 32, wallet="0x" + "ef" * 20, source_user_id="uid",
            post_sqrt=2 ** 96, src_out=180, paid_usd=100)
UNPROVEN = {"reason": "wallet_net_post_units_unproven"}


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_LANE_SHARED_HINT_CHAINS", CHAIN)
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", CHAIN)
    monkeypatch.setattr(src, "_pools", {})


def _event(kind="v4_shared", pool=POOL, post_pool=None, venue="v4", proof=True):
    p = dict(BASE, route_kind=kind, pool=pool)
    if proof:
        p["source_fill"] = proof_for(p, pool=post_pool or pool, venue=venue)
        p["source_fill"]["token_decimals"] = 0
    return LaneEvent(p, decimals=0, supply=1000)


def test_a_v4_shared_event_hints_its_receipt_bound_pool():
    ev = _event(pool=POOL.upper().replace("0X", "0x"))
    assert ev.evm_chase_reference.payload() == dict(UNPROVEN, hint_pool=POOL)
    assert src.attempt_hint(CHAIN, ev.evm_chase_reference, ev.mint) == POOL


def test_the_knob_off_leaves_the_post_byte_identical(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_LANE_SHARED_HINT_CHAINS", "")
    ev = _event()
    assert ev.evm_chase_reference.payload() == UNPROVEN
    assert src.attempt_hint(CHAIN, ev.evm_chase_reference, ev.mint) is None


@pytest.mark.parametrize("kw", [
    dict(kind="split_shared"),                       # not v4_shared, even with a V4 pool id
    dict(kind="v3_shared"),
    dict(kind="pons_shared"),
    dict(post_pool="0x" + "cd" * 32),                # the receipt names another pool
    dict(venue="v3"),                                # the receipt's leg is not V4
    dict(pool=V3_POOL),                              # not a V4 pool id
    dict(pool=None),
    dict(proof=False),                               # no receipt at all
])
def test_anything_not_bound_to_a_v4_receipt_leg_gives_no_hint(kw):
    ev = _event(**kw)
    assert ev.evm_chase_reference.payload() == UNPROVEN
    assert src.attempt_hint(CHAIN, ev.evm_chase_reference, ev.mint) is None


def test_the_chase_reference_still_keeps_the_wallet_average(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    monkeypatch.setattr(cfg, "memecoin_evm_post_trade_chains", lambda: frozenset({CHAIN}))
    on = _event().evm_chase_reference
    monkeypatch.setenv("MEMECOIN_EVM_LANE_SHARED_HINT_CHAINS", "")
    off = _event().evm_chase_reference
    assert "ratio" not in on.payload()
    for our_pool in (POOL, None, "0xother"):
        assert on.resolve(0.5, our_pool) == off.resolve(0.5, our_pool) == (
            0.5, "avg_fill", "no_ratio wallet_net_post_units_unproven")


def test_the_receipt_pool_reader_and_the_pair_seed_ignore_the_hint(monkeypatch):
    from sniper.services.memecoin_real_runner import _receipt_pool
    from sniper.execution import evm_v2_exit_quote as V
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_PAIR_SEED_CHAINS", CHAIN)
    monkeypatch.setattr(V, "_PAIR_SEED", {})
    ev = _event()
    assert ev.evm_chase_reference.payload()["hint_pool"] == POOL
    assert _receipt_pool(ev) is None
    assert src.seed_pairs(CHAIN, ev.evm_chase_reference, ev.mint) is False
    assert V.seeded_pair_tokens(CHAIN, ev.mint) is None


def test_an_onchain_v4_post_still_hints_its_own_pool_first():
    ref = pt.EvmChaseReference(CHAIN, post={"venue": "v4", "pool": POOL, "ratio": 1.0,
                                            "hint_pool": "0x" + "cd" * 32})
    assert src.hint_from_reference(ref) == POOL


def _split_event(*, pools=None, mutate=None):
    p = dict(BASE, route_kind="split_shared", pool=None, post_ratio=None)
    proof = proof_for(p, pool=POOL)
    proof["source_post"] = dict(
        reason="split_route", venue="v4", pools=pools if pools is not None else [POOL, "0x" + "cd" * 32],
        pool_output_atomic=proof["acquired_atomic"], receipt_routes_complete=True)
    if mutate is not None:
        mutate(proof)
    p["source_fill"] = proof
    return LaneEvent(p, decimals=18, supply=None)


def test_split_receipt_keeps_bounded_entry_hints_without_a_source_price(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade")
    monkeypatch.setattr(cfg, "memecoin_evm_post_trade_chains", lambda: frozenset({CHAIN}))
    monkeypatch.setenv("MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS", CHAIN)
    ev = _split_event()
    pools = (POOL, "0x" + "cd" * 32)
    assert ev.evm_chase_reference.payload() == dict(UNPROVEN, hint_pools=pools)
    assert src.attempt_hint(CHAIN, ev.evm_chase_reference, ev.mint) == pools
    assert src.hint_from_reference(ev.evm_chase_reference) == pools
    assert src.safety_exit_hint(CHAIN, ev.evm_chase_reference) == pools
    assert ev.evm_chase_reference.resolve(0.5, POOL) == (
        0.5, "avg_fill", "no_ratio wallet_net_post_units_unproven")


@pytest.mark.parametrize("mutate", [
    lambda p: p["source_post"].update(pools=[POOL]),
    lambda p: p["source_post"].update(pools=[POOL, POOL]),
    lambda p: p["source_post"].update(pools=["0x" + f"{i:064x}" for i in range(9)]),
    lambda p: p["source_post"].update(pools=[POOL, "0x1234"]),
    lambda p: p["source_post"].update(receipt_routes_complete=False),
    lambda p: p["source_post"].update(venue="v3"),
    lambda p: p["source_post"].update(pool_output_atomic="1"),
    lambda p: p.update(source_user_id="other"),
    lambda p: p.update(network_id="56"),
])
def test_invalid_split_proof_cannot_supply_entry_hints(mutate):
    assert _split_event(mutate=mutate).evm_chase_reference.payload() == UNPROVEN


def test_split_hints_obey_existing_shared_hint_rollback(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_LANE_SHARED_HINT_CHAINS", "")
    assert _split_event().evm_chase_reference.payload() == UNPROVEN
