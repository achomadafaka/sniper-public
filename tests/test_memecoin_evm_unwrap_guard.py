"""WETH unwrap on the funded entry path (2026-09-28, Alpha GO; reservation 4158 on 4663).

The 4663 unwrap for attempt ad2304bf was mined and REVERTED, and the reservation then held its slot
for 11 minutes as "ambiguous" until the stale pass read the same failed receipt. The gas burned was
exactly 60,000 x 20,098,463 wei, i.e. the whole fixed 60k limit: most likely out of gas (4663 is
Nitro, where gasUsed includes the L1 data component). The receipt's gasUsed is unverified. It is
1 tx_failed against 242 accepted 4663 unwrap pipelines since 09-01.

(1) An unwrap larger than the WETH the executor just read is refused before anything is signed.
(2) A pre-transaction receipt that says FAILED releases the reservation at once
    (pre_transaction_reverted); not_found and unknown stay ambiguous.
(3) Every funded WETH withdraw/deposit uses one limit, WETH_CALL_GAS_LIMIT = 150,000.
Plus trace facts on every unwrap: amount, WETH balance, the executor's ETH/USD (P2) and the gas
limit; the gate's ETH/USD (P1) goes on the gate's balance-read mark.
"""
import ast
import pathlib

import pytest

from sniper.execution import evm_real_seams as S
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_gas_topup as G
from test_memecoin_evm_entry_executor import _QUOTE, _TraceRecorder, _c, _seams

AMOUNT = 10 ** 16


def _pre(amount=AMOUNT):
    return {"to": "0xweth", "data": "0x2e1a7d4d" + format(amount, "064x"),
            "value": "0x0", "gas": str(G.WETH_CALL_GAS_LIMIT)}


def _usdc_with_raw(raw):
    values = iter((100.0, 90.0))

    def fn():
        return next(values, 90.0)
    fn.last_raw = lambda: raw
    return fn


def _enter(*, raw, pipeline=None, pipeline_absent=False, amount=AMOUNT, trace=None,
           native_float_fn=None):
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663")
    quote = {**_QUOTE, "pre_transactions": [_pre(amount)]}
    if native_float_fn is not None:
        quote["transaction"] = dict(_QUOTE["transaction"], value=hex(amount))
    seams, calls = _seams(quote=quote)
    if raw != "absent":
        seams["usdc_fn"] = _usdc_with_raw(raw)
    seen = []

    def default_pipeline(pre_tx):
        seen.append(pre_tx)
        m.set_pending_broadcast(c, pid, "0xpre", 6, "2026-07-09T00:00:01Z")
        return {"status": "accepted", "tx_hash": "0xpre", "nonce": 6}
    kwargs = {} if pipeline_absent else {"native_pre_pipeline_fn": pipeline or default_pipeline}
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        sell_token_usd=2655.73, trace=trace, native_float_fn=native_float_fn, **kwargs, **seams)
    row = dict(c.execute("SELECT status, resolution_reason, recovery_status "
                         "FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone())
    return result, calls, seen, row


# --- (1) the guard ------------------------------------------------------------------------------

def test_an_unwrap_above_the_weth_balance_refuses_before_any_signature():
    result, calls, seen, row = _enter(raw=AMOUNT - 1)
    assert result["status"] == "aborted" and result["reason"] == "unwrap exceeds quote balance"
    assert seen == [] and calls["sign"] == 0 and calls["send"] == 0
    assert (row["status"], row["resolution_reason"]) == ("failed", "unwrap_exceeds_quote_balance")


def test_the_sequential_unwrap_path_is_guarded_too():
    result, calls, _seen, row = _enter(raw=AMOUNT - 1, pipeline_absent=True)
    assert result["reason"] == "unwrap exceeds quote balance" and calls["sign"] == 0
    assert row["resolution_reason"] == "unwrap_exceeds_quote_balance"


@pytest.mark.parametrize("raw", [AMOUNT, AMOUNT + 1, None, "absent"])
def test_an_unwrap_within_or_of_unknown_balance_proceeds_as_today(raw):
    """Equal is allowed; an unreadable (None) or unavailable raw balance is unknown, never zero."""
    result, calls, seen, _row = _enter(raw=raw)
    assert result["status"] == "filled" and len(seen) == 1


# --- (2) a reverted pre-transaction releases at once ---------------------------------------------

def test_a_reverted_unwrap_releases_the_reservation_immediately():
    result, calls, _seen, row = _enter(
        raw=AMOUNT, pipeline=lambda _p: {"status": "reverted", "tx_hash": "0xpre", "nonce": 6})
    assert result["status"] == "aborted" and "reverted" in result["reason"]
    assert calls["sign"] == 0 and calls["send"] == 0, "the swap is never signed"
    assert (row["status"], row["resolution_reason"]) == ("failed", "pre_transaction_reverted")


@pytest.mark.parametrize("status", ["unknown", "ambiguous"])
def test_an_unknown_unwrap_stays_ambiguous_exactly_as_today(status):
    result, calls, _seen, row = _enter(
        raw=AMOUNT, pipeline=lambda _p: {"status": status, "tx_hash": "0xpre", "nonce": 6})
    assert result["status"] == "unknown" and result["reason"] == "native pre-transaction ambiguous"
    assert row["status"] == "reserved" and row["recovery_status"] == "native_pre_transaction_ambiguous"


@pytest.mark.parametrize("receipt, status", [
    ("confirmed", "confirmed"), ("failed", "reverted"), ("not_found", "unknown"),
    ("unknown", "unknown"), (None, "unknown")])
def test_the_pipeline_maps_only_a_failed_receipt_to_reverted(receipt, status):
    assert S.pre_transaction_status(receipt) == status


# --- trace facts ----------------------------------------------------------------------------------

def test_every_unwrap_records_its_amount_balance_price_and_gas_limit():
    trace = _TraceRecorder()
    _enter(raw=AMOUNT + 5, trace=trace)
    facts = [md for name, md in trace.substages if name == "evm.unwrap_facts"]
    assert facts == [{"outcome": "ok", "attempt_no": 1, "unwrap_amount_wei": AMOUNT,
                      "weth_balance_wei": AMOUNT + 5, "quote_price_usd": 2655.73,
                      "unwrap_gas_limit": G.WETH_CALL_GAS_LIMIT}]
    refused = _TraceRecorder()
    _enter(raw=AMOUNT - 1, trace=refused)
    assert [md["outcome"] for name, md in refused.substages if name == "evm.unwrap_facts"] == [
        "refused"]


def test_the_pipeline_mark_carries_the_signed_gas_price_and_limit():
    trace = _TraceRecorder()
    _enter(raw=AMOUNT, trace=trace, pipeline=lambda _p: {
        "status": "reverted", "tx_hash": "0xpre", "nonce": 6,
        "gas_price_wei": 20_098_463, "gas_limit": 150_000})
    mark = [md for name, md in trace.substages if name == "evm.pre_transaction_pipeline"][0]
    assert (mark["gas_price_wei"], mark["gas_limit"]) == (20_098_463, 150_000)


def test_the_signed_gas_is_read_from_the_bytes_without_a_network_call():
    from eth_account import Account
    acct = Account.create()
    signed = acct.sign_transaction({"to": "0x" + "11" * 20, "data": "0x2e1a7d4d" + "00" * 32,
                                    "value": 0, "gas": 150_000, "gasPrice": 20_098_463,
                                    "nonce": 2513, "chainId": 4663})
    raw = "0x" + bytes(signed.raw_transaction).hex()
    assert S.signed_gas_facts(raw) == {"gas_price_wei": 20_098_463, "gas_limit": 150_000}
    assert S.signed_gas_facts("0xnot-a-transaction") == {}


def test_the_executor_balance_keeps_the_raw_word_it_read(monkeypatch):
    from eth_account import Account
    words = {"answer": hex(AMOUNT)}

    def http(_method, _url, headers=None, body=None, **_kw):
        return 200, ({"result": words["answer"]} if words["answer"] else {"error": "boom"}), None
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
                                   http=http, price_fn=lambda: 2655.73)
    balance_fn, price = ex["execution_balance_usd_fn"](2655.73)
    assert price == 2655.73
    assert balance_fn() == pytest.approx(AMOUNT / 1e18 * 2655.73) and balance_fn.last_raw() == AMOUNT
    words["answer"] = None
    assert balance_fn() == 0.0 and balance_fn.last_raw() is None, "a failed read is unknown, not 0"


def test_the_gate_price_can_be_read_without_fetching(monkeypatch):
    from eth_account import Account
    fetched = []
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
                                   http=lambda *a, **k: (200, {"result": "0x0"}, None),
                                   price_fn=lambda: fetched.append(1) or 2655.73)
    assert ex["quote_price_peek_fn"]() is None and fetched == []
    ex["warm_quote_price_fn"]()
    peek = ex["quote_price_peek_fn"]()
    assert peek["px"] == 2655.73 and peek["age_ms"] >= 0 and fetched == [1]


# --- (3) one gas limit for every funded WETH call ---------------------------------------------------

def test_one_named_limit_for_every_weth_call():
    assert G.WETH_CALL_GAS_LIMIT == 150_000
    assert G.UNWRAP_GAS_LIMIT == G.WETH_CALL_GAS_LIMIT == G.WRAP_GAS_LIMIT


_WETH_MODULES = ("evm_v4_quote", "evm_v4_multihop_tx", "evm_pons_v2_quote", "evm_launchpad_curve",
                 "evm_flap_curve", "evm_real_seams")
_WETH_MARKERS = ("2e1a7d4d", "d0e30db0", "WITHDRAW", "encode_withdraw")


@pytest.mark.parametrize("module", _WETH_MODULES)
def test_no_weth_call_carries_a_literal_gas_limit(module):
    """Every dict that builds a WETH withdraw/deposit takes its gas from the constant."""
    path = pathlib.Path(__file__).resolve().parents[1] / "sniper" / "execution" / (module + ".py")
    src = path.read_text()
    found = 0
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value: v for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "gas" not in keys or "data" not in keys:
            continue
        data_src = ast.get_source_segment(src, keys["data"]) or ""
        if not any(marker in data_src for marker in _WETH_MARKERS):
            continue
        found += 1
        assert not isinstance(keys["gas"], ast.Constant), (
            "%s:%d literal gas on a WETH call" % (module, node.lineno))
    assert found, "no WETH call found in %s -- the guard would pass vacuously" % module


def test_the_higher_limit_is_affordable_at_tonights_native_balance():
    """4663 at 03:12Z: native 2,944,329,114,944,000 wei; the reverted unwrap's price 20,098,463."""
    native, gas_price = 2_944_329_114_944_000, 20_098_463
    need = G.WETH_CALL_GAS_LIMIT * gas_price
    assert need == 3_014_769_450_000 and need < native / 900


# --- the gate's own ETH/USD read (P1) -------------------------------------------------------------

@pytest.mark.parametrize("peek, fields", [
    (lambda: {"px": 2655.73, "age_ms": 12.0}, {"quote_price_usd": 2655.73, "quote_price_age_ms": 12.0}),
    (lambda: None, {}), (None, {})])
def test_the_gate_balance_mark_carries_the_gate_price(monkeypatch, peek, fields):
    from types import SimpleNamespace
    from sniper.services import memecoin_evm_leg as leg
    from sniper.services import memecoin_real_runner as R
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(R, "_gate_balance_cache", {})
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS_EVM", False, raising=False)
    monkeypatch.setattr(leg, "evm_real_entry", lambda conn, ev, **kw: {"status": "unknown"})
    ctx = {"usdc_fn": lambda: 28.68, "native_fn": lambda: 0.0029,
           "token_safety_fn": lambda mint, **kw: (True, "ok"),
           "execute_fn": lambda *a, **k: {"status": "unknown"}, "kill_path": None}
    if peek is not None:
        ctx["quote_price_peek_fn"] = peek
    marks = []
    trace = SimpleNamespace(attempt_id="a", mark=lambda *a, **k: True, finish=lambda *a, **k: True,
                            mark_substage=lambda name, **kw: marks.append({"name": name, **kw}))
    R._make_evm_entry_fn({"4663": ctx})(None, SimpleNamespace(mint="0xabc"), route="4663",
                                        day="2026-09-28", now_iso="2026-09-28T03:01:31+00:00",
                                        trade_size=10.0, trace=trace)
    gate = [x for x in marks if x["name"] == "timing_gate_balance_read"]
    assert gate and {k: gate[0][k] for k in fields} == fields
    if not fields:
        assert "quote_price_usd" not in gate[0]


def test_the_evm_leg_context_hands_the_gate_its_price_peek():
    import inspect
    from sniper.services import memecoin_evm_leg as leg
    assert '"quote_price_peek_fn": ex.get("quote_price_peek_fn")' in inspect.getsource(
        leg.build_real_evm_context)


def test_real_probe_shortfall_facts_are_retained_on_existing_unwrap_refusal_trace():
    from sniper.execution.evm_native_float import NativeFloatProbe
    floor = 3_300_000_000_000_000
    short = 10 ** 15
    native = AMOUNT + floor - short
    probe = NativeFloatProbe(lambda: native, quote_token='0xweth',
        gas_price_fn=lambda: 1, low_wei=0, partial=True, reserve_floor_wei=floor,
        book_facts={'book_count': 0, 'book_pending_count': 0, 'book_open_count': 0})
    trace = _TraceRecorder()
    result, calls, _, _ = _enter(raw=short - 1, trace=trace,
                                 native_float_fn=lambda quote: probe)
    assert result['reason'] == 'unwrap exceeds quote balance'
    assert calls['sign'] == 0 and calls['send'] == 0
    facts = [row for name, row in trace.substages if name == 'evm.unwrap_facts'][0]
    assert facts['native_balance_wei'] == native
    assert facts['weth_balance_wei'] == short - 1
    assert facts['book_floor_wei'] == floor
    assert facts['required_native_keep_wei'] == floor
    assert facts['shortfall_wei'] == short
    assert facts['book_pending_count'] == 0


def test_gate_price_trace_keeps_prepared_authority_and_original_age():
    from sniper.services.memecoin_real_runner import _gate_price_fields
    tick = {"px": 740.0, "age_ms": 900.0, "source": "native_price_prepared"}
    assert _gate_price_fields({"quote_price_peek_fn": lambda: tick}) == {
        "quote_price_usd": 740.0, "quote_price_age_ms": 900.0,
        "quote_price_source": "native_price_prepared"}
