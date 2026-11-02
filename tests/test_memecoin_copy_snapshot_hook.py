import sqlite3
import json

import pytest
from types import SimpleNamespace
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_copy_trader import copy_event_to_arms

def _ev(mint="MINTA", price=0.001):
    return SimpleNamespace(mint=mint, side="buy", their_price=price, their_size_usd=2000.0,
                           fomo_event_id="e1", source_user_id="u1", network_id="solana",
                           source_handle="alice", ticker="AAA", their_market_cap=1_000_000.0,
                           is_first_buy=True, lag_s=1.0, signal_source="global_feed",
                           received_at_normalized="2026-07-12T10:00:00+00:00")

def test_rejected_buy_still_writes_snapshot():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    # fill_fn returns 0 -> 'ignored' branch (a gate rejection); snapshot must still be written.
    copy_event_to_arms(c, _ev(), 100.0, "2026-07-12T10:00:00+00:00", arms=["alpha_b"],
                       fill_fn=lambda *a, **k: (0.0, "x"), stats_fn=lambda *a, **k: {})
    row = c.execute("SELECT action FROM memecoin_entry_snapshots WHERE upstream_event_id='e1'").fetchone()
    assert row is not None and row["action"] == "ignored"


def test_open_duplicate_skips_paper_quote_and_records_actual_decision():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    first = _ev()
    assert copy_event_to_arms(
        c, first, 100.0, "2026-07-12T10:00:00+00:00", arms=["alpha_b"],
        fill_fn=lambda *a, **k: (0.001, "test_fill"),
    ) == {"alpha_b": "opened"}
    duplicate = _ev()
    duplicate.fomo_event_id = "e2"

    def no_second_quote(*args, **kwargs):
        raise AssertionError("duplicate bought another quote")

    assert copy_event_to_arms(
        c, duplicate, 100.0, "2026-07-12T10:01:00+00:00", arms=["alpha_b"],
        fill_fn=no_second_quote,
    ) == {"alpha_b": "skipped_dup"}
    assert c.execute(
        "SELECT action FROM memecoin_entry_snapshots WHERE upstream_event_id='e2'"
    ).fetchone()["action"] == "skipped_dup"
    assert c.execute(
        "SELECT COUNT(*) FROM memecoin_positions WHERE strategy='alpha_b'"
    ).fetchone()[0] == 1


def test_one_duplicate_arm_still_prices_new_arm():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    assert copy_event_to_arms(
        c, _ev(), 100.0, "2026-07-12T10:00:00+00:00", arms=["alpha_b"],
        fill_fn=lambda *a, **k: (0.001, "test_fill"),
    ) == {"alpha_b": "opened"}
    second = _ev()
    second.fomo_event_id = "e2"
    calls = []

    def fill(*args, **kwargs):
        calls.append(args)
        return 0.001, "test_fill"

    assert copy_event_to_arms(
        c, second, 100.0, "2026-07-12T10:01:00+00:00",
        arms=["alpha_b", "control"], fill_fn=fill,
    ) == {"alpha_b": "skipped_dup", "control": "opened"}
    assert len(calls) == 1


def test_chase_rejection_keeps_episode_anchor_and_current_executable_price():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    first = _ev(price=1.0)
    copy_event_to_arms(
        c, first, 100.0, "2026-07-12T10:00:00+00:00", arms=["alpha_b"],
        fill_fn=lambda *a, **k: (1.0, "x"), stats_fn=lambda *a, **k: {},
    )
    late = _ev(price=1.1)
    late.fomo_event_id = "e2"
    late.source_user_id = "u2"  # same coin episode, but no duplicate open position
    late.source_handle = "bob"
    copy_event_to_arms(
        c, late, 100.0, "2026-07-12T10:05:00+00:00", arms=["alpha_b"],
        # 2.0 is +100% over the 1.0 source price: over FUNDED's cap (30/35) as well as the
        # retired paper 15%, so the chase branch is still the one under test.
        fill_fn=lambda *a, **k: (2.0, "x"), stats_fn=lambda *a, **k: {},
    )
    row = c.execute(
        "SELECT action, episode_id, features_json FROM memecoin_entry_snapshots "
        "WHERE upstream_event_id='e2'",
    ).fetchone()
    features = json.loads(row["features_json"])
    assert row["action"] == "skipped_chase_over_funded_cap"
    assert row["episode_id"] is not None
    assert features["episode_entry_sequence"] == 2
    assert features["episode_first_executable_price"] == 1.0
    assert features["episode_executable_runup_pct"] == 100.0   # follows the 2.0 fill above


@pytest.mark.parametrize("source_price, fill_price, expected", [
    (1.0, 1.2, "opened"), (1.0, 1.5, "skipped_chase_over_funded_cap"),
    (None, 1.2, "opened"),
])
def test_chase_model_is_explicit_on_admissions_and_refusals(
        monkeypatch, source_price, fill_price, expected):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT", 10.0)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    ev = _ev(price=source_price)
    ev.network_id = "4663"
    ev.their_market_cap = 30_000.0
    result = copy_event_to_arms(
        c, ev, 100.0, "2026-07-12T10:00:00+00:00", arms=["alpha_b"],
        fill_fn=lambda *a, **k: (fill_price, "x"), stats_fn=lambda *a, **k: {},
    )
    assert result == {"alpha_b": expected}
    row = c.execute("SELECT action, features_json FROM memecoin_entry_snapshots").fetchone()
    assert row["action"] == expected
    features = json.loads(row["features_json"])
    assert features["paper_chase_model"] == "band_cap_approximation_v1"
    assert features["paper_chase_cap_pct"] == (40.0 if source_price else None)
    assert features["paper_chase_gap_pct"] == (
        pytest.approx((fill_price / source_price - 1) * 100) if source_price else None)
