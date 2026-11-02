"""An ordinary EVM entry books its coin and its cost from the swap's OWN canonical receipt, never a
wallet balance delta (Alpha 2026-10-06, after Codex EVM-03).

296 of 302 ordinary EVM fills 09-06..10-06 booked qty = balanceOf(after) - balanceOf(before): a
window of quote, sign, send and confirm that carries every other transfer into the wallet. The
receipt names only this transaction. Network scripted at the JSON-RPC seam, real executor.
"""
import json
import sqlite3

import pytest
from eth_account import Account
from eth_utils import keccak

from sniper.execution import evm_real_seams as S
from sniper.persistence import db_memecoin as m

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MINT = "0x" + "77" * 20
POOL = "0x" + "44" * 20
FEE = "0x" + "99" * 20
BLOCK = "0x" + "bc" * 32
E18 = 10 ** 18


def _topic(a):
    return "0x" + "0" * 24 + a[2:].lower()


def _log(token, src, dst, amount, h, i):
    return {"address": token, "topics": [TRANSFER, _topic(src), _topic(dst)],
            "data": "0x%064x" % amount, "transactionHash": h, "blockHash": BLOCK,
            "logIndex": hex(i)}


class Chain:
    """Receipt, transaction and block answers for the swap the executor sends."""

    def __init__(self, chain, owner, flows, *, block_misses=0):
        self.chain, self.owner, self.flows = chain, owner.lower(), flows
        self.block_misses, self.hash, self.calls = block_misses, None, []

    def http(self, method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth, params = req["method"], req.get("params") or []
        self.calls.append(meth)
        if meth == "eth_chainId":
            return 200, {"result": hex(int(self.chain))}, None
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if meth == "eth_sendRawTransaction":
            self.hash = "0x" + keccak(hexstr=params[0]).hex()
            return 200, {"result": self.hash}, None
        if meth == "eth_call" and str(params[0].get("data", "")).startswith("0x313ce567"):
            return 200, {"result": "0x%064x" % 18}, None                   # decimals()
        if meth == "eth_getTransactionReceipt" and self.hash and params[0] == self.hash:
            logs = [_log(t, s, d, a, self.hash, i)
                    for i, (t, s, d, a) in enumerate(self.flows(self.owner))]
            return 200, {"result": {"status": "0x1", "transactionHash": self.hash,
                                    "blockHash": BLOCK, "blockNumber": "0x10", "from": self.owner,
                                    "logs": logs}}, None
        if meth == "eth_getTransactionByHash" and self.hash and params[0] == self.hash:
            return 200, {"result": {"hash": self.hash, "from": self.owner, "blockHash": BLOCK,
                                    "blockNumber": "0x10", "value": "0x0", "to": POOL}}, None
        if meth == "eth_getBlockByNumber" and params[0] == "0x10":
            if self.block_misses > 0:                  # a node a block behind: not canonical yet
                self.block_misses -= 1
                return 200, {"result": None}, None
            return 200, {"result": {"hash": BLOCK, "number": "0x10", "timestamp": "0x6a2c7000"}}, None
        return 200, {"result": "0x0"}, None


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]
        it["i"] += 1
        return v
    return f


def _enter(monkeypatch, flows, *, chain="8453", tokens=(0.0, 104.0), block_misses=0):
    """One ordinary entry through the real executor. `tokens` is what the wallet balance reads say
    (before, after): 104 after means 100 coins arrived from somewhere else in the window."""
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint=MINT, source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id=chain)
    acct = Account.create()
    net = Chain(chain, acct.address, flows, block_misses=block_misses)
    seen = {}

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in seen:
            out = str(int(seen[pair[::-1]] * 0.97))
        else:
            seen[pair] = int(amount)
            out = "1"
        return {"sellAmount": str(amount), "buyAmount": out, "issues": {"allowance": None},
                "transaction": {"to": POOL, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}
    sleeps = []
    ex = S.build_real_evm_executor(acct=acct, chain_id=chain, rpc_url="https://rpc", http=net.http,
                                   venue_quote_fn=venue_quote, usdc_fn=_seq([100.0, 90.0]),
                                   token_fn=_seq(list(tokens)))
    import threading
    import time
    me, real_sleep = threading.current_thread(), time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s) if threading.current_thread() is me
                        else real_sleep(s))                         # this test's thread only
    result = ex["execute_fn"](c, pid, MINT, 10.0, "2026-10-06T00:00:01Z")
    return c, pid, result, net, sleeps


def _weth(chain="8453"):
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    return CHAIN_QUOTE[chain]["token"].lower()


def _buy(coin=4, debit=0, chain="8453"):
    """We pay 10 WETH to the pool and get `coin` coins; `debit` coins leave us in the same tx."""
    def flows(owner):
        out = [(_weth(chain), owner, POOL, 10 * E18), (MINT, POOL, owner, coin * E18)]
        if debit:
            out.append((MINT, owner, FEE, debit * E18))
        return out
    return flows


def _fill(c, result):
    return c.execute("SELECT qty, notional FROM memecoin_fills WHERE position_id=? AND kind='entry'",
                     (result["position_id"],)).fetchone()


@pytest.mark.parametrize("chain", ["4663", "56", "8453"])
def test_the_receipt_books_this_swap_never_the_wallet_balance_delta(monkeypatch, chain):
    c, pid, result, net, _ = _enter(monkeypatch, _buy(coin=4, chain=chain), chain=chain)
    assert result["status"] == "filled", result
    fill = _fill(c, result)
    assert fill["qty"] == pytest.approx(4.0)                        # not the delta's 104
    assert fill["notional"] == pytest.approx(10.0)                  # 10 WETH out at $1
    # the booking reuses the confirmation's receipt: one receipt read, one canonical block read
    assert net.calls.count("eth_getTransactionReceipt") == 1, net.calls
    assert net.calls.count("eth_getBlockByNumber") == 1, net.calls
    assert "eth_getTransactionByHash" not in net.calls             # the WETH spend is in the receipt


@pytest.mark.parametrize("chain", ["57073", "4663", "56", "8453"])
def test_the_receipt_books_the_owners_net_coin(monkeypatch, chain):
    # The gross->net fix (Alpha amendment 3): 1/57073 summed every credit (erc20_received_in_receipt);
    # a credit of 100 then a fee debit of 30 is 70 coins.
    c, pid, result, _net, _ = _enter(monkeypatch, _buy(coin=100, debit=30, chain=chain), chain=chain)
    assert result["status"] == "filled", result
    assert _fill(c, result)["qty"] == pytest.approx(70.0)


def test_a_lagging_receipt_is_read_again_before_anomaly(monkeypatch):
    c, pid, result, _net, sleeps = _enter(monkeypatch, _buy(coin=4), block_misses=2)
    assert result["status"] == "filled", result
    # the coin and the spend each met the lagging block once; one second later both read it
    assert _fill(c, result)["qty"] == pytest.approx(4.0) and sleeps == [1.0]


def test_a_receipt_that_proves_nothing_keeps_the_reservation_and_books_no_delta(monkeypatch):
    c, pid, result, _net, sleeps = _enter(monkeypatch, _buy(coin=0))   # no coin in the receipt
    assert result["status"] == "anomaly", result
    assert sleeps == [1.0, 1.0, 1.0]                                # bounded: three re-reads
    assert m.list_open_positions(c, mode="real") == []
    row = m.pending_row(c, pid)
    assert row["status"] == "reserved" and row["tx_sig"]             # owned, for the recovery


def test_the_entry_binds_the_quote_price_the_recovery_books_with(monkeypatch):
    c, pid, result, _net, _ = _enter(monkeypatch, _buy(coin=4))
    assert m.pending_row(c, pid)["evm_quote_asset_usd"] == pytest.approx(1.0)


# ---- a confirmed ordinary buy across a restart or a lagging receipt: the recovery books it ----

HASH = "0x" + "ab" * 32


def _recover(monkeypatch, tmp_path, chain, logs, *, value="0x0", quote_usd=2500.0, signal="onchain_watch"):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from test_memecoin_evm_context_keys import _production_context
    from sniper.execution import evm_wallet
    monkeypatch.setattr(evm_wallet, "erc20_decimals", lambda mint, url, http=None: 18)  # the leg binds it
    ctx = _production_context(monkeypatch, tmp_path, chain)
    owner = ctx["address"].lower()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint=MINT, source_user_id="src", strategy="alpha_b", notional=50.0,
                            reserved_at="2026-10-06T00:00:00Z", network_id=chain, signal_source=signal)
    m.bind_entry_submission(c, pid, tx_identity=HASH, signed_payload_b64="0xraw",
                            signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None,
                            nonce=7, backend="rpc", bound_at="2026-10-06T00:00:01Z",
                            evm_quote_asset_usd=quote_usd)
    receipt = {"transactionHash": HASH, "blockHash": BLOCK, "blockNumber": "0x10", "status": "0x1",
               "from": owner, "logs": [_log(t, s.replace("OWNER", owner), d.replace("OWNER", owner), a,
                                            HASH, i) for i, (t, s, d, a) in enumerate(logs)]}
    tx = {"hash": HASH, "from": owner, "blockHash": BLOCK, "blockNumber": "0x10", "to": POOL,
          "value": value}
    block = {"hash": BLOCK, "number": "0x10", "timestamp": hex(1790900000)}
    calls = []

    def rpc(url, method, params, http=None):
        calls.append(method)
        if method == "eth_call":
            return {"result": "0x%064x" % 18}
        return {"result": {"eth_getTransactionReceipt": receipt, "eth_getTransactionByHash": tx,
                           "eth_getBlockByNumber": block}.get(method)}
    monkeypatch.setattr(S, "_rpc", rpc)
    got = ctx["recover_confirmed_entry_fn"](c, m.pending_row(c, pid), "2026-10-06T00:01:00Z")
    return c, pid, got, calls


@pytest.mark.parametrize("chain", ["4663", "56", "8453"])
def test_the_recovery_books_an_ordinary_buy_from_its_receipt_net(monkeypatch, tmp_path, chain):
    weth = _weth(chain)
    logs = [(weth, "OWNER", POOL, 2 * 10 ** 16), (MINT, POOL, "OWNER", 100 * E18),
            (MINT, "OWNER", FEE, 30 * E18)]
    c, pid, got, calls = _recover(monkeypatch, tmp_path, chain, logs)
    assert got and got["status"] == "filled", got
    pos = c.execute("SELECT * FROM memecoin_positions").fetchone()
    assert m.open_qty(c, pos["id"]) == pytest.approx(70.0)          # net, not the gross 100
    assert pos["entry_notional"] == pytest.approx(0.02 * 2500.0)
    assert m.pending_row(c, pid)["status"] == "confirmed"


def test_the_recovery_books_a_native_paid_buy_at_its_value(monkeypatch, tmp_path):
    logs = [(MINT, POOL, "OWNER", 5 * E18)]
    c, pid, got, _calls = _recover(monkeypatch, tmp_path, "4663", logs, value=hex(10 ** 16))
    assert got and got["status"] == "filled", got
    pos = c.execute("SELECT * FROM memecoin_positions").fetchone()
    assert pos["entry_notional"] == pytest.approx(25.0) and m.open_qty(c, pos["id"]) == 5.0


def test_a_receipt_without_our_coin_leaves_the_reservation_to_the_stale_sweep(monkeypatch, tmp_path):
    c, pid, got, _calls = _recover(monkeypatch, tmp_path, "4663", [(_weth("4663"), "OWNER", POOL, 10)])
    assert not got and m.pending_row(c, pid)["status"] == "reserved"


def test_the_old_balance_window_is_logged_beside_the_receipt_off_the_wallet_lock(monkeypatch, caplog):
    # Gamma LOW on d1a670375: the diagnostic read never runs on the executor's (locked) thread.
    import logging
    import threading
    from sniper.execution import evm_swap_executor as X
    caller, readers, workers = threading.current_thread(), [], []
    real = X.receipt_vs_delta_later

    def spy(token_fn, *a, **k):
        def reading(mint):
            readers.append(threading.current_thread())
            return token_fn(mint)
        worker = real(reading, *a, **k)
        workers.append(worker)
        return worker
    monkeypatch.setattr(X, "receipt_vs_delta_later", spy)
    with caplog.at_level(logging.WARNING):         # the runner's effective level: last resort
        c, pid, result, _net, _ = _enter(monkeypatch, _buy(coin=4))
        assert result["status"] == "filled"
        for w in workers:
            w.join(5)
    assert readers and caller not in readers                         # never on the locked thread
    line = [r.getMessage() for r in caplog.records if "evm.receipt_vs_delta" in r.getMessage()]
    assert line and "receipt_got=4.0 delta_got=104.0 agree=False" in line[-1], line


def test_a_diagnostic_that_cannot_start_is_dropped_and_counted():
    from sniper.execution import evm_swap_executor as X
    before = X.RECEIPT_VS_DELTA_DROPPED[0]
    assert X._RECEIPT_VS_DELTA_SLOT.acquire(blocking=False)          # one already outstanding
    try:
        assert X.receipt_vs_delta_later(lambda m: 1.0, "0xm", 0.0, 1.0, 7, "0xh") is None
    finally:
        X._RECEIPT_VS_DELTA_SLOT.release()
    assert X.RECEIPT_VS_DELTA_DROPPED[0] == before + 1


def test_a_dropped_diagnostic_is_logged_at_the_runners_level(caplog):
    import logging
    from sniper.execution import evm_swap_executor as X
    assert X._RECEIPT_VS_DELTA_SLOT.acquire(blocking=False)
    try:
        with caplog.at_level(logging.WARNING):
            X.receipt_vs_delta_later(lambda m: 1.0, "0xm", 0.0, 1.0, 7, "0xh")
    finally:
        X._RECEIPT_VS_DELTA_SLOT.release()
    assert any("evm.receipt_vs_delta dropped pos=7" in r.getMessage() for r in caplog.records)


# ---- Alpha ruling 2026-10-06: exit proceeds from the sell's receipt; native only when exclusive ----

def _proceeds_rpc(monkeypatch, *, logs, block_txs, value="0x0", before=10 ** 18, after=10 ** 18):
    owner = "0x" + "12" * 20
    receipt = {"transactionHash": HASH, "blockHash": BLOCK, "blockNumber": "0x10", "status": "0x1",
               "from": owner, "gasUsed": "0x5208", "effectiveGasPrice": "0x3b9aca00",
               "logs": [_log(t, s, d, a, HASH, i) for i, (t, s, d, a) in enumerate(logs(owner))]}
    tx = {"hash": HASH, "from": owner, "value": value, "blockHash": BLOCK, "blockNumber": "0x10"}
    calls = []

    def rpc(url, method, params, http=None):
        calls.append(method)
        if method == "eth_getTransactionReceipt":
            return {"result": receipt}
        if method == "eth_getTransactionByHash":
            return {"result": tx}
        if method == "eth_getBlockByNumber":
            full = params[1] is True
            return {"result": {"hash": BLOCK, "number": "0x10", "timestamp": "0x1",
                               "transactions": block_txs(owner) if full else []}}
        if method == "eth_getBalance":
            return {"result": hex(before if params[1] == "0xf" else after)}
        raise AssertionError(method)
    monkeypatch.setattr(S, "_rpc", rpc)
    return owner, calls


def test_erc20_proceeds_are_the_owners_net_quote_inflow(monkeypatch):
    weth = _weth("8453")
    owner, _ = _proceeds_rpc(monkeypatch, block_txs=lambda o: [],
                             logs=lambda o: [(MINT, o, POOL, 5 * E18), (weth, POOL, o, 3 * E18),
                                             (weth, o, FEE, E18)])
    atomic, source = S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=weth,
                                            native=False)
    assert (atomic, source) == (2 * E18, "receipt_net")


PONS = "0x" + "06" * 20
CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"


def _curve_sell(emitter, maker, recipient, tokens_in, quote_out, index):
    """A Pons V2 CurveSell log: maker and recipient indexed; tokensIn, quoteOut, two fee words.
    Measured on position 70915 (4663, tx 0x3549b689...): quoteOut equals the recipient's block
    balance delta plus gas to the wei."""
    words = (tokens_in, quote_out, quote_out // 100, quote_out // 50)
    return {"address": emitter, "topics": [CURVE_SELL, "0x" + "0" * 24 + maker[2:],
                                           "0x" + "0" * 24 + recipient[2:]],
            "data": "0x" + "".join("%064x" % w for w in words), "transactionHash": HASH,
            "blockHash": BLOCK, "logIndex": hex(index)}


def _pons_receipt(monkeypatch, sells, *, to=PONS):
    owner, calls = _proceeds_rpc(monkeypatch, logs=lambda o: [(MINT, o, PONS, 5 * E18)],
                                 block_txs=lambda o: [{"hash": HASH, "from": o, "to": to}],
                                 after=10 ** 18 + 9 * E18)
    receipt = S._rpc("https://rpc", "eth_getTransactionReceipt", [HASH])["result"]
    receipt["to"] = to
    receipt["logs"] += [_curve_sell(e, owner, owner if r is None else r, 5 * E18, q, 10 + i)
                        for i, (e, r, q) in enumerate(sells(owner))]
    calls.clear()
    return owner, receipt, calls


def test_a_pons_native_payout_books_the_curve_sells_quote_out_with_no_block_reads(monkeypatch):
    owner, receipt, calls = _pons_receipt(monkeypatch, lambda o: [(PONS, None, 2 * E18)])
    assert S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=_weth("4663"),
                                  native=True, receipt=receipt) == (2 * E18, "pons_curve_sell")
    assert "eth_getBlockByNumber" not in calls and "eth_getBalance" not in calls


def test_split_pons_sells_to_us_sum(monkeypatch):
    owner, receipt, _ = _pons_receipt(monkeypatch, lambda o: [(PONS, None, 2 * E18), (PONS, None, E18)])
    assert S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=_weth("4663"),
                                  native=True, receipt=receipt) == (3 * E18, "pons_curve_sell")


@pytest.mark.parametrize("why, sells", [
    ("a CurveSell-shaped log from another contract (the coin's own hook can emit one)",
     lambda o: [(MINT, None, 2 * E18)]),
    ("a curve sell paying someone else", lambda o: [(PONS, FEE, 2 * E18)]),
    ("a zero quote out", lambda o: [(PONS, None, 0)]),
    ("no venue event at all", lambda o: []),
])
def test_a_native_payout_without_a_tx_bound_proof_is_unknown_never_the_block_delta(monkeypatch, why, sells):
    """Codex N2: block exclusivity cannot see an outsider's INTERNAL native transfer to us, so the
    block delta is never booked; the balance here (+9) would have been."""
    owner, receipt, calls = _pons_receipt(monkeypatch, sells)
    assert S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=_weth("4663"),
                                  native=True, receipt=receipt) == (None, "unknown"), why
    assert "eth_getBalance" not in calls


def test_every_funded_chain_requires_receipt_proceeds():
    import inspect
    src = inspect.getsource(S.build_real_evm_executor)
    assert src.count("require_receipt_proceeds=True") == 2
    assert 'require_receipt_proceeds=chain in {"1", "57073"}' not in src


PORTAL_56 = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
DANTE_WEI = 88_347_101_713_776_368


def _withdrawal(token, src, wei, index):
    return {"address": token, "topics": [WITHDRAWAL, _topic(src)], "data": "0x%064x" % wei,
            "transactionHash": HASH, "blockHash": BLOCK, "logIndex": hex(index)}


def _portal_receipt(owner, *, to=PORTAL_56, sender=None, src=PORTAL_56, token=None, wei=DANTE_WEI):
    """Position 72574 (但丁, 56) sold through the flap Portal, tx 0x23b3c527...: the Portal routed
    the graduated coin to WBNB, unwrapped it (Withdrawal src=Portal) and sent the BNB to us by an
    internal call, so no log names us. Our balance delta at block 126639248 plus the gas of the
    sell and of the same-block approve equals the withdrawal to the wei."""
    return {"transactionHash": HASH, "blockHash": BLOCK, "status": "0x1", "to": to,
            "from": sender or owner,
            "logs": [_log(MINT, owner, to, 5 * E18, HASH, 0),
                     _withdrawal(token or _weth("56"), src, wei, 1)]}


def test_a_flap_portal_sell_books_the_wbnb_it_unwrapped_for_us():
    owner = "0x" + "12" * 20
    assert S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=_weth("56"),
                                  native=True, receipt=_portal_receipt(owner)) == (
        DANTE_WEI, "flap_portal_unwrap")


@pytest.mark.parametrize("why, kw", [
    ("not the Portal: a generic router may keep a fee after unwrapping", {"to": POOL, "src": POOL}),
    ("someone else's transaction", {"sender": FEE}),
    ("an unwrap by another contract in our sell", {"src": POOL}),
    ("a Withdrawal-shaped log from a token that is not wrapped native", {"token": MINT}),
    ("a zero unwrap", {"wei": 0}),
])
def test_a_portal_payout_without_its_proof_stays_unknown(why, kw):
    owner = "0x" + "12" * 20
    assert S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=_weth("56"),
                                  native=True, receipt=_portal_receipt(owner, **kw)) == (
        None, "unknown"), why


@pytest.mark.parametrize("extra", [0, 1, DANTE_WEI])
def test_multiple_portal_withdrawals_do_not_prove_our_proceeds(extra):
    owner = "0x" + "12" * 20
    receipt = _portal_receipt(owner)
    receipt["logs"].append(_withdrawal(_weth("56"), PORTAL_56, extra, 2))
    assert S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=_weth("56"),
                                  native=True, receipt=receipt) == (None, "unknown")


def test_an_unrelated_withdrawal_does_not_invalidate_our_portal_proof():
    owner = "0x" + "12" * 20
    receipt = _portal_receipt(owner)
    receipt["logs"].append(_withdrawal(_weth("56"), POOL, DANTE_WEI, 2))
    assert S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=_weth("56"),
                                  native=True, receipt=receipt) == (DANTE_WEI, "flap_portal_unwrap")


@pytest.mark.parametrize("data", ["wrong", "0x" + "0" * 60 + "_001", "0x" + "0" * 60 + "+001"])
def test_a_malformed_portal_withdrawal_is_unknown(data):
    owner = "0x" + "12" * 20
    receipt = _portal_receipt(owner)
    receipt["logs"][1]["data"] = data
    assert S.exit_proceeds_atomic(HASH, "https://rpc", owner=owner, quote_token=_weth("56"),
                                  native=True, receipt=receipt) == (None, "unknown")
