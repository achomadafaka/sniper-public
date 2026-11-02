"""Sizing values an open position at its last FRESH executable mark; exposure keeps cost basis.

THE BUG. Sizing equity was `cash + COST BASIS of open positions`, so a bag that had gone to nothing
kept its full purchase price in the equity that decides the NEXT clip. Live on 2026-09-06: LARP
(position 50693) cost $75.22, its last executable mark was $5.08, and tracking had been paused since
2026-09-04T20:36. With $195.90 of 4663 cash over three slots that produced a $90.37 clip where
honest equity gives $65.30 — every later 4663 bet 38% too large, funded by an asset we could not
sell. Solana on comparable cash was sizing $65.11, which is the shape the fix restores.

THE CARVE-OUT. `MEMECOIN_MAX_TOTAL_EXPOSURE_USD` must keep reading COST. The cap bounds money
COMMITTED, not money recoverable: a dead bag has spent its capital and must keep occupying the cap
until it is actually closed. If marks fed the cap it would loosen exactly when the book is full of
unsellable bags — the worst moment to allow another entry — and a fixed dollar limit would drift
with price rather than with our decisions.

Offline. Real objects, an in-memory database, no network.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_risk import real_trade_size

NOW = "2026-09-06T13:00:00+00:00"
FRESH = "2026-09-06T12:59:00+00:00"          # 60s old
STALE = "2026-09-04T20:36:16+00:00"          # 41 hours old, the live LARP stamp


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


OLD_OPEN = "2026-09-01T00:00:00+00:00"       # long past the freshness bound
JUST_OPENED = "2026-09-06T12:59:30+00:00"    # 30s old


def _open_real(c, mint, *, cost, entry_price=1.0, last_price=None, last_at=None, paused_at=None,
               network="solana", opened_at=OLD_OPEN):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                          entry_price=entry_price, entry_notional=cost, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at=opened_at)
    c.execute("UPDATE memecoin_positions SET execution_mode='real', network_id=?, "
              "last_executable_price=?, last_executable_at=?, tracking_paused_at=? WHERE id=?",
              (network, last_price, last_at, paused_at, pid))
    c.commit()
    return pid


def _snap(c, **kw):
    return m.real_open_snapshot(c, now_iso=NOW, max_age_s=300.0, **kw)


# --- the two valuations ---------------------------------------------------------------------------

def test_a_fresh_mark_values_the_position_at_the_mark_not_at_cost():
    c = _c()
    _open_real(c, "M1", cost=100.0, entry_price=1.0, last_price=0.25, last_at=FRESH)

    snap = _snap(c)

    assert snap["notional"] == pytest.approx(100.0)          # committed
    assert snap["mark_notional"] == pytest.approx(25.0)      # recoverable


def test_a_mark_above_entry_raises_the_sizing_value():
    """The rule is not a floor at zero-or-cost: a winner sizes off what it is worth now."""
    c = _c()
    _open_real(c, "M1", cost=100.0, entry_price=1.0, last_price=3.0, last_at=FRESH)

    assert _snap(c)["mark_notional"] == pytest.approx(300.0)


@pytest.mark.parametrize("kw,why", [
    ({"last_price": 0.25, "last_at": STALE}, "a stale mark"),
    ({"last_price": 0.25, "last_at": FRESH, "paused_at": "2026-09-04T20:36:17+00:00"}, "paused"),
    ({"last_price": None, "last_at": FRESH}, "no mark price"),
    ({"last_price": None, "last_at": None}, "never marked, and old enough to have been"),
    ({"last_price": 0.0, "last_at": FRESH}, "a zero mark price"),
    ({"last_price": 0.25, "last_at": None}, "no mark timestamp"),
    ({"last_price": 0.25, "last_at": "not-a-timestamp"}, "an unparseable timestamp"),
])
def test_an_unknown_position_is_worth_zero_never_cost(kw, why):
    """One-way on purpose: missing data may only SHRINK a clip, because the failure being fixed is
    a dead bag inflating the next bet."""
    c = _c()
    _open_real(c, "M1", cost=100.0, entry_price=1.0, **kw)

    snap = _snap(c)

    assert snap["mark_notional"] == 0.0, why
    assert snap["notional"] == pytest.approx(100.0), "cost basis is unaffected by " + why


def test_a_mark_stamped_in_the_future_is_not_trusted():
    c = _c()
    _open_real(c, "M1", cost=100.0, entry_price=1.0, last_price=0.25,
               last_at="2026-09-07T00:00:00+00:00")

    assert _snap(c)["mark_notional"] == 0.0


def test_the_freshness_bound_is_the_knob():
    c = _c()
    _open_real(c, "M1", cost=100.0, entry_price=1.0, last_price=0.25, last_at=FRESH)   # 60s old

    assert m.real_open_snapshot(c, now_iso=NOW, max_age_s=30.0)["mark_notional"] == 0.0
    assert m.real_open_snapshot(c, now_iso=NOW, max_age_s=120.0)["mark_notional"] \
        == pytest.approx(25.0)


def test_the_default_bound_is_five_times_the_measured_p99():
    """228 funded positions closed in the 7 days to 2026-09-06: the age of `last_executable_at` at
    close was p50 0s, p90 2s, p99 61s. The sweep itself runs p50 4s / p90 32s / max 117s."""
    from sniper.settings import memecoin as cfg

    assert cfg.MEMECOIN_SIZING_MARK_MAX_AGE_S == 300.0


# --- the carve-out Alpha asked to be pinned --------------------------------------------------------

def test_a_dead_bag_still_consumes_exposure():
    """DO NOT 'SIMPLIFY' THIS INTO ONE NUMBER. The exposure cap bounds money COMMITTED. A bag worth
    nothing has still spent its capital, and letting marks feed the cap would loosen it exactly
    when the book is full of unsellable bags."""
    c = _c()
    _open_real(c, "DEAD", cost=75.22, entry_price=1.0, last_price=0.0675, last_at=STALE,
               paused_at="2026-09-04T20:36:17+00:00")

    book = m.funded_book_snapshot(c)

    assert book["notional"] == pytest.approx(75.22), "exposure must still see the full commitment"
    assert book["open_notional"] == pytest.approx(75.22)
    assert book["mark_notional"] == 0.0
    assert book["open_mark_notional"] == 0.0


def test_a_reservation_counts_at_cost_in_both_views():
    """A reservation has no position and therefore no mark, and its cash is already committed."""
    c = _c()
    m.reserve_pending(c, mint="P1", source_user_id="u", strategy="s", notional=20.0,
                      reserved_at="t", network_id="solana")

    book = m.funded_book_snapshot(c)

    assert book["notional"] == pytest.approx(20.0)
    assert book["mark_notional"] == pytest.approx(20.0)


# --- the live LARP case, end to end ----------------------------------------------------------------

def test_the_live_larp_case_sizes_the_4663_clip_down_to_cash_alone(monkeypatch):
    """LARP cost $75.22, mark $5.08, paused 2026-09-04T20:36. With $195.90 of 4663 cash over three
    slots the old equity gave $90.37; the honest one gives $65.30, which lands on the $65.11 Solana
    was sizing on comparable cash."""
    from sniper.services import memecoin_risk as R

    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_SIZING", "bankroll", raising=False)
    monkeypatch.setattr(R.cfg, "MEMECOIN_MIN_TRADE_USD", 5.0, raising=False)
    c = _c()
    _open_real(c, "LARP", cost=75.22, entry_price=1.0, last_price=0.0675, last_at=STALE,
               paused_at="2026-09-04T20:36:17+00:00", network="4663")
    snap = _snap(c, network="4663")

    old_clip = real_trade_size(usdc_balance=195.90, open_notional_usd=snap["notional"],
                               max_concurrent=3)
    new_clip = real_trade_size(usdc_balance=195.90, open_notional_usd=snap["mark_notional"],
                               max_concurrent=3)

    assert old_clip == pytest.approx(90.37, abs=0.01)
    assert new_clip == pytest.approx(65.30, abs=0.01)
    assert new_clip < old_clip


def test_a_healthy_book_is_unchanged_by_the_fix():
    """The fix must not shrink sizing on a live, freshly marked position holding its value."""
    c = _c()
    _open_real(c, "GOOD", cost=50.0, entry_price=1.0, last_price=1.0, last_at=FRESH)

    snap = _snap(c)

    assert snap["mark_notional"] == pytest.approx(snap["notional"])


def test_an_empty_book_is_zero_in_both_views():
    assert _snap(_c()) == {"count": 0, "notional": 0.0, "mark_notional": 0.0,
                           "mints": set(), "coin_ids": set()}


# --- a position too new to have a mark is worth its COST, not zero ---------------------------------

def test_a_freshly_opened_unmarked_position_is_worth_its_cost():
    """We just paid that price, so cost IS the most recent executable evidence we hold.

    MEASURED on funded positions since 2026-08-30: time from open to a first executable mark is
    p50 90s and p90 701s, and 23 of 247 consecutive entry pairs opened within 120s of each other —
    so about one entry in ten would otherwise be sized on equity short by a whole clip.
    """
    c = _c()
    _open_real(c, "NEW", cost=40.0, opened_at=JUST_OPENED)

    assert _snap(c)["mark_notional"] == pytest.approx(40.0)


def test_its_twin_past_the_bound_is_worth_zero():
    """Past the bound the ABSENCE of a mark is evidence, not latency."""
    c = _c()
    _open_real(c, "OLD", cost=40.0, opened_at=OLD_OPEN)

    assert _snap(c)["mark_notional"] == 0.0


def test_a_freshly_opened_but_paused_position_is_still_zero():
    """Paused beats new: the operator has said this position is not being tracked."""
    c = _c()
    _open_real(c, "NEWPAUSED", cost=40.0, opened_at=JUST_OPENED,
               paused_at="2026-09-06T12:59:45+00:00")

    assert _snap(c)["mark_notional"] == 0.0


def test_a_fresh_mark_still_beats_the_cost_fallback():
    """The fallback is for the absence of a mark, never a substitute for one we have."""
    c = _c()
    _open_real(c, "BOTH", cost=100.0, entry_price=1.0, last_price=0.25, last_at=FRESH,
               opened_at=JUST_OPENED)

    assert _snap(c)["mark_notional"] == pytest.approx(25.0)


def test_an_unreadable_opened_at_falls_closed_to_zero():
    c = _c()
    _open_real(c, "BAD", cost=40.0, opened_at="not-a-timestamp")

    assert _snap(c)["mark_notional"] == 0.0


def test_the_cost_fallback_cannot_rescue_a_dead_bag():
    """The one-way rule survives the refinement: the dead-bag case is BY DEFINITION old, so it can
    never reach the cost fallback."""
    c = _c()
    _open_real(c, "LARP", cost=75.22, entry_price=1.0, last_price=0.0675, last_at=STALE,
               paused_at="2026-09-04T20:36:17+00:00", opened_at="2026-09-03T00:00:00+00:00")

    assert _snap(c)["mark_notional"] == 0.0


@pytest.mark.parametrize("network", ["solana", "4663"])
def test_risk_context_values_only_chain_local_marks(monkeypatch, network):
    from sniper.services.memecoin_risk import build_memecoin_risk_context
    from sniper.services.memecoin_evm_risk import build_evm_risk_context
    from sniper.util.clock import real_utc_now_iso

    c = _c()
    now = real_utc_now_iso()
    _open_real(c, "LOCAL", cost=10.0, last_price=0.25, last_at=now, network=network)
    _open_real(c, "REMOTE", cost=25.0, last_price=3.0, last_at=now,
               network="8453")
    valued_costs = []
    value = m._mark_value_usd

    def record_value(cost, *args):
        valued_costs.append(cost)
        return value(cost, *args)

    monkeypatch.setattr(m, "_mark_value_usd", record_value)
    if network == "solana":
        ctx = build_memecoin_risk_context(
            c, usdc_balance=300.0, sol_balance=1.0, day=now[:10])
    else:
        ctx = build_evm_risk_context(
            c, usdc_balance=300.0, native_balance=1.0, day=now[:10], network_id=network)

    assert ctx.effective_exposure_usd == 35.0
    assert ctx.chain_exposure_usd == 10.0
    assert valued_costs == [10.0]
