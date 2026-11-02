"""EVM on-chain SELL path, SHADOW build (Alpha GO on aef3d5719, 2026-09-29).

Spec docs/superpowers/specs/2026-09-29-memecoin-evm-onchain-sell-path-design.md. The watcher records
a source's own sale with the proof that it IS a sale; the mirror does not act on it. Proof (a): the
wallet received proceeds in the same transaction (an ERC-20 proceeds token, or native by balance
delta). Proof (b): a Relay-envelope swap, the coin sent to the pinned Relay contracts and a proceeds
token swapped back toward them. The fixture is a REAL J7-confirmed sale (56, badabeepp), whose USDC
went to Relay and never to the wallet.
"""
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_onchain_watch as W

FIXTURE = json.load(open(Path(__file__).parent / "fixtures" / "memecoin"
                         / "evm_sell_relay_56_badabeepp.json"))
WALLET = FIXTURE["wallet"]
COIN = "0xa92115ecef64c943a1d878ab120ec4a5908a7777"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
PAIR = "0x" + "77" * 20
OTHER = "0x" + "66" * 20


def _topic(a):
    return "0x" + "0" * 24 + a[2:]


def _transfer(token, frm, to, amount):
    return {"address": token, "topics": [W.TRANSFER_TOPIC, _topic(frm), _topic(to)],
            "data": "0x%064x" % amount}


def _receipt(*logs, status="0x1"):
    return {"status": status, "logs": list(logs)}


def test_the_real_relay_sale_is_proved_by_the_envelope_swap():
    sales = W.wallet_sales_from_receipt(FIXTURE["receipt"], WALLET, chain="56", relay_verified=True)
    assert len(sales) == 1, sales
    sale = sales[0]
    assert sale["mint"] == COIN and sale["proof"] == "b_relay" and sale["sold_atomic"] > 0
    # the proceeds went to Relay, never to the wallet: proof (a) cannot hold on this receipt
    assert not any(W._topic_address(lg["topics"][2]) == WALLET
                   for lg in FIXTURE["receipt"]["logs"]
                   if len(lg["topics"]) == 3 and lg["topics"][0] == W.TRANSFER_TOPIC
                   and lg["address"].lower() != COIN)


def test_the_relay_proof_needs_the_pinned_code_verified():
    assert W.wallet_sales_from_receipt(FIXTURE["receipt"], WALLET, chain="56",
                                       relay_verified=False) == []


def test_a_plain_transfer_to_another_wallet_is_not_a_sale():
    r = _receipt(_transfer(COIN, WALLET, OTHER, 10 ** 18))
    assert W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=True) == []
    assert W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=True,
                                       native_delta=0) == []


def test_a_direct_sell_paid_in_a_proceeds_token_is_proof_a():
    r = _receipt(_transfer(COIN, WALLET, PAIR, 10 ** 18), _transfer(WBNB, PAIR, WALLET, 5 * 10 ** 16))
    sales = W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=False)
    assert sales == [{"mint": COIN, "sold_atomic": 10 ** 18, "proof": "a_erc20",
                      "proceeds_token": WBNB, "proceeds_atomic": 5 * 10 ** 16}]


def test_a_sell_paid_in_native_is_proved_by_the_balance_delta():
    r = _receipt(_transfer(COIN, WALLET, PAIR, 10 ** 18), _transfer(WBNB, PAIR, OTHER, 5 * 10 ** 16))
    assert W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=False) == []
    sales = W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=False,
                                        native_delta=5 * 10 ** 16)
    assert sales[0]["proof"] == "a_native" and sales[0]["proceeds_token"] == "native"


def test_a_buys_pay_leg_is_not_a_sale():
    usdc = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
    r = _receipt(_transfer(usdc, WALLET, PAIR, 10 ** 18), _transfer(COIN, PAIR, WALLET, 10 ** 20))
    assert W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=True,
                                       native_delta=0) == []


def test_a_reverted_receipt_is_nothing():
    r = _receipt(_transfer(COIN, WALLET, PAIR, 10 ** 18), _transfer(WBNB, PAIR, WALLET, 5),
                 status="0x0")
    assert W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=True) == []


def test_two_coins_sold_in_one_tx_are_two_sales_with_unattributed_proceeds():
    coin2 = "0x" + "c2" * 20
    r = _receipt(_transfer(COIN, WALLET, PAIR, 10 ** 18), _transfer(coin2, WALLET, PAIR, 3),
                 _transfer(WBNB, PAIR, WALLET, 7))
    sales = W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=False)
    assert sorted(s["mint"] for s in sales) == sorted([COIN, coin2])
    assert all(s["proof"] == "a_erc20" and s["proceeds_atomic"] is None for s in sales)


@pytest.mark.parametrize("token, frm, removed, want", [
    (COIN, WALLET, False, True),
    (WBNB, WALLET, False, False),            # the chain quote leaving the wallet: a buy's pay leg
    ("0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", WALLET, False, False),   # Relay payment token
    (COIN, OTHER, False, False),             # not a watched sender
    (COIN, WALLET, True, False),             # reorged out
])
def test_the_arrival_prefilter(token, frm, removed, want):
    log = dict(_transfer(token, frm, PAIR, 1), removed=removed)
    assert W.sell_candidate(log, {WALLET}, "56") is want


# --- the push stream carries the sell filter on the SAME socket ----------------------------------------

def _push_run(on_sell_transfer, script_extra):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from test_memecoin_onchain_push_lanes import (ARMED, MANAGER, _clock, _connect, _push,
                                                  _stop_after, _sub_reply)
    from sniper.execution import evm_v4_quote as V
    from sniper.services import memecoin_onchain_push as P
    sent, buys = [], []
    script = [_sub_reply(1, "0xheads"), _push("0xheads", {"number": "0x1"})] + script_extra(_sub_reply, _push)
    connect_fn = _connect([script], sent)
    P.run_push_stream("wss://x", on_transfer=buys.append, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER,
                      init_topic=V._INIT_TOPIC, connect_fn=connect_fn, sleep_fn=lambda _s: None,
                      now_fn=_clock(step=0.0), should_stop=_stop_after(connect_fn, 2),
                      on_sell_transfer=on_sell_transfer)
    msgs = [m if isinstance(m, dict) else json.loads(m) for m in sent]
    subs = [m["params"] for m in msgs if m.get("method") == "eth_subscribe"]
    return subs, buys, ARMED


def test_the_sell_filter_names_the_watched_wallets_as_senders():
    from sniper.services import memecoin_onchain_push as P
    params = P.sell_transfer_filter(["0x" + "11" * 20])
    assert params == ["logs", {"topics": [P.TRANSFER_TOPIC, ["0x" + "0" * 24 + "11" * 20], None]}]
    assert P.sell_transfer_filter([]) is None


def test_a_sell_handler_adds_the_filter_and_gets_only_its_own_logs():
    sells = []
    log = {"address": COIN, "topics": [W.TRANSFER_TOPIC, "0x" + "0" * 24 + "11" * 20, _topic(PAIR)],
           "data": "0x1"}
    subs, buys, armed = _push_run(sells.append, lambda reply, push: [
        reply(2, "0xtransfers"), reply(3, "0xinit"), reply(4, "0xsells"), push("0xsells", log)])
    assert subs[-1] == ["logs", {"topics": [W.TRANSFER_TOPIC, ["0x" + "0" * 24 + armed[2:]], None]}]
    assert sells == [log] and buys == []


def test_without_a_sell_handler_the_socket_carries_todays_filters():
    subs, _buys, _armed = _push_run(None, lambda reply, push: [])
    assert subs and not any(s[0] == "logs" and len(s[1].get("topics") or []) > 1
                            and s[1]["topics"][1] is not None for s in subs), subs


# --- the watcher: decode on its own worker, write the SHADOW table, never wake the mirror -----------

def _watch_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "evmwatch_sell_test", Path(__file__).resolve().parents[1] / "scripts/research/onchain_evm_wallet_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._configure("56")
    return mod


def _shadow_conn(tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_onchain_evm_sell_shadow_table(conn)
    return conn


def _reader(receipt, tx, *, balances=None, token_balance=0):
    calls = []

    def read(method, params):
        calls.append(method)
        if method == "eth_getTransactionReceipt":
            return receipt
        if method == "eth_getTransactionByHash":
            return tx
        if method == "eth_getBalance":
            return hex((balances or {}).get(params[1], 0))
        if method == "eth_call":
            return "0x%064x" % token_balance
        raise AssertionError(method)
    return read, calls


def test_the_real_relay_sale_is_written_to_the_shadow_table_with_its_proof(tmp_path, monkeypatch):
    mod = _watch_module()
    monkeypatch.setattr(mod, "relay_code_verified", lambda chain, clock=None: True)
    conn = _shadow_conn(tmp_path)
    log = next(lg for lg in FIXTURE["receipt"]["logs"]
               if len(lg["topics"]) == 3 and W._topic_address(lg["topics"][1]) == WALLET)
    log = dict(log, transactionHash=FIXTURE["receipt"]["transactionHash"],
               blockNumber=FIXTURE["receipt"]["blockNumber"])
    read, calls = _reader(FIXTURE["receipt"], FIXTURE["transaction"], token_balance=5)
    sales = mod.decode_sell(log, watched={WALLET: ("u-bada", "badabeepp")}, read_fn=read, conn=conn,
                            arrival="2026-09-29T02:05:38.602+00:00")
    assert [s["proof"] for s in sales] == ["b_relay"]
    assert "eth_getBalance" not in calls, "a proved Relay sale needs no native read"
    row = dict(conn.execute("SELECT * FROM onchain_evm_sell_shadow").fetchone())
    assert (row["network_id"], row["tx_hash"], row["address"], row["mint"], row["proof"]) == (
        "56", FIXTURE["receipt"]["transactionHash"], WALLET, COIN, "b_relay")
    assert row["source_user_id"] == "u-bada" and row["post_atomic"] == "5"
    assert int(row["pre_atomic"]) == 5 + int(row["sold_atomic"]), "atomics are TEXT, exact"
    assert row["detected_at"] == "2026-09-29T02:05:38.602+00:00"


def test_a_plain_transfer_writes_nothing_after_the_native_check(tmp_path):
    mod = _watch_module()
    conn = _shadow_conn(tmp_path)
    receipt = dict(_receipt(_transfer(COIN, WALLET, OTHER, 10 ** 18)), transactionHash="0xplain",
                   blockNumber="0x10", gasUsed="0x1", effectiveGasPrice="0x1")
    log = dict(receipt["logs"][0], transactionHash="0xplain", blockNumber="0x10")
    read, calls = _reader(receipt, {"from": WALLET}, balances={"0x10": 100, "0xf": 101})
    assert mod.decode_sell(log, watched={WALLET: ("u", "h")}, read_fn=read, conn=conn,
                           arrival="2026-09-29T03:00:00+00:00") == []
    assert calls.count("eth_getBalance") == 2
    assert conn.execute("SELECT COUNT(*) FROM onchain_evm_sell_shadow").fetchone()[0] == 0


def test_the_shadow_sell_rows_never_reach_the_solana_mirror_reader(tmp_path):
    """The Solana mirror reads onchain_wallet_sells_v2; the shadow writes only its own table. Since
    2026-10-07 it wakes the runner's on-chain lane, whose EVM sell mirror reads the shadow for
    listed wallets only (test_memecoin_bsc_sniper_arm.py)."""
    import inspect
    mod = _watch_module()
    src = inspect.getsource(mod.decode_sell)
    assert "onchain_evm_sell_shadow" in src or "record_onchain_evm_sell_shadow" in src
    assert "record_onchain_sell(" not in src


def test_the_shadow_is_off_by_default_and_named_per_chain():
    from sniper.settings import memecoin as cfg
    assert cfg.memecoin_onchain_evm_sell_shadow_chains({}) == frozenset()
    assert cfg.memecoin_onchain_evm_sell_shadow_chains(
        {"MEMECOIN_ONCHAIN_EVM_SELL_SHADOW_CHAINS": "bsc, 8453"}) == {"56", "8453"}


def test_the_watcher_hands_the_socket_a_sell_handler_only_under_the_knob():
    import inspect
    src = inspect.getsource(_watch_module().run_push)
    assert "on_sell_transfer=on_sell_transfer if sell_shadow else None" in src
    assert "memecoin_onchain_evm_sell_shadow_chains()" in src


# --- Gamma MED: the sell reads never touch the funded custody RPC ----------------------------------

def test_the_sell_reads_use_the_tx_read_host_and_never_custody(monkeypatch):
    mod = _watch_module()
    custody = "https://rpc.mainnet.chain.robinhood.com/k"
    monkeypatch.setattr(mod, "evm_rpc_for_chain", lambda chain: custody)
    for var in ("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", "MEMECOIN_ONCHAIN_EVM_RPC_URL_56"):
        monkeypatch.delenv(var, raising=False)
    assert mod.sell_read_url("56") is None, "nothing but custody: read nothing"
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_56", custody + "/")
    assert mod.sell_read_url("56") is None, "the watcher's own host IS custody (4663 today)"
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_56", "https://bsc-dataseed1.defibit.io")
    assert mod.sell_read_url("56") == "https://bsc-dataseed1.defibit.io"
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", "https://bsc-rpc.publicnode.com")
    assert mod.sell_read_url("56") == "https://bsc-rpc.publicnode.com", "the tx-read host first"


def test_the_sell_handler_reads_through_the_sell_host_only():
    import inspect
    src = inspect.getsource(_watch_module().run_push)
    assert "read_url = sell_read_url()" in src and '_sell_count("no_read_host")' in src
    assert "read_fn=_sell_read_fn(read_url, fallback=sell_fallback_url())" in src


# --- Gamma LOW: the native proof is block-level, and says so --------------------------------------

def test_the_native_proof_is_block_level_a_known_limit():
    """A plain transfer in a block where the wallet ALSO received native from another transaction
    reads as a_native: the balance delta cannot tell the two apart. Pinned so the limit is explicit;
    the shadow read reports a_native on its own for that reason."""
    r = _receipt(_transfer(COIN, WALLET, OTHER, 10 ** 18))
    sales = W.wallet_sales_from_receipt(r, WALLET, chain="56", relay_verified=False,
                                        native_delta=5 * 10 ** 16)
    assert [s["proof"] for s in sales] == ["a_native"]


# --- Gamma blocker (2026-09-29): sell reads never enter the funded RPC pool --------------------------
# publicnode is an EXACT member of MEMECOIN_EVM_RPC_POOL_URLS_4663/_8453, so evm_wallet._rpc expanded a
# sell read to the funded pool (custody, preconf, Alchemy), hedged it, and let a sell-side 429 penalize
# and quarantine members in the watcher's process-wide pool state.

def test_a_pooled_sell_url_is_read_by_direct_post_and_never_touches_the_pool(monkeypatch):
    mod = _watch_module()
    from sniper.execution import evm_rpc_pool, evm_wallet

    def forbidden(*_a, **_k):
        raise AssertionError("the sell path reached the funded pool")
    for name in ("pool_for", "choose", "penalize", "record", "note_refusal", "chain_of"):
        monkeypatch.setattr(evm_rpc_pool, name, forbidden)
    monkeypatch.setattr(evm_wallet, "_rpc", forbidden)
    monkeypatch.setattr(mod, "_rpc", forbidden)
    monkeypatch.setattr(mod, "_counted_rpc", forbidden)
    posts = []
    answers = [(429, None), (200, {"jsonrpc": "2.0", "id": 1, "result": {"status": "0x1"}})]

    def post(url, body, timeout):
        posts.append((url, body["method"]))
        return answers.pop(0)
    monkeypatch.setattr(mod, "_http_post", post)
    mod._SELL_HOSTS.clear()
    clock = [0.0]
    read = mod._sell_read_fn("https://base-rpc.publicnode.com", clock=lambda: clock[0])
    assert read("eth_getTransactionReceipt", ["0xt"]) is None, "a 429 is no answer"
    assert read("eth_getTransactionReceipt", ["0xt"]) is None, "and cools this host only"
    clock[0] += 61.0
    assert read("eth_getTransactionReceipt", ["0xt"]) == {"status": "0x1"}
    assert posts == [("https://base-rpc.publicnode.com", "eth_getTransactionReceipt")] * 2


def test_the_sell_handler_reads_through_its_own_transport():
    import inspect
    src = inspect.getsource(_watch_module().run_push)
    assert "read_fn=_sell_read_fn(read_url, fallback=sell_fallback_url())" in src
    assert "_counted_rpc(read_url" not in src


def test_the_cooling_line_names_the_reader(monkeypatch):
    """Gamma LOW: the buy reader falls back to the read host, the sell reader has no fallback. A
    4663 log read must tell which was cooled."""
    mod = _watch_module()
    said = []
    monkeypatch.setattr(mod, "_say", said.append)
    monkeypatch.setattr(mod, "_http_post", lambda url, body, timeout: (429, None))
    mod._SELL_HOSTS.clear()
    mod._sell_read_fn("https://x.test")("eth_getTransactionReceipt", ["0xt"])
    buy = mod.TxReadHost("https://y.test", cooldown_s=60, timeout_s=1, post_fn=lambda *a: (429, None))
    buy.read("eth_getTransactionReceipt", ["0xt"])
    assert "sell" in said[0] and "no fallback" in said[0], said
    assert "buy" in said[1] and "read host meanwhile" in said[1], said


def test_a_written_sale_wakes_the_runner_lane_and_a_non_sale_does_not(tmp_path, monkeypatch):
    """2026-10-07: the runner's on-chain lane mirrors a listed wallet's sale; the watcher wakes it
    after the write, as it does after a detection."""
    mod = _watch_module()
    monkeypatch.setattr(mod, "relay_code_verified", lambda chain, clock=None: True)
    woken = []
    monkeypatch.setattr(mod, "send_wake", lambda address: woken.append(address))
    conn = _shadow_conn(tmp_path)
    log = next(lg for lg in FIXTURE["receipt"]["logs"]
               if len(lg["topics"]) == 3 and W._topic_address(lg["topics"][1]) == WALLET)
    log = dict(log, transactionHash=FIXTURE["receipt"]["transactionHash"],
               blockNumber=FIXTURE["receipt"]["blockNumber"])
    read, _calls = _reader(FIXTURE["receipt"], FIXTURE["transaction"], token_balance=5)
    mod.decode_sell(log, watched={WALLET: ("u-bada", "badabeepp")}, read_fn=read, conn=conn,
                    arrival="2026-09-29T02:05:38.602+00:00")
    assert len(woken) == 1
    receipt = dict(_receipt(_transfer(COIN, WALLET, OTHER, 10 ** 18)), transactionHash="0xplain",
                   blockNumber="0x10", gasUsed="0x1", effectiveGasPrice="0x1")
    plain = dict(receipt["logs"][0], transactionHash="0xplain", blockNumber="0x10")
    read, _calls = _reader(receipt, {"from": WALLET}, balances={"0x10": 100, "0xf": 101})
    mod.decode_sell(plain, watched={WALLET: ("u", "h")}, read_fn=read, conn=conn,
                    arrival="2026-09-29T03:00:00+00:00")
    assert len(woken) == 1, "no sale, no wake"
