"""Desk intake step 1 (spec 2026-10-27-desk-intake-candidate-path-design.md sections 2-3, operator bounds brain
03c884f4): a PumpSwap candidate carries its intake origin; the intake-v1 hard filter has no market-cap band, only
safety bounds, and computes mcap from the mint account's own supply."""
import copy

import pytest

from sniper.services import memecoin_desk_contract as K
from test_memecoin_desk_contract import AT, evidence, policy, rehash
from test_memecoin_desk_pumpswap_contract import ps_candidate

INTAKE = {"feed": "pumpfun", "filter": "active", "provider_rank": 3, "rank_kind": "list_position",
          "observed_at": 1793099987.7, "host_received_at": 1793099987.75, "clock_offset_s": 0.05,
          "intake_row_ref": "pumpfun:12:2"}
RULE = {"version": "intake-v1", "mcap_safety": [5_000, 50_000_000], "liq_min_quote_sol": 40}


def intake_candidate(quote_sol=200, base_atomic=2 * 10**14, supply="1000000000000000", sol_usd=150.0, **intake):
    c = ps_candidate()
    state = c["snapshot"]["fields"]["pool_state"]["value"]
    state.update(quote_reserve_atomic=str(int(quote_sol * 10**9)), base_reserve_atomic=str(base_atomic),
                 supply_atomic=supply, sol_usd=sol_usd)
    c["intake"] = {**INTAKE, **intake}
    return rehash(c)


def ev(c, supply="1000000000000000"):
    e = evidence(c)
    e["checks"]["token_safety"].update(supply_atomic=supply, decimals=6)
    return e


def pol(**rule):
    return {**policy(), "intake": {**RULE, **rule}}


def decide(c, e=None, p=None):
    return K.hard_filter(c, evidence=ev(c) if e is None else e, policy=pol() if p is None else p, evaluated_at=AT)


def test_an_intake_candidate_validates_and_keeps_its_origin():
    c = K.validate_candidate(intake_candidate())
    assert c["intake"]["feed"] == "pumpfun" and c["intake"]["filter"] == "active"


@pytest.mark.parametrize("bad", [{"feed": "gmgn"}, {"filter": "Bad Name"}, {"provider_rank": 0},
                                 {"rank_kind": "score"}, {"observed_at": None}, {"intake_row_ref": ""}])
def test_a_malformed_intake_origin_is_refused(bad):
    with pytest.raises(ValueError, match="candidate_intake"):
        K.validate_candidate(intake_candidate(**bad))


def test_inside_the_bounds_passes_without_any_band():
    assert decide(intake_candidate()) ["reason"] == "pass"        # mcap 150k, liq 30k: no 30k-100k band applies
    assert decide(intake_candidate(base_atomic=10**12))["reason"] == "pass"   # mcap 30M: far above any old band


def test_mcap_uses_the_mint_supply_not_an_assumed_billion():
    # same pool, a 10x larger supply: mcap 1.5M instead of 150k; a 1e3 supply coin falls under the 5k floor
    assert decide(intake_candidate(supply="10000000000000000"), ev(intake_candidate(supply="10000000000000000"),
                  supply="10000000000000000"))["reason"] == "pass"
    small = intake_candidate(supply="1000000000000")
    assert decide(small, ev(small, supply="1000000000000"))["reason"] == "kill:mcap_safety"


@pytest.mark.parametrize("kw,reason", [({"quote_sol": 39}, "kill:liq_min"),                 # 39 SOL < 40 SOL
                                       ({"base_atomic": 10**10}, "kill:mcap_safety")])       # mcap 3B
def test_outside_the_safety_bounds_is_killed(kw, reason):
    assert decide(intake_candidate(**kw))["reason"] == reason


def test_unknown_or_disagreeing_supply_refuses():
    c = intake_candidate()
    e = ev(c)
    del e["checks"]["token_safety"]["supply_atomic"]
    assert decide(c, e)["reason"] == "unknown:market_data:supply"
    assert decide(c, ev(c, supply="999"))["reason"] == "unknown:market_data:supply_mismatch"


def test_round_trip_limit_and_mandatory_proofs_are_unchanged():
    c = intake_candidate()
    e = ev(c)
    e["checks"]["sell_probe"]["round_trip_loss_pct"] = 8.5
    assert decide(c, e)["reason"] == "kill:round_trip_loss_pct"
    e = ev(c)
    e["checks"]["sell_probe"] = {"status": "unknown", "reason": "pool_stale"}
    assert decide(c, e)["reason"] == "unknown:sell_probe:pool_stale"


def test_policy_and_candidate_must_agree_on_intake():
    assert decide(intake_candidate(), p=pol(version="intake-v0"))["reason"] == "unknown:policy:intake"
    assert decide(intake_candidate(), p={**pol(), "mcap_band": [5000, 100000]})["reason"] == "unknown:policy:intake"
    plain = ps_candidate()
    assert K.hard_filter(plain, evidence=evidence(plain), policy=pol(), evaluated_at=AT)["reason"] == \
        "unknown:candidate:not_intake"


def test_band_cohorts_are_unchanged():
    c = intake_candidate(base_atomic=10**12)                    # mcap 30M
    got = K.hard_filter(c, evidence=ev(c), policy=policy(), evaluated_at=AT)["reason"]
    assert got in ("kill:mcap_band", "unknown:market_data:mcap_usd")   # today's band path, not intake-v1


def test_mcap_prices_on_the_effective_quote_and_liquidity_on_the_real_one():
    """Delta f0b143965 (1): real 200 SOL + proven virtual 200 SOL, base 1e12, supply 1e15, FX 150 -> 60M (above the
    50M bound), not 30M; liquidity stays 200 SOL x 150."""
    c = ps_candidate()
    state = c["snapshot"]["fields"]["pool_state"]["value"]
    state.update(quote_reserve_atomic=str(200 * 10**9), virtual_quote_atomic=str(200 * 10**9),
                 base_reserve_atomic=str(10**12), supply_atomic="1000000000000000", sol_usd=150.0)
    c["intake"] = dict(INTAKE)
    c = rehash(c)
    assert decide(c)["reason"] == "kill:mcap_safety"


def test_missing_or_disagreeing_decimals_refuse():
    """Delta (2): parsed supply AND decimals are required."""
    c = intake_candidate()
    e = ev(c)
    del e["checks"]["token_safety"]["decimals"]
    assert decide(c, e)["reason"] == "unknown:market_data:supply"
    e = ev(c)
    e["checks"]["token_safety"]["decimals"] = 9                 # pool_state says 6
    assert decide(c, e)["reason"] == "unknown:market_data:supply_mismatch"


def test_an_unhashable_feed_is_a_typed_refusal():
    """Delta (3): feed=[] raised TypeError."""
    with pytest.raises(ValueError, match="candidate_intake"):
        K.validate_candidate(intake_candidate(feed=[]))


def _late_read(c, *, source="intake_mint_read", intake=True):
    state = c["snapshot"]["fields"]["pool_state"]["value"]
    row = c["snapshot"]["fields"]["pool_state"]
    state.update(metadata_source=source, metadata_known_at=row["known_at"] + 30, metadata_slot=state["slot"] + 50,
                 cut_slot=state["slot"] + 1)
    if not intake:
        c.pop("intake", None)
    return rehash(c)


def test_a_labelled_late_mint_read_is_valid_only_on_an_intake_candidate():
    """Alpha/Delta 2026-10-27: the assembler's mint read lands after the print (later clock and slot than the cut);
    only metadata_source 'intake_mint_read' on an intake candidate may carry it."""
    K.validate_candidate(_late_read(intake_candidate()))
    with pytest.raises(ValueError):
        K.validate_candidate(_late_read(intake_candidate(), source="verified_mint:local"))
    with pytest.raises(ValueError):
        K.validate_candidate(_late_read(intake_candidate(), intake=False))


def test_the_verdict_price_accepts_a_late_slot_only_for_the_labelled_mint_read():
    from sniper.services import memecoin_desk as desk
    ps = {"base_reserve_atomic": str(2 * 10**14), "quote_reserve_atomic": str(200 * 10**9), "quote_mint": K.WSOL_MINT,
          "base_decimals": 6, "supply_atomic": "1000000000000000", "virtual_quote_atomic": "0", "lp_fee_bps": 20,
          "protocol_fee_bps": 5, "creator_fee_bps": 30, "slot": 7, "cut_slot": 8, "metadata_slot": 60,
          "metadata_known_at": 100.0, "reserve_known_at": 100.0, "virtual_quote_known_at": 100.0, "virtual_quote_slot": 7,
          "virtual_quote_source": "pumpswap_grpc:buy_event_virtual_quote", "map_known_at": 99.0, "sol_usd": 150.0,
          "sol_age_s": 1.0, "metadata_source": "intake_mint_read"}
    assert desk.verdict_price_pumpswap(ps, as_of=101.0, sol_usd=150.0, sol_age_s=1.0, clip_usd=25, basis="asof") is not None
    assert desk.verdict_price_pumpswap({**ps, "metadata_source": "verified_mint:local"}, as_of=101.0, sol_usd=150.0,
                                       sol_age_s=1.0, clip_usd=25, basis="asof") is None


def test_the_liquidity_floor_is_sol_not_usd():
    """Operator 2026-10-27 (Gamma): a fresh graduation (~85 SOL) passes a 40 SOL floor whatever SOL/USD is."""
    for fx in (60.0, 150.0):                                    # 85 SOL = 5.1k or 12.75k USD: both pass
        c = intake_candidate(quote_sol=85, base_atomic=2 * 10**14, sol_usd=fx)
        assert decide(c)["reason"] == "pass", fx
