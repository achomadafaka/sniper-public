"""8453 PICNIC af22bb097bbb (2026-10-06): the plain (non-pipeline) unwrap path took a flashblocks
PRECONFIRMATION receipt (340 ms; Base seals every 2 s) as "confirmed", then sent the swap to custody,
which still held the pre-unwrap ETH: insufficient funds. 3 of 3 8453 unwrap entries since 09-15
failed this way. After a confirmed unwrap, the swap waits until every healthy pool member shows the
ETH (the 4663 pipeline's check), or stops ambiguous and re-wraps. Entries with no unwrap are
unchanged: no balance read at all."""
import json
import sqlite3

import pytest
from eth_account import Account
from eth_utils import keccak

from sniper.execution import evm_real_seams as seams, evm_rpc_pool as pool
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

PRECONF, CUSTODY = "https://preconf.invalid", "https://custody.invalid"
AMOUNT = 10 * 10**18


@pytest.fixture
def entry(monkeypatch):
    pool.reset_for_tests()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"8453": (CUSTODY, PRECONF)})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_NATIVE_VISIBLE_WAIT_S", 0.6)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False)
    for name in ("MEMECOIN_EVM_V2_ENTRY_ENABLED", "MEMECOIN_EVM_V4_ENABLED",
                 "MEMECOIN_EVM_RELAY_ENABLED", "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED"):
        monkeypatch.setattr(cfg, name, False)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663")
    monkeypatch.setattr(seams, "quote_usd_price", lambda *_a, **_k: 1.0)
    for url in (CUSTODY, PRECONF):
        pool.mark_identity(url, True)
        for _ in range(5):
            pool.record(url, 1, True)

    def run(*, custody_balance, unwrap=True, visible_after=None, fee=None, extra_pre=False):
        # `fee`: the unwrap receipt's gas fields; the chain then debits what they say was paid,
        # as a real gas-paying unwrap does (EVM-01). None = today's gas-free fake.
        fee = dict(fee or {})
        debit = (int(fee.get("debit", 0)) if "debit" in fee
                 else int(fee.get("gasUsed", "0x0"), 16) * int(fee.get("effectiveGasPrice", "0x0"), 16)
                 + int(fee.get("l1Fee", "0x0"), 16))
        fee.pop("debit", None)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(conn)
        mint = "0x" + "33" * 20
        pid = m.reserve_pending(conn, mint=mint, source_user_id="u", strategy="alpha_b",
                                notional=10.0, reserved_at="2026-10-06T21:15:43Z", network_id="8453")
        requests, sends = [], []
        weth = seams.CHAIN_QUOTE["8453"]["token"]
        reads = {"custody": 0}

        def quote(_sell, _buy, _amount, **_kwargs):
            return {"sellAmount": str(AMOUNT), "buyAmount": "5000000000000000000",
                    "issues": {"allowance": None},
                    "transaction": {"to": "0x" + "44" * 20, "data": "0x", "value": hex(AMOUNT)},
                    "pre_transactions": ([{"to": weth, "data": "0x2e1a7d4d" + f"{AMOUNT:064x}",
                                           "value": "0x0", "gas": 150000}] if unwrap else [])
                    + ([{"to": "0x" + "55" * 20, "data": "0x095ea7b3" + "00" * 64,
                         "value": "0x0", "gas": 60000}] if extra_pre else [])}

        def http(_method, url, body=None, timeout=10, **_kwargs):
            req = json.loads(body)
            method = req["method"]
            requests.append((method, url))
            if method == "eth_chainId":
                result = "0x2105"
            elif method == "eth_getTransactionCount":
                result = "0x7"
            elif method in ("eth_gasPrice", "eth_maxPriorityFeePerGas"):
                result = "0x3b9aca00"
            elif method == "eth_sendRawTransaction":
                sends.append(req["params"][0])
                result = "0x" + keccak(hexstr=req["params"][0]).hex().removeprefix("0x")
            elif method == "eth_getTransactionReceipt":
                result = {"status": "0x1", "transactionHash": req["params"][0], "blockNumber": "0x1"}
            elif method == "eth_getBalance":
                if url == CUSTODY:
                    reads["custody"] += 1
                    shown = (custody_balance + AMOUNT - debit
                             if visible_after is not None and reads["custody"] > visible_after
                             else custody_balance)
                else:
                    shown = custody_balance + AMOUNT - debit   # the preconf node already has it
                result = hex(shown)
            elif method in ("eth_getTransactionByHash", "eth_getBlockByNumber"):
                result = None
            elif method == "eth_call":
                result = "0x0"
            elif method == "eth_estimateGas":
                result = "0x30d40"
            else:
                raise AssertionError(method)
            return 200, {"result": result}, None

        balances = iter([100.0, 90.0])
        tokens = iter([0.0, 5.0])
        from evm_receipt_fake import wrap
        acct = Account.create()
        executor = seams.build_real_evm_executor(
            acct=acct, chain_id="8453", rpc_url=CUSTODY,
            http=wrap(http, owner=acct.address, mint=mint, quote=weth, coin=5,
                      value=hex(AMOUNT), receipt_extra=fee or None),  # the swap's receipt: native paid
            venue_quote_fn=quote, usdc_fn=lambda: next(balances, 90.0),
            token_fn=lambda _mint: next(tokens, 5.0))
        result = executor["execute_fn"](conn, pid, mint, 10.0, "2026-10-06T21:15:44Z")
        row = m.pending_row(conn, pid)
        conn.close()
        return result, row, requests, sends

    yield run
    pool.reset_for_tests()


def test_a_preconfirmed_unwrap_custody_cannot_see_never_signs_the_swap(entry):
    result, row, requests, sends = entry(custody_balance=0)
    assert result["status"] == "unknown" and result["reason"] == "native pre-transaction ambiguous"
    assert row["recovery_status"] == "native_pre_transaction_ambiguous"
    assert row["cleanup_status"] == "confirmed"          # the landed ETH is re-wrapped
    assert len(sends) == 2                                # the unwrap and the re-wrap, never a swap
    methods = [method for method, _url in requests]
    send, receipt = methods.index("eth_sendRawTransaction"), methods.index("eth_getTransactionReceipt")
    assert methods.index("eth_getBalance") < send          # the pre-unwrap baseline, once
    assert "eth_getBalance" in methods[receipt:]           # visibility only after the receipt


def test_a_wallet_already_holding_the_value_still_waits_for_the_unwrap(entry):
    """Gamma LOW on 883012fdb: native already >= the unwrap but < value + gas passed a
    `balance >= unwrap` check at once, while custody still had the pre-unwrap balance. The need is
    the pre-unwrap native PLUS the unwrap."""
    result, row, _requests, sends = entry(custody_balance=AMOUNT)
    assert result["status"] == "unknown" and row["recovery_status"] == "native_pre_transaction_ambiguous"
    assert len(sends) == 2                                # the unwrap and the re-wrap, never a swap
    result, row, _requests, sends = entry(custody_balance=AMOUNT, visible_after=2)
    assert result["status"] == "filled" and len(sends) == 2


def test_the_swap_goes_out_once_custody_shows_the_eth(entry):
    result, row, _requests, sends = entry(custody_balance=0, visible_after=2)
    assert result["status"] == "filled" and row["status"] == "confirmed"
    assert len(sends) == 2                                # the unwrap and the swap


def test_an_entry_with_no_unwrap_reads_no_balance(entry):
    result, _row, requests, sends = entry(custody_balance=0, unwrap=False)
    assert not any(method == "eth_getBalance" for method, _url in requests)
    assert len(sends) == 1


def test_the_plain_path_polls_each_member_no_faster_than_every_quarter_second(entry):
    _result, _row, requests, _sends = entry(custody_balance=0)
    custody_reads = [r for r in requests if r == ("eth_getBalance", CUSTODY)]
    # one pre-unwrap baseline, then 0.6 s budget at >= 0.25 s per round: at most 3 rounds (+1)
    assert 2 <= len(custody_reads) <= 5, len(custody_reads)


def _plain_enter(visible_fn, trace=None):
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _seams
    from test_memecoin_evm_unwrap_guard import _pre
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="8453")
    seams_kw, calls = _seams(quote={**_QUOTE, "pre_transactions": [_pre()]})
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-07-09T00:00:01Z", sell_token_usd=2655.73,
        trace=trace, native_visible_fn=visible_fn, **seams_kw)
    return result, calls


def test_a_broken_visibility_check_falls_back_to_todays_send():
    """The check is a wait, never a new way to lose an entry: if it raises, the swap goes as before."""
    def broken(_wei):
        raise RuntimeError("bug")
    result, _calls = _plain_enter(broken)
    assert result["status"] == "filled"


def test_the_wait_is_recorded_as_evm_native_visible():
    from test_memecoin_evm_entry_executor import _TraceRecorder
    trace = _TraceRecorder()
    seen = []
    result, _calls = _plain_enter(lambda wei: seen.append(wei) or True, trace=trace)
    assert result["status"] == "filled" and seen == [10 ** 16]
    marks = [kw for name, kw in trace.substages if name == "evm.native_visible"]
    assert len(marks) == 1 and marks[0]["visible"] is True and marks[0]["need_wei"] == 10 ** 16
    assert marks[0]["native_before_wei"] is None          # no baseline reader: need = the unwrap
    assert marks[0]["wait_ms"] >= 0


# X402ID (8453, reservation 4885, 2026-10-07): unwrap 0x38608bb1 paid gasUsed 35,140 x 0.009 gwei
# + l1Fee 8,243,662,207 wei, so custody showed exactly need - 324,503,662,207 wei for 3 s and the
# entry stopped ambiguous. A settled gas-paying unwrap is before + unwrap - paid, never more.
X402ID_FEE = {"gasUsed": hex(35140), "effectiveGasPrice": hex(9_000_000), "l1Fee": hex(8_243_662_207)}
GAS_FLOAT = 10 ** 16                                     # native the wallet held to pay for gas


def test_a_gas_paying_unwrap_custody_shows_lets_the_swap_go(entry):
    result, row, _requests, sends = entry(custody_balance=GAS_FLOAT, visible_after=1, fee=X402ID_FEE)
    assert result["status"] == "filled" and row["status"] == "confirmed"
    assert len(sends) == 2                                # the unwrap and the swap


def test_a_stale_custody_still_stops_ambiguous_with_gas_paid(entry):
    """The baseline stays: a node still at the pre-unwrap balance never passes."""
    result, row, _requests, sends = entry(custody_balance=GAS_FLOAT, fee=X402ID_FEE)
    assert result["status"] == "unknown" and row["recovery_status"] == "native_pre_transaction_ambiguous"
    assert row["cleanup_status"] == "confirmed" and len(sends) == 2


@pytest.mark.parametrize("fields", [
    {"gasUsed": hex(35140), "effectiveGasPrice": hex(9_000_000), "debit": 324_503_662_207},  # no l1Fee
    {"effectiveGasPrice": hex(9_000_000), "l1Fee": hex(8_243_662_207), "debit": 324_503_662_207},
    {"gasUsed": "0xzz", "effectiveGasPrice": hex(9_000_000), "debit": 324_503_662_207},
])
def test_an_unreadable_fee_keeps_the_full_need_and_stops_ambiguous(entry, fields):
    """No guessed tolerance: a receipt that cannot price the whole fee leaves need at before + unwrap,
    so a real debit stops the entry ambiguous (re-wrapped), never sends against a short balance."""
    result, row, _requests, sends = entry(custody_balance=GAS_FLOAT, visible_after=1, fee=fields)
    assert result["status"] == "unknown" and row["cleanup_status"] == "confirmed"
    assert len(sends) == 2


def test_a_later_pre_transaction_is_sent_only_after_the_unwrap_check(entry):
    """Alpha's ordering edge: the check runs in the unwrap's own iteration, so a gas-paying
    pre-transaction after it cannot lower the balance inside the window it measures."""
    result, _row, requests, sends = entry(custody_balance=GAS_FLOAT, visible_after=1,
                                          fee=X402ID_FEE, extra_pre=True)
    assert result["status"] == "filled" and len(sends) == 3     # unwrap, the later pre-tx, swap
    sent_at = [i for i, (method, _u) in enumerate(requests) if method == "eth_sendRawTransaction"]
    checks = [i for i, r in enumerate(requests) if r == ("eth_getBalance", CUSTODY)]
    assert checks[-1] < sent_at[1]          # every visibility read precedes the second send
