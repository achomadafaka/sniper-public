"""The copy cap measures marginal market movement, never either trader's ticket average."""
import pytest

from sniper.execution import entry_attribution as attribution


@pytest.mark.parametrize("current, state", [((270, 100), "pass"), ((271, 100), "refuse"),
                                            ((150, 100), "pass")])
def test_exact_market_boundary_after_source_buy(current, state):
    # Source moved $1 -> $2. Our size/fees/slippage are deliberately not inputs.
    result = attribution.market_movement_check(
        source_reserves=(200, 100), current_reserves=current, cap_pct=35)
    assert result["state"] == state
    assert result["basis"] == "intervening_market"
    assert result["movement_pct"] == pytest.approx((current[0] / 200 - 1) * 100)


@pytest.mark.parametrize("bad", [None, (0, 1), (1, 0), (-1, 2), (True, 1), (1.0, 2),
                                 (1,), (1, 2, 3)])
def test_unknown_is_explicit_and_never_uses_average_or_quote(bad):
    assert attribution.market_movement_check(
        source_reserves=bad, current_reserves=(2, 1), cap_pct=35)["state"] == "unknown"


def test_shared_quote_does_not_tighten_slippage_against_a_source_average(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.execution import solana_swap_executor as ex
    from test_memecoin_entry_chase_quote_recheck import _c, _Deps, _run
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35)
    conn, pid = _c(source_price=.001)
    deps = _Deps()
    built = []
    original = deps.sign_fn
    deps.sign_fn = lambda quote: (built.append(dict(quote)), original(quote))[1]
    result = _run(conn, pid, deps, chase_max_pct=35)
    assert result["status"] == "filled", result
    assert built[0]["otherAmountThreshold"] == "470000000"
    assert deps.calls.count("quote") == 1
    conn.close()


def test_display_chase_cannot_trigger_an_allowance_rpc_in_movement_mode(monkeypatch):
    from types import SimpleNamespace
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    event = SimpleNamespace(their_price=1, chase_reference_price=2)
    assert runner.chase_ok(event, our_price=100, max_pct=35)
    assert runner.chase_impact_allowance(
        event, network_id="solana", quote_fn=lambda *_: pytest.fail("no allowance call")) == (
            None, "intervening_market")
