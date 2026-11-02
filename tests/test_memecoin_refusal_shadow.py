"""Tests for the refusal shadow lane. They guard the EXIT CODE, not the happy path.

Spec: Alpha, 2026-09-23, HANDOFF.md "REFUSAL SHADOW LANE SPEC".
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.services import memecoin_refusal_shadow as shadow

Q96 = 2 ** 96


class Policy:
    """The live shape of FundedExitPolicy, with the values the funded runner carried on 2026-09-23."""
    def __init__(self, tp_pct=25.0, stop_pct=35.0, stop_debounce_sweeps=2, catastrophe_pct=80.0,
                 max_hold_s=14400.0):
        self.tp_pct = tp_pct
        self.stop_pct = stop_pct
        self.stop_debounce_sweeps = stop_debounce_sweeps
        self.catastrophe_pct = catastrophe_pct
        self.max_hold_s = max_hold_s


def bars(prices, *, start=1000, step=15):
    """Bars at `step`-second spacing: (epoch, price, liquidity, sqrt_price)."""
    return [(start + i * step, p, 10 ** 18, Q96) for i, p in enumerate(prices)]


# --- the family filter --------------------------------------------------------------------------

def test_market_families_are_admitted_and_governance_is_not():
    assert shadow.is_market_family("mcap over ceiling")
    assert shadow.is_market_family("quiet_hours")
    for fam in ("not_approved", "disarmed", "unroutable_chain", "kill switch", "filled"):
        assert not shadow.is_market_family(fam)


def test_the_retired_bare_mcap_band_is_excluded_structurally():
    """`family()` sends the retired bare strings to an `other:` name, and no `other:` is admitted.

    Measured 2026-09-23: 42 Solana, 33 on chain 56 and 13 on 4663 refusals at >=$700 over 14 days
    still carry the bare `over_ceiling` string. Admitting it would merge a retired gate with the
    live `mcap over ceiling` one, which is the defect Gamma's 2026-08-26 spec had to correct.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "scripts", "research"))
    from refusal_family_outcomes import family
    assert family("over_ceiling").startswith("other:")
    assert not shadow.is_market_family(family("over_ceiling"))
    assert shadow.is_market_family(family("mcap $773,993 over ceiling $250,000 (over_ceiling)"))


def test_no_other_family_is_ever_admitted():
    for fam in shadow.MARKET_FAMILIES:
        assert not fam.startswith("other:")


# --- the price decode ---------------------------------------------------------------------------

def test_a_missing_base_decimal_still_prices_and_is_labelled_atomic():
    """Requiring both decimals would discard ~90% of ticked pools; the factor cancels in a ratio."""
    price = shadow.sqrt_price_to_quote_per_token(Q96, quote_side=0, token0_decimals=18,
                                                 token1_decimals=None)
    assert price == pytest.approx(1.0)
    assert shadow.price_basis(0, 18, None) == "atomic_ratio"
    assert shadow.price_basis(1, 18, 6) == "quote_per_token"


def test_an_unreadable_pool_is_none_never_zero():
    for bad in ({"sqrt_price_x96": 0}, {"sqrt_price_x96": -1}, {"sqrt_price_x96": None},
                {"quote_side": 7}, {"quote_side": None}):
        kw = dict(sqrt_price_x96=Q96, quote_side=1, token0_decimals=18, token1_decimals=18)
        kw.update(bad)
        assert shadow.sqrt_price_to_quote_per_token(**kw) is None


def test_the_decimal_shift_is_applied_when_both_sides_are_known():
    # base 18, quote 6 -> the atomic ratio is scaled by 10**12.
    assert shadow.sqrt_price_to_quote_per_token(
        Q96, quote_side=1, token0_decimals=18, token1_decimals=6) == pytest.approx(1e12)


def test_a_second_with_several_swaps_keeps_the_last_one():
    ticks = [(100.1, Q96, 1), (100.9, Q96 * 2, 1), (101.0, Q96 * 3, 1)]
    out = shadow.bars_1s(ticks, quote_side=1, token0_decimals=18, token1_decimals=18)
    assert [b[0] for b in out] == [100, 101]
    assert out[0][3] == Q96 * 2           # the later swap in second 100 wins


# --- the exit simulation ------------------------------------------------------------------------

def test_take_profit_fires_at_the_live_threshold_not_the_policy_name():
    """follow-30-50-v1 was minted at +30/-50 and runs at +25/-35; the knobs decide."""
    price, reason, after = shadow.simulate_exit(
        bars([1.0, 1.10, 1.26]), fill_epoch=1000, fill_price=1.0, policy=Policy())
    assert reason == "tp" and price == pytest.approx(1.26) and after == pytest.approx(30)


def test_the_stop_needs_two_consecutive_sweeps_and_a_recovery_resets_the_count():
    one_dip = shadow.simulate_exit(bars([1.0, 0.6, 0.9, 0.95]), fill_epoch=1000, fill_price=1.0,
                                  policy=Policy())
    assert one_dip[1] == "open_at_horizon", "a single breaching sweep must not sell"
    two = shadow.simulate_exit(bars([1.0, 0.6, 0.6, 0.9]), fill_epoch=1000, fill_price=1.0,
                               policy=Policy())
    # The first sweep is at fill+15 and the second at fill+30, so the second consecutive breach
    # sells at +30 s, not +45 s.
    assert two[1] == "stop" and two[2] == pytest.approx(30)


def test_the_catastrophe_floor_fires_immediately_with_no_debounce():
    price, reason, after = shadow.simulate_exit(
        bars([1.0, 0.1]), fill_epoch=1000, fill_price=1.0, policy=Policy())
    assert reason == "catastrophe" and after == pytest.approx(15)


def test_a_position_open_at_the_horizon_is_censored_never_closed_at_the_last_bar():
    price, reason, after = shadow.simulate_exit(
        bars([1.0, 1.05, 0.9]), fill_epoch=1000, fill_price=1.0, policy=Policy(), horizon_s=45)
    assert reason == "open_at_horizon" and after is None


def test_a_zero_fill_never_divides():
    assert shadow.simulate_exit(bars([1.0]), fill_epoch=1000, fill_price=0.0,
                                policy=Policy())[1] == "no_fill"
    assert shadow.pnl(0.0, 1.0, 57.5) == (None, None)
    assert shadow.pnl(1.0, 1.25, 57.5)[0] == pytest.approx(14.375)


def test_a_pool_that_stops_trading_holds_its_last_spot_price():
    """A V3 spot price only moves on a swap, so silence is a flat price, not a fall.

    This biases AGAINST the stop firing, in the same optimistic direction as the missing exit
    impact. A coin that became untradeable models as flat and lands in `open_at_horizon`.
    """
    out = shadow.simulate_exit(bars([1.0, 0.9]), fill_epoch=1000, fill_price=1.0,
                               policy=Policy(), horizon_s=600)
    assert out[1] == "open_at_horizon"


# --- the fill model -----------------------------------------------------------------------------

def test_the_fill_pays_impact_and_an_uncomputable_impact_is_unknown_not_zero():
    b = bars([1.0, 1.0, 1.0], step=5)          # bars at 1000, 1005, 1010
    at_signal, fill, impact = shadow.model_fill(
        b, decision_epoch=1002, fill_lag_s=6.0, clip_usd=57.5, quote_is_token0=False,
        quote_decimals=18, quote_usd=2732.82)
    assert at_signal == pytest.approx(1.0)
    assert impact is not None and impact > 0
    assert fill > 1.0, "a buy moves the pool against itself"
    _, fill2, impact2 = shadow.model_fill(
        b, decision_epoch=1002, fill_lag_s=6.0, clip_usd=57.5, quote_is_token0=False,
        quote_decimals=18, quote_usd=None)        # no USD price for the quote unit
    assert impact2 is None and fill2 == pytest.approx(1.0)


def test_no_bar_before_the_fill_means_no_fill_not_a_guessed_one():
    b = bars([1.0], start=2000)
    at_signal, fill, impact = shadow.model_fill(
        b, decision_epoch=1000, fill_lag_s=6.0, clip_usd=57.5, quote_is_token0=False,
        quote_decimals=18, quote_usd=1.0)
    assert at_signal is None and fill is None and impact is None


# --- the store ----------------------------------------------------------------------------------

ROW = {"network_id": "4663", "mint": "0xabc", "origin_event_id": "swap:x:1", "attempt_id": "a1",
       "family": "mcap over ceiling", "terminal_reason": "mcap $1 over ceiling $2",
       "terminal_status": "refused", "source_size_usd": 2996.97,
       "decision_started_at": "2026-09-23T09:28:48.004126+00:00",
       "signal_received_at": "2026-09-23T09:28:48.001415+00:00"}


def test_the_identity_is_network_mint_and_origin(tmp_path):
    conn = shadow.connect(str(tmp_path / "s.db"))
    assert shadow.enqueue(conn, ROW, enqueued_at="2026-09-23T10:00:00+00:00") is True
    assert shadow.enqueue(conn, ROW, enqueued_at="2026-09-23T10:01:00+00:00") is False, \
        "a repeat of the same origin must not double-capture"
    other = dict(ROW, origin_event_id="swap:x:2")
    assert shadow.enqueue(conn, other, enqueued_at="2026-09-23T10:01:00+00:00") is True
    assert conn.execute("SELECT COUNT(*) FROM memecoin_refusal_shadow").fetchone()[0] == 2


def test_the_same_mint_on_two_chains_is_two_rows(tmp_path):
    conn = shadow.connect(str(tmp_path / "s.db"))
    shadow.enqueue(conn, ROW, enqueued_at="2026-09-23T10:00:00+00:00")
    assert shadow.enqueue(conn, dict(ROW, network_id="8453"),
                          enqueued_at="2026-09-23T10:00:00+00:00") is True


def test_a_broken_write_never_raises_into_the_caller(tmp_path):
    """This lane reads the funded ledger. A throw here must not be able to delay anything funded."""
    conn = shadow.connect(str(tmp_path / "s.db"))
    conn.execute("DROP TABLE memecoin_refusal_shadow")
    conn.commit()
    assert shadow.enqueue(conn, ROW, enqueued_at="2026-09-23T10:00:00+00:00") is False
    assert shadow.record_sample(conn, 1, sampled=1) is False


def test_a_pending_row_starts_at_minus_one_so_it_is_not_a_measured_zero(tmp_path):
    conn = shadow.connect(str(tmp_path / "s.db"))
    shadow.enqueue(conn, ROW, enqueued_at="2026-09-23T10:00:00+00:00")
    assert conn.execute("SELECT sampled FROM memecoin_refusal_shadow").fetchone()[0] == -1


def test_record_sample_writes_only_known_columns(tmp_path):
    conn = shadow.connect(str(tmp_path / "s.db"))
    shadow.enqueue(conn, ROW, enqueued_at="2026-09-23T10:00:00+00:00")
    rid = conn.execute("SELECT id FROM memecoin_refusal_shadow").fetchone()[0]
    assert shadow.record_sample(conn, rid, sampled=1, sample_reason="ok", pnl_usd=1.5,
                                nonsense_column="dropped") is True
    row = conn.execute("SELECT sampled, sample_reason, pnl_usd FROM memecoin_refusal_shadow "
                       "WHERE id=?", (rid,)).fetchone()
    assert row == (1, "ok", 1.5)


def test_the_sampler_opens_nothing_but_its_own_store(tmp_path, monkeypatch):
    """A guard against the whole class: this lane must never hold a writable funded handle."""
    opened = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        opened.append(str(target))
        return real(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    conn = shadow.connect(str(tmp_path / "s.db"))
    shadow.enqueue(conn, ROW, enqueued_at="2026-09-23T10:00:00+00:00")
    assert opened and all("sniper_tracker.db" not in t for t in opened)
    assert all("memecoin_onchain_volume.db" not in t for t in opened)


def test_a_readonly_store_cannot_be_written(tmp_path):
    path = str(tmp_path / "s.db")
    shadow.connect(path).close()
    ro = shadow.connect(path, readonly=True)
    assert shadow.enqueue(ro, ROW, enqueued_at="2026-09-23T10:00:00+00:00") is False
