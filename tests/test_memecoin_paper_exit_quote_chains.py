"""`executable_exit_fill` must reach the route quote on EVM chains, not just Solana.

AUDIT 2026-08-05. The wrapper resolved token decimals with the Solana-only `token_decimals()`
and hardcoded `None` for every other chain, so `qty_atomic` was `None` and it returned
`qty_unresolved` BEFORE ever calling `quote_exit()` — which has a complete, working 4663 sell
branch. Live ledger at the time: 868/868 chain-4663 paper ENTRIES priced `route_quote`, and
0 of 387 full exits did. Every 4663 close booked a `noquote` leg and the report censored it,
leaving all eight forward arms with zero measurable closes on that chain.
"""
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_copy_trader as ct
from sniper.services import memecoin_quotes as q


def _never(what):
    def _boom(*_a, **_kw):
        raise AssertionError(f"must not call {what}")
    return _boom


def test_evm_exit_resolves_decimals_and_reaches_the_route_quote(monkeypatch):
    seen = {}

    def fake_quote_exit(position, qty_atomic, **kwargs):
        seen.update(position=position, qty_atomic=qty_atomic)
        return SimpleNamespace(unit_price=0.002)      # WETH per token

    monkeypatch.setattr(q, "quote_exit", fake_quote_exit)
    monkeypatch.setattr(q, "erc20_decimals", lambda mint, rpc, **kw: 18)
    monkeypatch.setattr(q, "token_decimals", _never("the Solana decimals RPC on an EVM chain"))
    monkeypatch.setattr(q, "quote_usd_price", lambda chain, **kw: 4000.0)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "http://rpc.invalid")

    price, source = ct.executable_exit_fill("0xTOKEN", "4663", 2.5)

    assert seen["qty_atomic"] == 2_500_000_000_000_000_000, "human qty -> 18dp atomic"
    assert seen["position"]["token_decimals"] == 18, "pass decimals so quote_exit re-resolves nothing"
    assert source == "route_quote"
    # 4663 quotes in WETH; the wrapper must still convert to USD per token (CHAIN_QUOTE kind='eth').
    assert price == pytest.approx(0.002 * 4000.0)


def test_evm_exit_stays_fail_closed_when_decimals_are_unresolvable(monkeypatch):
    monkeypatch.setattr(q, "quote_exit", _never("quote_exit without a decimals answer"))
    monkeypatch.setattr(q, "erc20_decimals", lambda mint, rpc, **kw: None)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "http://rpc.invalid")

    assert ct.executable_exit_fill("0xTOKEN", "4663", 2.5) == (None, "qty_unresolved")


def test_evm_exit_without_a_configured_rpc_does_not_call_the_chain(monkeypatch):
    monkeypatch.setattr(q, "quote_exit", _never("quote_exit with no RPC configured"))
    monkeypatch.setattr(q, "erc20_decimals", _never("erc20_decimals with no RPC configured"))
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_4663", raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL", raising=False)

    assert ct.executable_exit_fill("0xTOKEN", "4663", 2.5) == (None, "qty_unresolved")


def test_solana_exit_path_is_unchanged(monkeypatch):
    seen = {}

    def fake_quote_exit(position, qty_atomic, **kwargs):
        seen.update(position=position, qty_atomic=qty_atomic)
        return SimpleNamespace(unit_price=0.005)     # already USD per token

    monkeypatch.setattr(q, "quote_exit", fake_quote_exit)
    monkeypatch.setattr(q, "token_decimals", lambda mint, rpc, **kw: 9)
    monkeypatch.setattr(q, "erc20_decimals", _never("the EVM decimals call on Solana"))

    price, source = ct.executable_exit_fill("SoLMiNT", "solana", 3.0)

    assert seen["qty_atomic"] == 3_000_000_000
    assert (price, source) == (0.005, "route_quote")
