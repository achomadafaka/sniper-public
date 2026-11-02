"""The J7 native quote, SHADOW ONLY (design section 12 B1). Computed beside Jupiter, never used.

Pinned on the live fee fixture (tests/fixtures/memecoin/pump_fee_config_frames.json): the account
derivations, the native math with the decoded fees against what the programs actually paid out,
and the shadow's contract with execute_entry (one line, no wait, never raises).
"""
import base64
import json
import struct
import threading
import time
from pathlib import Path

import pytest

from sniper.execution import pump_fees as PF
from sniper.execution import solana_native_entry_quote as NQ
from sniper.execution.pump_curve_buy import parse_trade_event
from sniper.execution.pump_native_exit import PUMP_PROGRAM
from sniper.execution.pumpswap_buy import (
    AMM_PROGRAM, is_pump_migrated_pool, mint_supply_and_decimals, parse_buy_event,
    parse_pool_account)

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" /
                 "pump_fee_config_frames.json").read_text())
AMM_CFG = PF.parse_fee_config(FX["fee_config_amm"]["data_b64"])


def test_the_derived_accounts_are_the_live_ones():
    pools = 0
    for key, b64 in FX["pool_accounts_b64"].items():
        facts = parse_pool_account(b64)
        if is_pump_migrated_pool(facts):
            assert NQ.accounts_for(facts["base_mint"])["pool"] == key
            pools += 1
    assert pools == 11
    acc = NQ.accounts_for(next(iter(FX["curve_accounts_b64"])))
    assert acc["curve_fee_config"] == FX["fee_config_curve"]["key"]
    assert acc["amm_fee_config"] == FX["fee_config_amm"]["key"]


def test_native_pumpswap_math_with_decoded_fees_matches_what_the_program_paid():
    """Every buy_exact_quote_in in the fixture: the total spend (quote_amount_in) priced on the
    event's own pre-trade reserves, the Pool's virtual_quote and the DECODED tier. The program
    takes its fees separately, so it pays at most a few hundred atomic units less (< 1e-5 %)."""
    n = 0
    for frame in FX["amm_frames"]:
        ev = parse_buy_event(frame["line"])
        if ev["ix_name"] != "buy_exact_quote_in":
            continue
        pool = parse_pool_account(FX["pool_accounts_b64"][ev["pool"]])
        supply = mint_supply_and_decimals(FX["mint_accounts_b64"][pool["base_mint"]])["base_supply"]
        fees = PF.amm_fee_bps(AMM_CFG, pool, base_supply=supply,
                              base_reserve=ev["pool_base_token_reserves"],
                              quote_reserve=ev["pool_quote_token_reserves"])
        state = {"venue": "pumpswap", "base_reserve": ev["pool_base_token_reserves"],
                 "quote_reserve": ev["pool_quote_token_reserves"],
                 "virtual_quote": pool["virtual_quote"], "fees": fees}
        out = NQ.native_out(state, ev["quote_amount_in"])
        assert 0 <= out - ev["base_amount_out"] <= max(1000, ev["base_amount_out"] // 10**7), frame["sig"]
        n += 1
    assert n == 20


def _rpc_for(accounts, slot=451000000):
    """A getMultipleAccounts stub over a {address: account-dict} map."""
    calls = []

    def rpc(method, params):
        assert method == "getMultipleAccounts"
        calls.append(list(params[0]))
        return {"context": {"slot": slot}, "value": [accounts.get(k) for k in params[0]]}
    return rpc, calls


def _token_account(amount):
    return base64.b64encode(bytes(64) + struct.pack("<Q", amount) + bytes(97)).decode()


def _cfgs(mint):
    acc = NQ.accounts_for(mint)
    return acc, {acc["curve_fee_config"]: {"data": [FX["fee_config_curve"]["data_b64"], "base64"]},
                 acc["amm_fee_config"]: {"data": [FX["fee_config_amm"]["data_b64"], "base64"]}}


def test_a_live_curve_reads_as_the_curve_venue_with_its_flat_fees():
    mint = next(m for m, b64 in FX["curve_accounts_b64"].items()
                if base64.b64decode(b64)[48] == 0 and base64.b64decode(b64)[81] == 0)
    acc, accounts = _cfgs(mint)
    accounts[acc["curve"]] = {"owner": PUMP_PROGRAM, "data": [FX["curve_accounts_b64"][mint], "base64"]}
    rpc, calls = _rpc_for(accounts)
    state = NQ.read_native_state(mint, rpc)
    assert state["venue"] == "curve" and state["fees"] == (0, 95, 30) and len(calls) == 1
    raw = base64.b64decode(FX["curve_accounts_b64"][mint])
    assert state["virtual_sol_reserves"] == struct.unpack_from("<Q", raw, 16)[0]


def test_a_mayhem_curve_is_refused_not_priced():
    mint = next(m for m, b64 in FX["curve_accounts_b64"].items()
                if base64.b64decode(b64)[48] == 0 and base64.b64decode(b64)[81] == 1)
    acc, accounts = _cfgs(mint)
    accounts[acc["curve"]] = {"owner": PUMP_PROGRAM, "data": [FX["curve_accounts_b64"][mint], "base64"]}
    state = NQ.read_native_state(mint, _rpc_for(accounts)[0])
    assert state["reason"] == "mayhem_or_fee_unmodelled" and "fees" not in state


def test_a_graduated_coin_reads_its_canonical_pool_and_vaults():
    key, b64 = next((k, v) for k, v in FX["pool_accounts_b64"].items()
                    if is_pump_migrated_pool(parse_pool_account(v)))
    facts = parse_pool_account(b64)
    mint = facts["base_mint"]
    acc, accounts = _cfgs(mint)
    assert acc["pool"] == key
    accounts[key] = {"owner": AMM_PROGRAM, "data": [b64, "base64"]}
    accounts[facts["pool_base_token_account"]] = {"data": [_token_account(10**14), "base64"]}
    accounts[facts["pool_quote_token_account"]] = {"data": [_token_account(50 * 10**9), "base64"]}
    accounts[mint] = {"data": [FX["mint_accounts_b64"][mint], "base64"]}
    rpc, calls = _rpc_for(accounts)
    state = NQ.read_native_state(mint, rpc)
    assert state["venue"] == "pumpswap" and len(calls) == 2
    assert (state["base_reserve"], state["quote_reserve"]) == (10**14, 50 * 10**9)
    assert state["fees"] == PF.amm_fee_bps(
        AMM_CFG, facts, base_supply=mint_supply_and_decimals(FX["mint_accounts_b64"][mint])["base_supply"],
        base_reserve=10**14, quote_reserve=50 * 10**9)


REAL_MINT = next(iter(FX["curve_accounts_b64"]))


def test_a_coin_with_neither_account_is_named_not_priced():
    state = NQ.read_native_state(REAL_MINT, _rpc_for({})[0])
    assert state["reason"] == "no_curve_or_canonical_pool"


def _quote(mint, jup_in, jup_out, amm_key="AmmKey1111"):
    return {"outAmount": str(jup_out), "otherAmountThreshold": str(jup_out * 99 // 100),
            "slippageBps": 100,
            "routePlan": [{"swapInfo": {"inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                                        "outputMint": NQ.WSOL_MINT, "inAmount": "150000000",
                                        "outAmount": str(jup_in), "label": "BisonFi"}},
                          {"swapInfo": {"inputMint": NQ.WSOL_MINT, "outputMint": mint,
                                        "inAmount": str(jup_in), "outAmount": str(jup_out),
                                        "label": "Pump.fun Amm", "ammKey": amm_key,
                                        "updateContextSlot": 451000001}}]}


STATE = {"venue": "pumpswap", "account": "AmmKey1111", "slot": 451000000,
         "base_reserve": 10**14, "quote_reserve": 50 * 10**9, "virtual_quote": 0, "fees": (20, 5, 95)}


def test_the_line_compares_the_coin_hop_on_the_same_sol_input():
    mint = "Mint1111pump"
    out = NQ.native_out(STATE, 10**9)
    line = NQ.shadow_line(mint, STATE, _quote(mint, 10**9, out), usd_amount=150.0, read_ms=41.0,
                          jup_ms=90.0, native_first=True, attempt_id="a1")
    assert "reason=ok" in line and "native_out=%d" % out in line and "delta_pct=0.0000" in line
    assert "same_pool=True" in line and "jup_label=Pump.fun Amm" in line
    assert "native_min=%d" % (out * 9900 // 10000) in line and "attempt=a1" in line


def test_the_shadow_logs_once_whichever_side_finishes_last():
    mint, lines = REAL_MINT, []
    for read_first in (True, False):
        gate = threading.Event()

        def rpc(method, params):
            gate.wait(2)
            raise RuntimeError("read")      # a failed read still logs, named
        shadow = NQ.NativeQuoteShadow(mint, 150.0, rpc=rpc, log=lines.append, start=False)
        if read_first:
            gate.set()
            shadow.read()
            shadow.offer_jupiter(_quote(mint, 10**9, 10**12))
        else:
            shadow.offer_jupiter(_quote(mint, 10**9, 10**12))
            gate.set()
            shadow.read()
        shadow.offer_jupiter(_quote(mint, 10**9, 10**12))     # a second offer changes nothing
        assert "reason=read_failed:RuntimeError" in lines[-1]
    assert len(lines) == 2 and "native_first=True" in lines[0] and "native_first=False" in lines[1]


def test_no_jupiter_quote_means_no_line():
    lines = []
    shadow = NQ.NativeQuoteShadow(REAL_MINT, 150.0, rpc=_rpc_for({})[0], log=lines.append,
                                  start=False)
    shadow.read()
    assert lines == []


def test_modes_and_the_pump_prefilter(monkeypatch):
    from sniper.settings import memecoin as cfg
    for value, expected in (("shadow", "shadow"), ("live", "shadow"), ("off", "off"), ("x", "off")):
        monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_NATIVE_QUOTE", value)
        assert NQ.mode() == expected
    assert NQ.qualifies("7ga6rtE9qSb3wdEiDCpTu2kHqoGVfT52jD8ign1pump")
    assert not NQ.qualifies("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") and not NQ.qualifies(None)


def _execute(native_shadow_fn):
    from test_memecoin_entry_drift import _conn, _reverse_quote
    from sniper.execution.solana_swap_executor import execute_entry
    from sniper.persistence import db_memecoin as m
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint="MINT", source_user_id="trader", strategy="alpha_b", notional=12.0,
        reserved_at="2026-07-15T12:00:00+00:00", network_id="solana",
        origin_event_id="exact-event", attempt_id="attempt-1", signal_source="global_feed",
        source_price=0.02, source_market_cap=1_000_000.0,
        observed_at="2026-07-15T11:59:59+00:00", clock_domain="observed_real_utc")
    usdc = iter((100.0, 88.0))
    token = iter(((0.0, 6), (500.0, 6)))
    quote = {"inAmount": "10000000", "outAmount": "500000000", "otherAmountThreshold": "470000000",
             "priceImpactPct": "0.01", "routePlan": [{"swapInfo": {"ammKey": "AMM1"}}]}
    result = execute_entry(
        conn, pending_id=pending_id, mint="MINT", usd_amount=12.0, strategy="alpha_b",
        source_user_id="trader", now_iso="2026-07-15T12:00:02+00:00",
        quote_fn=lambda _mint, _usd: quote, reverse_quote_fn=_reverse_quote,
        sign_fn=lambda _quote: (b"signed", "sig"), send_fn=lambda _raw: True,
        confirm_fn=lambda _sig: "confirmed", usdc_fn=lambda: next(usdc),
        token_fn=lambda _mint: next(token), native_shadow_fn=native_shadow_fn)
    return result, quote


def test_execute_entry_hands_the_shadow_its_jupiter_quote_and_fills_as_before():
    seen = []

    class Shadow:
        def offer_jupiter(self, quote):
            seen.append(("offer", quote))

    def start(mint, usd, pending):
        seen.append(("start", mint, usd, pending.get("attempt_id")))
        return Shadow()
    result, quote = _execute(start)
    assert result["status"] == "filled"
    assert seen == [("start", "MINT", 12.0, "attempt-1"), ("offer", quote)]


@pytest.mark.parametrize("where", ["start", "offer"])
def test_a_failing_shadow_never_touches_the_entry(where):
    class Shadow:
        def offer_jupiter(self, quote):
            raise RuntimeError("offer")

    def start(mint, usd, pending):
        if where == "start":
            raise RuntimeError("start")
        return Shadow()
    result, _quote = _execute(start)
    assert result["status"] == "filled"


@pytest.mark.parametrize("mode,mint,max_accounts,wired", [
    ("shadow", "7ga6rtE9qSb3wdEiDCpTu2kHqoGVfT52jD8ign1pump", None, True),
    ("live", "7ga6rtE9qSb3wdEiDCpTu2kHqoGVfT52jD8ign1pump", None, True),     # live runs as shadow
    ("off", "7ga6rtE9qSb3wdEiDCpTu2kHqoGVfT52jD8ign1pump", None, False),
    ("shadow", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", None, False),  # not a pump mint
    ("shadow", "7ga6rtE9qSb3wdEiDCpTu2kHqoGVfT52jD8ign1pump", 20, False),    # the oversize retry
])
def test_the_funded_binding_wires_the_shadow_only_where_it_should(monkeypatch, mode, mint,
                                                                    max_accounts, wired):
    import sqlite3
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    from sniper.execution import solana_swap_executor
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    from sniper.execution.submission_transport import AttemptState
    from test_memecoin_fast_submit_wiring import StubTransport
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_NATIVE_QUOTE", mode)
    seen = {}

    def fake_execute_entry(conn, **kw):
        seen["fn"] = kw.get("native_shadow_fn")
        return {"status": "aborted", "reason": "stub"}
    monkeypatch.setattr(solana_swap_executor, "execute_entry", fake_execute_entry)
    started = []
    monkeypatch.setattr(NQ, "NativeQuoteShadow",
                        lambda *a, **k: started.append((a, sorted(k))) or "shadow")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint=mint, source_user_id="u", strategy="alpha_b", notional=10.0,
                            reserved_at="t", network_id="solana", attempt_id="att-1")
    ex = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                      http=lambda *_a, **_k: pytest.fail("unexpected provider read"),
                                      submission_transport=StubTransport(AttemptState.ACCEPTED),
                                      usdc_fn=lambda: 10.0, token_fn=lambda _m: 0.0)
    ex["execute_fn"](conn, pid, mint, 10.0, "t", max_accounts=max_accounts,
                      entry_min_slot=100, entry_max_slot=101,
                      entry_head_fn=lambda: (100, time.monotonic()))
    assert (seen["fn"] is not None) is wired
    if wired:
        assert seen["fn"](mint, 10.0, {"attempt_id": "att-1"}) == "shadow"
        assert started == [((mint, 10.0), ["attempt_id", "rpc"])]


def test_a_mayhem_curve_is_priced_only_on_request_and_says_so():
    mint = next(m for m, b64 in FX["curve_accounts_b64"].items()
                if base64.b64decode(b64)[48] == 0 and base64.b64decode(b64)[81] == 1)
    acc, accounts = _cfgs(mint)
    accounts[acc["curve"]] = {"owner": PUMP_PROGRAM, "data": [FX["curve_accounts_b64"][mint], "base64"]}
    state = NQ.read_native_state(mint, _rpc_for(accounts)[0], allow_mayhem=True)
    assert state["venue"] == "curve" and state["fees"] == (0, 95, 30) and state["mayhem_mode"] is True
