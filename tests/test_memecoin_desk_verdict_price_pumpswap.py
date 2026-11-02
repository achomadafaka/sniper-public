"""verdict_price_pumpswap: the S6 cap for a graduated (PumpSwap) desk coin from Codex's pool_state.
Any unknown input = None = the leg refuses (never a guessed virtual quote, decimals or fee)."""
import pytest

from sniper.services import memecoin_desk as D
from sniper.services import memecoin_origin as O

POOL = {"base_reserve_atomic": "200000000000000", "quote_reserve_atomic": "85000000000",  # 2e8 tokens (6 dp), 85 SOL
        "quote_mint": D.WSOL_MINT, "base_decimals": 6, "supply_atomic": "1000000000000000",
        "virtual_quote_atomic": "15000000000", "lp_fee_bps": 20, "protocol_fee_bps": 5,
        "creator_fee_bps": 5, "slot": 100, "metadata_known_at": 100.0, "reserve_known_at": 119.0,
        "virtual_quote_known_at": 110.0, "virtual_quote_slot": 99, "virtual_quote_source": "legacy_buy_event",
        "metadata_slot": 90, "cut_slot": 101}


def _vp(**change):
    return D.verdict_price_pumpswap({**POOL, **change}, as_of=120.0, sol_usd=100.0, sol_age_s=5.0, clip_usd=25)


def test_the_pool_cap_prices_reserves_plus_virtual_in_the_mints_decimals():
    vp = _vp()
    depth_usd = (85 + 15) * 100.0
    assert vp["ref_price_usd"] == pytest.approx(depth_usd / 2e8)
    assert vp["fee_pct"] == pytest.approx(0.30) and vp["impact_pct"] == pytest.approx(25 / depth_usd * 100)
    assert vp["cap_pct"] == pytest.approx(3 + 0.30 + 0.25)
    assert vp["mcap_usd"] == pytest.approx(vp["ref_price_usd"] * 1e9) and vp["liq_usd"] == pytest.approx(8500.0)
    assert vp["source"] == "pumpswap:pool_state" and O.validate_verdict_price(vp) == (True, None)


@pytest.mark.parametrize("change", [
    {"virtual_quote_atomic": None}, {"base_decimals": None}, {"lp_fee_bps": None}, {"creator_fee_bps": None},
    {"metadata_known_at": None}, {"supply_atomic": None}, {"slot": None},
    {"quote_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},           # USDC quote: not modelled
    {"base_reserve_atomic": "0"}, {"base_reserve_atomic": "-5"}, {"base_reserve_atomic": "1.5"},
    {"base_decimals": 6.0}, {"lp_fee_bps": -1}, {"lp_fee_bps": True},
    {"metadata_known_at": 125.0}, {"metadata_known_at": 120.0}, {"virtual_quote_known_at": 120.0},                                               # known after the decision
    {"reserve_known_at": None}, {"reserve_known_at": 120.5},                    # no time / a future reserve
    {"reserve_known_at": 120.0 - 61},                                           # reserves older than 60 s
])
def test_any_unknown_or_bad_input_has_no_cap(change):
    assert _vp(**change) is None


@pytest.mark.parametrize("kw", [{"sol_usd": None}, {"sol_usd": 0}, {"clip_usd": 0}, {"as_of": float("nan")},
                                {"sol_age_s": 121}, {"sol_age_s": -1}, {"sol_age_s": None}])
def test_bad_context_has_no_cap(kw):
    args = {"as_of": 120.0, "sol_usd": 100.0, "sol_age_s": 5.0, "clip_usd": 25, **kw}
    assert D.verdict_price_pumpswap(POOL, **args) is None


def test_a_zero_virtual_quote_is_a_known_zero_not_unknown():
    vp = _vp(virtual_quote_atomic="0")
    assert vp["ref_price_usd"] == pytest.approx(85 * 100.0 / 2e8)


def test_reserves_exactly_at_the_age_bound_still_price():
    assert _vp(reserve_known_at=120.0 - 60) is not None and _vp(reserve_known_at=120.0) is None   # strict cut



@pytest.mark.parametrize("change", [
    {"slot": "bad"}, {"slot": 0}, {"slot": True}, {"slot": 1.5},
    {"lp_fee_bps": 9999, "protocol_fee_bps": 1},                               # aggregate fee >= 100 %
    {"lp_fee_bps": 10_000}, {"quote_reserve_atomic": "9" * 400}, {"quote_reserve_atomic": str(2 ** 64)},
    {"base_reserve_atomic": "0012"}, {"base_reserve_atomic": " 12"}, {"base_reserve_atomic": "1e9"},
    {"metadata_known_at": -1.0}, {"metadata_slot": 101}, {"metadata_slot": "x"},
])
def test_codex_trust_boundary_cases_have_no_cap_and_never_raise(change):
    assert _vp(**change) is None


def test_an_absent_virtual_quote_is_unknown_not_zero():
    ps = dict(POOL)
    del ps["virtual_quote_atomic"]
    assert D.verdict_price_pumpswap(ps, as_of=120.0, sol_usd=100.0, sol_age_s=5.0, clip_usd=25) is None


def test_a_bad_config_value_is_no_cap_not_a_crash(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_VERDICT_TOLERANCE_PCT", "x")
    assert _vp() is None


def _ps_candidate(**pool):
    return {"venue": "pumpswap", "snapshot": {"emission_at": 120.0, "fields": {"pool_state": {
        "value": {**POOL, "sol_usd": 100.0, "sol_age_s": 5.0, **pool}, "source": "ps", "known_at": 120.0,
        "slot": 100, "completeness": "complete"}}}}


def test_the_shared_boundary_prices_a_pumpswap_candidate_from_its_pool_state():
    vp = D.priced_verdict(_ps_candidate(), clip_usd=25, basis="asof")
    assert vp == D.verdict_price_pumpswap(POOL, as_of=120.0, sol_usd=100.0, sol_age_s=5.0, clip_usd=25)


def test_an_incomplete_pool_state_or_a_curve_price_never_prices_pumpswap():
    c = _ps_candidate()
    c["snapshot"]["fields"]["pool_state"]["completeness"] = "partial"
    c["snapshot"]["fields"]["mcap_usd"] = {"value": 41000.0, "source": "x:curve_reserves", "completeness": "complete"}
    c["snapshot"]["fields"]["liq_usd"] = {"value": 5000.0, "source": "x", "completeness": "complete"}
    assert D.priced_verdict(c, clip_usd=25, basis="asof") is None



@pytest.mark.parametrize("change", [{"virtual_quote_known_at": None}, {"virtual_quote_known_at": 121.0},
                                    {"virtual_quote_known_at": 120.0 - 61}, {"virtual_quote_slot": 0},
                                    {"virtual_quote_slot": "1"}, {"virtual_quote_slot": None},
                                    {"virtual_quote_source": ""}, {"virtual_quote_source": None}])
def test_the_virtual_quote_needs_its_own_fresh_proof_time(change):
    assert _vp(**change) is None



@pytest.mark.parametrize("pool, reason", [
    (None, "unmapped_pool"), ({"metadata_known_at": None}, "metadata_not_yet_known"),
    ({"metadata_known_at": 130.0}, "metadata_not_yet_known"), ({"reserve_known_at": 30.0}, "reserve_stale"),
    ({"sol_age_s": 500.0}, "sol_stale"), ({"lp_fee_bps": None}, "fee_unknown"),
    ({"virtual_quote_atomic": None}, "virtual_quote_unproven"), ({"virtual_quote_known_at": 10.0}, "virtual_quote_unproven"),
    ({"slot": "bad"}, "pool_state_malformed"), ({}, None), ("incomplete", "pool_state_incomplete")])
def test_an_unpriceable_candidate_names_one_typed_censor_reason(pool, reason):
    c = _ps_candidate(**(pool if isinstance(pool, dict) else {}))
    if pool == "incomplete":
        c["snapshot"]["fields"]["pool_state"]["completeness"] = "partial"
    if pool is None:
        del c["snapshot"]["fields"]["pool_state"]
    assert D.pumpswap_unpriced_reason(c) == reason
    assert (D.priced_verdict(c, clip_usd=25, basis="asof") is None) == (reason is not None)



@pytest.mark.parametrize("change", [{"virtual_quote_slot": 101}, {"virtual_quote_source": " legacy"},
                                    {"virtual_quote_source": "free text!"}, {"metadata_slot": None},
                                    {"metadata_slot": 0}, {"metadata_slot": 101}, {"cut_slot": 100},
                                    {"cut_slot": None}])
def test_codex_second_pass_boundary_cases(change):
    assert _vp(**change) is None


def test_metadata_read_after_the_reserve_swap_but_before_the_cut_still_prices():
    assert _vp(metadata_slot=100) is not None



@pytest.mark.parametrize("change", [{"metadata_known_at": 10 ** 400}, {"reserve_known_at": 10 ** 400},
                                    {"virtual_quote_known_at": 10 ** 400}, {"sol_age_s": 10 ** 400},
                                    {"sol_usd": 10 ** 400}])
def test_a_giant_int_is_a_typed_refusal_never_an_overflow(change):
    c = _ps_candidate(**change)
    assert D.priced_verdict(c, clip_usd=25, basis="asof") is None
    assert isinstance(D.pumpswap_unpriced_reason(c), str)


def test_the_metadata_validator_refuses_a_giant_int():
    from test_memecoin_desk_origin import VERDICT_PRICE
    assert O.validate_verdict_price({**VERDICT_PRICE, "liq_usd": 10 ** 400})[0] is False
