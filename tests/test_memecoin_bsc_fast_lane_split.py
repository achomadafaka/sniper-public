"""BSC 56 fast lane v1.1: Relay-solver SPLIT routes (spec 2026-10-02-memecoin-bsc-56-fast-lane-design.md).
11 of 79 armed 56 detections fell back `leg_does_not_bind`: the solver bought the coin from our
CREATE2 PCS V2 pair AND other venues in one transaction, so the owner's net coin exceeds the pair's
payout and the shared decoder (the funded chase reference) rightly refuses to bind one leg to the whole
buy. The lane buys on the V2 pair only, so it re-derives that pair's leg from the receipt itself.
Real receipts (trimmed to the logs the decoders read): tests/fixtures/memecoin/bsc_split_route_receipts.json."""
import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sniper.services import memecoin_bsc_fast_lane as bsc  # noqa: E402
from sniper.services import memecoin_evm_post_trade as pt  # noqa: E402

CASES = json.load(open(Path(__file__).parent / "fixtures" / "memecoin" /
                       "bsc_split_route_receipts.json"))["cases"]


def _case(prefix):
    (tx, case), = [(t, c) for t, c in CASES.items() if t.startswith(prefix)]
    return tx, case


def _decide(case, *, owner=None, hop=None):
    post = pt.source_post_trade("56", case["receipt"], case["mint"], owner=case["owner"])
    return post, bsc.decide_from_receipt(
        case["receipt"], mint=case["mint"], evm_post=post, chase_pct=30, premium=1.5, seq=10 ** 9,
        hop_state_fn=hop or (lambda pair, seq: (7 * 10 ** 21, 3 * 10 ** 21, 10 ** 9 - 1)),
        owner=case["owner"] if owner is None else owner)


@pytest.mark.parametrize("prefix", ["0xc72660a2", "0x53b149b4", "0x79567b69"])
def test_a_split_route_buys_on_its_v2_pair_leg(prefix):
    tx, case = _case(prefix)
    post, out = _decide(case)
    assert post["reason"] == "leg_does_not_bind"               # the shared decoder is unchanged
    assert out["reason"] == "would_send", out
    assert out["split"] is True and bsc.our_pair(case["mint"], out["quote"]) == out["pool"]
    assert out["hop"] and out["hop"]["source"] == "tape"
    assert out["src_out"] > 0 and out["cap"] > 0


def test_the_leg_is_the_pairs_own_swap_and_its_exact_tax():
    tx, case = _case("0xc72660a2")
    _post, out = _decide(case)
    r = case["receipt"]
    (swap,) = [l for l in r["logs"] if l["address"].lower() == out["pool"]
               and l["topics"][0].lower() == pt.V2_SWAP]
    a0in, a1in, a0out, a1out = [int(swap["data"][2 + i:66 + i], 16) for i in range(0, 256, 64)]
    assert (out["src_in"], out["src_out"]) == ((a0in, a1out) if out["coin1"] else (a1in, a0out))
    assert out["tax"] == 0                                    # the pair paid one transfer, untaxed


def test_two_v2_pairs_paying_the_coin_is_not_one_leg():
    import copy
    tx, case = _case("0xc72660a2")
    two = copy.deepcopy(case)
    fake = "0x" + "ab" * 20
    two["receipt"]["logs"] += [
        {"address": case["mint"], "topics": [pt.TRANSFER, "0x" + "0" * 24 + fake[2:],
                                             "0x" + "0" * 24 + case["owner"][2:]],
         "data": hex(10 ** 18), "logIndex": "0x999"},
        {"address": fake, "topics": [pt.V2_SWAP, "0x" + "0" * 64, "0x" + "0" * 64],
         "data": "0x" + "0" * 63 + "1" + "0" * 64 + "0" * 64 + "0" * 63 + "1", "logIndex": "0x99a"}]
    _post, out = _decide(two)
    assert out["reason"] == "split:no_single_v2_leg"


def test_an_owner_who_got_less_than_the_pairs_payout_is_not_bound():
    tx, case = _case("0xc72660a2")
    _post, out = _decide(case, owner="0x" + "12" * 20)        # received none of it
    assert out["reason"] == "split:owner_short"


def test_without_an_owner_the_split_leg_is_never_taken():
    tx, case = _case("0xc72660a2")
    post = pt.source_post_trade("56", case["receipt"], case["mint"], owner=case["owner"])
    out = bsc.decide_from_receipt(case["receipt"], mint=case["mint"], evm_post=post, chase_pct=30,
                                  premium=1.5, seq=10 ** 9, hop_state_fn=lambda p, s: None)
    assert out["reason"] == "post:leg_does_not_bind"


def test_a_tax_to_the_token_is_exact_and_one_to_anyone_else_stays_unknown():
    import copy
    tx, case = _case("0x74d0031f")                          # the pair pays 1% to the token itself
    _post, out = _decide(case)
    assert out["reason"] == "would_send" and 0 < out["tax"] < Fraction(2, 100)
    other = copy.deepcopy(case)                             # the same 1% to a fee wallet instead
    for log in other["receipt"]["logs"]:
        if (log["address"].lower() == case["mint"] and log["topics"][0].lower() == pt.TRANSFER
                and log["topics"][2][-40:].lower() == case["mint"][2:]):
            log["topics"][2] = "0x" + "0" * 24 + "77" * 20
    _post, out = _decide(other)
    assert out["reason"] == "tax_unknown"                   # the funded rule: never guessed


def test_a_quote_without_a_hop_state_falls_back():
    tx, case = _case("0x2606ccf3")                           # quoted in 0x21caef8a (no hop pair)
    _post, out = _decide(case, hop=lambda pair, seq: None)
    assert out["reason"] == "hop_state_stale:unknown" and out.get("split") is True


def test_the_watcher_passes_the_source_wallet_as_owner():
    import inspect
    src = inspect.getsource(bsc.make_watcher_signal)
    assert 'owner=row.get("wallet")' in src



# ---- Gamma LOWs on 6e88a9c62 ----

def test_an_owner_who_forwards_the_coin_on_in_the_same_transaction_is_short():
    import copy
    tx, case = _case("0xc72660a2")
    fwd = copy.deepcopy(case)
    _post, out = _decide(case)
    got = out["src_out"]
    fwd["receipt"]["logs"].append(                            # the owner passes 20% straight on
        {"address": case["mint"], "topics": [pt.TRANSFER, "0x" + "0" * 24 + case["owner"][2:],
                                             "0x" + "0" * 24 + "cd" * 20],
         "data": hex(got // 5), "logIndex": "0x998"})
    _post, out = _decide(fwd)
    assert out["reason"] == "split:owner_short"


def test_the_datagram_says_whether_the_decision_was_a_split_route():
    from test_memecoin_bsc_fast_lane import ROW, _decide as v1_decide, _synthetic
    tx, case = _case("0xc72660a2")
    _post, out = _decide(case)
    row = dict(ROW, buy=dict(ROW["buy"], mint=case["mint"]))
    assert bsc.lane_payload(row, out, decimals=18, bnb_usd=600.0, bnb_usd_at=1.0,
                            sent_epoch=1.0)["split"] is True
    receipt, mint, post = _synthetic()
    plain = v1_decide(receipt, mint, post)
    assert bsc.lane_payload(dict(ROW, buy=dict(ROW["buy"], mint=mint)), plain, decimals=18,
                            bnb_usd=600.0, bnb_usd_at=1.0, sent_epoch=1.0)["split"] is False
