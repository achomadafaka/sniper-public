"""MEMECOIN_MCAP_HISTORY_PRICED (523c9968, 2026-10-01): the Solana admission ceiling tests a PRICED mcap.

The on-chain lane's ceiling read stage-2 history, and with fewer than 3 readings that was the median
of stale J7 mcaps with the source price ignored. fmpumpguy's A71Uf3jw passed a $250,000 ceiling at
$131,269 (one J7 reading at 0.0001396) while the source paid 0.000576 on a ~982M supply: ~$566k.
Narrowed by Alpha: the knob changes ONLY that ceiling decision. Band, entry_mcap on every chain and the
exit targets keep today's value; the priced value is a shadow fact beside today's.
docs/audits/2026-10-01-solana-mcap-gate-two-lanes.md has the rule-5 maps."""
import json
import math
import sqlite3
import types
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as RR
from sniper.settings import memecoin as cfg

A71U = "A71Uf3jwg57fNedSao9eFwm9eKSLYFbVJ85T4fuXpump"
# The one J7 reading the gate saw (memecoin_events 64803741, frankdegods, 2026-09-29T16:22:49Z)
# and the source's average fill (onchain_wallet_detections 3467).
J7_MCAP, J7_PRICE = 131269.485894105, 0.000139633458319783
SOURCE_PRICE = 0.000576452224935897


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _reading(c, eid, mint, mcap, price, network_id="solana"):
    m.insert_event(c, {
        "fomo_event_id": eid, "source_user_id": "other", "source_handle": "o", "mint": mint,
        "ticker": "T", "side": "buy", "their_price": price, "their_market_cap": mcap,
        "their_size_usd": 1.0, "is_first_buy": 0, "created_at": eid, "received_at": eid,
        "lag_s": 0.1, "network_id": network_id})


@pytest.fixture
def knob_on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MCAP_HISTORY_PRICED", True)


# --- the resolver: priced only when the caller asks, priced or None --------------------------------

def test_one_reading_is_priced_at_the_callers_price():
    c = _conn()
    _reading(c, "a", A71U, J7_MCAP, J7_PRICE)
    _reading(c, "b", A71U, 0.0, 0.000582)              # fmpumpguy's own J7 row: no mcap, never counts
    got = m.resolve_mint_mcap(c, A71U, network_id="solana", price=SOURCE_PRICE, priced=True)
    assert got == pytest.approx(J7_MCAP / J7_PRICE * SOURCE_PRICE)   # ~$541,926


def test_two_readings_use_their_median_supply():
    c = _conn()
    _reading(c, "a", "M", 21_565.1, 2.05137e-05)       # EThAHDXi's two readings, 13 h old
    _reading(c, "b", "M", 24_761.9, 2.43964e-05)
    supplies = [21_565.1 / 2.05137e-05, 24_761.9 / 2.43964e-05]
    assert m.resolve_mint_mcap(c, "M", price=0.000189944, priced=True) == pytest.approx(
        sum(supplies) / 2 * 0.000189944)


def test_readings_that_disagree_on_supply_are_still_priced():
    """3+ readings whose implied supplies spread past a third: today's falls back to the median mcap,
    price ignored; priced uses the median supply."""
    c = _conn()
    for i, (mcap, price) in enumerate(((100_000, 1e-4), (200_000, 1e-4), (900_000, 1e-4))):
        _reading(c, str(i), "M", mcap, price)
    assert m.resolve_mint_mcap(c, "M", price=5e-4) == 200_000
    assert m.resolve_mint_mcap(c, "M", price=5e-4, priced=True) == pytest.approx(2e9 * 5e-4)


@pytest.mark.parametrize("price", [None, 0, 0.0, -1e-4, float("nan")])
def test_no_usable_price_is_unpriced_never_the_stale_median(price):
    c = _conn()
    _reading(c, "a", A71U, J7_MCAP, J7_PRICE)
    assert m.resolve_mint_mcap(c, A71U, price=price, priced=True) is None
    assert m.resolve_mint_mcap(c, A71U, price=price) == pytest.approx(J7_MCAP)    # today's


@pytest.mark.parametrize("reading_price", [0.0, None, 1.0, 1e-12])
def test_a_reading_with_no_or_an_implausible_supply_is_not_priced(reading_price):
    """price 0/None implies no supply; 1.0 implies 131k tokens and 1e-12 implies 1.3e17: both
    outside [1e6, 1e13]."""
    c = _conn()
    _reading(c, "a", "M", J7_MCAP, reading_price)
    assert m.resolve_mint_mcap(c, "M", price=SOURCE_PRICE, priced=True) is None


def test_an_implausible_reading_is_skipped_beside_a_plausible_one():
    c = _conn()
    _reading(c, "a", "M", J7_MCAP, J7_PRICE)           # 940M tokens
    _reading(c, "b", "M", 500_000, 1.0)                # 500k tokens: implausible
    assert m.resolve_mint_mcap(c, "M", price=SOURCE_PRICE, priced=True) == pytest.approx(
        J7_MCAP / J7_PRICE * SOURCE_PRICE)


def test_the_capture_fallback_is_priced_too():
    c = _conn()
    c.execute("INSERT INTO memecoin_quote_observations_v2 (sweep_id, network_id, mint, observed_at, "
              "price, market_cap, quality, contexts) VALUES ('s', 'solana', 'Q', 't', ?, ?, 'ok', '[]')",
              (J7_PRICE, J7_MCAP))
    assert m.resolve_mint_mcap(c, "Q", network_id="solana", price=SOURCE_PRICE, priced=True) == (
        pytest.approx(J7_MCAP / J7_PRICE * SOURCE_PRICE))
    assert m.resolve_mint_mcap(c, "Q", network_id="solana", price=SOURCE_PRICE) == pytest.approx(J7_MCAP)


def test_the_executors_entry_mcap_is_todays_with_the_knob_on(knob_on):
    """Executors call resolve_mint_mcap(price=fill_price) without `priced`: the knob never reaches
    entry_mcap on any chain, so the exit targets derived from it do not move."""
    c = _conn()
    _reading(c, "a", A71U, J7_MCAP, J7_PRICE)
    assert m.resolve_mint_mcap(c, A71U, network_id="solana", price=0.000606) == pytest.approx(J7_MCAP)


# --- pump mints: source price x 1e9, proven structurally, never by the suffix (Alpha, 2026-10-01) ------

def _detection(valuation, mint=A71U, price=SOURCE_PRICE):
    from sniper.services.memecoin_onchain_lane import OnchainEv
    return OnchainEv(dict(mint=mint, source_user_id="u", handle="fmpumpguy", network_id="solana",
                          source_size_usd=1976.55, source_price=price,
                          source_valuation_json=json.dumps(valuation),
                          signature="2XX2e5QAfjH1", detected_at="2026-09-29T16:26:33.512666+00:00"))


A71U_POOL = "H2UTFeuwA6yQP2L3DegdvtA4wCCwLJjKwq2zXepzU1ae"   # the slot lane's line for this buy
PAMM = {"basis": "pump_event_net_sol", "program_id": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}
CURVE = {"basis": "pump_event_net_sol", "program_id": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"}


def test_the_watcher_records_the_pumpswap_pool_and_it_is_the_canonical_migration_pool():
    """A real PumpSwap receipt: the valuation names the event's pool, and that pool is the one pump's
    pool-authority PDA creates for this mint (the slot lane's not_pump_mint test, derived, no read)."""
    from sniper.execution.solana_native_entry_quote import accounts_for
    from sniper.services.memecoin_source_valuation import value_source_buy
    fx = json.loads((Path(__file__).parent / "fixtures/memecoin/confirmed_source_buys.json").read_text())
    tx = fx["untaxxable"]
    facts = value_source_buy(tx, tx["meta"]["postTokenBalances"][0]["owner"], sol_usd=(120.0, 100.0),
                             now=105.0)
    assert facts["program_id"] == PAMM["program_id"]
    assert facts["pool"] == accounts_for(facts["mint"])["pool"]
    assert accounts_for(A71U)["pool"] == A71U_POOL


@pytest.mark.parametrize("valuation, network, proven", [
    (CURVE, "solana", True),                                  # the curve trades only pump's mints
    ({**PAMM, "pool": A71U_POOL}, "solana", True),           # the canonical migration pool
    ({**PAMM, "pool": "CmcL1kMyUDtQ3ae4w3ZvB3Nh5HQbBoW6Rk5QjSfC9Cz8"}, "solana", False),
    (PAMM, "solana", False),                                  # a row from before the pool was kept
    ({"basis": "route_owner_quote_out"}, "solana", False),
    (CURVE, "8453", False),
])
def test_a_pump_mint_is_proven_by_its_program_and_pool_never_by_its_suffix(valuation, network, proven):
    from sniper.services.memecoin_onchain_lane import pump_mint_proven
    ev = _detection(valuation)
    ev.network_id = network
    assert pump_mint_proven(ev) is proven
    ground = _detection({**PAMM, "pool": A71U_POOL}, mint="9twiuSdTVkwtAC9XQDG57dFRhF4iqPih461HJfMZKci9")
    assert pump_mint_proven(ground) is False                 # another mint's pool proves nothing


# --- the production gate: THE CHECK (fails if A71Uf3jw prices back at $131k) -------------------------

def _gate(monkeypatch, c, ev, seen=None):
    # Initiation is covered by captured receipt tests; this seam exercises the priced mcap gate.
    monkeypatch.setattr(RR, "source_initiation_refusal", lambda *_: None)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_MCAP_USD", 0.0)
    m.add_wallet(c, "u", "fmpumpguy")
    m.set_wallet_real_approved(c, "u", True, chains=["solana"], force=True)
    handed = []
    monkeypatch.setattr(RR, "real_entry_hook",
                        lambda *a, **k: (handed.append(k.get("classification_context")),
                                         {"status": "filled"})[1])
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)
    if seen is not None:
        from sniper.services import memecoin_market_state as MS
        real = MS.classify_funded_entry
        monkeypatch.setattr(MS, "classify_funded_entry",
                            lambda *a, **k: (lambda c_: (seen.append(c_.to_dict()), c_)[1])(real(*a, **k)))
    hook = RR.make_real_hook(None, {"solana": object()},
                             now_fn=lambda: "2026-09-29T16:26:33.465192+00:00", trade_size=10.0,
                             max_age_s=None)
    res = hook(c, ev)
    res["_handed"] = handed
    return res


def _a71u_history():
    c = _conn()
    _reading(c, "2026-09-29T16:22:49.776Z", A71U, J7_MCAP, J7_PRICE)
    return c


def _j7_event():
    return types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None, mint=A71U,
                                 network_id="solana", their_price=SOURCE_PRICE)


@pytest.mark.parametrize("ev, priced, basis", [
    (lambda: _detection({**PAMM, "pool": A71U_POOL}), SOURCE_PRICE * 1e9, "pump_supply"),   # $576,452
    (_j7_event, J7_MCAP / J7_PRICE * SOURCE_PRICE, "history_priced"),                      # $541,926
])
def test_A71Uf3jw_is_refused_by_the_ceiling_alone_and_keeps_todays_band(monkeypatch, knob_on, ev,
                                                                        priced, basis):
    seen = []
    res = _gate(monkeypatch, _a71u_history(), ev(), seen)
    assert res["status"] == "skipped_mcap", res
    assert res["mcap"] == pytest.approx(priced)
    assert "over ceiling $250,000 (over_ceiling)" in res["reason"]
    facts = seen[-1]["facts"]
    assert facts["effective_mcap"] == pytest.approx(J7_MCAP)            # today's value, today's band
    assert facts["mcap_basis"] == "history" and seen[-1]["coin_band"] == "micro"
    assert facts["priced_mcap"] == pytest.approx(priced)
    assert facts["priced_mcap_basis"] == basis and facts["priced_coin_band"] == "small"


def test_A71Uf3jw_is_admitted_at_131k_with_the_knob_off_and_the_shadow_still_rides(monkeypatch):
    ev = _detection({**PAMM, "pool": A71U_POOL})
    res = _gate(monkeypatch, _a71u_history(), ev)
    assert res["status"] == "filled"
    assert res["_handed"][0]["effective_mcap"] == pytest.approx(J7_MCAP)
    assert res["_handed"][0]["mcap_basis"] == "history"
    assert ev.priced_mcap_shadow == (pytest.approx(SOURCE_PRICE * 1e9), "pump_supply")


def test_an_admitted_coin_keeps_todays_band_with_the_knob_on(monkeypatch, knob_on):
    """Priced under the ceiling: admitted, and everything handed on is today's value."""
    ev = _detection({**PAMM, "pool": A71U_POOL}, price=0.0002)          # $200k priced
    res = _gate(monkeypatch, _a71u_history(), ev)
    assert res["status"] == "filled"
    assert res["_handed"][0]["effective_mcap"] == pytest.approx(J7_MCAP)
    assert ev.priced_mcap_shadow[0] == pytest.approx(200_000.0)


def test_no_history_means_no_shadow_and_today_s_later_stages(monkeypatch, knob_on):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_DEX_MCAP_FALLBACK", False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MCAP_UNRESOLVED", "refuse")
    monkeypatch.setattr(RR, "_route_band_mcap", lambda *a, **k: None)
    monkeypatch.setattr(RR, "_default_solana_mcap_fn", lambda: (lambda mint: None))
    ev = _detection({**PAMM, "pool": A71U_POOL})
    res = _gate(monkeypatch, _conn(), ev)
    assert res["status"] == "skipped_mcap" and res["mcap"] is None, res
    assert getattr(ev, "priced_mcap_shadow", None) is None


def test_an_event_asked_twice_never_carries_an_old_shadow(monkeypatch):
    ev = _detection({**PAMM, "pool": A71U_POOL})
    _gate(monkeypatch, _a71u_history(), ev)
    assert ev.priced_mcap_shadow is not None
    monkeypatch.setattr(RR, "_route_band_mcap", lambda *a, **k: None)
    monkeypatch.setattr(RR, "_default_solana_mcap_fn", lambda: (lambda mint: None))
    _gate(monkeypatch, _conn(), ev)                                     # no history this time
    assert ev.priced_mcap_shadow is None


def test_an_immutable_event_is_still_gated_on_the_priced_value(monkeypatch, knob_on):
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class Frozen:
        side: str = "buy"
        source_user_id: str = "u"
        their_market_cap: float = None
        mint: str = A71U
        network_id: str = "solana"
        their_price: float = SOURCE_PRICE
    res = _gate(monkeypatch, _a71u_history(), Frozen())
    assert res["status"] == "skipped_mcap" and res["mcap"] == pytest.approx(
        J7_MCAP / J7_PRICE * SOURCE_PRICE), res


def test_an_absurd_priced_value_gives_no_shadow_and_no_refusal(monkeypatch, knob_on):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ABSURD_MCAP_USD", 500_000.0)    # the pump value, $576k
    ev = _detection({**PAMM, "pool": A71U_POOL})
    assert _gate(monkeypatch, _a71u_history(), ev)["status"] == "filled"
    assert ev.priced_mcap_shadow is None


@pytest.mark.parametrize("templates, status", [("on", "filled"), ("off", "skipped_mcap")])
def test_auto_templates_admit_a_scalp_sized_priced_coin_as_today(monkeypatch, knob_on, templates, status):
    monkeypatch.setattr(cfg, "MEMECOIN_AUTO_TEMPLATES", templates)
    monkeypatch.setattr(cfg, "MEMECOIN_SCALP_MCAP_USD", 500_000.0)
    assert _gate(monkeypatch, _a71u_history(), _detection({**PAMM, "pool": A71U_POOL}))["status"] == status


def test_a_refusal_before_the_mcap_stage_never_classifies_with_an_old_shadow(monkeypatch):
    ev = _detection({**PAMM, "pool": A71U_POOL})
    ev.priced_mcap_shadow = (999.0, "pump_supply")                         # left by an earlier pass
    seen = []
    from sniper.services import memecoin_market_state as MS
    real = MS.classify_funded_entry
    monkeypatch.setattr(MS, "classify_funded_entry",
                        lambda *a, **k: (seen.append(k.get("priced_mcap_shadow")), real(*a, **k))[1])
    monkeypatch.setattr(RR, "real_entry_hook", lambda *a, **k: {"status": "filled"})
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)
    c = _conn()                                                         # wallet not armed: early refusal
    hook = RR.make_real_hook(None, {"solana": object()},
                             now_fn=lambda: "2026-09-29T16:26:33.465192+00:00", trade_size=10.0,
                             max_age_s=None)
    hook(c, ev)
    assert seen and all(x is None for x in seen), seen


def test_another_chain_gets_no_shadow_and_prices_its_own_history(monkeypatch, knob_on):
    """The Solana shadow stays Solana-only. An EVM gate prices sparse history itself (2026-10-07,
    d31fed65): one reading's supply (~940M) at the source's 0.000576 is ~$541k, over $250k."""
    c = _conn()
    _reading(c, "2026-09-29T16:22:49.776Z", "0xCOIN", J7_MCAP, J7_PRICE, network_id="8453")
    m.add_wallet(c, "u", "w")
    m.set_wallet_real_approved(c, "u", True, chains=["8453"], force=True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {})
    monkeypatch.setattr(RR, "real_entry_hook", lambda *a, **k: {"status": "filled"})
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)
    hook = RR.make_real_hook(None, {"8453": object()},
                             now_fn=lambda: "2026-09-29T16:26:33.465192+00:00", trade_size=10.0,
                             max_age_s=None)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None, mint="0xCOIN",
                               network_id="8453", their_price=SOURCE_PRICE)
    res = hook(c, ev)
    assert res["status"] == "skipped_mcap" and res["reason"].endswith("(over_ceiling)")
    assert res["mcap"] == pytest.approx(J7_MCAP / J7_PRICE * SOURCE_PRICE)
    assert getattr(ev, "priced_mcap_shadow", None) is None


def test_the_knob_parses_from_env_text():
    assert cfg.MEMECOIN_MCAP_HISTORY_PRICED is False
    for text, want in (("1", True), ("true", True), ("0", False), ("", False), ("off", False)):
        assert cfg.mcap_history_priced(text) is want
    assert math.isfinite(m.PRICED_SUPPLY_MIN) and m.PRICED_SUPPLY_MIN < m.PRICED_SUPPLY_MAX
