"""The flap.sh curve rung inside the real entry/exit ladder (evm_real_seams.py).

Mirrors tests/test_memecoin_evm_launchpad_curve_ladder.py. Locks the property Gamma named after
4dea7ba9 passed review with a gap in it: the LADDER'S OWN exit result must carry
quote_quality == "exact" or be absent -- never a non-exact quote reaching preview pricing.
Testing the module function in isolation is exactly what let that gap through review before;
this tests the boundary the real caller (preview_exit_fn, fastlane) actually uses.
"""
import sniper.execution.evm_flap_curve as curve_mod
from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback

PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
YEE = "0x8d411a22bf0194b2f5ef665f8a2c7a9430ee7777"
NATIVE = "0x" + "00" * 20
WETH = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TAKER = "0xtaker"


def _state(**overrides):
    reserve = 304288643025442896
    r = 6140000000000000000
    h = 107036752000000000000000000
    k = 6797205657280000000000000000
    state = {
        "status": 1, "reserve": reserve, "circulating_supply": 52272443043027083493254115,
        "price": 6109695396, "token_version": 5, "r": r, "h": h, "k": k,
        "dex_supply_thresh": 800000000000000000000000000, "quote_token": NATIVE,
        "native_to_quote_swap_enabled": 0, "extension_id": "0" * 64, "creator_fee_bps": 100,
        "portal": PORTAL, "mint": YEE,
    }
    state.update(overrides)
    state["coin_reserve"] = (state["k"] * 10 ** 18) // (state["reserve"] + state["r"]) - state["h"]
    state["fee_bps"] = curve_mod._PLATFORM_FEE_BPS + state["creator_fee_bps"]
    return state


def _on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", True)


def _stub(monkeypatch, state, *, override_available=True):
    monkeypatch.setattr(
        curve_mod, "curve_state",
        lambda chain, mint, rpc_url, http=None: state if str(mint).lower() == YEE else None)

    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        if str(input_token).lower() == NATIVE:
            return curve_mod.curve_buy_out(state, amount_atomic)
        return curve_mod.curve_sell_out(state, amount_atomic) if overrides is not None else None
    monkeypatch.setattr(curve_mod, "simulate_swap_exact_input", simulate_fn)

    def overrides_fn(mint, spender, amount_atomic, *, taker, rpc_url, http=None):
        return {"stand-in": True} if override_available else None
    monkeypatch.setattr(curve_mod, "discover_sell_overrides", overrides_fn)


def test_ladder_skips_flap_curve_when_the_flag_is_off():
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="56", quote_token=WETH, taker=TAKER,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    assert fn(WETH, YEE, 10 ** 16) is None
    names = [r["rung"] for r in rungs]
    assert "flap_curve" in names
    assert rungs[names.index("flap_curve")]["skipped"] is True


def test_ladder_exit_through_flap_curve_carries_an_exact_quote(monkeypatch):
    _on(monkeypatch)
    state = _state()
    _stub(monkeypatch, state)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="56", quote_token=WETH, taker=TAKER,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    result = fn(YEE, WETH, 10 ** 18)
    assert result is not None
    assert result["route"]["protocol"] == "flap_curve"
    assert result["route"]["quote_quality"] == "exact"
    assert result["route"]["quote_basis"] == "allowance_override"


def test_ladder_exit_through_flap_curve_is_absent_not_partial_when_unquotable(monkeypatch):
    """The property Gamma asked to lock: when the rung cannot produce an exact answer, the
    ladder's own result is None, never a non-exact quote reaching preview pricing."""
    _on(monkeypatch)
    state = _state()
    _stub(monkeypatch, state, override_available=False)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="56", quote_token=WETH, taker=TAKER,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    result = fn(YEE, WETH, 10 ** 18)
    assert result is None
    names = [r["rung"] for r in rungs]
    assert rungs[names.index("flap_curve")]["hit"] is False


def test_ladder_exit_through_flap_curve_refuses_a_graduated_coin_at_real_reserve_zero(monkeypatch):
    """The known, accepted gap (Gamma/Alpha, 2026-09-22): a graduated coin (real reserve 0)
    still cannot exit through this rung even with the ladder wired correctly -- the ladder must
    not paper over it, only fall through to whatever other rung can price the graduated pool."""
    _on(monkeypatch)
    state = _state(reserve=0, circulating_supply=900000000000000000000000000)
    _stub(monkeypatch, state)
    rungs = []
    fn = venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="56", quote_token=WETH, taker=TAKER,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append,
    )
    result = fn(YEE, WETH, 10 ** 18)
    assert result is None
