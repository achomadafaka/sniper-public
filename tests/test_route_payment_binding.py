"""Gamma's N1 residual 1 (2026-10-07): the whole-receipt average that caps a routed copy's premium
counts only the USDC that went INTO the route.

A routed source buy's net USDC out also carries platform/referral fees (0.1-2% on routed buys
09-26..10-07) and could carry an unrelated transfer; counting them raises the source's average and
loosens MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT. The watcher's receipt proof now carries
route_payment_atomic: the owner's USDC legs bound to swaps by the call tree (a leg whose invoker
pays a route account back), or the share of a router hop's outflow that reaches such swaps. A fee
into a sink, direct or passed through, counts nothing; no call tree is unknown. A receipt resume
swaps that amount into the whole average, and with the cap on, a receipt without it does not
resume.
"""
import json
from pathlib import Path

import pytest
from solders.keypair import Keypair

from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_source_initiation as I
from sniper.services.memecoin_source_valuation import USDC, route_payment_atomic, value_source_buy

OWNER = str(Keypair().pubkey())
ACCTS = ["OwnUsdc", "OwnCoin", "RtrUsdc", "FeeWallet", "PoolCoin"]
KEYS = [OWNER] + [str(Keypair().pubkey()) for _ in ACCTS]
OWN, COIN_ACCT, RTR, FEEW, POOLV = KEYS[1:]
COIN = str(Keypair().pubkey())
OTHER = str(Keypair().pubkey())
MINT = {OWN: USDC, COIN_ACCT: COIN, RTR: USDC, FEEW: USDC, POOLV: COIN}


def _xfer(src, dst, auth, amount, height):
    return {"program": "spl-token", "stackHeight": height, "parsed": {"type": "transferChecked", "info": {
        "source": src, "destination": dst, "authority": auth, "mint": MINT[src],
        "tokenAmount": {"amount": str(amount), "decimals": 6}}}}


def _call(height):
    return {"programId": "Prog", "stackHeight": height, "data": ""}


def _tx(*top_level):
    """Each top-level instruction is a list of (height, instruction) children."""
    rows = [{"accountIndex": KEYS.index(a), "mint": m, "owner": OWNER if a in (OWN, COIN_ACCT) else OTHER,
             "uiTokenAmount": {"amount": "0", "decimals": 6}} for a, m in MINT.items()]
    return {"transaction": {"message": {"accountKeys": [{"pubkey": k} for k in KEYS],
                                        "instructions": [{"programId": "Top", "data": ""} for _ in top_level]}},
            "meta": {"preTokenBalances": rows, "postTokenBalances": rows,
                     "innerInstructions": [{"index": i, "instructions": ixs}
                                           for i, ixs in enumerate(top_level) if ixs]}}


# The router (height 1) calls a pool (2); the pool takes the owner's USDC and pays the coin (3).
SWAP = [_call(2), _xfer(OWN, RTR, OWNER, 99_000_000, 3), _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 3)]
FEE_IN_ROUTER = [_xfer(OWN, FEEW, OWNER, 1_000_000, 2)]                 # the router's own child
FEE_ALONE = [[_xfer(OWN, FEEW, OWNER, 500_000, 2)]]                     # another top-level program


def test_only_a_leg_whose_invoker_pays_the_owner_back_counts():
    tx = _tx(SWAP + FEE_IN_ROUTER, *FEE_ALONE)
    assert route_payment_atomic(tx, OWNER, 100_500_000) == 99_000_000


def test_a_refund_caps_it_at_the_net_paid():
    assert route_payment_atomic(_tx(SWAP), OWNER, 98_000_000) == 98_000_000


def test_fees_alone_are_no_route():
    assert route_payment_atomic(_tx(FEE_IN_ROUTER, *FEE_ALONE), OWNER, 1_500_000) is None


def test_a_payout_to_someone_else_does_not_pair_a_leg():
    away = [_call(2), _xfer(OWN, RTR, OWNER, 99_000_000, 3), _xfer(POOLV, RTR, OTHER, 5_000, 3)]
    assert route_payment_atomic(_tx(away), OWNER, 99_000_000) is None


def test_no_call_tree_is_unknown():
    bare = [dict(ix) for ix in SWAP]
    bare[1].pop("stackHeight")
    assert route_payment_atomic(_tx(bare), OWNER, 99_000_000) is None


ROUTER_ACCT, FEE_SINK = str(Keypair().pubkey()), str(Keypair().pubkey())
POOL_USDC = str(Keypair().pubkey())


def _hop_tx(*groups):
    """Like _tx, with the router's own USDC account, a fee sink and a pool's USDC vault."""
    tx = _tx(*groups)
    extra = [ROUTER_ACCT, FEE_SINK, POOL_USDC]
    tx["transaction"]["message"]["accountKeys"] += [{"pubkey": k} for k in extra]
    rows = [{"accountIndex": len(KEYS) + i, "mint": USDC, "owner": OTHER,
             "uiTokenAmount": {"amount": "0", "decimals": 6}} for i in range(len(extra))]
    tx["meta"]["preTokenBalances"] += rows
    tx["meta"]["postTokenBalances"] += rows
    return tx


def _usdc(src, dst, auth, amount, height):
    ix = _xfer(OWN, RTR, auth, amount, height)
    ix["parsed"]["info"].update(source=src, destination=dst)
    return ix


def test_a_router_hop_counts_what_reaches_the_pools_not_the_router_fee():
    """proVF4's shape (27MRyWXYrE): the owner pays the router's account; the router takes its fee
    from there and pays the pool, which pays the owner."""
    hop = [_usdc(OWN, ROUTER_ACCT, OWNER, 100_000_000, 2),
           _usdc(ROUTER_ACCT, FEE_SINK, OTHER, 1_000_000, 2),
           _call(2), _usdc(ROUTER_ACCT, POOL_USDC, OTHER, 99_000_000, 3),
           _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 3)]
    assert route_payment_atomic(_hop_tx(hop), OWNER, 100_000_000) == 99_000_000


def test_a_passthrough_fee_counts_nothing():
    """DFlow's shape (4rTe5hhHyX): the fee passes through an account into a sink that never sends."""
    passthrough = [_usdc(OWN, ROUTER_ACCT, OWNER, 1_000_000, 2),
                   _usdc(ROUTER_ACCT, FEE_SINK, OTHER, 1_000_000, 2)]
    assert route_payment_atomic(_hop_tx(passthrough + SWAP), OWNER, 100_000_000) == 99_000_000


@pytest.mark.parametrize("fixture, owner, net, route", [
    ("sol-route-provf4-27MRyWXY.json", "5dB6rj9C", 3_000_000_000, 2_995_651_575),
    ("sol-route-dflow-4rTe5hhH.json", "4CRX74nx", 529_686_722, 527_041_467),
], ids=["provf4_router_hop", "dflow_prop_amm"])
def test_real_routes_bind_their_swap_legs(fixture, owner, net, route):
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin" / fixture).read_text())
    owner = next(b["owner"] for b in tx["meta"]["preTokenBalances"] if b["owner"].startswith(owner))
    assert route_payment_atomic(tx, owner, net) == route


def test_a_real_usdc_routed_receipt_carries_its_route_payment():
    """PLT 2ebfcf66: a $500 USDC routed buy (USDC -> temporary wSOL -> coin)."""
    owner = "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv"
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin/plt-route-2ebfcf66.json").read_text())
    facts = value_source_buy(tx, owner, sol_usd=None, now=105.)
    proof = I.solana_receipt_initiation(tx, owner, facts)
    assert proof["payment_atomic"] == "500000000"
    assert 0 < int(proof["route_payment_atomic"]) <= 500_000_000


# --- the resume ---------------------------------------------------------------------------------

def _resume(monkeypatch, route, cap=35.0, unknown=None):
    from test_slot_lane_receipt_resume import setup
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", cap)
    buy, ev, queued, h, clock = setup(monkeypatch)
    proof = dict(ev.source_initiation)
    if route is not None:
        proof["route_payment_atomic"] = str(route(buy))
    if unknown is not None:
        proof["route_payment_unknown"] = unknown
    ev.source_initiation = proof
    h.remember(buy)
    clock.now += .3
    return buy, h.offer(ev), queued


def test_a_resume_averages_over_the_route_payment(monkeypatch):
    buy, ok, queued = _resume(monkeypatch, lambda b: b.routed.usdc_spent_atomic * 99 // 100)
    assert ok and queued[0].routed.usdc_spent_atomic == buy.routed.usdc_spent_atomic * 99 // 100
    assert queued[0].routed.token_delta_atomic == buy.routed.token_delta_atomic


@pytest.mark.parametrize("route", [None, lambda b: b.routed.usdc_spent_atomic + 1, lambda b: 0],
                         ids=["missing", "over_spent", "zero"])
def test_with_the_cap_on_an_unbound_route_payment_does_not_resume(monkeypatch, caplog, route):
    import logging
    with caplog.at_level(logging.WARNING):
        _buy, ok, queued = _resume(monkeypatch, route)
    assert not ok and not queued
    assert any("receipt resume refused route_payment_unknown" in r.getMessage() for r in caplog.records)


def test_with_the_cap_off_a_receipt_without_it_still_resumes(monkeypatch):
    buy, ok, queued = _resume(monkeypatch, None, cap=0.0)
    assert ok and queued[0].routed == buy.routed


# --- EVM-03 (Codex two-day review 2026-10-07): a fee sibling of the swap's own invoker ---------

def test_a_fee_under_the_swaps_own_invoker_is_unknown_not_paid():
    """The audit's repro: one router instruction directly invokes the 99 USDC purchase, a 1 USDC
    platform fee and the coin payout. The coin payout made both USDC outflows look like swap
    payments (100 USDC). Mint and destination cannot tell the two sinks apart, so it is unknown."""
    tx = _tx([_xfer(OWN, RTR, OWNER, 99_000_000, 2), _xfer(OWN, FEEW, OWNER, 1_000_000, 2),
              _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 2)])
    assert route_payment_atomic(tx, OWNER, 100_000_000) is None


def test_a_fee_under_the_pools_invoker_inside_a_router_hop_is_unknown():
    """The intermediate shape: the owner pays a router account; the pool's own instruction takes
    the pool payment AND a fee out of that hop beside the coin payout."""
    hop = [_usdc(OWN, ROUTER_ACCT, OWNER, 100_000_000, 2),
           _call(2), _usdc(ROUTER_ACCT, POOL_USDC, OTHER, 99_000_000, 3),
           _usdc(ROUTER_ACCT, FEE_SINK, OTHER, 1_000_000, 3),
           _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 3)]
    assert route_payment_atomic(_hop_tx(hop), OWNER, 100_000_000) is None


def test_a_refund_or_an_unrelated_inbound_beside_the_swap_keeps_the_payment():
    """Inbound transfers are not a second payment: the swap stays bound."""
    refund = [_call(2), _xfer(OWN, RTR, OWNER, 99_000_000, 3), _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 3),
              _usdc(POOL_USDC, OWN, OTHER, 1_000_000, 3)]
    assert route_payment_atomic(_hop_tx(refund), OWNER, 98_000_000) == 98_000_000


def test_two_swaps_under_separate_invokers_both_count():
    second = [_call(2), _usdc(OWN, POOL_USDC, OWNER, 50_000_000, 3), _xfer(POOLV, COIN_ACCT, OTHER, 2_000, 3)]
    assert route_payment_atomic(_hop_tx(SWAP + second), OWNER, 149_000_000) == 149_000_000


@pytest.mark.parametrize("groups, why", [
    (lambda: _tx([_xfer(OWN, RTR, OWNER, 99_000_000, 2), _xfer(OWN, FEEW, OWNER, 1_000_000, 2),
                  _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 2)]), "fee_sibling:d=0:p=Top:s=1.0%"),
    (lambda: _hop_tx([_usdc(OWN, ROUTER_ACCT, OWNER, 100_000_000, 2), _call(2),
                      _usdc(ROUTER_ACCT, POOL_USDC, OTHER, 99_000_000, 3),
                      _usdc(ROUTER_ACCT, FEE_SINK, OTHER, 1_000_000, 3),
                      _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 3)]), "fee_sibling:d=1:p=Prog:s=1.0%"),
    (lambda: _tx(FEE_IN_ROUTER, *FEE_ALONE), "no_swap_leg"),
], ids=["direct", "hop", "fees_only"])
def test_an_unknown_route_payment_names_its_class(groups, why):
    """Alpha's GO on EVM-03: the forward coverage loss must be countable by class."""
    from sniper.services.memecoin_source_valuation import route_payment_evidence
    assert route_payment_evidence(groups(), OWNER, 100_000_000) == (None, why)


def test_no_call_tree_names_its_class():
    from sniper.services.memecoin_source_valuation import route_payment_evidence
    bare = [dict(ix) for ix in SWAP]
    bare[1].pop("stackHeight")
    assert route_payment_evidence(_tx(bare), OWNER, 99_000_000) == (None, "no_call_tree")


def test_the_receipt_proof_carries_the_class_and_the_resume_line_prints_it(monkeypatch, caplog):
    import logging
    from sniper.services import memecoin_source_valuation as V
    owner = "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv"
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin/plt-route-2ebfcf66.json").read_text())
    facts = value_source_buy(tx, owner, sol_usd=None, now=105.)
    monkeypatch.setattr(V, "route_payment_evidence", lambda *a: (None, "fee_sibling:depth=0"))
    proof = I.solana_receipt_initiation(tx, owner, facts)
    assert "route_payment_atomic" not in proof and proof["route_payment_unknown"] == "fee_sibling:depth=0"
    with caplog.at_level(logging.WARNING):
        _buy, ok, _q = _resume(monkeypatch, None, unknown="fee_sibling:depth=0")
    assert not ok
    assert any("receipt resume refused route_payment_unknown:fee_sibling:depth=0" in r.getMessage()
               for r in caplog.records)


def test_two_swaps_under_one_invoker_read_as_a_large_sibling_share():
    """Gamma on 8c5562ef8: the class must tell fees (~0.1-2%) from a second swap (>5%) and name the
    invoking program, so the forward coverage loss grades per venue."""
    from sniper.services.memecoin_source_valuation import route_payment_evidence
    tx = _tx([_xfer(OWN, RTR, OWNER, 60_000_000, 2), _xfer(OWN, FEEW, OWNER, 40_000_000, 2),
              _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 2)])
    _route, why = route_payment_evidence(tx, OWNER, 100_000_000)
    assert why == "fee_sibling:d=0:p=Top:s=66.7%" and len(why) <= 48


# --- the counter-transfer one CPI level down (2026-10-07, 4EFmaKjg) ----------------------------------

def test_a_prop_amm_paying_back_from_a_nested_call_binds_its_leg():
    """4EFmaKjg (probablyinsider, DAMM v2 via DFlow, 23:27Z): the owner's 99.05 USDC goes to a prop AMM
    whose instruction pays the USDT back from a NESTED call (height 4), not beside the payment
    (height 3). The sibling rule saw no swap, every leg read 0, and the resume refused
    route_payment_unknown:no_swap_leg. The leg counts when the invoker's own call tree moves tokens
    from outside the route into it and moves nothing else out of a route account."""
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin/sol-route-dflow-nested-4EFmaKjg.json").read_text())
    owner = "7XUAo3ugnHFtUgUamYB2rxhXPDCgz8zSbTausvriAs3p"
    assert route_payment_atomic(tx, owner, 100_000_000) == 99_050_000   # the 0.95 USDC of fees stay out
    facts = value_source_buy(tx, owner, sol_usd=None, now=105.)
    proof = I.solana_receipt_initiation(tx, owner, facts)
    assert proof["route_payment_atomic"] == "99050000" and "route_payment_unknown" not in proof


def test_a_nested_payout_counts_only_when_nothing_else_leaves_the_route_under_that_invoker():
    nested = [_call(2), _xfer(OWN, RTR, OWNER, 99_000_000, 3), _call(3), _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 4)]
    assert route_payment_atomic(_hop_tx(nested), OWNER, 100_000_000) == 99_000_000
    # A fee out of a route account under the same invoker: unknown which outflow bought the coin.
    with_fee = [_call(2), _xfer(OWN, RTR, OWNER, 99_000_000, 3), _usdc(OWN, FEE_SINK, OWNER, 1_000_000, 3),
                _call(3), _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 4)]
    from sniper.services.memecoin_source_valuation import route_payment_evidence
    assert route_payment_evidence(_hop_tx(with_fee), OWNER, 100_000_000) == (None, "no_swap_leg")


def test_a_payment_into_a_router_hop_keeps_the_share_rule_even_with_a_nested_inflow():
    """The nested rule is for a payment straight to a pool. A payment into a route hop (an account
    that pays on) is judged by what reaches the pools, here 99 of 100: never counted whole."""
    into_hop = [_call(2), _usdc(OWN, ROUTER_ACCT, OWNER, 100_000_000, 3), _call(3),
                _xfer(POOLV, COIN_ACCT, OTHER, 5_000, 4)]
    onward = [_usdc(ROUTER_ACCT, FEE_SINK, OTHER, 1_000_000, 2),
              _call(2), _usdc(ROUTER_ACCT, POOL_USDC, OTHER, 99_000_000, 3), _xfer(POOLV, COIN_ACCT, OTHER, 1, 3)]
    assert route_payment_atomic(_hop_tx(into_hop, onward), OWNER, 100_000_000) == 99_000_000
