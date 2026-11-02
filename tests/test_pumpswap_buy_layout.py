"""pAMM buy_exact_quote_in takes 26 accounts on a pool with a coin creator and 25 on one without
(Gamma FAIL c63be39c42dd, 2026-10-07). With the default coin_creator the program reads remaining
account [23] as the buyback fee recipient; our pool-v2 PDA there reverted 6053
BuybackFeeRecipientNotAuthorized (probablyinsider 4g2ToCku 10-07, Roveryaps 22hNSUXL 09-30).
Read across 43 source buys on 43 pools: 41 real-creator pools all 26 with pool-v2 at [23], the two
default-creator pools both 25. The fixtures are four of those real instructions."""
import json
from pathlib import Path

import pytest

from sniper.execution import pumpswap_buy as pb

FIXTURES = json.loads((Path(__file__).parent / "fixtures/pumpswap_buy_layouts.json").read_text())
CASES = sorted(k for k in FIXTURES if not k.startswith("_"))


def _ours(f):
    acc = f["accounts"]
    facts = dict(pb.parse_pool_account(f["pool_account_b64"]),
                 base_token_program=acc[11][0], quote_token_program=acc[12][0])
    buyback = acc[23][0] if len(acc) == 25 else acc[24][0]     # the source's own pick from the list
    return [[str(k), w] for k, s, w in pb.pumpswap_buy_accounts(
        user=acc[1][0], pool=acc[0][0], pool_facts=facts, protocol_fee_recipient=acc[9][0],
        protocol_fee_recipient_token_account=acc[10][0], buyback_fee_recipient=buyback)]


@pytest.mark.parametrize("case", CASES)
def test_our_buy_accounts_match_the_real_instruction(case):
    f = FIXTURES[case]
    ours, theirs = _ours(f), f["accounts"]
    assert len(ours) == len(theirs) == int(case[:2])
    assert [k for k, _ in ours] == [k for k, _ in theirs]
    # Writability past the IDL's 23. Before it, tx-level flags carry other instructions' needs (two
    # sources mark global_volume_accumulator writable; our read-only one filled 42 times).
    assert [w for _, w in ours[23:]] == [w for _, w in theirs[23:]]


def test_the_layout_is_chosen_by_the_pools_coin_creator():
    for case in CASES:
        facts = pb.parse_pool_account(FIXTURES[case]["pool_account_b64"])
        assert (facts["coin_creator"] == "11111111111111111111111111111111") == case.startswith("25_")
