"""EVM wallet receipt classification for the direct funded detection lane."""
import importlib.util
import sqlite3
from pathlib import Path

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_evm_onchain_watch import direct_buy_from_receipt, profile_evm_address


WALLET = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73".lower()
MINT = "0x" + "33" * 20
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _address_topic(address):
    return "0x" + address.removeprefix("0x").rjust(64, "0")


def _transfer(token, sender, receiver, amount):
    return {
        "address": token,
        "topics": [TRANSFER, _address_topic(sender), _address_topic(receiver)],
        "data": "0x" + format(amount, "064x"),
    }


def _watch_script():
    path = Path("scripts/research/onchain_evm_wallet_watch.py")
    spec = importlib.util.spec_from_file_location("test_evm_onchain_watch_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watcher_uses_the_runtime_state_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DB_PATH", raising=False)

    watcher = _watch_script()

    assert watcher.DB == tmp_path / "sniper_tracker.db"
    assert watcher.HEARTBEAT == tmp_path / "data" / "onchain_evm_watch.heartbeat"


def test_the_watch_list_is_the_verified_book_not_a_fomo_profile(monkeypatch):
    """The profile-address path is gone (2026-09-16): refuted on 45 of 50 checks, and the fomo
    account is banned. The watcher reads proven addresses out of the ledger instead."""
    watcher = _watch_script()
    assert not hasattr(watcher, "sync_addresses")
    assert not hasattr(watcher, "get_profile")


def test_receipt_detects_a_weth_paid_token_buy_with_source_price():
    receipt = {
        "status": "0x1",
        "logs": [
            _transfer(WETH, WALLET, ROUTER, 10 ** 18),
            _transfer(MINT, ROUTER, WALLET, 200 * 10 ** 18),
        ],
    }
    tx = {"from": WALLET, "value": "0x0"}

    buy = direct_buy_from_receipt(
        receipt, tx, WALLET, weth=WETH, quote_usd=2500.0, decimals_fn=lambda _mint: 18)

    assert buy == {
        "mint": MINT,
        "token_delta": 200.0,
        "source_size_usd": 2500.0,
        "source_price": 12.5,
        "source_initiation": None,  # Observation survives missing mined transaction identity.
    }


def test_receipt_refuses_an_ambiguous_token_output():
    receipt = {
        "status": "0x1",
        "logs": [
            _transfer(WETH, WALLET, ROUTER, 10 ** 18),
            _transfer(MINT, ROUTER, WALLET, 200 * 10 ** 18),
            _transfer("0x" + "44" * 20, ROUTER, WALLET, 300 * 10 ** 18),
        ],
    }

    assert direct_buy_from_receipt(
        receipt, {"from": WALLET, "value": "0x0"}, WALLET,
        weth=WETH, quote_usd=2500.0, decimals_fn=lambda _mint: 18) is None


def test_profile_address_accepts_only_one_evm_address():
    assert profile_evm_address({"evmAddress": WALLET}) == WALLET
    assert profile_evm_address({"evmAddress": "not-an-address"}) is None


def test_relayer_requires_wallet_payment_and_net_acquisition():
    receipt = {"status": "0x1", "logs": [
        _transfer(WETH, WALLET, ROUTER, 10 ** 18),
        _transfer(MINT, ROUTER, WALLET, 200 * 10 ** 18)]}
    buy = direct_buy_from_receipt(receipt, {"from": ROUTER}, WALLET,
                                 weth=WETH, quote_usd=2500, decimals_fn=lambda _: 18)
    assert buy and buy["token_delta"] == 200
    receipt["logs"].append(_transfer(MINT, WALLET, ROUTER, 200 * 10 ** 18))
    assert direct_buy_from_receipt(receipt, {"from": WALLET}, WALLET,
                                  weth=WETH, quote_usd=2500, decimals_fn=lambda _: 18) is None


def test_quote_self_transfer_and_refund_are_not_payment():
    for quote_logs in [
        [_transfer(WETH, WALLET, WALLET, 10 ** 18)],
        [_transfer(WETH, WALLET, ROUTER, 10 ** 18),
         _transfer(WETH, ROUTER, WALLET, 10 ** 18)],
    ]:
        receipt = {"status": "0x1", "logs": quote_logs + [
            _transfer(MINT, ROUTER, WALLET, 200 * 10 ** 18)]}
        assert direct_buy_from_receipt(receipt, {"from": WALLET}, WALLET,
                                      weth=WETH, quote_usd=2500, decimals_fn=lambda _: 18) is None


def test_payment_to_an_unrelated_party_does_not_prove_a_swap():
    receipt = {"status": "0x1", "logs": [
        _transfer(WETH, WALLET, "0x" + "99" * 20, 10 ** 18),
        _transfer(MINT, ROUTER, WALLET, 200 * 10 ** 18)]}
    assert direct_buy_from_receipt(receipt, {"from": WALLET}, WALLET,
                                  weth=WETH, quote_usd=2500, decimals_fn=lambda _: 18) is None


def test_push_detects_a_verified_wallet_that_paid_through_a_relayer():
    watcher = _watch_script()
    receipt = {"status": "0x1", "logs": [
        _transfer(watcher.WETH, WALLET, ROUTER, 10 ** 18),
        _transfer(MINT, ROUTER, WALLET, 200 * 10 ** 18)]}
    def read(method, _params):
        return {"from": ROUTER} if method == "eth_getTransactionByHash" else receipt
    row = watcher.push_detection("offline", {"transactionHash": "0xtx"},
                                  {WALLET: ("u1", "alice")}, decimals_cache={MINT: 18},
                                  quote_usd=2500, now="2026-09-16", read_fn=read)
    assert row and row["wallet"] == WALLET and row["source_user_id"] == "u1"


def test_net_zero_router_token_leg_cannot_bind_a_stranger_gift():
    receipt = {'status':'0x1','logs':[
        _transfer(MINT,ROUTER,WALLET,1),
        _transfer(MINT,WALLET,ROUTER,1),
        _transfer(MINT,'0x'+'99'*20,WALLET,1000),
        _transfer(WETH,WALLET,ROUTER,10**18)]}
    assert direct_buy_from_receipt(receipt,{'from':WALLET},WALLET,weth=WETH,
                                  quote_usd=2500,decimals_fn=lambda _:0) is None


def test_native_payment_to_token_counterparty_remains_supported():
    receipt = {'status':'0x1','logs':[_transfer(MINT,ROUTER,WALLET,200*10**18)]}
    tx = {'from':WALLET,'to':ROUTER,'value':hex(10**18)}
    buy = direct_buy_from_receipt(receipt,tx,WALLET,weth=WETH,
                                 quote_usd=2500,decimals_fn=lambda _:18)
    assert buy and buy['source_size_usd'] == 2500 and buy['token_delta'] == 200
    tx['to'] = '0x'+'99'*20
    assert direct_buy_from_receipt(receipt,tx,WALLET,weth=WETH,
                                  quote_usd=2500,decimals_fn=lambda _:18) is None
