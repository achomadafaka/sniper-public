"""A long-held funded position stops taking an automatic SLOT (operator 2026-10-07: "long hold
positions can be ignored, also i dont want them to be counted as using a slot since they act like
dust"). Funded non-manual closes since 09-15 (n=717): hold p50 88 s, p95 1,067 s; the 21 held
>= 1,800 s closed at -31% on entry.

MEMECOIN_SLOT_FREE_AFTER_S (code default 0 = off): a position open at least that long is left out of
`automatic_funded_count`, the one count both slot gates read. Unchanged: the exposure cap (cost
basis), the gas-reserve count, one-position-per-coin, reservations, exits."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as evm_risk

NOW = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    import sniper.settings.memecoin as cfg
    import sniper.util.clock as clock
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_FREE_AFTER_S", 1800.0, raising=False)
    monkeypatch.setattr(clock, "real_utc_now_iso", lambda: NOW.isoformat())
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, mint, *, age_s=None, opened_at=None, network="4663", source="onchain_watch"):
    if opened_at is None:
        opened_at = (NOW - timedelta(seconds=age_s)).isoformat()
    m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                    entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                    entry_lag_s=1.0, their_size_usd=100.0, opened_at=opened_at,
                    strategy="alpha_b", execution_mode="real", network_id=network,
                    signal_source=source)


def test_a_position_held_past_the_bound_takes_no_slot(db):
    _open(db, "0xold", age_s=2000)
    _open(db, "0xnew", age_s=100)
    assert m.automatic_funded_count(db, network="4663") == 1
    assert m.automatic_funded_count(db) == 1


def test_the_bound_is_inclusive_and_per_chain_and_global_agree(db):
    _open(db, "0xedge", age_s=1800)
    _open(db, "SolMint", age_s=1799, network="solana")
    assert m.automatic_funded_count(db, network="4663") == 0
    assert m.automatic_funded_count(db, network="solana") == 1
    assert m.automatic_funded_count(db) == 1


@pytest.mark.parametrize("opened_at", ["", "not a time", "2026-10-07T17:00:00+00:00"])
def test_an_unknown_or_future_open_time_still_takes_a_slot(db, opened_at):
    _open(db, "0xodd", opened_at=opened_at)
    assert m.automatic_funded_count(db, network="4663") == 1


def test_a_reservation_always_takes_a_slot(db):
    m.reserve_pending(db, mint="0xres", source_user_id="u", strategy="s", notional=20.0,
                      reserved_at=(NOW - timedelta(hours=5)).isoformat(), network_id="4663")
    assert m.automatic_funded_count(db, network="4663") == 1


def test_zero_restores_todays_count(db, monkeypatch):
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_FREE_AFTER_S", 0.0, raising=False)
    _open(db, "0xold", age_s=99999)
    assert m.automatic_funded_count(db, network="4663") == 1


def test_exposure_gas_count_and_coin_dedup_still_see_the_old_bag(db):
    _open(db, "0xold", age_s=5000)
    assert m.funded_book_snapshot(db)["notional"] == pytest.approx(50.0), "exposure keeps cost"
    assert m.real_open_snapshot(db, network="4663")["count"] == 1, "gas reserve still counts it"
    assert ("4663", "0xold") in {(n, mi) for n, mi in m.real_open_coin_ids(db)}, "one per coin"


def test_the_evm_gate_reads_the_freed_count(db):
    _open(db, "0xold", age_s=5000)
    ctx = evm_risk.build_evm_risk_context(db, usdc_balance=500.0, native_balance=1.0,
                                          day=NOW.date().isoformat(), network_id="4663")
    assert ctx.effective_concurrent == 0 and ctx.chain_automatic_count == 0


def test_a_naive_open_time_is_read_as_utc(db):
    _open(db, "0xnaive", opened_at=(NOW - timedelta(seconds=4000)).replace(tzinfo=None).isoformat())
    assert m.automatic_funded_count(db, network="4663") == 0


def test_the_stuck_bag_report_reads_the_gates_count(db):
    from sniper.services.memecoin_stuck_bags import stuck_bag_report
    _open(db, "0xold", age_s=5000)
    _open(db, "0xnew", age_s=10)
    assert stuck_bag_report(db, max_slots=2, now_iso=NOW.isoformat())["slots_used"] == 1
