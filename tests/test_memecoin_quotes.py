"""Task 1 (runtime) — quote-only modeled V6 paper fills. Every fill is quote evidence; nothing here
may sign, approve, broadcast, reserve, or load a funded key. Tests inject the quote seams so no
network is touched.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from sniper.analysis.memecoin_quote_failures import QuoteFailure as TypedQuoteFailure
from sniper.services import memecoin_quotes as q
from sniper.services.memecoin_quotes import (
    IdentityInvalid, ModeledLiveFill, QuoteFailure, QuoteStale, QuoteStatus, QuoteUnavailable,
    quote_entry, quote_exit, quote_exit_outcome,
)
from sniper.services.memecoin_v6_engine import fingerprint

FIXED_NOW = "2026-07-26T12:00:00+00:00"


@pytest.mark.parametrize("status", [
    QuoteStatus.ROUTE_ABSENT, QuoteStatus.PROVIDER_UNAVAILABLE, QuoteStatus.STALE,
])
def test_quote_failure_has_stable_semantic_identity(status):
    first = QuoteFailure(
        status=status, provider="jupiter", observed_at=FIXED_NOW, detail_code="no_route")
    second = QuoteFailure(
        status=status, provider="jupiter", observed_at=FIXED_NOW, detail_code="no_route")
    assert first.evidence_id == second.evidence_id


def test_provider_failure_identity_does_not_churn_with_poll_time():
    first = QuoteFailure(
        QuoteStatus.PROVIDER_UNAVAILABLE, "jupiter", FIXED_NOW, "rate_limit")
    second = QuoteFailure(
        QuoteStatus.PROVIDER_UNAVAILABLE, "jupiter",
        "2026-07-26T12:00:05+00:00", "rate_limit")
    assert first.evidence_id == second.evidence_id


def test_distinct_route_absence_checks_keep_distinct_identity():
    first = QuoteFailure(QuoteStatus.ROUTE_ABSENT, "jupiter", FIXED_NOW, "no_route")
    second = QuoteFailure(
        QuoteStatus.ROUTE_ABSENT, "jupiter", "2026-07-26T12:00:05+00:00", "no_route")
    assert first.evidence_id != second.evidence_id


def test_4663_exit_attempts_persisted_route_before_broad_discovery():
    calls = []
    position = evm_position(route_binding={
        "protocol": "uniswap_v2",
        "router": "0x" + "12" * 20,
        "tokens": ["0x" + "ab" * 20, "0x" + "cd" * 20],
        "market_pool": "0x" + "ef" * 20,
    })

    fill = quote_exit(
        position, 100, now_fn=_fixed_now,
        evm_bound_quote_fn=lambda *a, **k: calls.append("bound") or {
            "amount_out": 10, "route": dict(position["route_binding"]),
        },
        evm_quote_fn=lambda *a, **k: pytest.fail("broad discovery called"),
    )

    assert calls == ["bound"]
    assert fill.quote_out_atomic == 10


def test_provider_timeout_is_not_route_absent():
    outcome = quote_exit_outcome(
        sol_position(), 100, observed_at=FIXED_NOW,
        adapter=lambda *_: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    assert isinstance(outcome, QuoteFailure)
    assert outcome.status is QuoteStatus.PROVIDER_UNAVAILABLE


@pytest.mark.parametrize("message", [
    "solana exit quote unavailable",
    "4663 exit route quote unavailable",
    "jupiter route binding missing",
])
def test_runtime_route_failures_are_typed_route_absent(message):
    def unavailable(*_args, **_kwargs):
        raise QuoteUnavailable(message, failure=TypedQuoteFailure.NO_ROUTE)

    outcome = quote_exit_outcome(
        {"network_id": "solana", "mint": "M"}, 1,
        adapter=unavailable, observed_at=FIXED_NOW)
    assert outcome.status is QuoteStatus.ROUTE_ABSENT
    assert outcome.failure_class == TypedQuoteFailure.NO_ROUTE.value


def test_default_jupiter_rate_limit_is_provider_unavailable_not_route_absent():
    outcome = quote_exit_outcome(
        sol_position(), 100, observed_at=FIXED_NOW,
        http=lambda *a, **k: (429, {"error": "rate limit"}, None),
    )
    assert isinstance(outcome, QuoteFailure)
    assert outcome.status is QuoteStatus.PROVIDER_UNAVAILABLE
    assert outcome.failure_class == TypedQuoteFailure.RATE_LIMIT.value


def test_default_4663_rpc_outage_is_provider_unavailable_not_route_absent():
    outcome = quote_exit_outcome(
        evm_position(), 100, observed_at=FIXED_NOW,
        evm_quote_fn=lambda *a, **k: None, evm_rpc_url="https://read-rpc",
        http=lambda *a, **k: (503, {}, None),
    )
    assert isinstance(outcome, QuoteFailure)
    assert outcome.status is QuoteStatus.PROVIDER_UNAVAILABLE
    assert outcome.failure_class == TypedQuoteFailure.RPC_TRANSPORT.value


def test_default_4663_healthy_provider_with_no_route_is_route_absent():
    outcome = quote_exit_outcome(
        evm_position(), 100, observed_at=FIXED_NOW,
        evm_quote_fn=lambda *a, **k: None, evm_rpc_url="https://read-rpc",
        http=lambda *a, **k: (200, {"result": "0x123"}, None),
    )
    assert isinstance(outcome, QuoteFailure)
    assert outcome.status is QuoteStatus.ROUTE_ABSENT
    assert outcome.failure_class == TypedQuoteFailure.NO_ROUTE.value


def test_cached_modeled_fill_becomes_stale_against_current_observation_time():
    cached = quote_exit(
        sol_position(), 100, now_fn=lambda: FIXED_NOW,
        jupiter_exit_fn=lambda mint, amount, pct, **kw: sol_exit_quote(amount),
    )

    outcome = quote_exit_outcome(
        sol_position(), 100, observed_at="2026-07-26T12:00:11+00:00",
        adapter=lambda *_: cached,
    )

    assert isinstance(outcome, QuoteFailure)
    assert outcome.status is QuoteStatus.STALE


def sol_signal():
    return SimpleNamespace(network_id="solana", mint="MintSoLxxxx", ticker="PEP")


def evm_signal():
    return SimpleNamespace(network_id="4663", mint="0x" + "ab" * 20, ticker="ROB")


def sol_position(**over):
    base = {"network_id": "solana", "mint": "MintSoLxxxx", "token_decimals": 6,
            "net_pnl_computable": True}
    base.update(over)
    return base


def evm_position(**over):
    base = {"network_id": "4663", "mint": "0x" + "ab" * 20, "token_decimals": 18,
            "net_pnl_computable": True}
    base.update(over)
    return base


def sol_entry_quote(in_usdc=25_000_000, out_tokens=600_000_000):
    return {"inAmount": str(in_usdc), "outAmount": str(out_tokens),
            "routePlan": [{"swapInfo": {"label": "Raydium", "ammKey": "RayPool"}}],
            "slippageBps": 300,
            "priceImpactPct": "0.012"}


def sol_exit_quote(amount):
    # selling `amount` tokens -> USDC out
    return {"inAmount": str(amount), "outAmount": str(amount // 20),
            "routePlan": [{"swapInfo": {"label": "Orca", "ammKey": "OrcaPool"}}],
            "slippageBps": 300,
            "priceImpactPct": "0.01"}


def evm_amount_out(chain, sell_token, buy_token, amount_atomic, **kw):
    return {"amount_out": 987_000_000, "route": {
        "protocol": "uniswap_v4", "pool_fee": 3000,
        "pool_key": {"currency0": sell_token, "currency1": buy_token, "fee": 3000},
        "zero_for_one": True,
    }}


def _fixed_now():
    return FIXED_NOW


# ---- the plan's Step 1 tests -----------------------------------------------------------------

def test_entry_has_no_price_fallback():
    with pytest.raises(QuoteUnavailable):
        quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                    jupiter_entry_fn=lambda *a, **k: None,
                    token_decimals_fn=lambda *a, **k: 6)


def test_solana_quote_without_exact_amm_binding_is_rejected():
    raw = sol_entry_quote()
    del raw["routePlan"][0]["swapInfo"]["ammKey"]
    with pytest.raises(QuoteUnavailable, match="ammKey"):
        quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                    jupiter_entry_fn=lambda *a, **k: raw,
                    token_decimals_fn=lambda *a, **k: 6)


def test_exit_quotes_exact_requested_atomic_quantity():
    seen = []
    fill = quote_exit(sol_position(), 412_345, now_fn=_fixed_now,
                      jupiter_exit_fn=lambda mint, amount, pct, **kw:
                          seen.append(amount) or sol_exit_quote(amount))
    assert seen == [412_345]
    assert fill.token_qty_atomic == 412_345
    assert fill.quote_in_atomic == 412_345          # sell side = exact bag tokens


def test_4663_quote_never_loads_account_or_builds_transaction(monkeypatch):
    monkeypatch.setattr("sniper.execution.evm_wallet.load_account",
                        lambda *a, **k: pytest.fail("account loaded"), raising=False)
    fill = quote_entry(evm_signal(), Decimal("25"), now_fn=_fixed_now,
                       evm_quote_fn=evm_amount_out,
                       usd_to_quote_fn=lambda *a, **k: 10_000_000_000_000_000,  # 0.01 WETH atomic
                       erc20_decimals_fn=lambda *a, **k: 18)
    assert "calldata" not in fill.quote_payload
    assert "transaction" not in fill.quote_payload
    assert "permit" not in fill.quote_payload
    assert fill.network_id == "4663"
    assert fill.quote_in_asset == "WETH" and fill.quote_in_decimals == 18


# ---- additional exactness / safety cases -----------------------------------------------------

def test_fill_label_is_modeled_live_quote():
    fill = quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                       jupiter_entry_fn=lambda *a, **k: sol_entry_quote(),
                       token_decimals_fn=lambda *a, **k: 6)
    assert fill.fill_kind == "modeled_live_quote"
    assert isinstance(fill, ModeledLiveFill)


def test_entry_records_aware_timestamp_and_route_binding():
    fill = quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                       jupiter_entry_fn=lambda *a, **k: sol_entry_quote(),
                       token_decimals_fn=lambda *a, **k: 6)
    from datetime import datetime
    parsed = datetime.fromisoformat(fill.observed_at)
    assert parsed.tzinfo is not None and parsed.utcoffset() is not None
    assert isinstance(fill.route_binding, dict) and fill.route_binding    # non-empty


def test_fingerprint_matches_payload_and_is_stable():
    a = quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                    jupiter_entry_fn=lambda *a, **k: sol_entry_quote(),
                    token_decimals_fn=lambda *a, **k: 6)
    b = quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                    jupiter_entry_fn=lambda *a, **k: sol_entry_quote(),
                    token_decimals_fn=lambda *a, **k: 6)
    assert a.quote_fingerprint == fingerprint(a.quote_payload)
    assert a == b                                    # deterministic under fixed clock


def test_solana_entry_sets_token_side_and_positive_atomics():
    fill = quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                       jupiter_entry_fn=lambda *a, **k: sol_entry_quote(in_usdc=25_000_000,
                                                                        out_tokens=600_000_000),
                       token_decimals_fn=lambda *a, **k: 6)
    assert fill.side == "buy"
    assert fill.quote_in_asset == "USDC" and fill.quote_in_decimals == 6
    assert fill.quote_in_atomic == 25_000_000
    assert fill.token_qty_atomic == 600_000_000 == fill.quote_out_atomic
    assert fill.quote_out_asset == "MintSoLxxxx" and fill.quote_out_decimals == 6
    assert Decimal(fill.unit_price) > 0


def test_unresolved_decimals_is_quote_unavailable():
    with pytest.raises(QuoteUnavailable):
        quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                    jupiter_entry_fn=lambda *a, **k: sol_entry_quote(),
                    token_decimals_fn=lambda *a, **k: None)


def test_non_positive_output_is_quote_unavailable():
    with pytest.raises(QuoteUnavailable):
        quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                    jupiter_entry_fn=lambda *a, **k: sol_entry_quote(out_tokens=0),
                    token_decimals_fn=lambda *a, **k: 6)


def test_missing_evm_route_is_quote_unavailable():
    with pytest.raises(QuoteUnavailable):
        quote_entry(evm_signal(), Decimal("25"), now_fn=_fixed_now,
                    evm_quote_fn=lambda *a, **k: None,
                    usd_to_quote_fn=lambda *a, **k: 10_000_000_000_000_000,
                    erc20_decimals_fn=lambda *a, **k: 18)


def test_evm_usd_conversion_failure_is_quote_unavailable():
    with pytest.raises(QuoteUnavailable):
        quote_entry(evm_signal(), Decimal("25"), now_fn=_fixed_now,
                    evm_quote_fn=evm_amount_out,
                    usd_to_quote_fn=lambda *a, **k: None,
                    erc20_decimals_fn=lambda *a, **k: 18)


def test_invalid_evm_identity_is_rejected_before_any_quote_call():
    signal = SimpleNamespace(network_id="4663", mint="not-an-address", ticker="BAD")
    with pytest.raises(IdentityInvalid):
        quote_entry(signal, Decimal("25"), now_fn=_fixed_now,
                    evm_quote_fn=lambda *a, **k: pytest.fail("quoted invalid identity"),
                    usd_to_quote_fn=lambda *a, **k: pytest.fail("converted invalid identity"))


@pytest.mark.parametrize("notional", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_non_positive_or_non_finite_notional_is_rejected(notional):
    with pytest.raises(IdentityInvalid):
        quote_entry(sol_signal(), notional, now_fn=_fixed_now,
                    jupiter_entry_fn=lambda *a, **k: pytest.fail("quoted invalid notional"))


def test_stale_quote_is_rejected():
    ticks = iter(["2026-07-26T12:00:00+00:00", "2026-07-26T12:01:00+00:00"])  # 60s elapsed
    with pytest.raises(QuoteStale):
        quote_entry(sol_signal(), Decimal("25"), now_fn=lambda: next(ticks), max_age_s=10.0,
                    jupiter_entry_fn=lambda *a, **k: sol_entry_quote(),
                    token_decimals_fn=lambda *a, **k: 6)


def test_successful_fill_persists_measured_quote_age():
    ticks = iter(["2026-07-26T12:00:00+00:00", "2026-07-26T12:00:02+00:00"])
    fill = quote_entry(sol_signal(), Decimal("25"), now_fn=lambda: next(ticks), max_age_s=10.0,
                       jupiter_entry_fn=lambda *a, **k: sol_entry_quote(),
                       token_decimals_fn=lambda *a, **k: 6)
    assert fill.quote_age_s == 2.0


def test_exit_4663_sells_exact_quantity_to_weth():
    seen = []
    fill = quote_exit(evm_position(), 5_000_000_000, now_fn=_fixed_now,
                      evm_quote_fn=lambda chain, sell_token, buy_token, amount_atomic, **kw:
                          seen.append(amount_atomic) or {"amount_out": 4_000,
                                                         "route": {
                                                             "protocol": "uniswap_v2",
                                                             "router": "0x" + "11" * 20,
                                                             "tokens": [sell_token, buy_token],
                                                         }})
    assert seen == [5_000_000_000]
    assert fill.side == "sell"
    assert fill.token_qty_atomic == 5_000_000_000 == fill.quote_in_atomic
    assert fill.quote_out_asset == "WETH" and fill.quote_out_atomic == 4_000


def test_unknown_network_fees_make_net_pnl_uncomputable():
    sol = quote_entry(sol_signal(), Decimal("25"), now_fn=_fixed_now,
                      jupiter_entry_fn=lambda *a, **k: sol_entry_quote(),
                      token_decimals_fn=lambda *a, **k: 6)
    evm = quote_entry(evm_signal(), Decimal("25"), now_fn=_fixed_now,
                      evm_quote_fn=evm_amount_out,
                      usd_to_quote_fn=lambda *a, **k: 10_000_000_000_000_000,
                      erc20_decimals_fn=lambda *a, **k: 18)
    assert sol.net_pnl_computable is False and sol.costs["network_fee_quote_atomic"] is None
    assert evm.net_pnl_computable is False and evm.costs["network_fee_quote_atomic"] is None


def test_v4_best_route_preserves_complete_pool_binding(monkeypatch):
    pool_key = {"currency0": "0x0", "currency1": "0x1", "fee": 3000,
                "tickSpacing": 60, "hooks": "0x2"}
    monkeypatch.setattr(q, "v4_quote_amount_out",
                        lambda *a, **k: {"amount_out": 123, "pool_key": pool_key,
                                        "pool_id": "0xpoolid",
                                        "zero_for_one": True, "pool_fee": 3000})
    monkeypatch.setattr(q, "dex_v3_quote", lambda *a, **k: None)
    monkeypatch.setattr(q, "dex_v2_exit_quote", lambda *a, **k: None)
    result = q._evm_best_amount_out("4663", "0x" + "aa" * 20, "0x" + "bb" * 20,
                                    100, taker=q.PAPER_QUOTE_TAKER, rpc_url="x",
                                    http=None, gt_http=None)
    assert result["route"]["pool_key"] == pool_key
    assert result["route"]["pool_id"] == "0xpoolid"
    assert result["route"]["zero_for_one"] is True


def test_v6_v3_and_v2_quotes_receive_complete_isolated_route_config(monkeypatch):
    seen = []
    monkeypatch.setattr(q, "v4_quote_amount_out", lambda *a, **k: None)

    def capture(_chain, _sell, _buy, _amount, _taker, **kwargs):
        seen.append(kwargs)
        return None

    monkeypatch.setattr(q, "dex_v3_quote", capture)
    monkeypatch.setattr(q, "dex_v2_exit_quote", capture)
    assert q._evm_best_amount_out(
        "4663", "0x" + "aa" * 20, "0x" + "bb" * 20, 100,
        taker=q.PAPER_QUOTE_TAKER, rpc_url="https://read-only", http=None, gt_http=None) is None
    assert {"cfg_addr", "fee_tiers", "connectors", "slippage_bps", "call_fn"} <= seen[0].keys()
    assert {"cfg_router", "connector_tokens", "slippage_bps", "tax_buffer_bps",
            "call_fn"} <= seen[1].keys()


def test_v3_route_binding_resolves_every_factory_pool_and_market_pool():
    token = "0x" + "aa" * 20
    connector = "0x" + "bb" * 20
    weth = "0x" + "cc" * 20
    pools = iter(("0x" + "11" * 20, "0x" + "22" * 20))
    calls = []

    def call(to, data):
        calls.append((to, data))
        return "0x" + "00" * 12 + next(pools)[2:]

    route = q._bind_evm_route_pools(
        {"protocol": "uniswap_v3", "tokens": [weth, connector, token],
         "fees": [500, 10_000]},
        market_token=token, call_fn=call,
        factories={"v3_factory": "0x" + "33" * 20},
    )

    assert route["pools"] == ["0x" + "11" * 20, "0x" + "22" * 20]
    assert route["market_pool"] == "0x" + "22" * 20
    assert all(to == "0x" + "33" * 20 for to, _data in calls)
    assert [data[:10] for _to, data in calls] == ["0x1698ee82", "0x1698ee82"]


def test_v2_route_binding_fails_closed_when_any_pair_is_unresolved():
    route = q._bind_evm_route_pools(
        {"protocol": "uniswap_v2",
         "tokens": ["0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "cc" * 20],
         "router": "0x" + "44" * 20},
        market_token="0x" + "cc" * 20,
        call_fn=lambda _to, _data: "0x" + "00" * 32,
        factories={"v2_factory": "0x" + "55" * 20},
    )

    assert route is None


def test_factory_pool_binding_is_cached_for_repeated_quote_route():
    calls = []
    route = {"protocol": "uniswap_v3",
             "tokens": ["0x" + "dd" * 20, "0x" + "ee" * 20], "fees": [3000]}
    options = {
        "market_token": "0x" + "ee" * 20,
        "call_fn": lambda to, data: calls.append((to, data)) or (
            "0x" + "00" * 12 + "66" * 20),
        "factories": {"v3_factory": "0x" + "77" * 20},
    }

    first = q._bind_evm_route_pools(route, **options)
    second = q._bind_evm_route_pools(route, **options)

    assert first == second
    assert len(calls) == 1


# ---- Step 6: source + behavioral funded-surface guards ---------------------------------------

def test_module_source_has_no_funded_verbs():
    import inspect
    src = inspect.getsource(q)
    for forbidden in ("load_account", "private_key", "approve", "sign_",
                      "send_raw_transaction", "broadcast", "reserve_entry", "open_real_position"):
        assert forbidden not in src, f"forbidden funded verb in source: {forbidden}"


def test_quote_never_calls_funded_surfaces(monkeypatch):
    import sniper.execution.evm_wallet as wallet
    for name in ("load_account",):
        monkeypatch.setattr(wallet, name, lambda *a, **k: pytest.fail(f"{name} called"),
                            raising=False)
    # a normal quote still succeeds without any funded surface
    fill = quote_exit(sol_position(), 1_000, now_fn=_fixed_now,
                      jupiter_exit_fn=lambda mint, amount, pct, **kw: sol_exit_quote(amount))
    assert fill.fill_kind == "modeled_live_quote"


# --- route-floor-v1 -----------------------------------------------------------
# Both legs score the slippage floor, or neither does. Scoring the floor on one leg
# and the expected amount on the other manufactures a spread that does not exist.

_FLOOR_RAW = {
    "inAmount": "25000000",
    "outAmount": "1000",
    "otherAmountThreshold": "940",
    "slippageBps": 300,
    "routePlan": [{"swapInfo": {"ammKey": "Amm1111111111111111111111111111111111111111",
                                "inputMint": "So11111111111111111111111111111111111111112",
                                "outputMint": "Mint1111111111111111111111111111111111111111"}}],
}


def _entry(fill_model_version, raw=None):
    return q.quote_entry(
        SimpleNamespace(network_id="solana", mint="Mint1111111111111111111111111111111111111111"),
        25,
        fill_model_version=fill_model_version,
        jupiter_entry_fn=lambda *a, **k: dict(raw or _FLOOR_RAW),
        token_decimals_fn=lambda *a, **k: 6,
        now_fn=lambda: "2026-08-05T00:00:00Z",
    )


def _exit(fill_model_version, raw=None):
    return q.quote_exit(
        {"network_id": "solana", "mint": "Mint1111111111111111111111111111111111111111",
         "token_decimals": 6},
        1_000,
        fill_model_version=fill_model_version,
        jupiter_exit_fn=lambda *a, **k: dict(raw or _FLOOR_RAW),
        erc20_decimals_fn=lambda *a, **k: 6,
        now_fn=lambda: "2026-08-05T00:00:00Z",
    )


def test_route_floor_v1_scores_the_threshold_on_the_entry_leg():
    fill = _entry(q.ROUTE_FLOOR_FILL_MODEL)
    assert int(fill.token_qty_atomic) == 940
    assert fill.quote_payload["expected_out_atomic"] == 1000
    assert fill.quote_payload["fill_model_version"] == "route-floor-v1"


def test_route_floor_v1_scores_the_threshold_on_the_exit_leg():
    fill = _exit(q.ROUTE_FLOOR_FILL_MODEL)
    assert int(fill.quote_out_atomic) == 940
    assert fill.quote_payload["expected_out_atomic"] == 1000


def test_legacy_fill_model_still_scores_the_expected_amount():
    # Existing fomo-source positions must keep meaning exactly what they meant.
    assert int(_entry(q.LEGACY_FILL_MODEL).token_qty_atomic) == 1000
    assert int(_exit(q.LEGACY_FILL_MODEL).quote_out_atomic) == 1000


def test_route_floor_v1_requires_the_threshold():
    raw = {k: v for k, v in _FLOOR_RAW.items() if k != "otherAmountThreshold"}
    with pytest.raises(q.QuoteUnavailable, match="otherAmountThreshold"):
        _entry(q.ROUTE_FLOOR_FILL_MODEL, raw=raw)
    with pytest.raises(q.QuoteUnavailable, match="otherAmountThreshold"):
        _exit(q.ROUTE_FLOOR_FILL_MODEL, raw=raw)


def test_legacy_model_tolerates_a_missing_threshold():
    raw = {k: v for k, v in _FLOOR_RAW.items() if k != "otherAmountThreshold"}
    assert int(_entry(q.LEGACY_FILL_MODEL, raw=raw).token_qty_atomic) == 1000
