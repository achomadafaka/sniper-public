"""Per-wallet Solana cap lift: up to 150 for listed wallets' solana: entries, every other entry at
the 75 sanity ceiling, an over-ceiling entry clamped (operator order 2026-10-09 via Alpha, for
frankdegods and ResellCalendar; spec docs/superpowers/specs/2026-10-09-memecoin-wallet-cap-lift-design.md)."""
import math

from sniper.execution.pump_curve_buy import capped_min_out
from sniper.settings import memecoin as cfg

FRANK = "aefe2ddd-c580-5245-a2f5-e4ed62f7ef10"
RESELL = "ff03cead-db17-5f20-b8aa-b2cae7e15d6a"
OTHER = "aaaaaaaa-0000-0000-0000-000000000001"


# ------------------------------------------------------------------------------- the cap

def _parse(raw, lifted=frozenset({FRANK, RESELL})):
    refused = []
    return cfg.parse_copy_premium_by_wallet(raw, lifted=lifted, log=refused.append), refused


def test_an_unlisted_wallet_never_gets_a_per_wallet_cap_above_75():
    """THE CHECK (Alpha item 4): only a solana:<listed uid> entry may go above 75, up to 150. An
    over-ceiling entry CLAMPS to its ceiling, so a wrong edit order never cuts a cap to the global
    (Alpha 2026-10-09: Frank's solana 50 written as 150 before the list lands at 75, never 35)."""
    table, said = _parse("solana:%s=150,%s=150,solana:%s=151,solana:%s=150"
                         % (OTHER, FRANK, RESELL, FRANK))
    assert table == {"solana:%s" % OTHER: 75.0, FRANK: 75.0, "solana:%s" % RESELL: 150.0,
                     "solana:%s" % FRANK: 150.0}
    assert len(said) == 3 and all("clamped" in line for line in said)
    unlisted, _said = _parse("solana:%s=150" % FRANK, lifted=frozenset())
    assert unlisted == {"solana:%s" % FRANK: 75.0}
    every = {k: v for k, v in cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET.items()}
    for key, pct in every.items():                      # the live table obeys it too
        chain, _sep, uid = key.rpartition(":")
        assert pct <= cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_CEILING_PCT or (
            chain == "solana" and uid in cfg.MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS), key


def test_without_the_list_the_ceiling_is_75_and_a_malformed_entry_is_still_dropped():
    table, said = _parse("solana:%s=50,solana:%s=0,bad" % (FRANK, RESELL), lifted=frozenset())
    assert table == {"solana:%s" % FRANK: 50.0} and len(said) == 2


def test_a_listed_150_entry_lifts_both_solana_terms_and_nothing_else(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET",
                        {"solana:%s" % FRANK: 150.0, "solana:%s" % RESELL: 150.0})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {"solana:%s" % FRANK: 1.5})
    for uid in (FRANK, RESELL):
        assert cfg.copy_premium_cap(uid, "solana") == (150.0, "wallet")
        assert cfg.solana_wallet_chase_cap_pct(uid, 30.0) == 150.0
        assert cfg.copy_premium_cap(uid, "8453") == (35.0, "global")
    assert cfg.solana_wallet_chase_cap_pct(OTHER, 30.0) == 30.0


def test_at_150_the_signed_min_out_is_the_clip_at_two_and_a_half_times_the_source_average(monkeypatch):
    """Spec section 1: the haircut floor goes to <= 0 at any cap >= 100, so the price floor alone
    is signed, and its binding term is the source's average (below its post-trade price)."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    expected, cost_usdc, post, avg = 1_000_000_000_000, 110_000_000, 0.00010, 0.00008   # 6 decimals
    min_out, output_floor, price_floor = capped_min_out(
        expected, 150.0, source_price_usd=post, source_cost_usdc_atomic=cost_usdc,
        source_price_cap_pct=150.0, base_decimals=6, source_average_price_usd=avg, copy_cap_pct=150.0)
    assert output_floor <= 0 and min_out == price_floor
    assert math.isclose(min_out, 110.0 / (avg * 2.5) * 1e6, rel_tol=1e-6)


def test_the_first_slot_build_of_a_listed_wallet_logs_its_cap_and_min_out_bound_once(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS", frozenset({FRANK}))
    lane.CAP_LIFT_LOGGED.discard(FRANK)
    limit = {"base_decimals": 6, "cost_usdc_atomic": 110_000_000, "source_average_price_usd": 0.00008,
             "min_tokens_atomic": int(110.0 / (0.00008 * 2.5) * 1e6)}
    line = lane.log_cap_lift_once(FRANK, limit, {"cap_used": 150.0, "band_used": 150.0})
    assert "cap_used=150.0" in line and "min_out_bound_x_source_avg=2.5" in line
    assert lane.log_cap_lift_once(FRANK, limit, {"cap_used": 150.0}) is None          # once
    assert lane.log_cap_lift_once(OTHER, limit, {"cap_used": 35.0}) is None           # unlisted
    lane.CAP_LIFT_LOGGED.discard(FRANK)
