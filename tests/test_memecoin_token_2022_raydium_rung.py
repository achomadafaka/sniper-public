"""A Token-2022 mint on the RAYDIUM RUNG: the chain's simulation decides, never a rule (2026-09-11).

HISTORY. The text below is the 2026-09-09 reasoning, kept because the coin-level correction it
describes still stands. The rung-level refusal it introduced was replaced on 2026-09-11 -- see the
section "the rung lets the CHAIN decide".

A Token-2022 mint disables the RAYDIUM RUNG, never the coin.

STOCKCAT (18:30) and SWEENIES (21:39) on 2026-09-08 both failed on chain with
`chain_error:ConstraintMintTokenProgram`, terminal_status `failed` — SUBMITTED, gas and a
reservation slot each. Both had `quote_route_target='raydium'`.

Mechanism, established by reading the build rather than guessing: on a SOL-funded BUY,
`raydium_swap_tx` skips its `inputAccount` branch entirely (the input is SOL), so no account is
passed and Raydium's own API derives a CLASSIC associated token account for the OUTPUT mint. For a
Token-2022 mint that address is wrong and the chain rejects the whole transaction.

THE FIRST FIX WAS AT THE WRONG LAYER AND THIS FILE REPLACES IT. It refused the mint in
`token_safety_ok`, which would have declined the coin outright — and 54 of the 60 most recent
funded Solana fills are Token-2022 mints, every one routed and filled through Jupiter, which
handles the program correctly. That refusal fired once before it was measured and removed; at
traffic it would have taken most of the Solana book, which is 93% of the funded bankroll.

So the refusal belongs on the rung that actually cannot build. Jupiter keeps serving these coins;
Raydium declines; the entry is refused only when Raydium was the last route.

Fee-on-transfer needs no special handling: the entry books `quantity_source='balance_delta'` from
the confirmed receipt, never the quote, so whatever the fee withholds is already absent from the
recorded fill.
"""
import json
from pathlib import Path

import pytest

import sniper.execution.solana_real_seams as R
from sniper.execution.solana_token_safety import TOKEN_2022_PROGRAM

CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOL = R._SOL_MINT
COIN = "8kgnZx4k87vM2aaHejq4kaoUtsbqwDZxuXWRgCNmSTNK"      # SWEENIES


def _owner_http(owner):
    def http(method, url, body=None, **kw):
        return 200, {"result": {"value": {"owner": owner}}}, None
    return http


@pytest.fixture(autouse=True)
def _rpc(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "http://stub")


# --- the program probe reports THREE states ------------------------------------------------------

def test_the_probe_distinguishes_2022_classic_and_unreadable():
    assert R._mint_is_token_2022(COIN, _owner_http(TOKEN_2022_PROGRAM)) is True
    assert R._mint_is_token_2022(COIN, _owner_http(CLASSIC)) is False
    for broken in (
        lambda m, u, body=None, **k: (500, None, None),
        lambda m, u, body=None, **k: (200, {"result": {"value": None}}, None),
        lambda m, u, body=None, **k: (200, {"error": {"code": -32000}}, None),
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rpc down")),
    ):
        assert R._mint_is_token_2022(COIN, broken) is None, "a failed read is not 'classic'"


def test_an_unconfigured_rpc_is_unknown_not_classic(monkeypatch):
    monkeypatch.delenv("MEMECOIN_SOLANA_RPC_URL", raising=False)
    assert R._mint_is_token_2022(COIN, _owner_http(CLASSIC)) is None


# --- the rung lets the CHAIN decide (2026-09-11) --------------------------------------------------
#
# The 2026-09-09 rule refused every Token-2022 output before posting. Replayed offline on
# 2026-09-11, SWEENIES and STOCKCAT themselves build with the Token-2022 program and the ATA
# program invoked and simulate clean; the rule had cost six fills in two days. Now the build is
# posted, then simulated on the read endpoint; the rung refuses only on the chain's error, and
# refuses when the simulation cannot be read.

def _build(monkeypatch, *, out_mint, probe, sim=None, tx="AA=="):
    """Drive raydium_swap_tx with the network stubbed: `sim` is the simulateTransaction reply
    (a dict to return, or an exception to raise); None means 'simulate must not be called'."""
    monkeypatch.setattr(R, "_mint_is_token_2022", lambda mint, http=None: probe)
    posted = {}

    def http(method, url, body=None, **kw):
        if "simulateTransaction" in (body or ""):
            posted["simulated"] = True
            assert sim is not None, "simulate must not be called on this path"
            if isinstance(sim, Exception):
                raise sim
            return 200, sim, None
        posted["url"] = url
        return 200, {"success": True, "data": [{"transaction": tx}]}, None

    quote = {"inputMint": SOL, "outputMint": out_mint, "_raydium_response": {}}
    result = R.raydium_swap_tx(quote, "Owner1111111111111111111111111111111111111", http=http)
    return result, posted


CLEAN = {"result": {"value": {"err": None, "logs": ["Program routeU success"]}}}
REJECTED = {"result": {"value": {"err": {"InstructionError": [2, {"Custom": 2003}]},
                                 "logs": ["Program log: AnchorError ... ConstraintMintTokenProgram"]}}}


def test_a_token_2022_buy_builds_when_the_chain_simulates_clean(monkeypatch):
    result, posted = _build(monkeypatch, out_mint=COIN, probe=True, sim=CLEAN)
    assert result == "AA=="
    assert posted.get("url", "").endswith("/transaction/swap-base-in") and posted.get("simulated")


def test_a_token_2022_buy_refuses_on_the_chain_error(monkeypatch):
    """THE REGRESSION SHAPE of 2026-09-08, now caught by the simulation instead of a rule."""
    result, posted = _build(monkeypatch, out_mint=COIN, probe=True, sim=REJECTED)
    assert result is None and posted.get("simulated")


REAL = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "raydium_t22_router_min_out.json")
                  .read_text())
MOVED = "Raydium simulation: price moved past min-out (router 0x26)"


def test_router_min_out_is_reported_as_a_moved_price_not_token_2022(monkeypatch):
    """Operator, 2026-09-27: "just relabel it, keep the slippage as is". Real frames: the same
    Raydium build of a Token-2022 coin simulates clean as quoted, and fails with router 0x26 once
    only its minimum out is raised. All 13 production refusals were that code."""
    R.take_build_refusal()
    miss = REAL["forced_min_out_miss"]
    result, posted = _build(monkeypatch, out_mint=REAL["mint"], probe=True,
                            sim={"result": {"value": miss["simulation"]}}, tx=miss["transaction"])
    assert result is None and posted.get("simulated")
    assert R.take_build_refusal() == MOVED
    clean = REAL["clean"]
    result, _ = _build(monkeypatch, out_mint=REAL["mint"], probe=True,
                       sim={"result": {"value": clean["simulation"]}}, tx=clean["transaction"])
    assert result == clean["transaction"] and R.take_build_refusal() is None


def test_other_simulation_errors_keep_the_token_2022_label(monkeypatch):
    """Only the router's own 0x26 line is a moved price; the same number from another program is
    not, and a real Token-2022 refusal keeps its label."""
    other = {"result": {"value": {"err": {"InstructionError": [0, {"Custom": 38}]},
                                  "logs": ["Program Other1111111111111111111111111111111111 failed: "
                                           "custom program error: 0x26"]}}}
    for sim in (REJECTED, other):
        R.take_build_refusal()
        result, _ = _build(monkeypatch, out_mint=COIN, probe=True, sim=sim)
        assert result is None
        assert R.take_build_refusal() == "Raydium simulation refused the Token-2022 buy"


def test_an_unreadable_simulation_refuses(monkeypatch):
    """A failed read is not 'clean' (MOX shape)."""
    for broken in ({"error": {"code": -32000}}, {"result": {"value": None}}, RuntimeError("rpc down")):
        result, posted = _build(monkeypatch, out_mint=COIN, probe=True, sim=broken)
        assert result is None and posted.get("simulated")


def test_an_unreadable_mint_is_simulated_not_refused(monkeypatch):
    """The probe no longer decides; an unreadable program just means 'ask the chain'."""
    result, posted = _build(monkeypatch, out_mint=COIN, probe=None, sim=CLEAN)
    assert result == "AA==" and posted.get("simulated")


def test_a_classic_mint_still_builds_without_simulating(monkeypatch):
    result, posted = _build(monkeypatch, out_mint=COIN, probe=False, sim=None)
    assert result is not None
    assert posted.get("url", "").endswith("/transaction/swap-base-in")
    assert "simulated" not in posted


def test_a_sol_output_is_not_probed(monkeypatch):
    """Selling into SOL has no token output account to derive; probing would be a wasted RPC."""
    calls = []
    monkeypatch.setattr(R, "_mint_is_token_2022",
                        lambda mint, http=None: calls.append(mint) or False)
    def http(method, url, body=None, **kw):
        return 200, {"success": True, "data": [{"transaction": "AA=="}]}, None
    R.raydium_swap_tx({"inputMint": SOL, "outputMint": SOL, "_raydium_response": {}},
                      "Owner1111111111111111111111111111111111111", http=http)
    assert calls == []


# --- the coin itself must NOT be refused ----------------------------------------------------------

def test_the_safety_gate_no_longer_refuses_a_safe_token_2022_mint(monkeypatch):
    """The heart of the correction. 54 of 60 recent funded fills are these mints."""
    import sniper.execution.solana_token_safety as S
    # `read_mint_account` is the seam since 2026-09-23: it returns (value, failure_reason) so a
    # transport failure, a node rejection and an account not yet visible stop sharing one reason
    # with a genuinely unparseable mint. A patch of the older `_mint_account` would no longer be
    # reached, and the test would pass on a code path nothing runs.
    monkeypatch.setattr(S, "read_mint_account", lambda mint, rpc, http: ({
        "owner": TOKEN_2022_PROGRAM,
        "data": {"parsed": {"info": {"freezeAuthority": None, "mintAuthority": None,
                                     "decimals": 6}}}}, None))
    ok, reason = S.token_safety_ok("M", "rpc", max_transfer_fee_bps=300)
    assert ok is True and reason == "ok"


def test_the_capability_switch_is_gone_not_left_inert():
    """An unused knob is worse than none — the next reader budgets trust for it."""
    import inspect
    import sniper.execution.solana_token_safety as S
    from sniper.settings import memecoin as cfg
    assert "allow_token_2022" not in inspect.signature(S.token_safety_ok).parameters
    assert not hasattr(cfg, "MEMECOIN_SOLANA_ALLOW_TOKEN_2022")
