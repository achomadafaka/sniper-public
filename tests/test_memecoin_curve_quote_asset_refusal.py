"""A curve quoted in a tokenized stock is an entry with no exit.

fries, position 55293, 2026-09-09: a pump.fun bonding curve quoted in McDonald's xStock (MCDx, a
Token-2022 mint with permanent-delegate / pausable / confidential-transfer extensions). Jupiter
quoted the sell and then could not build it -- `InstructionError[2, IllegalOwner]` at an ATA create
for the quote asset, identical under plain /swap, useSharedAccounts=false and a single hop to the
quote asset. The buy built fine. 15 exit attempts failed while the take-profit at +78% became -83%.
23 of the last 25 transactions on that curve were other wallets failing the same way.

The reverse quote is already fetched for the round-trip cap, so the refusal reads a route we hold
and costs no request. Same harness as test_memecoin_entry_impact_cap.py.
"""
import sqlite3

import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m

MCDX = "XsqE9cRRpzxcGKDXj1BJ7Xmg4GRhZoyY1KpmGSxAWT2"
USDC = ex.USDC_MINT
SOL = ex.SOL_MINT


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    return c, pid


def _leg(label, in_mint, out_mint):
    return {"swapInfo": {"label": label, "inputMint": in_mint, "outputMint": out_mint, "ammKey": "k"}}


class _Deps:
    def __init__(self, route_plan):
        self.calls = []
        self._route_plan = route_plan

    def quote_fn(self, mint, usd):
        return {"outAmount": "500", "otherAmountThreshold": "470", "priceImpactPct": 0.01}

    def reverse_quote_fn(self, mint, amount):
        out = str(int(amount) * 20_000)
        q = {"inAmount": str(amount), "outAmount": out, "otherAmountThreshold": out}
        if self._route_plan is not None:
            q["routePlan"] = self._route_plan
        return q

    def usdc_fn(self):
        return 90.0 if "sent" in self.calls else 100.0

    def token_fn(self, mint):
        return 500.0 if "sent" in self.calls else 0.0

    def sign_fn(self, quote):
        self.calls.append("sign"); return (b"signedtx", "SIG123")

    def send_fn(self, signed):
        self.calls.append("send"); self.calls.append("sent"); return True

    def confirm_fn(self, sig):
        self.calls.append("confirm"); return "confirmed"


def _run(c, pid, d):
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-09-09T20:05:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, impact_cap_pct=12.0)


def test_a_stock_quoted_curve_is_refused_before_anything_is_signed():
    """THE REGRESSION: fries' exit route -- Pump.fun pays MCDx, Whirlpool turns it into USDC."""
    c, pid = _c()
    d = _Deps([_leg("Pump.fun", "MINT", MCDX), _leg("Whirlpool", MCDX, USDC)])
    r = _run(c, pid, d)
    assert r["status"] == "aborted"
    assert "Pump.fun hop" in r["reason"] and MCDX[:8] in r["reason"]
    assert "sign" not in d.calls
    assert m.pending_totals(c) == (0, 0.0), "the reservation is released"


def test_a_sol_quoted_curve_passes_exactly_as_before():
    """Every curve entry that sold fine this month paid SOL at its Pump.fun hop."""
    c, pid = _c()
    d = _Deps([_leg("Pump.fun", "MINT", SOL), _leg("Whirlpool", SOL, USDC)])
    assert _run(c, pid, d)["status"] == "filled"


def test_a_direct_usdc_curve_and_a_non_pump_route_pass():
    c, pid = _c()
    assert _run(c, pid, _Deps([_leg("Pump.fun", "MINT", USDC)]))["status"] == "filled"
    c, pid = _c()
    assert _run(c, pid, _Deps([_leg("Raydium", "MINT", MCDX), _leg("Whirlpool", MCDX, USDC)]))["status"] == "filled", \
        "only the Pump.fun adapter is known to be unable to build the sell"


def test_a_quote_without_a_route_plan_is_not_judged():
    """The Raydium venue fallback carries no routePlan; the guard refuses one named shape only."""
    c, pid = _c()
    assert _run(c, pid, _Deps(None))["status"] == "filled"
    assert ex.unbuildable_curve_quote_asset(None) is None
    assert ex.unbuildable_curve_quote_asset({"routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}) is None


# ── narrowed to Token-2022 payout assets (2026-09-12, operator: "fix it") ───

FART = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"   # classic SPL


def _run_with_program(c, pid, d, programs):
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-09-12T17:47:30Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, impact_cap_pct=12.0,
        quote_asset_restricted_fn=lambda mint: programs.get(mint))


def test_a_classic_spl_payout_passes_the_fstr_case():
    """FSTR paid Fartcoin at its Pump.fun hop; Jupiter built and simulated that sell for a real
    holder. A proven classic mint is not the fries trap."""
    c, pid = _c()
    d = _Deps([_leg("Pump.fun", "MINT", FART), _leg("Scorch", FART, USDC)])
    assert _run_with_program(c, pid, d, {FART: False})["status"] == "filled"


def test_a_token_2022_payout_is_still_refused_the_fries_case():
    c, pid = _c()
    d = _Deps([_leg("Pump.fun", "MINT", MCDX), _leg("Whirlpool", MCDX, USDC)])
    r = _run_with_program(c, pid, d, {MCDX: True})
    assert r["status"] == "aborted" and MCDX[:8] in r["reason"] and "sign" not in d.calls


def test_an_unreadable_payout_program_is_refused():
    """None means the RPC did not answer. It must not read as classic."""
    c, pid = _c()
    d = _Deps([_leg("Pump.fun", "MINT", FART), _leg("Scorch", FART, USDC)])
    assert _run_with_program(c, pid, d, {})["status"] == "aborted"
    assert ex.unbuildable_curve_quote_asset(
        {"routePlan": [_leg("Pump.fun", "MINT", FART)]},
        lambda mint: (_ for _ in ()).throw(RuntimeError("rpc down"))) == FART


# ── narrowed to RESTRICTING extensions (2026-09-13, operator: "check and fix") ──

PUMP = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"   # Token-2022, transferHook only


def _parsed(owner, exts):
    return {"result": {"value": {"owner": owner, "data": {"parsed": {"info": {
        "extensions": [{"extension": e} for e in exts]}}}}}}


def test_the_reader_flags_only_restricting_extensions(monkeypatch):
    """MCDx (fries) carried permanentDelegate/defaultAccountState/pausable/confidential transfer;
    PUMP carries only a transferHook and a real {CAT} holder's sell through it simulated clean."""
    from sniper.execution import solana_real_seams as S
    from sniper.execution.solana_token_safety import TOKEN_2022_PROGRAM
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "https://rpc.example")
    answers = {
        MCDX: _parsed(TOKEN_2022_PROGRAM, ["metadataPointer", "permanentDelegate", "defaultAccountState",
                                           "scaledUiAmountConfig", "pausableConfig",
                                           "confidentialTransferMint", "transferHook", "tokenMetadata"]),
        PUMP: _parsed(TOKEN_2022_PROGRAM, ["transferHook", "metadataPointer", "tokenMetadata"]),
        FART: _parsed("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", []),
    }

    def http(method, url, headers=None, body=None):
        mint = __import__("json").loads(body)["params"][0]
        return (200, answers[mint], None) if mint in answers else (503, {}, None)
    monkeypatch.setattr(S, "_PAYOUT_RESTRICTED_CACHE", {})
    assert S._cached_payout_restricted(MCDX, http) is True
    assert S._cached_payout_restricted(PUMP, http) is False
    assert S._cached_payout_restricted(FART, http) is False
    assert S._cached_payout_restricted("UNREADABLE", http) is None
    assert "UNREADABLE" not in S._PAYOUT_RESTRICTED_CACHE, "no answer is never cached"


def test_a_pump_payout_curve_now_passes_the_cat_case():
    c, pid = _c()
    d = _Deps([_leg("Pump.fun", "MINT", PUMP), _leg("Scorch", PUMP, USDC)])
    assert _run_with_program(c, pid, d, {PUMP: False})["status"] == "filled"


# ── a restricted payout the native pump.fun sell can build is admitted (2026-09-13, DELULU) ──

LULU = "LULUmT9VMttkfAJE236LXJcYJ2tTP7nunrSWR5G1BdS"   # Lululemon xStock, fries-class extensions


def _run_native(c, pid, d, native):
    seen = []

    def native_fn(mint, amount):
        seen.append((mint, amount))
        if isinstance(native, Exception):
            raise native
        return native
    r = ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-09-13T19:43:38Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, impact_cap_pct=12.0,
        quote_asset_restricted_fn=lambda mint: True, native_exit_available_fn=native_fn)
    return r, seen


def test_a_restricted_payout_with_a_buildable_native_sell_is_admitted():
    """DELULU (f4d86a45): LULU payout, refused although the native sell (proven live on fries)
    builds for it. The check sizes the sell at the planned quantity, before anything is signed."""
    c, pid = _c()
    d = _Deps([_leg("Pump.fun", "MINT", LULU), _leg("Raydium CLMM", LULU, SOL)])
    r, seen = _run_native(c, pid, d, True)
    assert r["status"] == "filled"
    assert seen == [("MINT", 470)], "checked once, at the planned (minimum-out) quantity"


@pytest.mark.parametrize("native", [False, RuntimeError("rpc down")])
def test_a_restricted_payout_without_a_native_sell_is_still_refused(native):
    c, pid = _c()
    d = _Deps([_leg("Pump.fun", "MINT", LULU), _leg("Raydium CLMM", LULU, SOL)])
    r, seen = _run_native(c, pid, d, native)
    assert r["status"] == "aborted" and LULU[:8] in r["reason"] and "sign" not in d.calls
    assert seen == [("MINT", 470)]
    assert "send" not in d.calls
    assert "exit not verified" in r["reason"]
    assert "unsellable trap" not in r["reason"]
