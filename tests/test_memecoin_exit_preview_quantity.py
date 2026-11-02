"""Quote quantities come from the balance being quoted, without another provider read."""
import json

import pytest
from solders.keypair import Keypair

from sniper.execution import solana_real_seams as S
from sniper.settings import memecoin as cfg


# #532: a missing or mixed decimals is an unproved balance, a read failure with no quote (it used
# to read as an unknown quantity)
@pytest.mark.parametrize("amounts,decimals,want", [
    ([1_250_000, 750_000], [6, 6], 2.0),
    ([5, 7], [0, 0], 12.0),
    ([1_000_000], [None], "failure"),
    ([1_000_000, 10], [6, 0], "failure"),
])
def test_solana_preview_carries_its_actual_human_input_without_another_read(
        monkeypatch, amounts, decimals, want):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_PREVIEW_DEADLINE_S", 0)
    calls = []

    def http(method, url, headers=None, body=None, **kwargs):
        calls.append(json.loads(body)["method"])
        owner, mint = json.loads(body)["params"][0], json.loads(body)["params"][1]["mint"]
        accounts = []
        for i, (amount, dec) in enumerate(zip(amounts, decimals)):
            token = {"amount": str(amount)}
            if dec is not None:
                token["decimals"] = dec
            accounts.append({"pubkey": "acct%d" % i, "account": {
                "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {
                    "type": "account", "info": {"owner": owner, "mint": mint, "state": "initialized",
                                                "tokenAmount": token}}}}})
        return 200, {"result": {"value": accounts}}, None

    quoted = []
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda mint, atomic, slip, http=None:
                        quoted.append(atomic) or {"outAmount": "5000000"})
    executor = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://custody", http=http,
        usdc_fn=lambda: 0, token_fn=lambda mint: 0)
    result = executor["preview_exit_fn"]({"id": 1, "mint": "MintX"})

    if want == "failure":            # the balance failover may re-read; nothing else, and no quote
        assert result.failure is not None and quoted == [] and set(calls) == {"getTokenAccountsByOwner"}
        return
    assert calls == ["getTokenAccountsByOwner"]
    assert result.failure is None
    assert result.input_quantity == want
    assert result.amount_atomic == sum(amounts)
    assert quoted == [sum(amounts)]


@pytest.mark.parametrize("decimals,want", [(6, 2.0), (0, 2_000_000.0), (None, None)])
def test_evm_preview_only_uses_cached_input_decimals(monkeypatch, decimals, want):
    from eth_account import Account
    from sniper.execution import evm_real_seams as E, evm_wallet

    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *a, **k: 2_000_000)
    monkeypatch.setattr(E, "quote_usd_price", lambda *a, **k: 2_000.0)
    monkeypatch.setattr(E, "venue_quote_with_v2_fallback", lambda primary, **kwargs: primary)
    cached = []
    executor = E.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        http=lambda *a, **k: pytest.fail("quantity metadata must not read a provider"),
        venue_quote_fn=lambda *a, **k: {
            "buyAmount": "1000000000000000", "transaction": {"to": "0xrouter"}},
        cached_decimals_fn=lambda mint: cached.append(mint) or decimals,
        decimals_fn=lambda mint: pytest.fail("a cold decimals read is forbidden"),
        usdc_fn=lambda: 0, token_fn=lambda mint: 0)
    result = executor["preview_exit_fn"]({"mint": "0xtok"})

    assert result.failure is None
    assert result.input_quantity == want
    assert cached == ["0xtok"]


@pytest.mark.parametrize("position,cached_decimals,cold_result,want,cold_calls", [
    ({"_exit_custom_cap": True}, None, 6, 2.0, 1),
    ({"_mcap_exit_target": {"mcap_low": 1500}}, None, 6, 2.0, 1),
    ({}, None, 6, None, 0),
    ({"_exit_custom_cap": True}, 0, 6, 2_000_000.0, 0),
    ({"_exit_custom_cap": True}, None, None, None, 1),
])
def test_restored_custom_cap_warms_existing_decimals_cache_only_when_needed(
        monkeypatch, position, cached_decimals, cold_result, want, cold_calls):
    from eth_account import Account
    from sniper.execution import evm_real_seams as E, evm_wallet

    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *a, **k: 2_000_000)
    monkeypatch.setattr(E, "quote_usd_price", lambda *a, **k: 2000)
    monkeypatch.setattr(E, "venue_quote_with_v2_fallback", lambda primary, **kwargs: primary)
    cache, reads = {"0xtok": cached_decimals}, []

    def decimals(mint):
        reads.append(mint)
        cache[mint] = cold_result
        return cold_result

    executor = E.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        http=lambda *a, **k: pytest.fail("only the existing decimals callback may read"),
        venue_quote_fn=lambda *a, **k: {
            "buyAmount": "1000000000000000", "transaction": {"to": "0xrouter"}},
        cached_decimals_fn=cache.get, decimals_fn=decimals,
        usdc_fn=lambda: 0, token_fn=lambda mint: 0)
    pos = dict(position, mint="0xtok")
    assert executor["preview_exit_fn"](pos).input_quantity == want
    assert reads == ["0xtok"] * cold_calls
    if cold_result is not None:
        assert executor["preview_exit_fn"](pos).input_quantity == want
        assert reads == ["0xtok"] * cold_calls, "success reuses the existing cache"


def test_evm_context_passes_a_read_only_decimals_cache_lookup(monkeypatch, tmp_path):
    from sniper.execution import evm_real_seams as E, evm_wallet
    from sniper.services import memecoin_evm_leg as leg
    from test_memecoin_evm_leg import _chain_identity_http

    path = str(tmp_path / "evm.json")
    evm_wallet.generate_keypair_file(path)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", path)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://rpc")
    original = E.build_real_evm_executor
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(E, "build_real_evm_executor", build)
    context = leg.build_real_evm_context("4663", http=_chain_identity_http("4663"))
    token = "0x" + "ab" * 20
    peek = seen["cached_decimals_fn"]
    assert peek(token) is None
    context["put_token_decimals_fn"](token, 0)
    assert peek(token.upper()) == 0, "a miss must not cache None or fetch decimals"


@pytest.mark.parametrize("route", ["flap_quote_exit", "lifi"])
def test_evm_alternate_preview_keeps_the_quantity_of_its_actual_route(monkeypatch, route):
    from eth_account import Account
    from sniper.execution import evm_real_seams as E, evm_wallet, evm_flap_quote_exit as F
    from sniper.execution.exit_preview import ExitPreview, PreviewResult

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", route == "flap_quote_exit")
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *a, **k: 2_000_000)
    monkeypatch.setattr(F, "prewarm", lambda *a, **k: None)
    monkeypatch.setattr(F, "cached_eligibility", lambda *a, **k: True)
    monkeypatch.setattr(F, "preview_exit", lambda mint, atomic, **kwargs:
                        PreviewResult.succeeded(ExitPreview(5, atomic, route, "now")))
    monkeypatch.setattr(E, "classify_quote_miss", lambda rungs: ("no_route", {}))
    monkeypatch.setattr(E, "venue_quote_with_v2_fallback", lambda *a, **k: lambda *a, **k: None)
    executor = E.build_real_evm_executor(
        acct=Account.create(), chain_id="56" if route == "flap_quote_exit" else "4663", rpc_url="https://rpc",
        http=lambda *a, **k: pytest.fail("quantity metadata must not read a provider"),
        venue_quote_fn=lambda *a, **k: None,
        lifi_preview_fn=lambda **kwargs: PreviewResult.succeeded(ExitPreview(
            5, kwargs["amount_atomic"], route, "now")),
        cached_decimals_fn=lambda mint: 6, usdc_fn=lambda: 0, token_fn=lambda mint: 0)
    monkeypatch.setattr(E, "quote_usd_price", lambda *a, **k: 2000)
    result = executor["preview_exit_fn"]({"mint": "0xtok", "lifi_dependent": True})

    assert result.input_quantity == 2.0
    assert result.route_id == route
