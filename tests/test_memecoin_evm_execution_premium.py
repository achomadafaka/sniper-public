"""The execution premium cap on the FUNDED EVM leg (2026-08-30).

`execution_premium_decision` existed since 2026-08-23 but was reachable only from
`solana_swap_executor`. Every funded EVM entry paid whatever the route asked. These tests pin the
call site, its fail-CLOSED asymmetry with Solana, and the unit identity the comparison depends on.

The premium is the half that predicts the loss: realized return over 221 filled closes runs +1.07%
below 1.15x and -52.14% at 22% wins above 2.0x (n=18).
"""
import json

import pytest

from sniper.execution import evm_swap_executor as X


def _contract(display_price):
    """A funded contract carrying the one fact the premium gate reads."""
    return {"funded_contract_json": json.dumps(
        {"classification": {"facts": {"display_price": display_price}}})}


def _premium(monkeypatch, *, display_price, exact_price, source_price=None, cap=1.95):
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", cap, raising=False)
    return X._execution_premium(
        _contract(display_price), exact_price, None, attempt_no=1, revision="initial",
        source_price=source_price)


def test_route_at_twice_the_mid_is_refused(monkeypatch):
    """2.03x against a 1.95x cap: the shape that refused a live solana entry on 2026-08-30."""
    result = _premium(monkeypatch, display_price=1.0, exact_price=2.03)
    refusal = X._premium_refusal(result)
    assert refusal is not None, "a route at 2.03x the mid must not reach signing"
    reason, release_tag = refusal
    assert release_tag == "execution_premium"
    # Verbatim-compatible with the solana string so one alert and one refusal-capture pattern
    # cover both chains.
    assert reason == "route quote is 2.03x the chart mid (cap 1.95x)"


def test_route_inside_the_cap_proceeds(monkeypatch):
    result = _premium(monkeypatch, display_price=1.0, exact_price=1.10)
    assert result.allow is True
    assert X._premium_refusal(result) is None


def test_source_price_lifts_a_stale_chart_reference(monkeypatch):
    result = _premium(
        monkeypatch, display_price=0.00004, source_price=0.00006, exact_price=0.00008)
    assert result.allow is True
    assert result.reference_price == pytest.approx(0.00006)
    assert result.reference_basis == "source"


def test_missing_display_price_falls_through_to_the_remaining_entry_gates(monkeypatch):
    """An exact route with no display mark has no premium verdict.

    The executor keeps the exact-quote, route, impact, and manual-entry gates. It must not turn
    an absent display mark into a premium refusal.
    """
    result = _premium(monkeypatch, display_price=None, exact_price=1.0)
    assert result.allow is True
    assert X._premium_refusal(result) is None


def test_unpriceable_quote_fails_closed(monkeypatch):
    """No exact price means we do not know what we are about to pay."""
    result = _premium(monkeypatch, display_price=1.0, exact_price=None)
    refusal = X._premium_refusal(result)
    assert refusal is not None
    assert refusal[1] == "execution_premium_blind"


def test_cap_off_still_allows(monkeypatch):
    """A cap of 0 is the operator disabling the gate, NOT absent data. It must not fail closed,
    or setting the cap to 0 would halt the EVM lane instead of opening it."""
    result = _premium(monkeypatch, display_price=None, exact_price=None, cap=0)
    assert result.reason == "cap_off"
    assert X._premium_refusal(result) is None


def test_absent_contract_does_not_refuse(monkeypatch):
    """No funded contract (legacy / manual paths) leaves the gate undecided rather than blocking."""
    assert X._execution_premium(None, 1.0, None, attempt_no=1, revision="initial") is None
    assert X._premium_refusal(None) is None


def test_quote_price_is_usd_per_token_not_raw_sell_amount():
    """UNIT IDENTITY -- the precondition the whole comparison rests on.

    On 4663 the sell leg is WETH. `quote_unit_price` must convert it with
    `_sniper_sell_token_usd` before the premium sees it. Comparing a raw WETH amount against a USD
    mid is the 2026-07-30 defect that produced $3.47M of fictional PnL; here it would read as a
    ~2,478x premium and refuse every entry -- loud, but wrong.
    """
    from sniper.execution.entry_attribution import quote_unit_price
    # 0.02 WETH at $2,478/ETH buys 570,280 tokens (18 decimals): the real CryoPalmar quote.
    quote = {
        "sellAmount": str(2 * 10 ** 16),
        "buyAmount": str(570280 * 10 ** 18),
        "_sniper_sell_decimals": 18,
        "_sniper_buy_decimals": 18,
        "_sniper_sell_token_usd": 2478.0,
    }
    price = quote_unit_price(quote)
    # 0.02 * 2478 / 570280 == 8.69e-5 USD per token.
    assert price == pytest.approx(0.02 * 2478.0 / 570280, rel=1e-9)
    # The raw-amount misreading would be ~1e-13 or ~2478x off, not this.
    assert 1e-5 < price < 1e-4


def test_premium_is_evaluated_before_any_wallet_mutation():
    """Ordering, read off the source: the premium call must precede approval, pre-transaction and
    signing. A gate that decides after a mutation has already spent gas is not a gate.
    """
    import inspect
    src = inspect.getsource(X.execute_entry_evm)
    premium_at = src.index("_execution_premium(")
    # Match CALL sites (`name(`), never the parameter list -- the signature names every one of
    # these at index ~140 and would make this assertion trivially false.
    for mutation in ("approve_fn(", "sign_fn(", "send_fn("):
        assert mutation in src, f"{mutation} not called; update this test to the real seam"
        assert premium_at < src.index(mutation), (
            f"{mutation} runs before the execution premium is decided")


def test_the_evm_refusal_names_a_unit_fault_before_the_ratio_branch():
    """`_premium_refusal` checks `ratio is not None` FIRST, and a unit fault CARRIES a ratio.
    Without an earlier branch it prints "route quote is 395149.46x the source price" -- which is
    exactly how VEX was reported. This pins the ordering, not just the string."""
    from sniper.services.memecoin_market_state import ExecutionPremium
    premium = ExecutionPremium(False, 395149.46, 0.005877, 2487.26, 1.75,
                               "quote_unit_fault", 0.00629, "source")
    reason, tag = X._premium_refusal(premium)
    assert tag == "quote_unit_fault", tag
    assert "unusable rather than expensive" in reason
    assert "route quote is" not in reason, "still reporting a broken quote as a route premium"


# --- the chase gate must measure the COIN, not our route (2026-09-02) --------------------------
# `source_drift_pct` is quote/source and our quote carries our own route markup, so the gate
# reported the coin's movement PLUS the markup and the markup dominates by 3-10x. On all eight
# 4663 chase refusals of 2026-09-02 the coin FELL in seven, and every one was refused for chasing
# a pump that never happened. quote/display keeps being checked by `_execution_premium`.

from sniper.services.memecoin_market_state import ExecutionPremium


def _prem(display):
    return ExecutionPremium(True, 1.0, display, 1.0, 1.75, "ok", display, "display")


def test_the_gap_is_the_coin_not_our_markup():
    """FOX, from the tape: they filled at 8.12e-05, market 7.02e-05, our quote 1.19e-04."""
    pct, basis, display = X._chase_gap(_prem(7.02e-05), 8.12e-05,
                                       {"source_drift_pct": 46.9})
    assert basis == "market" and display == 7.02e-05
    assert pct == pytest.approx(-13.5, abs=0.5), "the coin FELL; the gate saw +46.9%"


def test_a_coin_that_really_ran_is_still_caught():
    """The gate must keep doing its actual job — this is not a way to admit everything."""
    pct, basis, _d = X._chase_gap(_prem(1.60), 1.00, {"source_drift_pct": 5.0})
    assert basis == "market" and pct == pytest.approx(60.0)


def test_no_market_price_keeps_the_OLD_contaminated_measure():
    """display_price is absent on ~43% of 4663 attempts and the premium gate abstains on exactly
    those rows. Failing open there would remove BOTH gates from half the book."""
    pct, basis, display = X._chase_gap(_prem(None), 8.12e-05, {"source_drift_pct": 46.9})
    assert basis == "quote_fallback" and display is None
    assert pct == 46.9, "must fall back to the old number, not to None"


def test_no_source_price_also_falls_back():
    pct, basis, _d = X._chase_gap(_prem(1.0), None, {"source_drift_pct": 12.0})
    assert basis == "quote_fallback" and pct == 12.0


def test_a_nonsense_price_is_not_trusted_as_a_market():
    """positive_price rejects zero/negative/NaN, so a broken feed falls back rather than dividing."""
    for bad in (0.0, -1.0, float("nan")):
        _pct, basis, _d = X._chase_gap(_prem(bad), 1.0, {"source_drift_pct": 9.0})
        assert basis == "quote_fallback", f"{bad!r} was treated as a market price"


def test_absent_premium_object_does_not_raise():
    """`_execution_premium` returns None when there is no funded contract."""
    pct, basis, _d = X._chase_gap(None, 1.0, {"source_drift_pct": 3.0})
    assert basis == "quote_fallback" and pct == 3.0


def test_the_refusal_names_which_basis_decided_it():
    """Two very different numbers now share one gate; a refusal that does not say which one
    decided is how they get conflated again."""
    import inspect
    src = inspect.getsource(X.execute_entry_evm)
    assert "basis %s" in src, "the chase refusal no longer reports its basis"


# ── our quote vs the source's price, per chain (2026-09-13, TWIN 57097) ──

def test_the_quote_over_source_cap_refuses_what_the_market_basis_passes(monkeypatch):
    """TWIN: chart 8.17e-05 (stale) vs source 1.0388e-04 read -21% on the market basis and passed;
    our quote was 1.5086e-04, +45% over the source."""
    from sniper.execution.evm_swap_executor import _quote_over_source_refusal
    monkeypatch.setenv("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663", "30")
    reason = _quote_over_source_refusal("4663", 1.5086053574424197e-04, 1.0388481993756203e-04)
    assert reason and "+45%" in reason and "cap 30%" in reason
    assert _quote_over_source_refusal("4663", 1.30e-04, 1.0388481993756203e-04) is None   # +25%
    assert _quote_over_source_refusal("56", 1.5086e-04, 1.0388e-04) is None, "unset chain: off"
    assert _quote_over_source_refusal("4663", None, 1.0388e-04) is None, "missing price passes"
