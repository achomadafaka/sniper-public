from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_market_state import classify_funded_entry
from sniper.settings import memecoin as cfg


DECISION = "2026-07-18T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _iso(seconds_before):
    at = datetime.fromisoformat(DECISION) - timedelta(seconds=seconds_before)
    return at.isoformat()


def _quote(conn, *, seconds_before, price, network="solana", mint="MINT", quality="ok"):
    seq = conn.execute("SELECT COUNT(*) FROM memecoin_quote_observations_v2").fetchone()[0] + 1
    conn.execute(
        "INSERT INTO memecoin_quote_observations_v2("
        "sweep_id,network_id,mint,observed_at,price,quality,contexts,capture_version) "
        "VALUES(?,?,?,?,?,?,?,2)",
        (f"q{seq}", network, mint, _iso(seconds_before), price, quality, "[]"),
    )


def _feed(conn, *, seconds_before, coin, buyer, network="solana"):
    seq = conn.execute("SELECT COUNT(*) FROM memecoin_feed_observations_v2").fetchone()[0] + 1
    conn.execute(
        "INSERT INTO memecoin_feed_observations_v2("
        "upstream_event_id,raw_type,normalized_side,source_user_id,network_id,mint,"
        "observed_at,raw_json,clock_domain) VALUES(?,?,?,?,?,?,?,?,?)",
        (f"e{seq}", "buy", "buy", buyer, network, coin, _iso(seconds_before), "{}",
         "observed_real_utc"),
    )


def _classify(conn, **overrides):
    args = {
        "network_id": "solana",
        "mint": "MINT",
        "decision_at": DECISION,
        "signal_source": "global_feed",
        "effective_mcap": 500_000.0,
        "assumed_micro": False,
        "market_stats": {},
        "base_clip_usd": 100.0,
        "consensus_n_60m": 0,
    }
    args.update(overrides)
    return classify_funded_entry(conn, **args)


@pytest.mark.parametrize(("mcap", "assumed", "band"), [
    (None, True, "assumed_nano"),
    (49_999.99, False, "nano"),
    (50_000.0, False, "micro"),
    (249_999.99, False, "micro"),
    (250_000.0, False, "small"),
    (999_999.99, False, "small"),
    (1_000_000.0, False, "mid"),
    (1_999_999.99, False, "mid"),
    (2_000_000.0, False, "out_of_scope"),
])
def test_coin_band_boundaries(mcap, assumed, band):
    result = _classify(_conn(), effective_mcap=mcap, assumed_micro=assumed)
    assert result.coin_band == band
    assert result.facts["effective_mcap"] is mcap or result.facts["effective_mcap"] == mcap
    if mcap is None:
        assert result.facts["effective_mcap"] is None


@pytest.mark.parametrize(("age", "price_rows", "expected"), [
    (3600.0, (), "fresh"),
    (3600.01, (), "young"),
    (86400.0, (), "young"),
    (86400.01, (), "mature"),
    (None, (), "unknown"),
    (90000.0, ((120, 1.0), (60, 1.1), (30, 1.25)), "revival"),
])
def test_lifecycle_boundaries(age, price_rows, expected):
    conn = _conn()
    for seconds_before, price in price_rows:
        _quote(conn, seconds_before=seconds_before, price=price)
    result = _classify(conn, market_stats={"pair_age_s": age, "price": 1.25})
    assert result.lifecycle == expected


@pytest.mark.parametrize(("liquidity", "base", "expected"), [
    (None, 100.0, "unknown"),
    (1_999.99, 100.0, "thin"),
    (2_000.0, 100.0, "balanced"),
    (9_999.99, 100.0, "balanced"),
    (10_000.0, 100.0, "deep"),
    (10_000.0, None, "unknown"),
])
def test_liquidity_is_relative_to_unscaled_base_clip(liquidity, base, expected):
    result = _classify(_conn(), market_stats={"liquidity": liquidity}, base_clip_usd=base)
    assert result.liquidity_class == expected



def test_a_class_built_from_our_own_pool_store_says_so(monkeypatch):
    """F6 recheck, 2026-09-23. `_fill_depth_from_pool_store` substitutes the VIRTUAL local
    liquidity 2*L*sqrt(P) of our cached pool state whenever the provider returned no pair, and
    every consumer then read it as if a provider had measured it. The class is worth keeping --
    the operator asked for it on 2026-09-22 because 2,071 of 2,254 funded 4663 attempts read
    `unknown` -- but it has to carry WHICH number produced it.

    The prefix stays `liquidity:` so every existing reader keeps matching."""
    result = _classify(_conn(), market_stats={"liquidity": 1_000.0,
                                              "liquidity_source": "pool_store"})
    assert result.liquidity_class == "thin"
    assert "liquidity:thin:pool_store" in result.reason_codes
    assert result.facts["liquidity_source"] == "pool_store"


def test_a_provider_measured_class_carries_no_source_label():
    """The other side: a number the provider actually published is labelled by its absence, and
    the reason code keeps the exact shape every earlier row used."""
    result = _classify(_conn(), market_stats={"liquidity": 1_000.0})
    assert "liquidity:thin" in result.reason_codes
    assert not any(r.startswith("liquidity:thin:") for r in result.reason_codes)
    assert result.facts["liquidity_source"] is None


def test_finalizing_against_the_real_clip_keeps_the_source_label():
    """`finalize_funded_classification` recomputes the class once the base clip is known -- which
    on 246 of 247 store-sourced 4663 attempts is the only reason the recorded class is still
    `unknown`. The recomputed reason must not silently lose the label."""
    from sniper.services.memecoin_market_state import finalize_funded_classification
    first = _classify(_conn(), market_stats={"liquidity": 1_000.0,
                                             "liquidity_source": "pool_store"},
                      base_clip_usd=None)
    assert first.liquidity_class == "unknown"
    final = finalize_funded_classification(first, base_clip_usd=100.0)
    assert final.liquidity_class == "thin"
    assert "liquidity:thin:pool_store" in final.reason_codes

def test_momentum_uses_exact_prior_tape_and_excludes_bad_or_future_rows():
    conn = _conn()
    _quote(conn, seconds_before=120, price=1.0)
    _quote(conn, seconds_before=60, price=2.0)
    _quote(conn, seconds_before=30, price=1.3)
    _quote(conn, seconds_before=-1, price=100.0)
    _quote(conn, seconds_before=20, price=100.0, network="4663")
    _quote(conn, seconds_before=10, price=100.0, quality="ratio_outlier")

    result = _classify(conn, market_stats={"price": 1.3})

    assert result.momentum_phase == "post_peak"
    assert result.facts["momentum_observations"] == 4
    assert result.facts["runup_pct"] == pytest.approx(100.0)
    assert result.facts["giveback_pct"] == pytest.approx(35.0)
    assert result.facts["peak_price"] == 2.0


@pytest.mark.parametrize(("rows", "current", "expected"), [
    (((20, 1.0), (10, 1.1)), 1.1, "unknown"),
    (((60, 1.0), (20, 1.04), (0, 1.09)), 1.09, "flat"),
    (((60, 1.0), (30, 1.1), (0, 1.2)), 1.2, "climbing"),
    (((60, 1.0), (30, 1.5), (0, 2.0)), 2.0, "extended"),
])
def test_momentum_states(rows, current, expected):
    conn = _conn()
    for seconds_before, price in rows:
        if seconds_before:
            _quote(conn, seconds_before=seconds_before, price=price)
    result = _classify(conn, market_stats={"price": current})
    assert result.momentum_phase == expected


def _baseline(conn, *, coins=3, buyers=3, buckets=10):
    for bucket in range(1, buckets + 1):
        # Complete baseline starts before the current 15-minute window.
        offset = 900 + bucket * 900 - 30
        for idx in range(max(coins, buyers)):
            _feed(
                conn,
                seconds_before=offset + idx,
                coin=f"B{bucket}-C{idx % coins}",
                buyer=f"B{bucket}-U{idx % buyers}",
            )


def test_hot_broad_regime_and_source_lane_are_explicit():
    conn = _conn()
    _baseline(conn, coins=3, buyers=3)
    for idx in range(6):
        _feed(conn, seconds_before=60 + idx, coin=f"NOW{idx}", buyer=f"U{idx % 5}")

    result = _classify(conn, signal_source="follow_activity")

    assert result.market_regime == "hot_broad"
    assert result.risk_scale == 1.0
    assert result.signal_source == "follow_activity"
    assert result.facts["signal_source"] == "follow_activity"


def test_hot_narrow_thin_normal_and_unknown_regimes():
    cases = []
    for current_coins, current_buyers, consensus, expected in (
        (1, 1, 2, "hot_narrow"),
        (1, 1, 0, "thin"),
        (3, 3, 0, "normal"),
    ):
        conn = _conn()
        _baseline(conn, coins=3, buyers=3)
        for idx in range(max(current_coins, current_buyers)):
            _feed(
                conn,
                seconds_before=60 + idx,
                coin=f"NOW{idx % current_coins}",
                buyer=f"U{idx % current_buyers}",
            )
        cases.append((_classify(conn, consensus_n_60m=consensus), expected))

    stale = _conn()
    _baseline(stale)
    _feed(stale, seconds_before=181, coin="NOW", buyer="U")
    cases.append((_classify(stale), "unknown"))

    for result, expected in cases:
        assert result.market_regime == expected
        assert result.risk_scale == (1.0 if expected == "normal" else 0.75)


def test_classifier_read_failure_is_explicit_and_fails_open_to_existing_gates():
    class BrokenConn:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("boom")

    result = _classify(BrokenConn(), market_stats={"pair_age_s": 100, "liquidity": 10_000})

    assert result.market_regime == "unknown"
    assert result.momentum_phase == "unknown"
    assert result.risk_scale == 0.75
    assert "classification_read_failed" in result.reason_codes
    assert result.data_quality == "critical_unknown"


def _post_peak_classification(*, phase="post_peak", peak=2.0, display=1.3,
                              runup=100.0, giveback=35.0):
    result = _classify(_conn())
    return {
        **result.to_dict(),
        "momentum_phase": phase,
        "facts": {
            **result.facts,
            "peak_price": peak,
            "display_price": display,
            "runup_pct": runup,
            "giveback_pct": giveback,
        },
    }


def test_post_peak_confirmation_never_blocks_without_nomination():
    from sniper.services.memecoin_market_state import confirm_post_peak

    result = confirm_post_peak(
        _post_peak_classification(phase="climbing"), exact_quote_price=1.0,
    )

    assert result.nominated is False
    assert result.confirmed is False
    assert result.reason == "not_nominated"


@pytest.mark.parametrize("exact", [None, 0.0, -1.0, float("nan"), float("inf"), "bad"])
def test_post_peak_nomination_fails_open_without_comparable_exact_price(exact):
    from sniper.services.memecoin_market_state import confirm_post_peak

    result = confirm_post_peak(_post_peak_classification(), exact_quote_price=exact)

    assert result.nominated is True
    assert result.confirmed is False
    assert result.reason == "exact_price_unavailable"


def test_post_peak_confirmation_uses_exact_route_giveback_threshold(monkeypatch):
    from sniper.services.memecoin_market_state import confirm_post_peak

    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT", 30.0)
    below = confirm_post_peak(_post_peak_classification(), exact_quote_price=1.41)
    boundary = confirm_post_peak(_post_peak_classification(), exact_quote_price=1.40)

    assert below.confirmed is False
    assert below.exact_giveback_pct == pytest.approx(29.5)
    assert below.reason == "exact_giveback_below_threshold"
    assert boundary.confirmed is True
    assert boundary.exact_giveback_pct == pytest.approx(30.0)
    assert boundary.reason == "post_peak_confirmed"


def test_regime_read_uses_the_observed_at_index():
    """210 ms per funded Solana entry on the live ledger (2026-09-26): julianday() alone cannot use
    an index. The regime read must carry a plain observed_at bound the planner can use."""
    conn = _conn()
    captured = []
    conn.set_trace_callback(captured.append)
    _classify(conn)
    conn.set_trace_callback(None)
    sql = next(s for s in captured if "FROM memecoin_feed_observations_v2" in s
               and "julianday" in s)
    plan = " ".join(str(tuple(r)) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "idx_mc_feed_v2_observed_at" in plan, plan


def test_regime_index_bound_keeps_a_row_stamped_with_a_non_utc_offset():
    """The coarse string bound must never drop a row the exact julianday bound keeps."""
    conn = _conn()
    _baseline(conn)
    base = conn.execute("SELECT COUNT(*) FROM memecoin_feed_observations_v2").fetchone()[0]
    before = _classify(conn).facts["regime_current_coins"]
    # 30 s before the decision, written as local +05:00 time (a later calendar string).
    at = (datetime.fromisoformat(DECISION) - timedelta(seconds=30)).astimezone(
        timezone(timedelta(hours=5))).isoformat()
    conn.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id,raw_type,normalized_side,"
        "source_user_id,network_id,mint,observed_at,raw_json) VALUES(?,?,?,?,?,?,?,?)",
        ("offset-row", "buy", "buy", "offset-buyer", "solana", "OFFSETCOIN", at, "{}"))
    assert conn.execute("SELECT COUNT(*) FROM memecoin_feed_observations_v2").fetchone()[0] == base + 1
    assert _classify(conn).facts["regime_current_coins"] == before + 1


@pytest.mark.parametrize("offset", [-23, -12, 0, 14, 23])
@pytest.mark.parametrize("separator", ["T", " "])
def test_regime_prefilter_preserves_full_classification_at_time_boundaries(offset, separator):
    conn = _conn()
    _baseline(conn)
    decision = datetime.fromisoformat(DECISION)
    start = decision - timedelta(seconds=cfg.MEMECOIN_FUNDED_REGIME_BUCKET_S *
                                  (cfg.MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS + 1))
    for i, at in enumerate((start - timedelta(milliseconds=1), start,
                           start + timedelta(milliseconds=1), decision - timedelta(milliseconds=1),
                           decision, decision + timedelta(milliseconds=1))):
        stamp = at.astimezone(timezone(timedelta(hours=offset))).isoformat(sep=separator)
        conn.execute("INSERT INTO memecoin_feed_observations_v2 "
                     "(upstream_event_id,raw_type,normalized_side,source_user_id,network_id,mint,observed_at,raw_json,clock_domain) "
                     "VALUES (?,'buy','buy',?,'solana',?,?, '{}','observed_real_utc')",
                     (f"boundary-{i}", f"buyer-{i}", f"COIN-{i}", stamp))

    class WithoutPrefilter:
        def execute(self, sql, args=()):
            if "WHERE observed_at>=? AND normalized_side='buy'" in sql:
                sql = sql.replace("WHERE observed_at>=? AND normalized_side='buy'", "WHERE normalized_side='buy'")
                args = args[1:]
            return conn.execute(sql, args)

    assert _classify(conn) == _classify(WithoutPrefilter())


def _market(kind):
    """A ledger whose market-wide regime is `kind` (no per-mint momentum, no consensus)."""
    if kind == "broken":
        class BrokenConn:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("boom")
        return BrokenConn()
    conn = _conn()
    if kind == "unknown":
        return conn
    _baseline(conn, coins=3, buyers=3)
    now = {"hot_broad": (6, 5), "thin": (1, 1), "normal": (3, 3)}[kind]
    for idx in range(max(now)):
        _feed(conn, seconds_before=60 + idx, coin=f"NOW{idx % now[0]}", buyer=f"U{idx % now[1]}")
    return conn


@pytest.mark.parametrize("kind", ["hot_broad", "thin", "normal", "unknown", "broken"])
def test_the_market_scale_is_the_scale_classify_gives_a_coin_with_no_momentum(kind):
    """The slot lane sizes its prefetched leg with `market_risk_scale` BEFORE any signal, so it must
    equal what classify_funded_entry gives the lane's buy (no consensus, no price history) at the
    same instant, including when the regime read fails."""
    from sniper.services.memecoin_market_state import market_risk_scale
    conn = _market(kind)
    expected = _classify(conn, signal_source="slot_lane", consensus_n_60m=None).risk_scale
    assert market_risk_scale(conn, decision_at=DECISION, signal_source="slot_lane") == expected
    scaled = cfg.MEMECOIN_FUNDED_REGIME_RISK_SCALE
    assert expected == (1.0 if kind in ("hot_broad", "normal") else scaled)


def test_a_per_coin_hot_narrow_is_the_one_scale_the_market_read_cannot_predict():
    """In a normal market a coin with consensus scales down; the market read says 1.0, so the lane's
    full leg no longer matches and the buy falls back (the lane's scale check)."""
    from sniper.services.memecoin_market_state import market_risk_scale
    conn = _market("normal")
    assert market_risk_scale(conn, decision_at=DECISION, signal_source="slot_lane") == 1.0
    assert _classify(conn, signal_source="slot_lane", consensus_n_60m=2).risk_scale == 0.75
