"""The native pump.fun exit reaches the executor through the same seams a Jupiter swap does.

fries, 55293, 2026-09-09: Jupiter quotes the sell on a stock-quoted curve and cannot build it.
`_exit_quote` must hand the executor the native composite instead; `jupiter_swap_tx` must return
its prebuilt, simulated transaction; and the fast builder (Jupiter /swap-instructions) must never
be asked to rebuild a venue it does not know -- the same exclusion the Raydium venue has.
"""
from solders.keypair import Keypair

from sniper.execution import solana_real_seams as S

MCDX = "XsqE9cRRpzxcGKDXj1BJ7Xmg4GRhZoyY1KpmGSxAWT2"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NATIVE = {"_venue": "pump_native", "inAmount": "5", "otherAmountThreshold": "3", "outputMint": USDC,
          "_native_swap": {"swapTransaction": "AAAA", "lastValidBlockHeight": 77}}


def _executor(**kw):
    return S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc",
                                        http=lambda *_a, **_k: (200, {}, None),
                                        usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0, **kw)


def test_jupiter_swap_tx_returns_the_prebuilt_native_transaction():
    assert S.jupiter_swap_tx(NATIVE, "user") == "AAAA"
    assert S.jupiter_swap_tx(NATIVE, "user", return_metadata=True) == NATIVE["_native_swap"]
    assert S.jupiter_swap_tx({"_venue": "pump_native"}, "user") is None, "no bytes, nothing to sign"


def test_exit_quote_substitutes_the_native_composite_when_jupiter_cannot_build(monkeypatch):
    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 5707019249939)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *_a, **_k: {
        "inAmount": "5707019249939", "outAmount": "27000000", "otherAmountThreshold": "26000000",
        "routePlan": [{"swapInfo": {"label": "Pump.fun", "inputMint": "MINT", "outputMint": MCDX}},
                      {"swapInfo": {"label": "Whirlpool", "inputMint": MCDX, "outputMint": USDC}}]})
    seen = {}

    def fake_native(mint, atomic, slip, *, user, rpc, jup_call):
        seen.update(mint=mint, atomic=atomic, slip=slip, user=str(user))
        return dict(NATIVE)
    import sniper.execution.pump_native_exit as N
    monkeypatch.setattr(N, "build_native_exit", fake_native)
    ex = _executor()
    q = ex["exit_quote_fn"]("MINT", 5707019.249939, 10.0)
    assert q["_venue"] == "pump_native"
    assert seen == {"mint": "MINT", "atomic": 5707019249939, "slip": 10.0, "user": str(ex["pubkey"])}


def test_exit_quote_keeps_a_sol_quoted_curve_on_jupiter(monkeypatch):
    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 10)
    jup = {"inAmount": "10", "outAmount": "9", "otherAmountThreshold": "8",
           "routePlan": [{"swapInfo": {"label": "Pump.fun", "inputMint": "MINT",
                                        "outputMint": "So11111111111111111111111111111111111111112"}}]}
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *_a, **_k: jup)
    import sniper.execution.pump_native_exit as N
    monkeypatch.setattr(N, "build_native_exit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build natively")))
    assert _executor()["exit_quote_fn"]("MINT", 1.0, 10.0) is jup


def test_a_native_build_that_did_not_simulate_clean_yields_no_quote(monkeypatch):
    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 10)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *_a, **_k: {
        "inAmount": "10", "outAmount": "9", "otherAmountThreshold": "8",
        "routePlan": [{"swapInfo": {"label": "Pump.fun", "inputMint": "MINT", "outputMint": MCDX}}]})
    import sniper.execution.pump_native_exit as N
    monkeypatch.setattr(N, "build_native_exit", lambda *a, **k: None)
    assert _executor()["exit_quote_fn"]("MINT", 1.0, 10.0) is None


# ── {CAT} (2026-09-13): unrestricted payouts go native first, then a packet-sized Jupiter route ──

PUMP = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
_PUMP_ROUTE = {"inAmount": "10", "outAmount": "9", "otherAmountThreshold": "8",
               "routePlan": [{"swapInfo": {"label": "Pump.fun", "inputMint": "MINT", "outputMint": PUMP}},
                             {"swapInfo": {"label": "Raydium CLMM", "inputMint": PUMP, "outputMint": USDC}}]}


def test_an_unrestricted_payout_still_tries_the_native_sell_first(monkeypatch):
    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 10)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *_a, **_k: dict(_PUMP_ROUTE))
    monkeypatch.setattr(S, "_cached_payout_restricted", lambda *_a, **_k: False)
    import sniper.execution.pump_native_exit as N
    monkeypatch.setattr(N, "build_native_exit", lambda *a, **k: dict(NATIVE))
    assert _executor()["exit_quote_fn"]("MINT", 1.0, 10.0)["_venue"] == "pump_native"


def test_without_a_native_sell_an_unrestricted_payout_gets_a_packet_sized_route(monkeypatch):
    """{CAT} pos 56745: Jupiter's routes through PUMP built at 1,446 / 1,290 bytes (> 1,232)."""
    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 10)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *_a, **_k: dict(_PUMP_ROUTE))
    monkeypatch.setattr(S, "_cached_payout_restricted", lambda *_a, **_k: False)
    import sniper.execution.pump_native_exit as N
    monkeypatch.setattr(N, "build_native_exit", lambda *a, **k: None)
    asked = {}
    capped = {"inAmount": "10", "outAmount": "8", "otherAmountThreshold": "7", "routePlan": []}

    def fake_quote(inp, out, amount, slippage_bps, http, max_accounts=None):
        asked.update(inp=inp, out=out, amount=amount, max_accounts=max_accounts)
        return capped
    monkeypatch.setattr(S, "_quote_with_fallback", fake_quote)
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", 48)
    assert _executor()["exit_quote_fn"]("MINT", 1.0, 10.0) is capped
    assert asked == {"inp": "MINT", "out": USDC, "amount": 10, "max_accounts": 48}


def test_sign_swap_tx_returns_nothing_for_an_empty_build():
    assert S.sign_swap_tx(None, Keypair()) is None
    assert S.sign_swap_tx({"swapTransaction": None}, Keypair()) is None


def test_oversize_native_exit_uses_the_capped_route(monkeypatch):
    import base64
    import sniper.execution.pump_native_exit as N
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: 10)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: dict(_PUMP_ROUTE))
    monkeypatch.setattr(S, "_cached_payout_restricted", lambda *a, **k: False)
    native = dict(NATIVE, _native_swap={"swapTransaction": base64.b64encode(b'x' * 1233).decode()})
    monkeypatch.setattr(N, "build_native_exit", lambda *a, **k: native)
    capped = {"inAmount": "10", "outAmount": "8"}
    monkeypatch.setattr(S, "_quote_with_fallback", lambda *a, **k: capped)
    assert _executor()["exit_quote_fn"]("MINT", 1, 10) is capped


def test_capped_quotes_cannot_fall_back_to_an_uncapped_raydium_route(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK", True)
    ray_calls = []
    monkeypatch.setattr(S, "_ray_quote", lambda *a: ray_calls.append(a) or {"outAmount": "100"})
    monkeypatch.setattr(S, "_jup_quote_status", lambda *a: (None, 400))
    assert S._quote_with_fallback("MINT", USDC, 10, 100, None, max_accounts=48) is None
    q = {"outAmount": "8", "priceImpactPct": "0.2"}
    monkeypatch.setattr(S, "_jup_quote_status", lambda *a: (q, 200))
    assert S._quote_with_fallback("MINT", USDC, 10, 100, None, max_accounts=48) is q
    assert ray_calls == []


def test_entry_native_proof_rejects_an_oversize_packet(monkeypatch):
    import base64
    import time
    import sniper.execution.pump_native_exit as N
    native = dict(NATIVE, _native_swap={"swapTransaction": base64.b64encode(b'x' * 1233).decode()})
    monkeypatch.setattr(N, "build_native_exit", lambda *a, **k: native)
    monkeypatch.setattr(S.m, "pending_row", lambda *a: {"strategy": "alpha_b", "source_user_id": "u"})
    import sniper.execution.solana_swap_executor as E
    monkeypatch.setattr(E, "execute_entry", lambda *a, **k: {"native_available": k["native_exit_available_fn"]("MINT", 10)})
    # (ok, reason) since 2026-09-18, so the entry guard can tell an oversize packet from a build
    # that never completed. Quant ref 7f6e294e was refused for the second while being the first.
    ok, reason = _executor(submission_transport=object())["execute_fn"](
        None, 1, "MINT", 1, "t", entry_min_slot=100, entry_max_slot=101,
        entry_head_fn=lambda: (100, time.monotonic()))["native_available"]
    assert ok is False
    assert reason.startswith("too_big:"), reason


def test_failed_capped_native_fallback_does_not_return_the_original_route(monkeypatch):
    import sniper.execution.pump_native_exit as N
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: 10)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: dict(_PUMP_ROUTE))
    monkeypatch.setattr(S, "_cached_payout_restricted", lambda *a, **k: False)
    monkeypatch.setattr(N, "build_native_exit", lambda *a, **k: None)
    monkeypatch.setattr(S, "_quote_with_fallback", lambda *a, **k: None)
    assert _executor()["exit_quote_fn"]("MINT", 1, 10) is None


def test_disabled_account_cap_keeps_the_unrestricted_jupiter_fallback(monkeypatch):
    import sniper.execution.pump_native_exit as N
    from sniper.settings import memecoin as cfg
    quote = dict(_PUMP_ROUTE)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", 0)
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: 10)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: quote)
    monkeypatch.setattr(S, "_cached_payout_restricted", lambda *a, **k: False)
    monkeypatch.setattr(N, "build_native_exit", lambda *a, **k: None)
    capped_calls = []
    monkeypatch.setattr(S, "_quote_with_fallback", lambda *a, **k: capped_calls.append(k))
    assert _executor()["exit_quote_fn"]("MINT", 1, 10) is quote
    assert capped_calls == []
