"""Offline route selection: native SOL quotes retain their own intent and simulation proof."""
import base64
import json
import sqlite3

import pytest
from solders.keypair import Keypair

from sniper.execution import pump_native_exit as N
from sniper.execution import solana_exit_executor as E
from sniper.execution import solana_real_seams as S
from sniper.persistence import db_memecoin as m

WSOL = "So11111111111111111111111111111111111111112"
NOW = "2026-10-23T09:00:00+00:00"


def native(amount=10, *, simulated=True, size=100):
    return {"_venue": "pump_native", "inputMint": "MINT", "inAmount": str(amount),
            "outputMint": S.USDC_MINT, "outAmount": "9000000",
            "otherAmountThreshold": "8000000", "_curve_leg": {"kind": "sol"},
            "_native_swap": {"swapTransaction": base64.b64encode(b"x" * size).decode(),
                             "simulated": simulated}}


def executor(monkeypatch, **kw):
    monkeypatch.setattr(S, "bound_pumpswap_pool", lambda *a, **k: None)
    return S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="funded", read_rpc_url="read-only",
        http=kw.pop("http", lambda *a, **k: pytest.fail("unexpected transport call")),
        usdc_fn=lambda: 100., token_fn=lambda _: 10., **kw)


def missing_quote(monkeypatch, status=400, body=None, *, native_fn=None):
    def http(*a, **k):
        return status, body, None

    seen = []
    def quote(mint, amount, slip, *, http, **kw):
        seen.append((mint, amount, slip, kw))
        http("GET", S.JUP_BASES[0] + "/quote")
        return None
    monkeypatch.setattr(S, "jupiter_exit_quote", quote)
    monkeypatch.setattr(S, "bound_pumpswap_pool", lambda *a, **k: None)
    monkeypatch.setattr(N, "native_curve_sell", native_fn or (lambda *a, **k: native(a[1])),
                        raising=False)
    ex = S.build_real_solana_executor(keypair=Keypair(), rpc_url="funded", read_rpc_url="read-only",
                                    http=http, usdc_fn=lambda: 100., token_fn=lambda _: 10.)
    return ex, seen


def test_explicit_route_refusal_uses_exact_atoms_without_a_second_quote_or_balance_read(monkeypatch):
    amount = 2**62 + 19
    seen = []
    def build(mint, atomic, slip, **kw):
        seen.append((mint, atomic, slip))
        return native(atomic)
    ex, quotes = missing_quote(monkeypatch, body={"errorCode": "COULD_NOT_FIND_ANY_ROUTE"}, native_fn=build)
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: pytest.fail("rung already read balance"))
    got = ex["exit_quote_fn"]("MINT", 1., 3., atomic=amount, position_id=7)
    assert got == native(amount)
    assert seen == [("MINT", amount, 3.)]
    assert len(quotes) == 1


@pytest.mark.parametrize("status,body", [
    (429, {"errorCode": "COULD_NOT_FIND_ANY_ROUTE"}), (408, {"error": "timeout"}),
    (401, {"error": "unauthorized"}), (403, {"error": "banned"}),
    (503, {"error": "upstream down"}), (400, {"error": "invalid request"}),
    (200, {}), (200, None)])
def test_infrastructure_or_untyped_empty_quote_never_triggers_native(monkeypatch, status, body):
    ex, _ = missing_quote(monkeypatch, status, body,
        native_fn=lambda *a, **k: pytest.fail("no explicit route refusal"))
    assert ex["exit_quote_fn"]("MINT", 10., 3., atomic=10) is None


@pytest.mark.parametrize("result", [None, "raise", "probe", "oversize"])
def test_new_native_failure_is_contained(monkeypatch, result):
    def build(*a, **kw):
        if result == "raise":
            raise TimeoutError("RPC unavailable")
        if result == "probe":
            return native(simulated=False)
        if result == "oversize":
            return native(size=1233)
        return None
    ex, _ = missing_quote(monkeypatch, body={"errorCode": "NO_ROUTES_FOUND"}, native_fn=build)
    assert ex["exit_quote_fn"]("MINT", 10., 3., atomic=10) is None


@pytest.mark.parametrize("simulated", [False, None, 1])
def test_unsimulated_sol_build_cannot_reach_signer(monkeypatch, simulated):
    q = native(simulated=simulated)
    assert S.jupiter_swap_tx(q, "user", return_metadata=True) is None
    monkeypatch.setattr(S, "sign_swap_tx", lambda *a: pytest.fail("probe reached signer"))
    monkeypatch.setattr(m, "record_exit_execution", lambda *a, **k: None)
    def execute(*a, sign_fn, **kw):
        assert sign_fn(q, before_sign=lambda: True) is None
        return {"status": "stuck"}
    monkeypatch.setattr(E, "execute_exit", execute)
    ex = executor(monkeypatch)
    assert ex["exit_fn"](None, {"id": 7, "mint": "MINT"}, "manual", NOW)["status"] == "stuck"


@pytest.mark.parametrize("source", [False, True])
def test_partial_selection_uses_existing_partial_atoms_and_position_binding(monkeypatch, source):
    amount = 2**62 + 19
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: amount)
    monkeypatch.setattr(m, "record_exit_execution", lambda *a, **k: None)
    def execute(*a, quote_fn, **kw):
        part = amount // 3 if source else int(amount * .25)
        q = quote_fn("MINT", 10., 3., atomic=part if source else None)
        assert q == native(part)
        return {"status": "partial_sold"}
    monkeypatch.setattr(E, "execute_partial_exit", execute)
    ex, _ = missing_quote(monkeypatch, body={"errorCode": "COULD_NOT_FIND_ANY_ROUTE"})
    assert ex["derisk_fn"](None, {"id": 7, "mint": "MINT"}, .25, NOW,
                           source_request={"request_json": "{}"} if source else None)["status"] == "partial_sold"


def test_partial_pumpswap_hint_keeps_position_identity(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_EXIT", True)
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: 100)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: None)
    monkeypatch.setattr(m, "record_exit_execution", lambda *a, **k: None)
    pools = []
    def execute(*a, quote_fn, **kw):
        assert quote_fn("MINT", 10., 3.) is None
        return {"status": "stuck"}
    monkeypatch.setattr(E, "execute_partial_exit", execute)
    ex = executor(monkeypatch, native_exit_hint_fn=lambda *a: True)
    monkeypatch.setattr(S, "bound_pumpswap_pool", lambda mint, position_id=None: pools.append((mint, position_id)))
    ex["derisk_fn"](None, {"id": 7, "mint": "MINT"}, .25, NOW)
    assert pools == [("MINT", 7)]


@pytest.mark.parametrize("partial", [False, True])
def test_failed_unsigned_jupiter_build_rebinds_native_intent_on_next_rung(monkeypatch, partial):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_OVERLAP_READS", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL", [3., 10.])
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_SLIPPAGE_LADDER", [3., 10.])
    ex = executor(monkeypatch, http=lambda *a, **k: (400, {"error": "Failed to simulate swap: IllegalOwner"}, None))
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: 10_000_000)
    jup = {"inputMint": "MINT", "inAmount": "10000000", "outputMint": S.USDC_MINT,
           "outAmount": "12000000", "otherAmountThreshold": "11000000",
           "routePlan": [{"swapInfo": {"label": "Pump.fun", "inputMint": "MINT", "outputMint": WSOL}}]}
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: dict(jup, inAmount=str(a[1])))
    monkeypatch.setattr(N, "native_curve_sell", lambda *a, **k: native(a[1]), raising=False)
    real_swap = S.jupiter_swap_tx
    def swap(q, *a, **kw):
        if q.get("_venue"):
            return real_swap(q, *a, **kw)
        kw["http"]("POST", S.JUP_BASES[0] + "/swap")
        return None
    monkeypatch.setattr(S, "jupiter_swap_tx", swap)
    signed = []
    monkeypatch.setattr(S, "sign_swap_tx", lambda payload, *a: signed.append(payload) or ("bytes", "sig") if payload else None)
    monkeypatch.setattr(S, "rpc_send", lambda *a, **k: None)  # ambiguous: retain durable identity and stop
    monkeypatch.setattr(S, "rpc_confirm", lambda *a, **k: "unknown")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="MINT", ticker="M",
        entry_price=1., entry_notional=10., entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=10., opened_at=NOW, strategy="alpha_b", execution_mode="real", network_id="solana")
    m.insert_fill(c, position_id=pid, kind="entry", price=1., qty=10., notional=10.,
                  fill_source="chain", fraction=1., created_at=NOW, tx_sig="entry")
    try:
        def run():
            pos = m.get_position(c, pid)
            return (ex["derisk_fn"](c, pos, .25, NOW) if partial else
                    ex["exit_fn"](c, pos, "manual", NOW))
        result = run()
        assert result["status"] == "unknown"
        assert len(signed) == 1
        row = c.execute("SELECT intent_json FROM memecoin_exit_intents "
                        "WHERE json_extract(intent_json,'$.tx_identity')='sig'").fetchone()
        bound = json.loads(row[0])
        assert bound["min_output_atomic"] == 8_000_000
        assert bound["sell_amount_atomic"] == (2_500_000 if partial else 10_000_000)
        assert m.get_position(c, pid)["derisk_sig" if partial else "close_sig"] == "sig"
        assert run()["status"] == ("already_derisked" if partial else "already_closing")
        assert len(signed) == 1
    finally:
        c.close()


@pytest.mark.parametrize("status,body,route", [
    (429, {"error": "rate limited"}, "valid"), (401, {"error": "key rejected"}, "valid"),
    (403, {"error": "banned"}, "valid"), (408, {"error": "timeout"}, "valid"),
    (503, {"error": "simulation unavailable"}, "valid"), (200, {}, "valid"),
    (400, {"error": "invalid request"}, "valid"),
    (400, {"error": "Failed to simulate swap: RPC timeout"}, "valid"),
    (400, {"error": "Failed to simulate swap: IllegalOwner"}, "malformed")])
def test_failed_build_needs_explicit_route_refusal_and_valid_hops(monkeypatch, status, body, route):
    q = {"inputMint": "MINT", "inAmount": "10", "outAmount": "9", "otherAmountThreshold": "8",
         "routePlan": [None] if route == "malformed" else [{"swapInfo": {
             "label": "Pump.fun", "inputMint": "MINT", "outputMint": WSOL}}]}
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: q)
    def swap(q, *a, http, **kw):
        http("POST", S.JUP_BASES[0] + "/swap")
        return None
    monkeypatch.setattr(S, "jupiter_swap_tx", swap)
    monkeypatch.setattr(N, "native_curve_sell", lambda *a, **k: pytest.fail("build did not prove route refusal"))
    monkeypatch.setattr(m, "record_exit_execution", lambda *a, **k: None)
    def execute(*a, quote_fn, sign_fn, **kw):
        assert sign_fn(q) is None
        assert quote_fn("MINT", 1., 10., atomic=10) is q
        return {"status": "stuck"}
    monkeypatch.setattr(E, "execute_exit", execute)
    ex = executor(monkeypatch, http=lambda *a, **k: (status, body, None))
    ex["exit_fn"](None, {"id": 7, "mint": "MINT"}, "manual", NOW)
