from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC, source_events_from_receipt


WALLET = "0x" + "11" * 20
POOL_A = "0x" + "22" * 20
POOL_B = "0x" + "33" * 20
TOKEN = "0x" + "44" * 20
WETH = "0x4200000000000000000000000000000000000006"
HASH = "0x" + "aa" * 32


def transfer(token, sender, recipient, amount, index):
    return {"address": token, "topics": [TRANSFER_TOPIC,
            "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:]],
            "data": hex(amount), "logIndex": hex(index), "transactionHash": HASH}


def receipt(logs):
    return {"status": "0x1", "transactionHash": HASH, "blockHash": "0x" + "bb" * 32,
            "blockNumber": "0x123", "logs": logs}


def transaction():
    return {"hash": HASH, "from": WALLET, "blockHash": "0x" + "bb" * 32,
            "blockNumber": "0x123"}


def test_two_pool_sale_is_one_complete_economic_event():
    logs = [transfer(TOKEN, WALLET, POOL_A, 12, 0),
            transfer(TOKEN, WALLET, POOL_B, 8, 1),
            transfer(WETH, POOL_A, WALLET, 10**15, 2)]
    events = source_events_from_receipt(receipt(logs), transaction(),
                                        wallet=WALLET, chain="57073", quote_usd=2500.0,
                                        decimals_fn=lambda _mint: 18,
                                        prebalance_atomic={TOKEN.lower(): 100},
                                        source_timestamp=1790671038)
    assert len(events) == 1
    assert events[0]["side"] == "sell"
    assert events[0]["token_amount_atomic"] == "20"
    assert events[0]["source_prebalance_atomic"] == "100"
    assert events[0]["origin_key"] == f"57073:{HASH}:{WALLET}:{TOKEN}:sell"


def test_delayed_balance_is_never_inferred_as_source_prebalance():
    logs = [transfer(TOKEN, WALLET, POOL_A, 25, 0),
            transfer(WETH, POOL_A, WALLET, 10**15, 1)]
    events = source_events_from_receipt(receipt(logs), transaction(),
                                        wallet=WALLET, chain="57073", quote_usd=2500.0,
                                        decimals_fn=lambda _mint: 18,
                                        prebalance_atomic=None,
                                        source_timestamp=1790671038)
    assert events[0]["token_amount_atomic"] == "25"
    assert events[0]["source_prebalance_atomic"] is None
    assert events[0]["coverage_complete"] is False


def test_plain_transfer_and_failed_transaction_are_not_sales():
    logs = [transfer(TOKEN, WALLET, POOL_A, 20, 0)]
    kwargs = dict(wallet=WALLET, chain="57073", quote_usd=2500.0,
                  decimals_fn=lambda _mint: 18, prebalance_atomic=None,
                  source_timestamp=1790671038)
    assert source_events_from_receipt(receipt(logs), transaction(), **kwargs) == []
    assert source_events_from_receipt({**receipt(logs), "status": "0x0"},
                                      transaction(), **kwargs) == []


def test_old_canonical_buy_keeps_source_clock_and_exact_quantity():
    logs = [transfer(WETH, WALLET, POOL_A, 10**15, 0),
            transfer(TOKEN, POOL_A, WALLET, 2 * 10**18, 1)]
    event = source_events_from_receipt(receipt(logs), transaction(),
                                       wallet=WALLET, chain="57073", quote_usd=2500.0,
                                       decimals_fn=lambda _mint: 18, prebalance_atomic=None,
                                       source_timestamp=1790671038)[0]
    assert event["side"] == "buy"
    assert event["token_amount_atomic"] == str(2 * 10**18)
    assert event["quote_amount_atomic"] == str(10**15)
    assert event["source_timestamp"] == 1790671038


def test_missing_block_time_cannot_be_restamped_as_fresh():
    logs = [transfer(WETH, WALLET, POOL_A, 10**15, 0),
            transfer(TOKEN, POOL_A, WALLET, 2 * 10**18, 1)]
    assert source_events_from_receipt(receipt(logs), transaction(),
                                      wallet=WALLET, chain="57073", quote_usd=2500.0,
                                      decimals_fn=lambda _mint: 18, prebalance_atomic=None,
                                      source_timestamp=None) == []
