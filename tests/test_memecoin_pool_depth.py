"""Pool-store depth: the math, the cache, and the one rule — it only ever fills a hole.

2026-09-22, operator: "fix the pool store thing so liquidity isnt unknown". Measured that day on
4663, the provider returned no pair on 1,982 of 2,254 attempts while our pool store already knew
pools for 2,060 of them.
"""
import sqlite3

import pytest

import sniper.persistence.db_memecoin as m
from sniper.services.memecoin_pool_depth import depth_for_pools, pool_state_slots, quote_tvl_usd

Q96 = 2 ** 96
T0 = "2026-09-22T20:00:00+00:00"


def test_quote_tvl_is_two_times_liquidity_times_sqrt_price():
    # sqrt(P) = 2 -> token1 side holds 2L; both sides are 4L in token1 terms.
    usd = quote_tvl_usd(1_000_000, 2 * Q96, quote_is_token0=False, quote_decimals=0, quote_usd=1.0)
    assert usd == pytest.approx(4_000_000.0)


def test_the_token0_side_uses_the_reciprocal():
    usd = quote_tvl_usd(1_000_000, 2 * Q96, quote_is_token0=True, quote_decimals=0, quote_usd=1.0)
    assert usd == pytest.approx(1_000_000.0)          # 2 * L / sqrt(P)


def test_decimals_and_unit_price_are_applied():
    usd = quote_tvl_usd(10 ** 18, 1 * Q96, quote_is_token0=False, quote_decimals=18, quote_usd=2748.83)
    assert usd == pytest.approx(2 * 2748.83)


@pytest.mark.parametrize("liquidity,sqrt_price,decimals,price", [
    (0, Q96, 18, 1.0), (-5, Q96, 18, 1.0), (10, 0, 18, 1.0), (10, Q96, 18, 0),
    (float("inf"), Q96, 18, 1.0), ("x", Q96, 18, 1.0), (10, Q96, -1, 1.0)])
def test_an_unreadable_pool_is_none_never_zero(liquidity, sqrt_price, decimals, price):
    """A zero would read as a measured empty pool; unknown depth must stay unknown."""
    assert quote_tvl_usd(liquidity, sqrt_price, quote_is_token0=False,
                         quote_decimals=decimals, quote_usd=price) is None


def test_the_virtual_value_is_not_tvl_on_a_narrow_range():
    """Codex review 2026-09-23 F6. 2*L*sqrt(P) is the VIRTUAL reserve of the current tick's
    liquidity, which equals real inventory only for full-range positions. A position of
    liquidity 1,000 concentrated in sqrt-price [0.999, 1.001] at price 1 really holds about 2.0 units;
    the virtual figure is 2,000, a thousandfold overstatement. The number is kept as a
    local-liquidity signal and must never be read as TVL."""
    import math as _math
    L, lo, hi = 1_000.0, 0.999, 1.001                          # sqrt-price bounds of the range
    actual = L * (1.0 - lo) + L * (1.0 / 1.0 - 1.0 / hi)       # y in range + x in range at P=1
    virtual = quote_tvl_usd(L, Q96, quote_is_token0=False, quote_decimals=0, quote_usd=1.0)
    assert actual == pytest.approx(1.999, rel=1e-3)
    assert virtual == pytest.approx(2_000.0)
    assert virtual / actual > 900


def test_the_deepest_pool_wins_and_a_failing_read_is_skipped():
    rows = [{"pool": "a", "token0": "q", "token1": "c", "quote": "q", "quote_decimals": 0},
            {"pool": "b", "token0": "c", "token1": "q", "quote": "q", "quote_decimals": 0},
            {"pool": "boom", "token0": "c", "token1": "q", "quote": "q", "quote_decimals": 0}]

    def state_fn(row):
        if row["pool"] == "boom":
            raise RuntimeError("rpc down")
        return (Q96, 10) if row["pool"] == "a" else (Q96, 500)
    depth, pool = depth_for_pools(rows, state_fn=state_fn, price_fn=lambda _q: 1.0)
    assert pool == "b" and depth == pytest.approx(1000.0)


def test_reading_nothing_returns_none():
    depth, pool = depth_for_pools([{"pool": "a", "token0": "q", "token1": "c", "quote": "q",
                                    "quote_decimals": 0}],
                                  state_fn=lambda _r: (None, None), price_fn=lambda _q: 1.0)
    assert depth is None and pool is None


def test_a_pool_id_of_the_wrong_width_is_refused():
    with pytest.raises(ValueError):
        pool_state_slots("0xdeadbeef")


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_pool_depth_cache(c)
    return c


def test_a_fresh_row_reads_back_and_a_stale_one_does_not():
    c = _conn()
    m.record_pool_depth(c, "4663", "0xmint", depth_usd=1234.5, pool="0xpool", quote="0xq",
                        pools_read=7, now_iso="2026-09-22T19:30:00+00:00")
    assert m.pool_depth_usd(c, "4663", "0xmint", now_iso=T0, max_age_s=3600) == pytest.approx(1234.5)
    assert m.pool_depth_usd(c, "4663", "0xmint", now_iso=T0, max_age_s=600) is None
    assert m.pool_depth_usd(c, "4663", "0xother", now_iso=T0, max_age_s=3600) is None


def test_a_future_row_is_refused():
    """A clock that ran backwards must not produce a negative age that passes the window."""
    c = _conn()
    m.record_pool_depth(c, "4663", "0xmint", depth_usd=10.0, pool=None, quote=None, pools_read=0,
                        now_iso="2026-09-22T21:00:00+00:00")
    assert m.pool_depth_usd(c, "4663", "0xmint", now_iso=T0, max_age_s=3600) is None


def test_the_upsert_keeps_one_row_per_coin():
    c = _conn()
    for depth in (1.0, 2.0, 3.0):
        m.record_pool_depth(c, "4663", "0xmint", depth_usd=depth, pool="0xp", quote="0xq",
                            pools_read=1, now_iso=T0)
    rows = c.execute("SELECT depth_usd FROM memecoin_pool_depth").fetchall()
    assert [r[0] for r in rows] == [3.0]


# --- the entry path: it fills a hole and never overrides the provider -------------------------

def _stats_with(conn, provider_liquidity, *, depth=None, depth_at=T0, now=T0):
    from sniper.services import memecoin_real_runner as rr
    if depth is not None:
        m.record_pool_depth(conn, "4663", "0xmint", depth_usd=depth, pool="0xp", quote="0xq",
                            pools_read=3, now_iso=depth_at)
    stats = {} if provider_liquidity is None else {"liquidity": provider_liquidity}
    return rr._funded_market_stats(stats, now, conn=conn, chain_id="4663", mint="0xmint")


def test_the_pool_store_fills_a_missing_provider_liquidity():
    out = _stats_with(_conn(), None, depth=5000.0)
    assert out["liquidity"] == pytest.approx(5000.0)
    assert out["liquidity_source"] == "pool_store"
    assert out["liquidity_pool_store_usd"] == pytest.approx(5000.0)


@pytest.mark.parametrize("provider", [42.0, 1e9])
def test_a_provider_number_is_never_overridden(provider):
    """The provider's TVL and ours are different quantities; ours must not replace a real one."""
    out = _stats_with(_conn(), provider, depth=5000.0)
    assert out["liquidity"] == pytest.approx(provider)
    assert "liquidity_source" not in out


def test_a_measured_zero_is_a_reading_not_a_hole():
    """Codex review 2026-09-23 A4: the fallback tested `> 0`, so a provider that answered
    liquidity 0 (a drained pool) was overwritten by a cached positive depth."""
    out = _stats_with(_conn(), 0.0, depth=50_000.0)
    assert out["liquidity"] == 0.0
    assert "liquidity_source" not in out


def test_a_stale_cache_leaves_the_hole_honest():
    out = _stats_with(_conn(), None, depth=5000.0, depth_at="2026-09-22T10:00:00+00:00")
    assert out.get("liquidity") is None and "liquidity_source" not in out


def test_no_cache_row_changes_nothing():
    out = _stats_with(_conn(), None)
    assert out.get("liquidity") is None and "liquidity_source" not in out


def test_the_fallback_is_off_when_the_window_is_zero(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_POOL_DEPTH_MAX_AGE_S", 0.0, raising=False)
    out = _stats_with(_conn(), None, depth=5000.0)
    assert out.get("liquidity") is None


def test_a_broken_cache_never_breaks_the_decision():
    """Fail-open: the alternative to this fallback is the unknown the attempt already had."""
    from sniper.services import memecoin_real_runner as rr
    broken = sqlite3.connect(":memory:")
    broken.execute("CREATE TABLE memecoin_pool_depth(nonsense TEXT)")
    out = rr._funded_market_stats({}, T0, conn=broken, chain_id="4663", mint="0xmint")
    assert out.get("liquidity") is None


def test_pair_age_still_derives_with_no_identity_passed():
    from sniper.services import memecoin_real_runner as rr
    out = rr._funded_market_stats({"pair_created_at": 1790000000000.0}, T0)
    assert out["pair_age_s"] > 0


# --- the entry impact gate (2026-09-23) -------------------------------------------------------

import math as _math

from sniper.services.memecoin_pool_depth import clip_impact_pct, impact_verdict


def _sqrt_x96_for(price_quote_per_mint, quote_decimals, mint_decimals=18):
    """sqrt_price_x96 for a pool whose quote is token0, from a whole-unit price."""
    atomic = price_quote_per_mint * (10 ** (quote_decimals - mint_decimals))
    return _math.sqrt(1.0 / atomic) * (2 ** 96)


ARENA = dict(liquidity=29277002188455995541954,
             sqrt_price_x96=_sqrt_x96_for(2.14256e-08, 18), quote_is_token0=True,
             quote_decimals=18, quote_usd=2758.79)
CSTR = dict(liquidity=59147624213780158,
            sqrt_price_x96=_sqrt_x96_for(2.2641e-05, 6), quote_is_token0=True,
            quote_decimals=6, quote_usd=1.0)


def test_it_reproduces_the_two_pools_measured_on_chain():
    """Both figures were derived independently on 2026-09-23 by walking the real V4 swap."""
    assert clip_impact_pct(37.5, **ARENA) == pytest.approx(0.32, abs=0.05)
    assert clip_impact_pct(100.0, **ARENA) == pytest.approx(0.85, abs=0.05)
    assert clip_impact_pct(37.5, **CSTR) == pytest.approx(13.32, rel=0.02)
    assert clip_impact_pct(100.0, **CSTR) == pytest.approx(35.53, rel=0.02)


def test_a_bigger_clip_always_costs_more():
    assert clip_impact_pct(100.0, **CSTR) > clip_impact_pct(37.5, **CSTR) > 0


def test_impact_falls_as_the_pool_deepens():
    """Twice the in-range liquidity absorbs the same clip for about half the impact."""
    one = clip_impact_pct(37.5, **CSTR)
    two = clip_impact_pct(37.5, **dict(CSTR, liquidity=CSTR["liquidity"] * 2))
    assert two < one
    assert two == pytest.approx(one / 2, rel=0.15)


def test_the_single_tick_number_is_neither_a_floor_nor_a_ceiling():
    """Codex review 2026-09-23 F2. The model holds liquidity constant across the clip. When the
    clip crosses into a DEEPER initialized tick the real average impact is smaller, so the number
    is not a floor; when it crosses into a thinner one it is larger, so it is not a ceiling. Two
    segments: L=1,000 up to sqrt(P)=1.001, then L=100,000. A $100 clip (quote is token1, P=1)
    spends $1 in the first segment and $99 in the second."""
    L1, L2, s0, s1 = 1_000.0, 100_000.0, 1.0, 1.001
    y1 = L1 * (s1 - s0)                                   # quote consumed to reach the boundary
    x1 = L1 * (1 / s0 - 1 / s1)                           # tokens out over segment one
    y2 = 100.0 - y1
    s2 = s1 + y2 / L2
    x2 = L2 * (1 / s1 - 1 / s2)
    exact = 100.0 * ((100.0 / (x1 + x2)) / (s0 ** 2) - 1.0)
    model = clip_impact_pct(100.0, liquidity=L1, sqrt_price_x96=int(s0 * Q96),
                            quote_is_token0=False, quote_decimals=0, quote_usd=1.0)
    assert y1 == pytest.approx(1.0) and exact == pytest.approx(0.2972, abs=0.001)
    assert model == pytest.approx(10.0, abs=0.01)
    assert model > exact * 30, "the single-tick number overstates a clip that crosses into depth"


def test_the_mint_decimals_are_never_an_input():
    """The impact is a RATIO of two prices that carry the same decimal factor, so it cancels. The
    function must therefore take no mint-decimals argument -- if one is ever added, the 17-orders-of
    -magnitude atomic-unit error of 2026-09-23 has a way back in."""
    import inspect

    assert "mint_decimals" not in inspect.signature(clip_impact_pct).parameters
    assert "mint_dec" not in inspect.signature(clip_impact_pct).parameters


def test_unreadable_state_is_none_not_zero():
    for bad in ({"liquidity": 0}, {"sqrt_price_x96": 0}, {"quote_usd": 0}, {"quote_decimals": 99},
                {"liquidity": None}, {"sqrt_price_x96": float("nan")}):
        assert clip_impact_pct(37.5, **dict(ARENA, **bad)) is None
    assert clip_impact_pct(0, **ARENA) is None


def test_the_gate_is_off_by_default_and_blocks_only_over_the_ceiling():
    assert impact_verdict(37.5, CSTR, 0) == (True, None, None)          # disabled
    allow, reason, pct = impact_verdict(37.5, ARENA, 5.0)
    assert allow and reason is None and pct == pytest.approx(0.32, abs=0.05)
    allow, reason, pct = impact_verdict(37.5, CSTR, 5.0)
    assert not allow and "over ceiling" in reason and pct > 13
    assert "floor" not in reason and "single-tick" in reason, "the reason must not claim a bound"


def test_an_impact_it_cannot_compute_never_blocks():
    """Fail-open is the whole safety argument: an unreadable pool must not become a refusal."""
    allow, reason, pct = impact_verdict(37.5, None, 5.0)
    assert allow and pct is None and "impact_unknown" in reason
    allow, reason, pct = impact_verdict(37.5, {"liquidity": 0}, 5.0)
    assert allow and pct is None and "impact_unknown" in reason


def test_the_gate_ships_disabled():
    """The knob must default to 0. This gate refuses real entries, and it was built from five coins;
    it goes live when the operator turns it on, not when it is merged."""
    import importlib

    import sniper.settings.memecoin as settings

    importlib.reload(settings)
    assert settings.MEMECOIN_ENTRY_MAX_IMPACT_PCT == 0.0


# --- the refresher must write a row the gate can actually read (2026-09-23) -------------------

from sniper.services.memecoin_pool_depth import pool_state_row


def test_pool_state_row_needs_every_field():
    row = {"quote_decimals": 6, "token0": "0xQ", "quote": "0xq"}
    good = pool_state_row(row, 13517971155303911452229632, 59147624213780158, 1.0)
    assert good["quote_is_token0"] is True and good["quote_decimals"] == 6
    for sqrt_p, liq, usd in ((0, 1, 1.0), (1, 0, 1.0), (1, 1, 0.0), (None, 1, 1.0), (1, None, 1.0),
                             (1, 1, None)):
        assert pool_state_row(row, sqrt_p, liq, usd) is None
    assert pool_state_row({"quote_decimals": None}, 1, 1, 1.0) is None


def test_a_refreshed_row_answers_the_impact_gate(tmp_path):
    """The regression that made the gate unfireable: the refresher wrote depth WITHOUT state, so
    every cached row returned None from pool_impact_inputs and no entry could ever be refused."""
    import sqlite3

    import sniper.persistence.db_memecoin as db

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    db.ensure_pool_depth_cache(conn)
    row = {"quote_decimals": 6, "token0": "0xq", "quote": "0xq", "pool": "0xpool"}
    state = pool_state_row(row, 13517971155303911452229632, 59147624213780158, 1.0)
    db.record_pool_depth(conn, "4663", "0xmint", depth_usd=500.0, pool="0xpool", quote="0xq",
                         pools_read=4, now_iso="2026-09-23T08:00:00+00:00", state=state)
    got = db.pool_impact_inputs(conn, "4663", "0xmint", now_iso="2026-09-23T08:05:00+00:00",
                                max_age_s=3600)
    assert got and got["liquidity"] == 59147624213780158 and got["quote_is_token0"] is True
    allow, reason, pct = impact_verdict(37.5, got, 5.0)
    assert pct is not None, "a refreshed row must produce a number, not an unknown"
    # and the same row written the OLD way answers nothing
    db.record_pool_depth(conn, "4663", "0xold", depth_usd=500.0, pool="0xpool", quote="0xq",
                         pools_read=4, now_iso="2026-09-23T08:00:00+00:00")
    assert db.pool_impact_inputs(conn, "4663", "0xold", now_iso="2026-09-23T08:05:00+00:00",
                                 max_age_s=3600) is None
    conn.close()


# --- F3: one unavailable conversion must not abort the refresh ----------------------------------

def test_one_unpriceable_quote_unit_does_not_kill_the_whole_refresh():
    """`volume_quote_prices.usd` is nullable and the map was built by calling float() on it, so ONE
    NULL raised TypeError before a single pool was read and every coin with valid stablecoin
    pricing went unrefreshed with it."""
    from scripts.refresh_pool_depth import usable_quote_prices

    prices, skipped = usable_quote_prices([("0xGOOD", 1.0), ("0xNULL", None), ("0xUSDC", 0.999)])
    assert prices == {"0xgood": 1.0, "0xusdc": 0.999}
    assert skipped == 1


@pytest.mark.parametrize("bad", [None, "", "n/a", float("nan"), 0.0, -1.0])
def test_an_unusable_price_is_absent_never_zero(bad):
    """A zero-priced quote unit would value its pools at nothing and write a confident $0 depth."""
    from scripts.refresh_pool_depth import usable_quote_prices

    prices, skipped = usable_quote_prices([("0xT", bad)])
    assert prices == {} and skipped == 1


def test_a_map_that_ends_up_empty_is_still_an_empty_map_not_an_exception():
    from scripts.refresh_pool_depth import usable_quote_prices

    assert usable_quote_prices([]) == ({}, 0)


# --- F4: a fresh pool read cannot make an old conversion price fresh ----------------------------

def test_a_stale_quote_unit_leaves_its_pools_unvalued():
    """4663 carried a quote unit 74,687 s old (20.7 h) beside one 18.6 s old, and the refresh
    valued pools with either. A price we cannot trust is UNKNOWN, so the token leaves the map."""
    from scripts.refresh_pool_depth import usable_quote_prices

    now = 1_790_180_000.0
    prices, skipped, observed, oldest = usable_quote_prices(
        [("0xFRESH", 1.0, now - 18.6), ("0xSTALE", 2_500.0, now - 74_687.0)],
        now=now, max_age_s=3600.0)
    assert prices == {"0xfresh": 1.0}
    assert skipped == 1
    assert observed == {"0xfresh": now - 18.6}
    assert oldest == pytest.approx(now - 18.6)


def test_a_null_observation_is_infinitely_old_not_fresh():
    from scripts.refresh_pool_depth import usable_quote_prices

    now = 1_790_180_000.0
    prices, skipped, _observed, _oldest = usable_quote_prices(
        [("0xT", 1.0, None)], now=now, max_age_s=3600.0)
    assert prices == {} and skipped == 1


def test_without_an_age_ceiling_the_map_is_the_old_two_value_shape():
    """The F3 callers keep working: the age legs run only when a `now` and a ceiling are given."""
    from scripts.refresh_pool_depth import usable_quote_prices

    assert usable_quote_prices([("0xT", 1.0, 1.0)]) == ({"0xt": 1.0}, 0)


def test_a_depth_row_is_as_old_as_its_oldest_evidence(tmp_path):
    """`as_of` is what `pool_impact_inputs` ages a row by, so a depth derived from an old price must
    carry the PRICE's stamp. With the wall clock it satisfied every freshness check downstream."""
    import sqlite3

    import sniper.persistence.db_memecoin as db
    from scripts.refresh_pool_depth import _iso_utc

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    db.ensure_pool_depth_cache(conn)
    row = {"quote_decimals": 6, "token0": "0xq", "quote": "0xq", "pool": "0xpool"}
    state = pool_state_row(row, 13517971155303911452229632, 59147624213780158, 1.0)
    now_iso = "2026-09-23T08:00:00+00:00"
    stale_price_epoch = 1_790_100_000.0            # hours before the read
    db.record_pool_depth(conn, "4663", "0xstale", depth_usd=500.0, pool="0xpool", quote="0xq",
                         pools_read=4, now_iso=_iso_utc(stale_price_epoch), state=state)
    db.record_pool_depth(conn, "4663", "0xfresh", depth_usd=500.0, pool="0xpool", quote="0xq",
                         pools_read=4, now_iso=now_iso, state=state)
    at = "2026-09-23T08:05:00+00:00"
    assert db.pool_impact_inputs(conn, "4663", "0xfresh", now_iso=at, max_age_s=3600) is not None
    assert db.pool_impact_inputs(conn, "4663", "0xstale", now_iso=at, max_age_s=3600) is None, \
        "a row dated by its oldest evidence must age out, not read fresh"
    conn.close()


def test_the_refresh_stamps_the_price_time_and_not_the_wall_clock():
    """A SOURCE GUARD: the whole defect is one call passing `real_utc_now_iso()` again."""
    from pathlib import Path
    src = Path("scripts/refresh_pool_depth.py").read_text()
    assert "now_iso=as_of_iso" in src
    assert "price_seen = price_observed.get" in src
