"""Offline, exact-unit descriptive flow; provider doubles expose only read methods."""
import importlib.util
import json
import base64
import hashlib
import struct

import base58
from copy import deepcopy

import pytest

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
WETH = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
def named_key(name):
    try:
        if len(base58.b58decode(name)) == 32:
            return name
    except ValueError:
        pass
    return base58.b58encode(hashlib.sha256(name.encode()).digest()).decode()


MINT = named_key("Coin")
SOL_UNIT = dict(chain="solana", uid="wallet", address=named_key("source"), mint=MINT,
                signature="source-signature", t0=1000, probe_age=False)


@pytest.fixture
def flow():
    spec = importlib.util.find_spec("sniper.services.wallet_universe_flow")
    assert spec is not None, "raw-unit flow collector is not implemented"
    return __import__("sniper.services.wallet_universe_flow", fromlist=["collect_solana"])


def sol_tx(sig, at, index, trader="other", quote=USDC, paid=100, bought=10,
           decimals=6, slot=None):
    # Real program/discriminator/CPI/event shape, without network dependence.
    from sniper.services.wallet_universe_sol_venues import PUMP, PUMP_EVENT_AUTHORITY, EVENT_IX, PUMP_BUY_EVENT, PUMP_SELL_EVENT
    token = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    trader, quote, pool = named_key(trader), named_key(quote), named_key("pool")
    keys = [trader, named_key("coin-account"), named_key("quote-account"), pool,
            named_key("pool-coin"), named_key("pool-quote"), MINT, quote, PUMP, token,
            named_key("config"), PUMP_EVENT_AUTHORITY, named_key("fee-owner"),
            named_key("fee-quote"), named_key("creator"), "11111111111111111111111111111111",
            named_key("ata-program"), named_key("fee-program")]
    pre, post = [], []
    for idx, owner, mint, start, end, dec in (
        (1, trader, MINT, 1000, 1000 + bought, 6),
        (2, trader, quote, 10000, 10000 - paid, decimals),
        (4, pool, MINT, 10000, 10000 - bought, 6),
        (5, pool, quote, 1000, 1000 + paid, decimals),
    ):
        for target, amount in ((pre, start), (post, end)):
            target.append(dict(accountIndex=idx, owner=owner, mint=mint, programId=token,
                               uiTokenAmount=dict(amount=str(amount), decimals=dec)))
    def checked(source, mint_index, destination, authority, amount, dec, depth=2):
        return dict(programIdIndex=9, accounts=[source, mint_index, destination, authority],
                    data=base58.b58encode(bytes([12]) + struct.pack("<Q", amount) + bytes([dec])).decode(),
                    stackHeight=depth)
    children = []
    if paid == 0:
        top = checked(4, 6, 1, 3, abs(bought), 6, 1)
        top.pop("stackHeight")
        logs = ["Program " + token + " invoke [1]", "Program " + token + " success"]
    else:
        buy = bought > 0
        name = "buy" if buy else "sell"
        accounts = [3, 0, 10, 6, 7, 1, 2, 4, 5, 12, 13, 9, 9, 15, 16, 11, 8, 13, 14, 10, 10, 10, 17]
        if not buy: accounts = accounts[:21]
        top = dict(programIdIndex=8, accounts=accounts,
                   data=base58.b58encode(hashlib.sha256(("global:" + name).encode()).digest()[:8]
                       + struct.pack("<QQ", abs(bought), abs(paid))).decode())
        children = [checked(4 if buy else 1, 6, 1 if buy else 4, 3 if buy else 0, abs(bought), 6),
                    checked(2 if buy else 5, 7, 5 if buy else 2, 0 if buy else 3, abs(paid), decimals)]
        event = ((PUMP_BUY_EVENT if buy else PUMP_SELL_EVENT) + struct.pack("<q13Q", at,
            abs(bought), abs(paid), 1000, 10000, 10000, 1000, abs(paid), 0, 0, 0, 0, abs(paid), abs(paid))
            + b"".join(base58.b58decode(keys[i]) for i in (3,0,1,2,12,13,14)) + struct.pack("<QQ",0,0))
        children.append(dict(programIdIndex=8, accounts=[11], data=base58.b58encode(EVENT_IX + event).decode(), stackHeight=2))
        logs = ["Program " + PUMP + " invoke [1]", "Program log: Instruction: " + name.title()]
        for _ in range(2): logs.extend(["Program " + token + " invoke [2]", "Program " + token + " success"])
        logs.extend(["Program data: " + base64.b64encode(event).decode(), "Program " + PUMP + " invoke [2]",
                     "Program " + PUMP + " success", "Program " + PUMP + " success"])
    return dict(blockTime=at, slot=slot if slot is not None else at, transactionIndex=index,
        transaction=dict(signatures=[sig], message=dict(accountKeys=keys,
                         header=dict(numRequiredSignatures=1), instructions=[top])),
        meta=dict(err=None, fee=5000, preBalances=[100000] * len(keys),
                  postBalances=[95000] + [100000] * (len(keys)-1),
                  preTokenBalances=pre, postTokenBalances=post, logMessages=logs,
                  innerInstructions=[dict(index=0, instructions=children)],
                  loadedAddresses=dict(writable=[], readonly=[])))


def unsupported_sol_tx(sig, at, index):
    tx = sol_tx(sig, at, index)
    from sniper.services.wallet_universe_sol_venues import PUMP
    unknown = named_key("unknown-swap-program")
    tx["transaction"]["message"]["accountKeys"][8] = unknown
    tx["meta"]["logMessages"] = [line.replace(PUMP, unknown) for line in tx["meta"]["logMessages"]]
    return tx


def sol_reader(pages):
    pending = iter(pages)
    def call(method, params):
        assert method == "getTransactionsForAddress"
        assert params[0] == MINT
        opts = params[1]
        if opts["transactionDetails"] == "signatures":
            return dict(data=[dict(signature="birth", blockTime=100, slot=100, err=None)],
                        paginationToken=None)
        assert opts["limit"] == 1000 and opts["sortOrder"] == "asc"
        assert opts["encoding"] == "json" and opts["maxSupportedTransactionVersion"] == 1
        assert opts["filters"] == dict(status="succeeded", blockTime=dict(gte=940, lte=1060))
        page = next(pending)
        if isinstance(page, BaseException):
            raise page
        return page
    return call


def source():
    return sol_tx("source-signature", 1000, 5, trader="source")


def quotes_by_asset(result):
    return {quote["asset"]: quote for quote in result["quotes"]}


def test_solana_open_window_waits_then_reads_the_full_tail(flow, monkeypatch):
    monkeypatch.setattr(flow, "_now", lambda: "1970-01-01T00:16:43+00:00")
    pending = flow.collect_solana(SOL_UNIT, lambda *_: pytest.fail("window is still open"))
    assert (pending["status"], pending["detail"], pending["after_complete"]) == (
        "partial", "waiting_after_window", False)
    monkeypatch.setattr(flow, "_now", lambda: "1970-01-01T00:17:40.500+00:00")
    boundary = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[source()], paginationToken=None)]),
                                  state=pending["state"])
    assert boundary["detail"] == "waiting_after_window" and boundary["after_complete"] is False
    monkeypatch.setattr(flow, "_now", lambda: "1970-01-01T00:17:41+00:00")
    closed = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[source(),
        sol_tx("late", 1060, 1, paid=700)], paginationToken=None)]), state=boundary["state"])
    assert closed["after_complete"] is True
    assert quotes_by_asset(closed)[USDC]["net_after_atomic"] == "700"


def test_fast_flow_windows_keep_exact_units_and_unknown_position_scope(flow):
    rows = [sol_tx("pre", 1000, 4, paid=999), source(),
        sol_tx("same-second", 1000, 6, paid=200),
        sol_tx("buy", 1005, 1, paid=300),
        sol_tx("sell", 1010, 1, bought=-10, paid=-100),
        sol_tx("sol", 1030, 1, quote=WSOL, decimals=9, paid=70),
        sol_tx("late", 1060, 1, paid=400)]
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=rows, paginationToken=None)]))
    measured = flow.flow_horizons(result)
    windows = {w["seconds"]: w for w in measured["windows"]}
    usdc = lambda n: next(q for q in windows[n]["quotes"] if q["asset"] == USDC)
    assert (usdc(5)["net_after_atomic"], usdc(5)["buy_atomic"], usdc(5)["sell_atomic"]) == ("500", "500", "0")
    assert usdc(10)["net_after_atomic"] == "400"
    assert usdc(60)["net_after_atomic"] == "800"
    assert windows[5]["verified_buyer_count"] == 1
    assert windows[30]["quotes"][1]["decimals"] == 9
    assert windows[120]["state"] == windows[300]["state"] == "not_captured"
    position = result["source"]["position_evidence"]
    assert (position["state"], position["visible_pre_balance_atomic"], position["net_received_atomic"]) == ("add_on", "1000", "10")
    from scripts.research import wallet_universe_metrics as worker
    unit = dict(SOL_UNIT, proof_addresses=[SOL_UNIT["address"]])
    published = worker.summarize(None, [unit], {}, {}, result["computed_at"],
                               results={worker.unit_key(unit): result})[0]
    assert published["recent"][0]["horizons"]["windows"][0]["quotes"][0]["net_after_atomic"] == "500"
    assert published["recent"][0]["source_position"]["state"] == "add_on"
    # Zero on the touched account cannot establish absence on untouched accounts.
    zero = source()
    zero["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"] = "0"
    zero["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "10"
    opened = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[zero], paginationToken=None)]))
    assert opened["source"]["position_evidence"]["state"] == "unknown"
    result["state"]["seen"].pop("buy")
    assert all(w["state"] == "unknown" and w["quotes"] is None
               for w in flow.flow_horizons(result)["windows"] if w["seconds"] <= 60)
    result["status"] = "partial"
    assert flow.flow_horizons(result)["windows"][0]["state"] == "unknown"


@pytest.mark.parametrize('phase', ['summarize', 'publish'])
def test_compact_horizons_survive_metadata_publication(tmp_path, flow, phase):
    from scripts.research import wallet_universe_metrics as worker
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[source(), sol_tx('buy', 1005, 1, paid=300)], paginationToken=None)]))
    expected = flow.flow_horizons(result)
    assert result['horizons'] == expected
    assert result['source_position']['state'] == 'add_on'
    unit = dict(SOL_UNIT, proof_addresses=[SOL_UNIT['address']])
    owners = {('solana', SOL_UNIT['address']): (SOL_UNIT['uid'], 'fixture')}
    db = worker.m.open_metrics(tmp_path / 'data/wallet_universe_metrics.db')
    try:
        db.execute('INSERT INTO flow_units VALUES (?,?,?,?)',
                   (worker.unit_key(unit), json.dumps(unit), json.dumps(result), result['computed_at']))
        db.commit()
        compact = json.loads(db.execute(f'SELECT {worker._FLOW_METADATA_SQL} FROM flow_units').fetchone()[0])
        assert 'records' not in compact['state'] and 'seen' not in compact['state'] and 'source' not in compact
        if phase == 'publish':
            worker.publish(tmp_path, db, [unit], owners, {}, {'state': 'ok'})
            row = json.loads((tmp_path / 'data/wallet_universe_metrics.json').read_text())['impact'][0]
        else:
            row = worker.summarize(db, [unit], owners, {}, result['computed_at'])[0]
        assert row['recent'][0]['horizons'] == expected
        assert row['recent'][0]['source_position']['state'] == 'add_on'
        assert expected['windows'][0]['quotes'][0]['net_after_atomic'] == '300'
    finally:
        db.close()


def test_solana_preserves_quote_units_and_excludes_source_aliases_and_ours(flow):
    rows = [sol_tx("before", 990, 1, paid=300), source(),
            sol_tx("alias", 1001, 1, trader="alias", paid=400),
            sol_tx("our", 1002, 1, paid=500),
            sol_tx("after-usdc", 1003, 1, paid=-200, bought=-10),
            sol_tx("after-weth", 1004, 1, quote=WETH, paid=50, decimals=8),
            sol_tx("after-sol", 1005, 1, quote=WSOL, paid=70, decimals=9)]
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=rows, paginationToken=None)]),
                                 excluded_addresses=[named_key("alias")], own_signatures=["our"])
    assert result["status"] == "complete" and result["source_found"] is True
    q = quotes_by_asset(result)
    assert (q[USDC]["net_before_atomic"], q[USDC]["net_after_atomic"],
            q[USDC]["delta_atomic"], q[USDC]["swaps"]) == ("300", "-200", "-500", 2)
    assert (q[WETH]["net_after_atomic"], q[WETH]["decimals"]) == ("50", 8)
    assert (q[WSOL]["net_after_atomic"], q[WSOL]["decimals"]) == ("70", 9)
    json.dumps(result)


def test_solana_same_second_uses_slot_and_transaction_order(flow):
    rows = [sol_tx("pre", 1000, 4), source(), sol_tx("post", 1000, 6, paid=200)]
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=rows, paginationToken=None)]))
    assert result["status"] == "complete"
    assert quotes_by_asset(result)[USDC]["delta_atomic"] == "100"


def test_solana_source_exotic_quote_buy_requires_real_signer_net_receipt(flow):
    exact = sol_tx("source-signature", 1000, 5, trader="source", quote="UnknownQuote")
    result = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[exact, sol_tx("post",1001,1)], paginationToken=None)]))
    assert result["status"] == "complete" and result["source_found"] is True
    assert quotes_by_asset(result)[USDC]["net_after_atomic"] == "100"


def test_solana_source_signer_airdrop_with_foreign_payer_buy_cannot_claim_source(flow):
    tx = sol_tx("source-signature", 1000, 5, trader="foreign-payer")
    message, meta = tx["transaction"]["message"], tx["meta"]
    # Both wallets sign, but the pool's authenticated buyer owns the user accounts.
    message["accountKeys"][1] = SOL_UNIT["address"]
    message["header"]["numRequiredSignatures"] = 2
    destination = len(message["accountKeys"])
    message["accountKeys"].append(named_key("source-airdrop-account"))
    meta["preBalances"].append(100000)
    meta["postBalances"].append(100000)
    for field, amount in (("preTokenBalances",0),("postTokenBalances",10)):
        meta[field].append(dict(accountIndex=destination, owner=SOL_UNIT["address"],mint=MINT,
            programId=message["accountKeys"][9],uiTokenAmount=dict(amount=str(amount),decimals=6)))
    # An additional direct transfer delivers ten tokens to the source signer.
    meta["postTokenBalances"][2]["uiTokenAmount"]["amount"] = "9980"
    message["instructions"].append(dict(programIdIndex=9,accounts=[4,6,destination,3],
        data=base58.b58encode(bytes([12])+struct.pack("<Q",10)+bytes([6])).decode()))
    token = message["accountKeys"][9]
    meta["logMessages"].extend(["Program " + token + " invoke [1]", "Program " + token + " success"])
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[tx],paginationToken=None)]))
    assert result["status"] == "censored" and result["source_found"] is False
    assert "source" in result["state"]["errors"][0]


def test_solana_budget_stop_preserves_pages_and_resumes_cursor(flow):
    one = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[sol_tx("pre", 990, 1), source()], paginationToken="next"),
        ValueError("budget_exhausted")]))
    assert one["status"] == "partial" and one["cursor"] == "next" and one["pages"] == 1
    def resume(method, params):
        assert params[1]["paginationToken"] == "next"
        return dict(data=[sol_tx("post", 1001, 1, paid=250)], paginationToken=None)
    two = flow.collect_solana(SOL_UNIT, resume, state=json.loads(json.dumps(one["state"])))
    assert two["status"] == "complete" and two["pages"] == 2
    assert quotes_by_asset(two)[USDC]["delta_atomic"] == "150"


def test_solana_page_limit_is_incomplete_and_retains_nonempty_cursor(flow):
    result = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source()], paginationToken="continue")]), max_pages=1)
    assert result["status"] == "partial" and result["cursor"] == "continue"
    assert "page" in result["detail"]


@pytest.mark.parametrize("bad", [None, {}, dict(data=None, paginationToken=None),
                                dict(data=[], paginationToken=3), dict(data=[])])
def test_solana_malformed_pages_never_complete_empty(flow, bad):
    result = flow.collect_solana(SOL_UNIT, sol_reader([bad]))
    assert result["status"] == "partial" and result["quotes"] == []


def test_solana_source_clock_mismatch_is_censored(flow):
    wrong = source()
    wrong["blockTime"] = 1001
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[wrong], paginationToken=None)]))
    assert result["status"] == "censored" and "source" in result["detail"]
    assert result["quotes"] == []


def test_solana_missing_source_is_censored_even_on_valid_empty_window(flow):
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[], paginationToken=None)]))
    assert result["status"] == "censored" and result["source_found"] is False
    assert result["quotes"] == []


def test_solana_airdrop_is_not_flow_and_unknown_swap_censors(flow):
    airdrop = sol_tx("airdrop", 1001, 1, paid=0)
    result = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source(), airdrop], paginationToken=None)]))
    assert result["status"] == "complete" and result["quotes"] == []
    assert result["nontrades"] == 1 and result["undecodable"] == 0
    unknown = unsupported_sol_tx("unsupported", 1002, 1)
    bad = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source(), unknown], paginationToken=None)]))
    assert bad["status"] == "censored" and bad["undecodable"] == 1


def test_solana_launch_has_no_pre_window_instead_of_zero(flow):
    unit = {**SOL_UNIT, "probe_age": True}
    pages = iter([dict(data=[dict(signature="birth", blockTime=980, slot=980, err=None)],
                       paginationToken=None),
                  dict(data=[source(), sol_tx("post", 1001, 1)], paginationToken=None)])
    result = flow.collect_solana(unit, lambda *_: next(pages))
    assert result["before_complete"] is False and result["before_detail"] == "no_pre_window"
    assert quotes_by_asset(result)[USDC]["net_before_atomic"] is None
    assert quotes_by_asset(result)[USDC]["delta_atomic"] is None


def test_solana_negative_atomic_balance_is_censored(flow):
    tx = sol_tx("bad", 1001, 1)
    tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "-1"
    result = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source(), tx], paginationToken=None)]))
    assert result["status"] == "censored" and result["undecodable"] == 1


def test_failed_continuation_keeps_last_successful_page_clock(flow):
    one = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source()], paginationToken="next")]), max_pages=1)
    two = flow.collect_solana(SOL_UNIT, sol_reader([ValueError("paused")]), state=one["state"])
    assert two["last_collect"] == one["last_collect"]
    assert two["pages"] == 1 and two["status"] == "partial"


def test_solana_unknown_quote_with_native_tip_is_not_synthetic_sol_flow(flow):
    unknown = sol_tx("unsupported", 1001, 1, quote="UnknownQuote")
    unknown["meta"]["postBalances"][0] -= 1000
    result = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source(), unknown], paginationToken=None)]))
    assert result["status"] == "complete"
    assert quotes_by_asset(result)[named_key("UnknownQuote")]["net_after_atomic"] == "100"
    assert quotes_by_asset(result)[named_key("UnknownQuote")]["symbol"] is None
    assert "native" not in quotes_by_asset(result) and WSOL not in quotes_by_asset(result)


TOKEN = "0x" + "11" * 20
QUOTE = "0x" + "22" * 20
POOL = "0x" + "33" * 20
SRC = "0x" + "aa" * 32
V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
EVM_UNIT = dict(chain="4663", uid="evm-wallet", address="0x" + "44" * 20,
                mint=TOKEN, signature=SRC, t0=1000)
POOLS = [dict(pool=POOL, protocol="v2", token0=TOKEN, token1=QUOTE,
              quote_side=1, token0_decimals=18, token1_decimals=6, quote_symbol="USDC")]


def block_hash(number):
    return "0x" + format(number, "064x")


def evm_log(block, index, quote, sig=None, log_index=0):
    # Literal V2 pool amounts: token leg is negative for buys, quote leg positive.
    values = [0 if quote > 0 else 10, max(quote, 0), 10 if quote > 0 else 0, max(-quote, 0)]
    return dict(address=POOL, blockNumber=hex(block), blockHash=block_hash(block),
                transactionHash=sig or "0x" + format(block * 100 + index, "064x"),
                transactionIndex=hex(index), logIndex=hex(log_index), removed=False,
                topics=[V2_SWAP, "0x" + "00" * 12 + "44" * 20, "0x" + "00" * 32],
                data="0x" + "".join(format(v, "064x") for v in values))


class EvmReader:
    def __init__(self, logs, receipt=None):
        self.logs = logs
        self.receipt = receipt or dict(transactionHash=SRC, status="0x1", blockNumber="0x64",
                                       **{"from": EVM_UNIT["address"]},
                                       transactionIndex="0x5", blockHash=block_hash(100),
                                       logs=[evm_log(100, 5, 100, SRC), transfer(POOL, EVM_UNIT["address"])])
    def __call__(self, method, params):
        if method == "eth_getTransactionReceipt":
            assert params == [SRC]
            return deepcopy(self.receipt)
        if method == "eth_getTransactionByHash":
            # Opaque historical mock transactions carry no authenticated envelope.
            return dict(hash=SRC,chainId=hex(4663),blockNumber=self.receipt["blockNumber"],
                blockHash=self.receipt["blockHash"],transactionIndex=self.receipt["transactionIndex"],
                **{"from":self.receipt["from"]},to=self.receipt.get("to"),input="0x",value="0x0")
        if method == "eth_blockNumber":
            return "0x78"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            return dict(number=hex(number), timestamp=hex(number * 10), hash=block_hash(number))
        if method == "eth_getLogs":
            opts = params[0]
            assert opts["address"] == POOL
            lo, hi = int(opts["fromBlock"], 16), int(opts["toBlock"], 16)
            assert lo >= 94 and hi <= 106
            return deepcopy([row for row in self.logs if lo <= int(row["blockNumber"], 16) <= hi])
        pytest.fail(f"unexpected read {method}")


def test_evm_exact_source_order_quote_flow_and_no_maker_owner_inference(flow):
    own = "0x" + "bb" * 32
    logs = [evm_log(99, 1, 100), evm_log(100, 4, 200), evm_log(100, 5, 100, SRC),
            evm_log(100, 6, -50), evm_log(101, 1, 400), evm_log(102, 1, 1000, own)]
    result = flow.collect_evm(EVM_UNIT, EvmReader(logs), pools=POOLS,
                              excluded_addresses=[EVM_UNIT["address"]], own_signatures=[own])
    assert result["status"] == "complete" and result["source_found"] is True
    q = quotes_by_asset(result)[QUOTE]
    assert (q["net_before_atomic"], q["net_after_atomic"], q["delta_atomic"]) == ("300", "350", "50")
    assert result["scope"] == "registered_pools" and result["owner_exclusion"] == "source_transaction_only"
    windows = flow.flow_horizons(result)["windows"]
    assert windows[0]["quotes"][0]["net_after_atomic"] == "-50"
    assert windows[1]["quotes"][0]["net_after_atomic"] == "350"
    assert windows[1]["verified_buyer_count"] is None and windows[1]["buyers_complete"] is False
    result["state"]["headers"].pop("101")
    assert flow.flow_horizons(result)["windows"][0]["quotes"] is None
    json.dumps(result)


@pytest.mark.parametrize("mutation", ["removed", "wrong_hash", "wrong_time", "wrong_source_hash"])
def test_evm_reorg_and_wrong_source_clock_never_complete(flow, mutation):
    logs = [evm_log(99, 1, 100), evm_log(100, 5, 100, SRC)]
    reader = EvmReader(logs)
    if mutation == "removed":
        logs[0]["removed"] = True
    if mutation == "wrong_hash":
        logs[0]["blockHash"] = "0x" + "ff" * 32
    if mutation == "wrong_time":
        unit = {**EVM_UNIT, "t0": 1001}
    else:
        unit = EVM_UNIT
    if mutation == "wrong_source_hash":
        reader.receipt["transactionHash"] = "0x" + "cc" * 32
    result = flow.collect_evm(unit, reader, pools=POOLS)
    assert result["status"] == "censored"


def test_evm_logs_failure_preserves_resumable_source_and_never_fabricates_zero(flow):
    reader = EvmReader([evm_log(100, 5, 100, SRC), evm_log(101, 1, 200)])
    def stop(method, params):
        if method == "eth_getLogs":
            raise ValueError("budget_exhausted")
        return reader(method, params)
    one = flow.collect_evm(EVM_UNIT, stop, pools=POOLS)
    assert one["status"] == "partial" and one["quotes"] == []
    two = flow.collect_evm(EVM_UNIT, reader, pools=POOLS, state=one["state"])
    assert two["status"] == "complete" and quotes_by_asset(two)[QUOTE]["net_after_atomic"] == "200"


def test_evm_malformed_swap_does_not_read_as_quiet_market(flow):
    bad = evm_log(101, 1, 300)
    bad["data"] = "0x00"
    result = flow.collect_evm(EVM_UNIT, EvmReader([evm_log(100, 5, 100, SRC), bad]), pools=POOLS)
    assert result["status"] == "censored" and result["undecodable"] == 1


@pytest.mark.parametrize("field,value", [("transactionHash", "0x" + "ff" * 32),
                                        ("blockHash", "0x" + "ff" * 32),
                                        ("transactionIndex", "0x4"), ("removed", True)])
def test_evm_source_receipt_logs_must_match_receipt_anchor(flow, field, value):
    logs = [evm_log(100, 5, 100, SRC), evm_log(101, 1, 200)]
    reader = EvmReader(logs)
    reader.receipt["logs"][0][field] = value
    result = flow.collect_evm(EVM_UNIT, reader, pools=POOLS)
    assert result["status"] == "censored" and result["source_found"] is False


def test_evm_source_sell_is_not_a_verified_source_buy(flow):
    sell = evm_log(100, 5, -100, SRC)
    reader = EvmReader([sell])
    reader.receipt["logs"] = [sell]
    result = flow.collect_evm(EVM_UNIT, reader, pools=POOLS)
    assert result["status"] == "censored" and result["source_found"] is False


def test_evm_log_window_must_include_receipt_source_swap(flow):
    result = flow.collect_evm(EVM_UNIT, EvmReader([]), pools=POOLS)
    assert result["status"] == "censored" and result["quotes"] == []


def test_evm_source_log_order_mismatch_is_censored(flow):
    wrong = evm_log(100, 6, 100, SRC)
    result = flow.collect_evm(EVM_UNIT, EvmReader([wrong]), pools=POOLS)
    assert result["status"] == "censored"


def test_solana_resumed_state_cannot_change_exclusions(flow):
    one = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source()], paginationToken="next")]), max_pages=1)
    with pytest.raises(ValueError, match="state_identity_mismatch"):
        flow.collect_solana(SOL_UNIT, lambda *_: pytest.fail("must not read"),
                            state=one["state"], excluded_addresses=["new"])


@pytest.mark.parametrize("sender", [None, "0x" + "99" * 20])
def test_evm_receipt_sender_must_be_proved_source_wallet(flow, sender):
    reader = EvmReader([evm_log(100, 5, 100, SRC)])
    reader.receipt["from"] = sender
    result = flow.collect_evm(EVM_UNIT, reader, pools=POOLS)
    assert result["status"] == "censored" and result["source_found"] is False


@pytest.mark.parametrize("protocol", ["v3", "v4", "pons_v2"])
def test_evm_venue_sign_and_pons_sell_word_order(flow, protocol):
    from sniper.services.memecoin_evm_v3_events import V3_SWAP
    from sniper.services.memecoin_evm_v4_events import V4_SWAP, V4_POOL_MANAGERS
    p = deepcopy(POOLS[0])
    p["protocol"] = protocol
    if protocol != "v3":
        p.update(token0=QUOTE, token1=TOKEN, quote_side=0,
                 token0_decimals=6, token1_decimals=18)
    if protocol == "v4":
        p.update(pool="0x" + "77" * 32, manager=V4_POOL_MANAGERS["4663"])
    def event(block, index, quote, sig=None):
        row = evm_log(block, index, quote, sig)
        if protocol == "v3":
            words = [-10 if quote > 0 else 10, quote, 1 << 96, 1000, 0]
            row["topics"][0] = V3_SWAP
        elif protocol == "v4":
            words = [-quote, 10 if quote > 0 else -10, 1 << 96, 1000, 0, 3000]
            row["address"] = p["manager"]
            row["topics"] = [V4_SWAP, p["pool"], "0x" + "00" * 12 + "44" * 20]
        else:
            # Pons Buy: quote input first. Sell: token input first.
            words = [quote, 10, 1, 2] if quote > 0 else [10, -quote, 1, 2]
            row["topics"][0] = ("0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
                                 if quote > 0 else "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df")
        row["data"] = "0x" + "".join(format(value % (1 << 256), "064x") for value in words)
        return row
    source_event = event(100, 5, 100, SRC)
    rows = [event(99, 1, 300), source_event, event(101, 1, -70)]
    reader = EvmReader(rows)
    reader.receipt["logs"] = [source_event, transfer(POOL, EVM_UNIT["address"])]
    def call(method, params):
        if method == "eth_getLogs":
            assert params[0]["address"] == p.get("manager", p["pool"])
            if protocol == "v4":
                assert params[0]["topics"][1] == p["pool"]
            return deepcopy(rows)
        return reader(method, params)
    result = flow.collect_evm(EVM_UNIT, call, pools=[p])
    assert result["status"] == "complete"
    q = quotes_by_asset(result)[QUOTE]
    assert (q["net_before_atomic"], q["net_after_atomic"], q["delta_atomic"]) == ("300", "-70", "-370")


def test_evm_budget_stop_mid_boundary_search_resumes(flow):
    reader = EvmReader([evm_log(100, 5, 100, SRC), evm_log(101, 1, 200)])
    def stop(method, params):
        if method == "eth_getBlockByNumber" and 100 < int(params[0], 16) < 120:
            raise ValueError("budget_exhausted")
        return reader(method, params)
    one = flow.collect_evm(EVM_UNIT, stop, pools=POOLS)
    assert one["status"] == "partial" and "from_block" in one["state"]
    two = flow.collect_evm(EVM_UNIT, reader, pools=POOLS, state=one["state"])
    assert two["status"] == "complete" and quotes_by_asset(two)[QUOTE]["net_after_atomic"] == "200"


def test_evm_two_hashes_cannot_claim_same_transaction_order(flow):
    first = evm_log(101, 1, 200)
    impossible = evm_log(101, 1, 300, "0x" + "dd" * 32, log_index=1)
    result = flow.collect_evm(EVM_UNIT, EvmReader([evm_log(100, 5, 100, SRC), first, impossible]), pools=POOLS)
    assert result["status"] == "censored" and result["undecodable"] == 1


@pytest.mark.parametrize("mutation", ["null_balance", "null_message", "nonobject_loaded"])
def test_solana_malformed_transaction_parts_censor_instead_of_crashing(flow, mutation):
    bad = sol_tx("malformed", 1001, 1)
    if mutation == "null_balance":
        bad["meta"]["postTokenBalances"][0] = None
    elif mutation == "null_message":
        bad["transaction"]["message"] = None
    else:
        bad["meta"]["loadedAddresses"] = ["bogus"]
    result = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source(), bad], paginationToken=None)]))
    assert result["status"] == "censored" and result["undecodable"] == 1


@pytest.mark.parametrize("mutation", ["null_receipt_log", "nonstring_topic"])
def test_evm_malformed_transaction_parts_censor_instead_of_crashing(flow, mutation):
    reader = EvmReader([evm_log(100, 5, 100, SRC), evm_log(101, 1, 200)])
    if mutation == "null_receipt_log":
        reader.receipt["logs"] = [None]
    else:
        reader.logs[1]["topics"][0] = 7
    result = flow.collect_evm(EVM_UNIT, reader, pools=POOLS)
    assert result["status"] == "censored"


def test_collection_timestamps_use_corrected_utc_clock(flow, monkeypatch):
    monkeypatch.setattr(flow, "real_utc_now_iso", lambda **_: "2026-10-04T12:00:00+00:00", raising=False)
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[source()], paginationToken=None)]))
    assert result["last_collect"] == "2026-10-04T12:00:00+00:00"
    assert result["computed_at"] == "2026-10-04T12:00:00+00:00"


def test_solana_undecodable_before_is_optional_and_does_not_censor_post_flow(flow):
    rows = [unsupported_sol_tx("unknown-pre", 990, 1), source(),
            sol_tx("post", 1001, 1, paid=250)]
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=rows, paginationToken=None)]))
    assert result["status"] == "complete" and result["after_complete"] is True
    assert result["before_complete"] is False and result["before_detail"] == "pre_window_undecodable"
    q = quotes_by_asset(result)[USDC]
    assert (q["net_before_atomic"], q["net_after_atomic"], q["delta_atomic"]) == (None, "250", None)
    assert result["undecodable"] == 1


def test_evm_undecodable_before_is_optional_and_does_not_censor_post_flow(flow):
    before = evm_log(100, 4, 300)
    before["data"] = "0x00"
    result = flow.collect_evm(EVM_UNIT, EvmReader([before, evm_log(100, 5, 100, SRC),
                                                evm_log(100, 6, 250)]), pools=POOLS)
    assert result["status"] == "complete" and result["after_complete"] is True
    assert result["before_complete"] is False and result["before_detail"] == "pre_window_undecodable"
    q = quotes_by_asset(result)[QUOTE]
    assert (q["net_before_atomic"], q["net_after_atomic"], q["delta_atomic"]) == (None, "250", None)


def test_prior_transaction_with_missing_order_cannot_be_classified_as_optional(flow):
    before = unsupported_sol_tx("unknown-pre", 990, 1)
    before["transactionIndex"] = None
    result = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[before, source(), sol_tx("post", 1001, 1)], paginationToken=None)]))
    assert result["status"] == "censored" and result["after_complete"] is False


def test_undecodable_post_flow_is_still_censored_and_after_is_unavailable(flow):
    bad = unsupported_sol_tx("unknown-post", 1001, 1)
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[source(), bad], paginationToken=None)]))
    assert result["status"] == "censored" and result["after_complete"] is False


def unrelated_router_swap():
    # Actual captured unused target accounts with other-mint swap CPIs.
    import runpy
    tx = runpy.run_path("tests/test_wallet_universe_sol_venues.py")["captured_tx"]("unused_router")
    real_mint = "H4KUxsEgCp2yDpFvyDuGM5k6XevSKKrrKUxZ3Zr6nyJc"
    tx["transaction"]["signatures"] = ["unrelated-router"]
    tx.update(blockTime=1001, slot=1001, transactionIndex=1)
    for side in ("preTokenBalances", "postTokenBalances"):
        for entry in tx["meta"][side]:
            if entry["mint"] == real_mint: entry["mint"] = MINT
    message = tx["transaction"]["message"]
    message["accountKeys"] = [MINT if key == real_mint else key for key in message["accountKeys"]]
    for field in ("writable", "readonly"):
        tx["meta"]["loadedAddresses"][field] = [MINT if key == real_mint else key
            for key in tx["meta"]["loadedAddresses"][field]]
    return tx


def test_solana_proved_unused_mint_in_router_is_not_an_undecodable_trade(flow):
    result = flow.collect_solana(SOL_UNIT, sol_reader([
        dict(data=[source(), unrelated_router_swap()], paginationToken=None)]))
    assert result["status"] == "complete" and result["after_complete"] is True
    assert result["quotes"] == [] and result["nontrades"] == 1 and result["undecodable"] == 0


@pytest.mark.parametrize("mutation", ["missing_cpis", "incomplete_cpis", "target_touched",
    "target_pool_changed", "missing_token_logs", "truncated_logs", "malformed_instruction"])
def test_solana_unused_mint_requires_complete_unchanged_and_untouched_token_evidence(flow, mutation):
    tx = unrelated_router_swap()
    if mutation == "missing_cpis": tx["meta"].pop("innerInstructions")
    if mutation == "incomplete_cpis": tx["meta"]["innerInstructions"][0]["instructions"].pop()
    if mutation == "target_touched":
        target = next(e["accountIndex"] for e in tx["meta"]["preTokenBalances"] if e["mint"] == MINT)
        child = next(c for g in tx["meta"]["innerInstructions"] for c in g["instructions"]
                     if len(c["accounts"]) >= 4 and base58.b58decode(c["data"])[0:1] == bytes([12]))
        child["accounts"][0] = target
    if mutation == "target_pool_changed":
        next(e for e in tx["meta"]["postTokenBalances"] if e["mint"] == MINT)["uiTokenAmount"]["amount"] = "9999"
    if mutation == "missing_token_logs": tx["meta"]["logMessages"] = ["Program log: Instruction: Swap2"]
    if mutation == "truncated_logs": tx["meta"]["logMessages"].append("Log truncated incorrectly")
    if mutation == "malformed_instruction": tx["meta"]["innerInstructions"][0]["instructions"][0]["accounts"] = None
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[source(), tx], paginationToken=None)]))
    assert result["status"] == "censored" and result["after_complete"] is False
    assert result["undecodable"] == 1 and result["nontrades"] == 0


def test_solana_decode_issues_retain_signature_and_specific_cause(flow):
    tx = unsupported_sol_tx("unsupported-quote-signature", 1001, 1)
    result = flow.collect_solana(SOL_UNIT, sol_reader([dict(data=[source(), tx], paginationToken=None)]))
    issue = result["state"]["issues"][0]
    assert issue["signature"] == "unsupported-quote-signature"
    assert issue["detail"] == "unsupported_target_venue" and issue["order"] == [1001, 1]


def test_evm_decode_issues_retain_exact_transaction_hash(flow):
    tx = evm_log(101, 1, 200)
    tx["data"] = "0x00"
    result = flow.collect_evm(EVM_UNIT, EvmReader([evm_log(100, 5, 100, SRC), tx]), pools=POOLS)
    assert result["state"]["issues"][0]["signature"] == tx["transactionHash"]


RELAY_ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
RELAY_PROXY = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def relay_unit(version="relay-envelope-linked-v1"):
    proof = dict(schema_version=1, proof_version=version, network_id="4663", tx_hash=SRC,
                 block_hash=block_hash(100), block_number=100, mint=TOKEN,
                 beneficiary=EVM_UNIT["address"], payer="0x" + "55" * 20,
                 source_user_id=EVM_UNIT["uid"])
    return {**EVM_UNIT, "source_slot": 100, "source_initiation": proof}


def transfer(source_address, destination, amount=10, *, index=1):
    return dict(address=TOKEN, transactionHash=SRC, blockNumber="0x64", blockHash=block_hash(100),
                transactionIndex="0x5", logIndex=hex(index), removed=False,
                topics=[TRANSFER, "0x" + source_address[2:].zfill(64),
                        "0x" + destination[2:].zfill(64)], data="0x" + format(amount, "064x"))


def relay_reader(unit):
    rows = [evm_log(100, 5, 100, SRC), evm_log(101, 1, 200)]
    reader = EvmReader(rows)
    reader.receipt["from"] = "0x" + "66" * 20
    reader.receipt["to"] = RELAY_PROXY
    reader.receipt["logs"][-1] = transfer(RELAY_ROUTER, unit["address"])
    return reader


@pytest.mark.parametrize("version", ["relay-envelope-linked-v1", "relay-envelope-other-target-v1", "relay-kyber-linked-v1", "relay-origin-paid-v1"])
def test_verified_relay_beneficiary_purchase_keeps_actual_sender_and_measures_flow(flow, version):
    unit = relay_unit(version)
    result = flow.collect_evm(unit, relay_reader(unit), pools=POOLS)
    assert result["status"] == "complete" and result["source_found"] is True
    proof = result["source_proof"]
    assert proof["kind"] == "relay_beneficiary_purchase" and proof["proof_version"] == version
    assert proof["transaction_sender"] == "0x" + "66" * 20
    assert proof["beneficiary_net_atomic"] == "10" and proof["initiating_wallet_verified"] is False
    assert quotes_by_asset(result)[QUOTE]["net_after_atomic"] == "200"


@pytest.mark.parametrize("field,value", [("schema_version", True), ("proof_version", "unknown-relay"),
    ("network_id", "56"), ("tx_hash", "0x" + "77" * 32), ("block_hash", "0x" + "77" * 32),
    ("block_number", 101), ("mint", QUOTE), ("beneficiary", "0x" + "77" * 20),
    ("payer", "0x" + "00" * 20), ("source_user_id", "some-other-wallet")])
def test_relay_proof_must_bind_every_persisted_identity_field(flow, field, value):
    unit = relay_unit()
    unit["source_initiation"][field] = value
    result = flow.collect_evm(unit, relay_reader(unit), pools=POOLS)
    assert result["status"] == "censored" and result["source_found"] is False


@pytest.mark.parametrize("mutation", ["missing_proof_and_unsupported_envelope", "airdrop", "pass_through", "wrong_proxy", "wrong_transfer_anchor", "no_pool_buy"])
def test_relay_unknown_airdrop_and_incoherent_receipts_stay_censored(flow, mutation):
    unit = relay_unit()
    reader = relay_reader(unit)
    if mutation == "missing_proof_and_unsupported_envelope": unit["source_initiation"] = None
    if mutation == "airdrop": reader.receipt["logs"][-1] = transfer("0x" + "77" * 20, unit["address"])
    if mutation == "pass_through":
        reader.receipt["logs"].append(transfer(unit["address"], "0x" + "77" * 20, index=2))
    if mutation == "wrong_proxy": reader.receipt["to"] = "0x" + "77" * 20
    if mutation == "wrong_transfer_anchor": reader.receipt["logs"][-1]["blockHash"] = "0x" + "77" * 32
    if mutation == "no_pool_buy": reader.receipt["logs"][0] = evm_log(100, 5, -100, SRC)
    result = flow.collect_evm(unit, reader, pools=POOLS)
    assert result["status"] == "censored" and result["source_found"] is False


def test_relay_proof_is_part_of_resumable_state_binding(flow):
    unit = relay_unit()
    reader = relay_reader(unit)
    one = flow.collect_evm(unit, reader, pools=POOLS)
    changed = deepcopy(unit)
    changed["source_initiation"]["payer"] = "0x" + "77" * 20
    with pytest.raises(ValueError, match="state_identity_mismatch"):
        flow.collect_evm(changed, reader, pools=POOLS, state=one["state"])


def historical_relay_reader(monkeypatch):
    import gzip
    from pathlib import Path
    from eth_utils import keccak
    from sniper.services import wallet_universe_relay as relay
    frame=json.load(gzip.open(Path(__file__).parent/"fixtures"/
        "relay_other_target_4663_2026-09-28.json.gz","rt"))["buys"][0]
    receipt,transaction=deepcopy(frame["receipt"]),deepcopy(frame["tx"])
    block,index=int(receipt["blockNumber"],16),int(receipt["transactionIndex"],16)
    t0=int(receipt["logs"][0]["blockTimestamp"],16)
    unit=dict(chain=frame["chain"],uid="historical-beneficiary",address=frame["wallet"],
        mint=frame["mint"],signature=transaction["hash"],t0=t0,source_slot=block,source_initiation=None)
    # An actual source Swap determines the known source-venue metadata for this fixture.
    from sniper.services.memecoin_evm_v3_events import V3_SWAP
    event=next(l for l in receipt["logs"] if l["topics"][0]==V3_SWAP)
    words=[int(event["data"][i:i+64],16) for i in range(2,130,64)]
    signed=[v-(1<<256) if v>=1<<255 else v for v in words]
    quote_side=0 if signed[0]>0 else 1
    quote="0x"+"22"*20
    pool=dict(pool=event["address"],protocol="v3",quote_side=quote_side,
        token0=quote if quote_side==0 else unit["mint"],
        token1=unit["mint"] if quote_side==0 else quote,token0_decimals=18,token1_decimals=18)
    digest="0x"+keccak(bytes.fromhex("6000")).hex()
    monkeypatch.setattr(relay,"CODE_HASHES",tuple((a,digest) for a,_ in relay.CODE_HASHES))
    calls=[]
    def call(method,params):
        calls.append((method,deepcopy(params)))
        if method=="eth_getTransactionReceipt": return deepcopy(receipt)
        if method=="eth_getTransactionByHash": return deepcopy(transaction)
        if method=="eth_getCode":
            assert params[1]==receipt["blockNumber"]
            return "0x6000"
        if method=="eth_blockNumber": return hex(block+120)
        if method=="eth_getBlockByNumber":
            number=int(params[0],16)
            return dict(number=hex(number),timestamp=hex(t0+(number-block)),
                hash=receipt["blockHash"] if number==block else block_hash(number))
        if method=="eth_getLogs": return [deepcopy(event)]
        pytest.fail(method)
    return unit,receipt,transaction,pool,call,calls


def test_historical_relay_recovers_research_proof_without_changing_original_unit_binding(flow,monkeypatch):
    unit,receipt,_,pool,call,calls=historical_relay_reader(monkeypatch)
    result=flow.collect_evm(unit,call,pools=[pool])
    assert result["status"]=="complete" and result["source_found"] is True
    assert unit["source_initiation"] is None and result["state"]["binding"]["source_initiation"] is None
    research=result["research_source_proof"]
    assert research==result["state"]["research_source_proof"]
    assert research["code_basis"]=="receipt_block" and research["beneficiary_only"] is True
    assert result["source_proof"]["initiating_wallet_verified"] is False
    assert result["source_proof"]["third_party_payment_possible"] is True
    resumed=flow.collect_evm(unit,call,pools=[pool],state=json.loads(json.dumps(result["state"])))
    assert resumed["status"]=="complete" and resumed["research_source_proof"]==research
    assert sum(method=="eth_getCode" for method,_ in calls)==2
    assert sum(method=="eth_getTransactionReceipt" for method,_ in calls)==2


def test_historical_relay_budget_stop_retains_first_code_read_and_exact_unit_binding(flow,monkeypatch):
    unit,_,_,pool,base,calls=historical_relay_reader(monkeypatch)
    def call(method,params):
        if method=="eth_getCode" and sum(m==method for m,_ in calls)==1:
            raise ValueError("budget_exhausted")
        return base(method,params)
    first=flow.collect_evm(unit,call,pools=[pool])
    assert first["status"]=="partial" and first["source_found"] is False
    assert first["state"]["binding"]["source_initiation"] is None
    assert len(first["state"]["research_relay_state"]["code"])==1
    final=flow.collect_evm(unit,base,pools=[pool],state=json.loads(json.dumps(first["state"])))
    assert final["status"]=="complete" and final["research_source_proof"]["beneficiary_only"] is True


def test_historical_relay_resume_rechecks_current_canonical_receipt_transfer(flow,monkeypatch):
    unit,receipt,_,pool,call,_=historical_relay_reader(monkeypatch)
    first=flow.collect_evm(unit,call,pools=[pool])
    assert first["status"]=="complete"
    incoming=next(l for l in receipt["logs"] if l["address"]==unit["mint"] and len(l["topics"])==3
        and l["topics"][2].lower().endswith(unit["address"][2:]))
    incoming["topics"][1]="0x"+"00"*12+"77"*20
    second=flow.collect_evm(unit,call,pools=[pool],state=first["state"])
    assert second["status"]=="censored" and second["source_found"] is False


def test_historical_relay_unavailable_block_code_is_partial_without_head_or_zero_flow(flow,monkeypatch):
    unit,_,_,pool,base,calls=historical_relay_reader(monkeypatch)
    def call(method,params):
        if method=="eth_getCode":
            calls.append((method,deepcopy(params)))
            return "0x"
        return base(method,params)
    result=flow.collect_evm(unit,call,pools=[pool])
    assert result["status"]=="partial" and result["source_found"] is False
    assert result["quotes"]==[] and result["after_complete"] is False
    assert result["detail"]=="research_relay_historical_code_unavailable"
    assert all(params[1]!="latest" for method,params in calls if method=="eth_getCode")


@pytest.mark.parametrize("mutation", ["absent", "wrong_recipient", "net_zero", "wrong_hash", "duplicate"])
def test_direct_source_buy_requires_canonical_positive_net_mint_delivery(flow, mutation):
    reader = EvmReader([evm_log(100, 5, 100, SRC)])
    incoming = transfer(POOL, EVM_UNIT["address"])
    reader.receipt["logs"] = [evm_log(100, 5, 100, SRC), incoming]
    if mutation == "absent": reader.receipt["logs"].pop()
    if mutation == "wrong_recipient": incoming["topics"][2] = "0x" + "77" * 32
    if mutation == "net_zero": reader.receipt["logs"].append(transfer(EVM_UNIT["address"], POOL, index=2))
    if mutation == "wrong_hash": incoming["blockHash"] = "0x" + "77" * 32
    if mutation == "duplicate": reader.receipt["logs"].append(deepcopy(incoming))
    result = flow.collect_evm(EVM_UNIT, reader, pools=POOLS)
    assert result["status"] == "censored" and result["source_found"] is False


@pytest.mark.parametrize("blocks_per_second,budget", [(2, 24), (10, 44)])
def test_dense_evm_boundaries_bracket_near_source_with_small_header_budget(flow, blocks_per_second, budget):
    source_block, t0 = 125_700_000, 1_790_000_000
    unit = {**EVM_UNIT, "t0": t0}
    lower = source_block - 60 * blocks_per_second
    upper = source_block + 60 * blocks_per_second + blocks_per_second - 1
    rows = [evm_log(lower, 1, 300), evm_log(source_block, 5, 100, SRC), evm_log(upper, 1, 200)]
    receipt = EvmReader(rows).receipt
    incoming = transfer(POOL, EVM_UNIT["address"])
    incoming.update(blockNumber=hex(source_block), blockHash=block_hash(source_block))
    receipt.update(blockNumber=hex(source_block), blockHash=block_hash(source_block), logs=[rows[1], incoming])
    header_reads = 0
    def call(method, params):
        nonlocal header_reads
        if method == "eth_getTransactionReceipt": return deepcopy(receipt)
        if method == "eth_blockNumber": return hex(source_block + 100_000)
        if method == "eth_getBlockByNumber":
            header_reads += 1
            if header_reads > budget: raise ValueError("header_budget")
            number = int(params[0], 16)
            return dict(number=hex(number), hash=block_hash(number),
                        timestamp=hex(t0 + (number - source_block) // blocks_per_second))
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            assert lower <= lo <= upper and hi == min(lo + 399, upper)
            return deepcopy([row for row in rows if lo <= int(row["blockNumber"], 16) <= hi])
        pytest.fail(method)
    result = flow.collect_evm(unit, call, pools=POOLS)
    assert result["status"] == "complete" and result["after_complete"] is True
    assert result["state"]["from_block"] == lower and result["state"]["to_block"] == upper
    assert quotes_by_asset(result)[QUOTE]["delta_atomic"] == "-100"


class EndpointReader(EvmReader):
    """Block 107 repeats block 106's timestamp (t0+60), the window's inclusive upper endpoint."""

    def __init__(self, head):
        super().__init__([evm_log(100, 5, 100, SRC), evm_log(106, 1, 100), evm_log(107, 1, 900)])
        self.head = head

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            if n > self.head:
                return None
            return dict(number=hex(n), timestamp=hex(1060 if n == 107 else n * 10), hash=block_hash(n))
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            return deepcopy([r for r in self.logs if lo <= int(r["blockNumber"], 16) <= hi])
        return super().__call__(method, params)


def test_evm_window_is_not_sealed_until_the_head_is_strictly_past_its_end(flow):
    # Codex W6: sealed through head 106 (stamped t0+60), then block 107 arrived with the same stamp;
    # the resumed range stayed at 100 atoms while a fresh read found 1,000.
    first = flow.collect_evm(EVM_UNIT, EndpointReader(106), pools=POOLS)
    assert first["status"] == "partial" and first["state"].get("to_block") is None
    resumed = flow.collect_evm(EVM_UNIT, EndpointReader(108), pools=POOLS, state=first["state"])
    fresh = flow.collect_evm(EVM_UNIT, EndpointReader(108), pools=POOLS)
    assert resumed["status"] == fresh["status"] == "complete"
    assert resumed["state"]["to_block"] == fresh["state"]["to_block"] == 107
    assert resumed["quotes"] == fresh["quotes"]


def test_an_old_completed_range_without_its_upper_bracket_is_collected_again(flow):
    # Codex night review W6 residual: a range completed under the old rule (sealed at a head stamped
    # t0+60) was trusted forever. Simulate the old cache: complete, to_block at that head, no header
    # for the next block. The resumed read must equal a fresh one.
    fresh = flow.collect_evm(EVM_UNIT, EndpointReader(108), pools=POOLS)
    old = deepcopy(fresh["state"])
    old["to_block"] = 106
    old["headers"].pop("107", None)
    old["headers"].pop("108", None)
    resumed = flow.collect_evm(EVM_UNIT, EndpointReader(108), pools=POOLS, state=old)
    assert resumed["status"] == "complete" and resumed["state"]["to_block"] == 107
    assert resumed["quotes"] == fresh["quotes"]


def test_a_valid_completed_range_is_not_read_again(flow):
    fresh = flow.collect_evm(EVM_UNIT, EndpointReader(108), pools=POOLS)
    calls = []

    def counting(method, params):
        calls.append(method)
        return EndpointReader(108)(method, params)
    again = flow.collect_evm(EVM_UNIT, counting, pools=POOLS, state=deepcopy(fresh["state"]))
    assert again["quotes"] == fresh["quotes"] and "eth_getLogs" not in calls
