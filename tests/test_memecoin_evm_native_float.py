"""4663 native-ETH float (spec docs/superpowers/specs/2026-09-27-memecoin-4663-native-float-design.md).

84 of 112 funded 4663 fills since 2026-09-13 paid a WETH.withdraw pre-transaction (p50 668 ms /
p90 1.32 s) before the swap, because the V4, pons and launchpad builders attach it to every
WETH-quoted buy. With MEMECOIN_EVM_NATIVE_FLOAT_CHAINS naming the chain and the native balance
covering amount + gas reserve, the executor skips that unwrap; the swap is byte-identical. A node
refusal for insufficient funds falls back to the unwrap once, inside the same entry.
"""
import sqlite3

import pytest

from sniper.execution import evm_native_float as F
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as m

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
AMOUNT = 12_600_000_000_000_000                      # 0.0126 ETH, the 4663 clip p50
UNWRAP = {"to": WETH, "data": "0x2e1a7d4d" + format(AMOUNT, "064x"), "value": "0x0", "gas": "60000"}
SWAP_TX = {"to": "0xrouter", "data": "0xbeef", "value": hex(AMOUNT), "gas": "700000"}
# A real 4663 node refusal, memecoin_submission_attempts 2026-09-17T21:17:56Z (entry, explicit_rejected).
NODE_REFUSAL = ("rejected: insufficient funds for gas * price + value: address "
                "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d have 5205253908938000 "
                "want 39757443447980416")
GAS_PRICE = 50_000_000                               # 0.05 gwei
LOW = 2_000_000_000_000_000                          # MEMECOIN_EVM_GAS_TOPUP_LOW_NATIVE 0.002
RESERVE = 700_000 * GAS_PRICE * 3 + LOW


# --- pure pieces --------------------------------------------------------------------------------

def test_the_float_covers_only_amount_plus_the_gas_reserve():
    assert F.float_covers(AMOUNT + RESERVE, AMOUNT, 700_000, GAS_PRICE, LOW) is True
    assert F.float_covers(AMOUNT + RESERVE - 1, AMOUNT, 700_000, GAS_PRICE, LOW) is False
    for bad in (None, 0):
        assert F.float_covers(bad, AMOUNT, 700_000, GAS_PRICE, LOW) is False
    assert F.float_covers(AMOUNT * 10, AMOUNT, 700_000, None, LOW) is False      # unknown gas price


def test_only_an_exact_unwrap_of_the_chain_weth_is_a_candidate():
    assert F.unwrap_amount(UNWRAP, WETH) == AMOUNT
    assert F.unwrap_amount(dict(UNWRAP, to="0xother"), WETH) is None
    assert F.unwrap_amount(dict(UNWRAP, data="0xd0e30db0"), WETH) is None        # a wrap, not an unwrap
    assert F.unwrap_amount(dict(UNWRAP, value="0x1"), WETH) is None
    assert F.unwrap_amount(dict(UNWRAP, data="0x2e1a7d4d" + "0" * 64), WETH) is None


def test_the_real_node_refusal_is_recognised_and_nothing_else_is():
    assert F.insufficient_native(NODE_REFUSAL) is True
    for other in (None, "", "nonce too low: address 0x898b tx: 819 state: 820", "deadline"):
        assert F.insufficient_native(other) is False


def test_the_flag_is_empty_by_default_and_parses_a_chain_list(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", raising=False)
    assert cfg.memecoin_evm_native_float_chains() == frozenset()
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", "4663, 8453")
    assert cfg.memecoin_evm_native_float_chains() == frozenset({"4663", "8453"})
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", "4663,abc")
    assert cfg.memecoin_evm_native_float_chains() == frozenset()                 # malformed = off


def test_the_probe_reads_native_on_its_own_thread_and_a_failed_read_keeps_the_unwrap():
    probe = F.NativeFloatProbe(lambda: AMOUNT + RESERVE, quote_token=WETH,
                               gas_price_fn=lambda: GAS_PRICE, low_wei=LOW)
    assert probe.covers(UNWRAP, SWAP_TX) is True

    def boom():
        raise TimeoutError("rpc")
    assert F.NativeFloatProbe(boom, quote_token=WETH, gas_price_fn=lambda: GAS_PRICE,
                              low_wei=LOW).covers(UNWRAP, SWAP_TX) is False
    assert F.NativeFloatProbe(lambda: None, quote_token=WETH, gas_price_fn=lambda: GAS_PRICE,
                              low_wei=LOW).covers(UNWRAP, SWAP_TX) is False


# --- through execute_entry_evm ------------------------------------------------------------------

def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _run(c, *, send_results, float_covers=True, usdc=(100.0, 100.0), token=(0.0, 5.0),
         shortfall=None, quote_raw=None, probe=None):
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-09-27T00:00:00Z", network_id="4663")
    quote = {"sellAmount": str(AMOUNT), "transaction": dict(SWAP_TX), "issues": {"allowance": None},
             "pre_transactions": [dict(UNWRAP)]}
    log = {"signed": [], "sent": 0, "pipelined": [], "released": [], "probes": 0}
    results = iter(send_results)
    nonce = {"n": 40}

    def sign_fn(tx):
        log["signed"].append(tx)
        nonce["n"] += 1
        return ("0xraw%d" % nonce["n"], "0xhash%d" % nonce["n"], nonce["n"])

    def send_fn(raw):
        log["sent"] += 1
        result = next(results)
        if result is False:
            from sniper.execution.evm_real_seams import record_send_rejection
            record_send_rejection(NODE_REFUSAL)
        return result

    def pipeline(pre):
        log["pipelined"].append(pre)
        return {"status": "accepted", "tx_hash": "0xpre", "nonce": 99}

    class Probe:
        def covers(self, pre, tx):
            return float_covers

        def shortfall(self, pre, tx):            # chain-1 unstall partial unwrap (2026-10-08)
            return shortfall

    def native_float_fn(q):
        log["probes"] += 1
        return probe if probe is not None else Probe()

    usdc_vals, token_vals = list(usdc), list(token)
    state = {"u": 0, "t": 0}

    def usdc_fn():
        v = usdc_vals[min(state["u"], len(usdc_vals) - 1)]; state["u"] += 1; return v
    if quote_raw is not None:
        usdc_fn.last_raw = lambda: quote_raw      # the WETH word the unwrap guard reads

    def token_fn(_mint):
        v = token_vals[min(state["t"], len(token_vals) - 1)]; state["t"] += 1; return v

    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-09-27T00:00:01Z",
        quote_fn=lambda mint, usd, **_k: dict(quote, pre_transactions=[dict(UNWRAP)]),
        approve_fn=lambda *_a: True, sign_fn=sign_fn, send_fn=send_fn,
        confirm_fn=lambda _h: "confirmed", usdc_fn=usdc_fn, token_fn=token_fn,
        sleep_fn=lambda _s: None, sell_decimals=18, sell_token_usd=2688.0,
        native_pre_pipeline_fn=pipeline, native_float_fn=native_float_fn,
        release_nonce_fn=lambda n: log["released"].append(n))
    return result, log


def test_a_covered_float_skips_the_unwrap_and_books_the_swap_value():
    result, log = _run(_c(), send_results=["0xhash41"])
    assert result["status"] == "filled"
    assert log["pipelined"] == [] and len(log["signed"]) == 1       # the swap only, no unwrap
    assert log["signed"][0] == SWAP_TX                              # byte-identical transaction
    # WETH never moved (usdc 100 -> 100); spend is the swap's own value at the quote's price.
    assert result["notional"] == pytest.approx(AMOUNT / 1e18 * 2688.0)


def test_a_short_float_keeps_the_unwrap():
    result, log = _run(_c(), send_results=["0xhash41"], float_covers=False, usdc=(100.0, 66.13))
    assert result["status"] == "filled"
    assert log["pipelined"] == [UNWRAP]


def test_an_insufficient_funds_refusal_falls_back_to_the_unwrap_once_in_the_same_entry():
    result, log = _run(_c(), send_results=[False, "0xhash42"], usdc=(100.0, 66.13))
    assert result["status"] == "filled"
    assert log["released"] == [41]                                  # the refused swap's nonce
    assert log["pipelined"] == [UNWRAP]                             # the fallback unwrapped
    assert log["probes"] == 1                                       # the float is not tried twice


def test_a_second_refusal_after_the_fallback_ends_the_entry():
    result, log = _run(_c(), send_results=[False, False])
    assert result["status"] == "failed" and "broadcast_rejected" in result["reason"]
    assert log["sent"] == 2


# --- chain-1 unstall (2026-10-08): spare native pays part, only the shortfall is unwrapped -------

def test_the_probe_names_the_shortfall_only_when_partial_and_spare_native_pays_part():
    spare = AMOUNT // 3
    probe = F.NativeFloatProbe(lambda: spare + RESERVE, quote_token=WETH,
                               gas_price_fn=lambda: GAS_PRICE, low_wei=LOW, partial=True,
                               reserve_floor_wei=0)
    assert probe.covers(UNWRAP, SWAP_TX) is False
    assert probe.shortfall(UNWRAP, SWAP_TX) == AMOUNT - spare
    off = F.NativeFloatProbe(lambda: spare + RESERVE, quote_token=WETH,
                             gas_price_fn=lambda: GAS_PRICE, low_wei=LOW)
    assert off.shortfall(UNWRAP, SWAP_TX) is None                       # 4663: today's full unwrap
    for native in (RESERVE, RESERVE - 1, None):                          # no spare, or unknown
        p = F.NativeFloatProbe(lambda n=native: n, quote_token=WETH, gas_price_fn=lambda: GAS_PRICE,
                               low_wei=LOW, partial=True, reserve_floor_wei=0)
        assert p.shortfall(UNWRAP, SWAP_TX) is None
    unknown_gas = F.NativeFloatProbe(lambda: spare + RESERVE, quote_token=WETH,
                                     gas_price_fn=lambda: None, low_wei=LOW, partial=True,
                                     reserve_floor_wei=0)
    assert unknown_gas.shortfall(UNWRAP, SWAP_TX) is None


def test_a_spendable_chain_keeps_the_full_book_reserve_and_an_unknown_one_keeps_the_unwrap():
    floor = RESERVE + 5_000_000_000_000_000                    # the book's reserve is the larger keep
    rich = F.NativeFloatProbe(lambda: AMOUNT + floor, quote_token=WETH, gas_price_fn=lambda: GAS_PRICE,
                              low_wei=LOW, partial=True, reserve_floor_wei=floor)
    assert rich.covers(UNWRAP, SWAP_TX) is True
    part = F.NativeFloatProbe(lambda: AMOUNT + floor - 1, quote_token=WETH,
                              gas_price_fn=lambda: GAS_PRICE, low_wei=LOW, partial=True,
                              reserve_floor_wei=floor)
    assert part.covers(UNWRAP, SWAP_TX) is False and part.shortfall(UNWRAP, SWAP_TX) == 1
    unknown = F.NativeFloatProbe(lambda: AMOUNT * 10, quote_token=WETH, gas_price_fn=lambda: GAS_PRICE,
                                 low_wei=LOW, partial=True, reserve_floor_wei=None)
    assert unknown.covers(UNWRAP, SWAP_TX) is False and unknown.shortfall(UNWRAP, SWAP_TX) is None
    plain = F.NativeFloatProbe(lambda: AMOUNT + RESERVE, quote_token=WETH,
                               gas_price_fn=lambda: GAS_PRICE, low_wei=LOW, reserve_floor_wei=None)
    assert plain.covers(UNWRAP, SWAP_TX) is True                          # 4663: unchanged


def test_a_partial_unwrap_withdraws_the_shortfall_and_books_the_swap_value():
    short = AMOUNT // 4
    result, log = _run(_c(), send_results=["0xhash41"], float_covers=False, shortfall=short,
                       usdc=(100.0, 91.0), quote_raw=short)
    assert result["status"] == "filled"
    [pre] = log["pipelined"]
    assert pre["data"] == "0x2e1a7d4d" + format(short, "064x") and pre["to"] == WETH
    assert log["signed"][-1]["value"] == SWAP_TX["value"]                 # the swap is unchanged
    assert result["notional"] == pytest.approx(AMOUNT / 1e18 * 2688.0)    # the value, not WETH's move


def test_a_shortfall_above_the_weth_balance_is_refused_before_anything_is_signed():
    short = AMOUNT // 2
    result, log = _run(_c(), send_results=["0xhash41"], float_covers=False, shortfall=short,
                       quote_raw=short - 1)
    assert result["status"] == "aborted" and "unwrap exceeds quote balance" in result["reason"]
    assert log["signed"] == [] and log["pipelined"] == []


def test_a_failed_swap_after_a_partial_unwrap_rewraps_exactly_the_shortfall():
    short = AMOUNT // 4
    c = _c()
    wrapped = []
    result, log = _run_with_cleanup(c, short, wrapped)
    assert result["status"] in ("failed", "aborted"), result
    assert wrapped == [short], wrapped                                     # never the full amount


def _run_with_cleanup(c, short, wrapped):
    """_run with a swap the node refuses for a non-funds reason and a cleanup seam."""
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-09-27T00:00:00Z", network_id="1")
    quote = {"sellAmount": str(AMOUNT), "transaction": dict(SWAP_TX), "issues": {"allowance": None},
             "pre_transactions": [dict(UNWRAP)]}
    log = {"pipelined": []}
    nonce = {"n": 40}

    def sign_fn(tx):
        nonce["n"] += 1
        return ("0xraw%d" % nonce["n"], "0xhash%d" % nonce["n"], nonce["n"])

    def send_fn(raw):
        from sniper.execution.evm_real_seams import record_send_rejection
        record_send_rejection("rejected: execution reverted: too little received")
        return False

    class Probe:
        def covers(self, pre, tx):
            return False

        def shortfall(self, pre, tx):
            return short

    def usdc_fn():
        return 100.0
    usdc_fn.last_raw = lambda: short

    def cleanup(amount):
        wrapped.append(amount)
        return "confirmed"
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="1", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-09-27T00:00:01Z",
        quote_fn=lambda mint, usd, **_k: dict(quote, pre_transactions=[dict(UNWRAP)]),
        approve_fn=lambda *_a: True, sign_fn=sign_fn, send_fn=send_fn,
        confirm_fn=lambda _h: "confirmed", usdc_fn=usdc_fn, token_fn=lambda _m: 0.0,
        sleep_fn=lambda _s: None, sell_decimals=18, sell_token_usd=2688.0,
        native_pre_pipeline_fn=lambda pre: (log["pipelined"].append(pre)
                                            or {"status": "accepted", "tx_hash": "0xpre", "nonce": 99}),
        native_float_fn=lambda q: Probe(), cleanup_native_fn=cleanup,
        release_nonce_fn=lambda n: None)
    return result, log


def test_probe_retains_only_consumed_native_gas_floor_and_shortfall_facts():
    floor = 4_000_000_000_000_000
    native = AMOUNT + floor - 17
    probe = F.NativeFloatProbe(lambda: native, quote_token=WETH,
        gas_price_fn=lambda: GAS_PRICE, low_wei=0, partial=True, reserve_floor_wei=floor)
    assert probe.covers(UNWRAP, SWAP_TX) is False
    assert probe.shortfall(UNWRAP, SWAP_TX) == 17
    facts = probe.decision_facts
    # Read timing (2026-10-21): when the read finished and how long the decision waited on it.
    assert facts.pop('native_read_ms') >= 0 and facts.pop('native_read_elapsed_ms') >= 0
    assert facts == {
        'native_balance_wei': native, 'swap_amount_wei': AMOUNT,
        'swap_gas_limit': 700000, 'gas_price_wei': GAS_PRICE,
        'book_floor_wei': floor, 'required_native_keep_wei': floor, 'shortfall_wei': 17,
        'computed_shortfall_wei': 17, 'native_source': 'probe', 'snapshot_age_ms': None}
    unknown = F.NativeFloatProbe(lambda: None, quote_token=WETH,
        gas_price_fn=lambda: None, low_wei=0, partial=True, reserve_floor_wei=None)
    assert unknown.covers(UNWRAP, SWAP_TX) is False
    assert unknown.shortfall(UNWRAP, SWAP_TX) is None
    assert unknown.decision_facts['native_balance_wei'] is None
    assert unknown.decision_facts['gas_price_wei'] is None
    assert unknown.decision_facts['required_native_keep_wei'] is None
