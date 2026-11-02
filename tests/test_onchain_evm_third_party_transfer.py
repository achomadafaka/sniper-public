"""The 56 watcher's "unsupported_relayer_swap" refusals with a MINED transaction (2026-09-28).

Gamma's census from 20:55:04Z counted 3 of 21 relay-labelled refusals whose retry read a mined tx.
All three are REAL frames here (tests/fixtures/memecoin/bsc_third_party_transfers_20260928.json, read
from bsc-dataseed1.defibit.io). None is a Relay transaction and none is a buy: a third party sent a
batch token distribution (299, 22 and 4 recipients) and a watched wallet was one recipient. The
decode was right to refuse; the LABEL was wrong, and a census reading the label counted airdrops as
relay-decoder misses. A refusal now says `not_relay_envelope` when the transaction is not the Relay
Permit2 call at all, and keeps `unsupported_relayer_swap` for a Relay call whose shape fails."""
import copy
import gzip
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_onchain_watch as M

HERE = Path(__file__).parent
FIX = json.load(open(HERE / "fixtures" / "memecoin" / "bsc_third_party_transfers_20260928.json"))
RELAY = json.load(gzip.open(HERE / "fixtures" / "relay_purchases_2026-09-28.json.gz", "rt"))
CASES = [(f, w) for f in FIX["frames"] for w in f["wallets"]]


def _no_reads(method, params):
    raise AssertionError("a refused proof must not read the chain: %s" % method)


@pytest.mark.parametrize("frame,wallet", CASES, ids=lambda x: x["hash"][:12] if isinstance(x, dict) else x[:10])
def test_a_third_party_distribution_is_not_a_buy_and_says_so(frame, wallet):
    assert frame["tx"]["from"].lower() != wallet and int(frame["tx"]["value"], 16) == 0
    assert M.direct_buy_from_receipt(frame["receipt"], frame["tx"], wallet,
                                     weth="0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", quote_usd=600.0,
                                     decimals_fn=lambda mint: 18) is None
    evidence = {}
    assert M.read_relayer_purchase(frame["receipt"], frame["tx"], wallet, chain="56", read_fn=_no_reads,
                                   evidence=evidence, any_route=True, live=True) is None
    assert evidence["reason"] == "not_relay_envelope"


def test_a_relay_call_whose_shape_fails_keeps_its_label():
    b = next(b for b in RELAY["buys"] if b["chain"] == "56")
    evidence = {}
    assert M.read_relayer_purchase(b["receipt"], b["tx"], "0x" + "ee" * 20, chain="56", read_fn=_no_reads,
                                   evidence=evidence, any_route=True, live=True) is None
    assert evidence["reason"] == "unsupported_relayer_swap"


def test_the_watcher_line_names_the_real_reason():
    import scripts.research.onchain_evm_wallet_watch as w
    frame, wallet = CASES[-1]

    def read(method, params):
        return {"eth_getTransactionByHash": frame["tx"], "eth_getTransactionReceipt": frame["receipt"]}[method]
    log = next(lg for lg in frame["receipt"]["logs"] if lg["topics"] and lg["topics"][0] == M.TRANSFER_TOPIC
               and lg["topics"][2][-40:] == wallet[2:])
    w._configure("56")
    try:
        outcome = {}
        row = w.push_detection("http://unused", log, {wallet: ("uid", "h")}, decimals_cache={log["address"].lower(): 18},
                               quote_usd=600.0, now="2026-09-28T21:39:05+00:00", read_fn=read, outcome=outcome)
    finally:
        w._configure("4663")
    assert row is None and outcome["reason"] == "no_buy" and outcome["decode"] == "not_relay_envelope"
