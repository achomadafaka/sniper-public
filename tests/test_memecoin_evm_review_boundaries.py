"""Offline producer/consumer regressions for R26-06, R26-07 and R26-08."""
import pytest

from sniper.execution import evm_mixed_hop_router as R, evm_real_seams as S
from sniper.services import memecoin_volume_writer as V, memecoin_bsc_v2_live as B
from sniper.services.memecoin_onchain_lane import OnchainEv
from test_memecoin_mixed_hop_rung import (
    COIN, WBNB, TAKER, CLIP, MIXED_POSITION, _executor, _ladder, _v3, _v2, _pairs, _Cfg,
)

TX = "0x" + "11" * 32


@pytest.mark.parametrize("origin,valid", [
    (None, True),  # actual producer below
    (f"swap:raybot:{TX}:buy", True),
    (f"onchain:{TX}", True),
    (f"onchain:8453:{TX}:{COIN}", False),
    (f"onchain:56:{TX}:0x{'22' * 20}", False),
    (f"onchain:56:0x123:{COIN}", False),
    (f"onchain:56:{TX}:{COIN}:extra", False),
])
def test_bsc_volume_origin_is_bound_to_chain_and_mint(monkeypatch, origin, valid):
    event = OnchainEv(dict(mint=COIN, source_user_id="u", handle="w", network_id="56",
                          source_size_usd=1000, source_price=1,
                          signature=f"56:{TX}:{COIN}", detected_at="2026-09-26T12:00:00+00:00"))
    calls = []
    monkeypatch.setattr(B, "read_m5", lambda *a: calls.append(a) or {"usd": 10000, "reason": "ok"})
    result = V.gate_evidence("56", COIN.upper(), origin or event.fomo_event_id,
                             stats={"pair_address": "pool", "quote_address": WBNB})
    assert result["reason"] == ("ok" if valid else "source_tx_unknown")
    assert calls == ([("pool", WBNB, COIN.upper(), TX)] if valid else [])


@pytest.mark.parametrize("healthy", [True, False])
def test_preferred_mixed_timeout_preserves_fallback_and_attribution(monkeypatch, healthy):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution import evm_dex_quote
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED", False)
    executor = _executor(monkeypatch)
    def timeout(*a, **kw):
        raise TimeoutError("mixed provider timeout")
    monkeypatch.setattr(R, "mixed_exit_quote", timeout)
    if not healthy:
        monkeypatch.setattr(evm_dex_quote, "dex_v3_quote", lambda *a, **kw: None)
    preview = executor["preview_exit_fn"](MIXED_POSITION)
    if healthy:
        assert preview.route_id == "0xv3dust"
    else:
        assert preview.failure == QuoteFailure.PROVIDER_TIMEOUT


@pytest.mark.parametrize("mode,expected", [
    ("unknown", "provider_rpc_transport"),
    ("timeout", "provider_timeout"),
    ("rate_limit", "provider_rate_limit"),
    ("override_error", "provider_rpc_transport"),
    ("revert", "no_route"),
    ("layout", "no_route"),
])
def test_real_mixed_proof_keeps_provider_failure_separate(monkeypatch, mode, expected):
    def call(*a, **kw):
        if mode == "timeout":
            raise TimeoutError("read timeout")
        if mode == "rate_limit":
            exc = RuntimeError("limited")
            exc.failure_type = "rate_limit"
            raise exc
        return (False, None, "STF") if mode == "revert" else (None, None, None)
    def overrides(*a, **kw):
        if mode == "override_error":
            raise RuntimeError("RPC unavailable")
        return None if mode == "layout" else {COIN: {}}
    monkeypatch.setattr(S, "_rpc_eth_call_result", call)
    real_entry = R.mixed_entry_quote
    def entry(chain, sell, buy, amount, taker, **kw):
        return real_entry(chain, sell, buy, amount, taker, rpc_url="https://rpc.invalid",
                          fund_overrides_fn=overrides, v3_quote_fn=_v3, v2_quote_fn=_v2,
                          pair_tokens_fn=_pairs, cfg=_Cfg)
    monkeypatch.setattr(R, "mixed_entry_quote", entry)
    ladder, rows = _ladder(monkeypatch)
    assert ladder(WBNB, COIN, CLIP) is None
    row = next(r for r in rows if r["rung"] == "mixed_v3_v2")
    assert S.classify_quote_miss([row])[0] == expected


def test_preferred_failure_trace_survives_fallback_without_old_preview_hits(monkeypatch):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution import evm_dex_quote
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED", False)
    executor = _executor(monkeypatch)
    monkeypatch.setattr(R, "mixed_exit_quote", lambda *a, **kw: (None, "gone"))
    assert executor["preview_exit_fn"](MIXED_POSITION).route_id == "0xv3dust"
    def timeout(*a, **kw):
        raise TimeoutError("timeout")
    monkeypatch.setattr(R, "mixed_exit_quote", timeout)
    monkeypatch.setattr(evm_dex_quote, "dex_v3_quote", lambda *a, **kw: None)
    assert executor["preview_exit_fn"](MIXED_POSITION).failure == QuoteFailure.PROVIDER_TIMEOUT


def test_rpc_simulation_retains_a_typed_timeout(monkeypatch):
    def timeout(*a, **kw):
        raise TimeoutError("RPC timed out")
    monkeypatch.setattr(S, "_rpc", timeout)
    monkeypatch.setattr("sniper.execution.evm_read_rpc._freshest_read_member", lambda u: u)
    status, kind, _ = S._rpc_eth_call_result({"to": TAKER}, TAKER, "https://rpc.invalid")
    assert status is None and kind == "timeout"


def test_mixed_ladder_passes_its_deadline_into_override_discovery(monkeypatch):
    import time
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_WAIT_S", 0.5)
    seen = []
    real_entry = R.mixed_entry_quote
    def overrides(*a, **kw):
        seen.append(kw.get("deadline"))
        return {COIN: {}}
    def simulated_exit(*a, **kw):
        from eth_abi import encode
        kw["result_out"]["result"] = "0x" + encode(["bytes[]"], [[
            b"", encode(["uint256"], [CLIP]), encode(["uint256"], [CLIP * 98 // 100])]]).hex()
        return True, None, None
    monkeypatch.setattr(S, "_rpc_eth_call_result", simulated_exit)
    def entry(chain, sell, buy, amount, taker, **kw):
        return real_entry(chain, sell, buy, amount, taker, v3_quote_fn=_v3, v2_quote_fn=_v2,
                          pair_tokens_fn=_pairs, cfg=_Cfg, **kw)
    monkeypatch.setattr(R, "mixed_entry_quote", entry)
    ladder, _ = _ladder(monkeypatch)
    ladder.fund_overrides_fn = overrides
    start = time.monotonic()
    assert ladder(WBNB, COIN, CLIP) is not None
    assert len(seen) == 1 and seen[0] is not None
    assert start < seen[0] <= time.monotonic() + 0.5
