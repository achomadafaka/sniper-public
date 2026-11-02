"""Chain-1 (Ethereum) entry min-out tolerance (operator order 2026-10-08 "fix this"; design 19ff3de41).

OG 4973 (tx 0x60b90520): submitted 4.5 s before block 26147894 with fees that cleared it, but
three other buys in that block (tx 4, 5, 233; ~18x our size) moved the V4 pool past the 3%
minimum. Flashbots Protect does not include a transaction that would revert, so it expired
unmined, 0.547% short. Chain 1 now signs its entries at MEMECOIN_EVM_ENTRY_SLIPPAGE_PCT_1
(default 8); every other chain keeps MEMECOIN_ENTRY_SLIPPAGE_PCT. The retry knob is never read
for it.
"""
import pytest

from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    for chain in ("1", "56", "4663", "8453", "5042"):
        monkeypatch.delenv("MEMECOIN_EVM_ENTRY_SLIPPAGE_PCT_%s" % chain, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_SLIPPAGE_PCT", 3.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT", 8.0)


def test_chain_1_signs_entries_at_8_pct_and_every_other_chain_at_the_global(monkeypatch):
    assert cfg.evm_entry_slippage_bps_for("1") == 800
    for chain in ("56", "4663", "8453", "5042", None):
        assert cfg.evm_entry_slippage_bps_for(chain) == 300
    monkeypatch.setenv("MEMECOIN_EVM_ENTRY_SLIPPAGE_PCT_1", "12")
    monkeypatch.setenv("MEMECOIN_EVM_ENTRY_SLIPPAGE_PCT_56", "5")
    assert cfg.evm_entry_slippage_bps_for("1") == 1200 and cfg.evm_entry_slippage_bps_for("56") == 500
    monkeypatch.setenv("MEMECOIN_EVM_ENTRY_SLIPPAGE_PCT_1", "0")
    assert cfg.evm_entry_slippage_bps_for("1") == 0              # the operator's choice: min = quote


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-1", "100", "250", " "])
def test_a_value_no_rung_can_sign_keeps_the_chain_default(monkeypatch, raw):
    monkeypatch.setenv("MEMECOIN_EVM_ENTRY_SLIPPAGE_PCT_1", raw)
    assert cfg.evm_entry_slippage_bps_for("1") == 800


def test_the_min_out_retry_knob_is_never_read(monkeypatch):
    """Alpha: a reused knob carries its live value (2026-10-07)."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT", 20.0)
    assert cfg.evm_entry_slippage_bps_for("1") == 800 and cfg.evm_entry_slippage_bps_for("56") == 300


def _executor(monkeypatch, chain, slips):
    from eth_account import Account
    from sniper.execution import evm_real_seams as seams, evm_swap_executor as executor
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "memecoin_chain1_fee_headroom", lambda: False)
    monkeypatch.setattr(seams.m, "pending_row", lambda *a: {"strategy": "fixture", "source_user_id": "u"})

    def venue(sell, buy, amount, *, slippage_bps=None, **kw):
        slips.append(slippage_bps)
        return {"sellAmount": str(amount), "buyAmount": "123", "transaction": {"to": "router"}}
    captured = []

    def execute(*a, quote_fn, **kw):
        captured.append(quote_fn("coin", 25))
        return {"status": "aborted", "reason": "fixture"}
    monkeypatch.setattr(executor, "execute_entry_evm", execute)
    ex = seams.build_real_evm_executor(acct=Account.create(), chain_id=chain, rpc_url="rpc",
                                      venue_quote_fn=venue, price_fn=lambda: 1000, usdc_fn=lambda: 300,
                                      http=lambda *a, **kw: (200, {"result": "0x1"}, None))
    ex["warm_quote_price_fn"]()
    return ex, captured


@pytest.mark.parametrize("chain,bps", [("1", 800), ("56", 300)])
def test_the_prepared_quote_is_signed_at_the_chains_tolerance_in_one_ladder(monkeypatch, chain, bps):
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    slips = []
    ex, captured = _executor(monkeypatch, chain, slips)
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=lambda: (25, None),
                          quote_fn=ex["prepare_entry_quote_fn"])
    early.worker.join(2)
    assert early.error is None, early.error
    ex["execute_fn"](None, 1, "coin", 25, "now", early_quote=early)
    assert slips == [bps]                    # one ladder: the executor reused the prepared quote
    assert captured[0]["_sniper_early_quote"] is True


@pytest.mark.parametrize("chain,bps", [("1", 800), ("56", 300)])
def test_without_a_prepared_quote_the_executor_asks_the_chains_tolerance(monkeypatch, chain, bps):
    slips = []
    ex, _captured = _executor(monkeypatch, chain, slips)
    ex["execute_fn"](None, 1, "coin", 25, "now")
    assert slips == [bps]


@pytest.mark.parametrize("retry_pct", [8.0, 20.0])
def test_robinhood_safety_entry_reuses_the_prepared_quote_at_its_actual_minimum(monkeypatch,
                                                                            retry_pct):
    """6df3ea77: the real safety-bearing executor asks 4663's retry tolerance immediately."""
    import sqlite3
    from eth_account import Account
    from sniper.execution import evm_real_seams as seams
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT", retry_pct)
    slips, safety_calls, checked, preflight = [], [], [], []

    def venue(sell, buy, amount, *, slippage_bps=None, **kw):
        slips.append(slippage_bps)
        return {"sellAmount": str(amount), "buyAmount": "1000",
                "minBuyAmount": str(1000 * (10000 - slippage_bps) // 10000),
                "transaction": {"to": "router"}}

    def check_quote(quote):
        checked.append(quote)
        return True, None

    def stop_before_mutation(quote, **kw):
        preflight.append(quote)
        return False, "fixture_preflight_stop"

    monkeypatch.setattr(seams, "preflight_weth_entry_quote", stop_before_mutation)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    seams.m.ensure_memecoin_tables(conn)
    pid = seams.m.reserve_pending(conn, mint="coin", source_user_id="u", strategy="fixture",
                                  notional=25, reserved_at="2026-10-13T13:58:15Z", network_id="4663")
    ex = seams.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="rpc", venue_quote_fn=venue,
        price_fn=lambda: 1000, usdc_fn=lambda: 300, token_fn=lambda mint: (0., 18),
        http=lambda *a, **kw: (200, {"result": "0x1"}, None))
    ex["warm_quote_price_fn"]()
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=lambda: (25, None),
                          quote_fn=ex["prepare_entry_quote_fn"])
    early.worker.join(2)
    assert early.error is None, early.error
    try:
        result = ex["execute_fn"](
            conn, pid, "coin", 25, early.result["observed_at"], early_quote=early,
            safety_gate_fn=lambda: (safety_calls.append("checked") or True, None),
            quote_safety_fn=check_quote)
        assert result["reason"] == "fixture_preflight_stop", result
        bps = int(round(retry_pct * 100))
        assert slips == [bps], "the safety-bearing executor must consume the first ladder"
        assert safety_calls == ["checked"] and len(checked) == len(preflight) == 1
        assert checked[0] is preflight[0]
        assert checked[0]["minBuyAmount"] == str(1000 * (10000 - bps) // 10000)
        assert checked[0]["_sniper_early_quote"] is True
        assert checked[0]["_sniper_capacity_quote"] is True
        assert checked[0]["_sniper_rungs"] == list(early.result["rungs"])
        assert checked[0]["_sniper_quote_observed_at"] == early.result["observed_at"]
        assert checked[0]["_sniper_quote_duration_ms"] == early.result["quote_duration_ms"]
    finally:
        conn.close()


@pytest.mark.parametrize("chain,bps", [("1", 800), ("56", 300), ("4663", 2000)])
def test_the_capacity_probe_asks_the_chains_tolerance_and_4663_keeps_its_retry_knob(monkeypatch, chain,
                                                                                    bps):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT", 20.0)
    slips = []
    ex, _captured = _executor(monkeypatch, chain, slips)
    ex["entry_quote_probe_fn"]("coin", 25)
    assert slips == [bps]


def test_the_signed_minimum_still_answers_to_the_copy_cap():
    """The cap rule (Alpha): copy_minimum_refusal checks the SIGNED minimum. At 8% a quote 30% over the
    source signs a worst price 41% over it, past the 35% cap; 20% over signs 30%, inside it."""
    from sniper.execution.entry_attribution import copy_minimum_refusal

    def quote(premium):
        expected = int(1_000_000 / (1 + premium))           # tokens for 1 unit at source price 1e-6
        return {"sellAmount": "1000000", "buyAmount": str(expected), "minBuyAmount": str(expected * 92 // 100),
                "sellToken": "0xq", "buyToken": "0xc", "_sniper_sell_decimals": 6, "_sniper_buy_decimals": 0}
    assert copy_minimum_refusal(quote(0.30), 1e-6, 35.0) is not None
    assert copy_minimum_refusal(quote(0.20), 1e-6, 35.0) is None
