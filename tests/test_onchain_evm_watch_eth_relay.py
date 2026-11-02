"""Chain 1's Relay envelope buys reach the watcher's decode (Alpha GO 2026-10-17).

The chain-1 watcher detected 0 of 95 J7 buys by armed wallets in 7 days while every watched address
received the coin and 68 of the 72 transactions found were delivered to it as transfers. 69 of 72
were the Relay Permit2 envelope (the 4663/56/8453 shape: approval proxy 0xccc88a9d, a Relay EOA
paying USDC); the decode ran only on RELAY_ENVELOPE_CHAINS = 4663, 56, 8453, so each printed
outcome=no_buy. The Relay proxy and router on chain 1 carry the pinned code hashes.

Pins: a recorded chain-1 envelope buy decodes and prices (USDC at its Kraken mark) through the live
push path; the other router shape (0xab830fcc) stays no_buy; MEMECOIN_EVM_RELAY_ENVELOPE_CHAINS
empty is the set before chain 1, read per decode.
"""
import json
from pathlib import Path

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_evm_onchain_watch as ow
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).resolve().parent / "fixtures" / "memecoin"
                 / "eth_relay_envelope_buys_20261017.json").read_text())
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


@pytest.fixture(autouse=True)
def _chain_1(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_RELAY_ENVELOPE_CHAINS", raising=False)
    monkeypatch.setattr(ow, "_RELAY_CODE_CACHE", {})
    monkeypatch.setattr(ow, "_CHAIN_ID_PINNED", set())
    monkeypatch.setattr(w, "_PAYMENT_PRICES", {})
    monkeypatch.setattr(w, "_fetch_payment_price", lambda pair: {"USDCUSD": 0.9998}.get(pair))
    monkeypatch.setattr(w, "erc20_decimals", lambda mint, rpc_url: 6 if mint.lower() == USDC else 18)
    monkeypatch.setattr(w, "capture_unsupported_relay", lambda *a, **k: None)   # no fixture writes
    w._configure("1")
    yield
    w._configure("4663")


def _reads(case):
    tx, receipt = FX[case]["tx"], FX[case]["receipt"]

    def read(method, params):
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_getCode":
            return FX["code"][str(params[0]).lower()]
        if method == "eth_getTransactionByHash":
            return tx
        if method == "eth_getTransactionReceipt":
            return receipt
        raise AssertionError(method)
    return read


def _detect(case):
    wallet = FX["wallets"][case]
    tx = FX[case]["tx"]
    mint_log = next(log for log in FX[case]["receipt"]["logs"]
                    if len(log["topics"]) == 3 and log["topics"][0] == ow.TRANSFER_TOPIC
                    and ow._topic_address(log["topics"][2]) == wallet)
    outcome = {}
    row = w.push_detection("https://read.test", dict(mint_log, transactionHash=tx["hash"]),
                           {wallet: ("u-" + case, case)}, decimals_cache={}, quote_usd=2650.0,
                           now="2026-10-15T01:53:01+00:00", read_fn=_reads(case), outcome=outcome,
                           sleep_fn=lambda s: None)
    return row, outcome


def test_a_chain_1_envelope_buy_decodes_and_prices_on_the_live_push_path():
    row, outcome = _detect("envelope")
    assert outcome["reason"] == "detected" and row is not None, outcome
    buy = row["buy"]
    assert buy["receipt_proof_version"] == ow.RELAY_OTHER_TARGET_PROOF_VERSION
    assert buy["payment_token"] == USDC and outcome["payment_token"] == USDC
    paid = int(buy["payment_atomic"])
    assert buy["source_size_usd"] == pytest.approx(paid / 1e6 * 0.9998)
    assert buy["source_price"] == pytest.approx(buy["source_size_usd"] / buy["token_delta"])
    assert row["wallet"] == FX["wallets"]["envelope"]


def test_the_other_router_shape_stays_no_buy():
    row, outcome = _detect("other_router")
    assert row is None and outcome["reason"] == "no_buy"
    assert outcome["decode"] == "not_relay_envelope"


def test_the_knob_empty_is_the_set_before_chain_1_read_per_decode(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_RELAY_ENVELOPE_CHAINS", "")
    row, outcome = _detect("envelope")
    assert row is None and outcome["reason"] == "no_buy"
    assert outcome["decode"] == "unsupported_relayer_swap"


@pytest.mark.parametrize("raw, want", [
    (None, {"4663", "56", "8453", "1"}), ("", {"4663", "56", "8453"}),
    ("4663,x", {"4663", "56", "8453"}), ("1", {"1"}), ("4663, 56", {"4663", "56"})])
def test_the_chain_list_knob(monkeypatch, raw, want):
    if raw is not None:
        monkeypatch.setenv("MEMECOIN_EVM_RELAY_ENVELOPE_CHAINS", raw)
    assert cfg.memecoin_evm_relay_envelope_chains() == want
    assert ow.relay_envelope_chains() == want


def test_the_watcher_starts_with_chain_1_and_its_usdc_ticker():
    assert "1" in w.RELAY_ENVELOPE_CHAINS
    assert w._PAYMENT_TICKERS[("1", USDC)] == "USDCUSD"


# --- the sell side (Alpha GO 2026-10-17, lambda/eth-relay-sells) ---------------------------------

def test_a_chain_1_relay_sale_decodes_b_relay_with_its_proceeds_token():
    """61 of 62 J7-proved chain-1 sales by watched wallets are Relay sales inside a 4337 bundle: the
    coin goes to the Relay contract and USDC on to Relay. Without chain-1 proceeds tokens the b_relay
    proof had no proceeds token to see, and every sale decoded to []."""
    sell = FX["sell"]
    sales = ow.wallet_sales_from_receipt(sell["receipt"], sell["wallet"], chain="1", relay_verified=True)
    hit = [s for s in sales if s["mint"] == sell["mint"]]
    assert len(hit) == 1 and hit[0]["proof"] == "b_relay", sales
    assert hit[0]["sold_atomic"] == 4928362179709806593052756     # the wallet's whole outflow
    assert ow.wallet_sales_from_receipt(sell["receipt"], sell["wallet"], chain="1",
                                        relay_verified=False) == []    # unverified code proves nothing


def test_a_chain_1_proceeds_token_transfer_is_not_a_sale_candidate():
    wallet = FX["sell"]["wallet"]
    log = {"topics": [ow.TRANSFER_TOPIC, "0x" + "0" * 24 + wallet[2:], "0x" + "0" * 24 + "ee" * 20],
           "address": USDC}
    assert not ow.sell_candidate(log, {wallet: ("u", "h")}, "1")
    assert ow.sell_candidate(dict(log, address=FX["sell"]["mint"]), {wallet: ("u", "h")}, "1")
