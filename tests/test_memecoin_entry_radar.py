import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _complete_facts(**overrides):
    facts = {
        "terminal_status": "filled",
        "terminal_reason": "filled",
        "network_id": "solana",
        "mint": "MINT",
        "signal_source": "global_feed",
        "source_price": 1.0,
        "quote_price": 1.05,
        "quote_age_ms": 20.0,
        "quote_id": "quote-1",
        "fill_price": 1.06,
        "entry_mcap": 1_000_000.0,
        "entry_liquidity": 50_000.0,
        "observed_at": "2026-07-16T10:00:00+00:00",
        "decision_started_at": "2026-07-16T10:00:00.100000+00:00",
        "clock_domain": "observed_real_utc",
        "claimed_source_age_s": 10.0,
        "observed_to_decision_ms": 100.0,
        "episode_executable_runup_pct": 5.0,
        "consensus_n_60m": 0,
    }
    facts.update(overrides)
    return facts


def test_rating_green_means_complete_without_v1_warning():
    from sniper.services.memecoin_entry_radar import rate_entry_facts

    rating, reasons = rate_entry_facts(_complete_facts())
    assert rating == "green"
    assert reasons == []


def test_existing_chase_refusal_is_red():
    from sniper.services.memecoin_entry_radar import rate_entry_facts

    rating, reasons = rate_entry_facts(_complete_facts(
        terminal_status="refused", terminal_reason="skipped_chase",
    ))
    assert rating == "red"
    assert "existing_refusal:skipped_chase" in reasons


def test_not_approved_is_yellow_not_candidate_red():
    from sniper.services.memecoin_entry_radar import rate_entry_facts

    rating, reasons = rate_entry_facts(_complete_facts(
        terminal_status="refused", terminal_reason="not_approved",
    ))
    assert rating == "yellow"
    assert "operator_not_approved" in reasons


def test_consensus_never_upgrades_missing_attribution():
    from sniper.services.memecoin_entry_radar import rate_entry_facts

    rating, reasons = rate_entry_facts(_complete_facts(
        entry_liquidity=None, consensus_n_60m=20,
    ))
    assert rating == "yellow"
    assert "missing_entry_liquidity" in reasons


def test_blank_quote_id_is_incomplete_attribution():
    from sniper.services.memecoin_entry_radar import rate_entry_facts

    rating, reasons = rate_entry_facts(_complete_facts(quote_id=""))
    assert rating == "yellow"
    assert "missing_quote_id" in reasons


def test_provisional_latency_mcap_and_episode_warnings_are_separate():
    from sniper.services.memecoin_entry_radar import rate_entry_facts

    rating, reasons = rate_entry_facts(_complete_facts(
        claimed_source_age_s=61.0,
        entry_mcap=30_000_000.0,
        episode_executable_runup_pct=16.0,
    ), chase_cap_pct=15.0)
    assert rating == "yellow"
    assert reasons == [
        "claimed_source_age_ge_60s_untrusted",
        "entry_mcap_ge_30m",
        "episode_runup_ge_chase_cap",
    ]


def test_attempt_rating_joins_exact_event_snapshot_and_is_immutable():
    from sniper.services.memecoin_entry_radar import record_attempt_rating

    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_entry_snapshots(upstream_event_id, episode_id, chain, mint, "
        "mcap, liquidity, consensus_n_60m, features_json, action) "
        "VALUES('event-1','episode-1','4663','0xSame',1000000,50000,3,?, 'opened')",
        (json.dumps({"episode_entry_sequence": 2,
                     "episode_executable_runup_pct": 12.0}),),
    )
    conn.commit()
    m.start_entry_attempt(
        conn, attempt_id="attempt-1", origin_event_id="event-1",
        signal_source="follow_activity", network_id="4663", mint="0xSame",
        source_user_id="u1", source_created_at="2026-07-16T10:00:00+00:00",
        source_price=1.0, observed_at="2026-07-16T10:00:10+00:00",
        clock_domain="observed_real_utc",
        decision_started_at="2026-07-16T10:00:10.100000+00:00",
    )
    m.finish_entry_attempt(
        conn, "attempt-1", "filled", "filled", "2026-07-16T10:00:11+00:00", "{}",
        network_id="4663", quote_price=1.1, quote_age_ms=20.0,
        quote_id="quote-1", fill_price=1.11, source_drift_pct=10.0,
    )

    first = record_attempt_rating(conn, "attempt-1")
    second = record_attempt_rating(conn, "attempt-1")
    assert second == first
    row = conn.execute("SELECT * FROM memecoin_radar_ratings").fetchone()
    facts = json.loads(row["facts_json"])
    assert row["attempt_id"] == "attempt-1"
    assert row["upstream_event_id"] == "event-1"
    assert row["snapshot_id"] is not None and row["episode_id"] == "episode-1"
    assert row["network_id"] == "4663" and row["mint"] == "0xSame"
    assert row["signal_source"] == "follow_activity"
    assert facts["observed_to_decision_ms"] == 100.0
    assert facts["claimed_source_age_s"] == 10.0
    assert facts["episode_entry_sequence"] == 2
    assert row["facts_hash"]

    conn.execute("UPDATE memecoin_entry_attempts SET source_price=2.0 WHERE attempt_id='attempt-1'")
    conn.commit()
    with pytest.raises(ValueError, match="immutable radar collision"):
        record_attempt_rating(conn, "attempt-1")


def test_real_hook_finalizes_radar_for_not_approved_attempt(monkeypatch):
    """The hook's WRITE path, so it must run with the write enabled.

    MEMECOIN_ENTRY_RADAR_RATINGS_ENABLED went to default 0 on 2026-09-22 when the Launch radar
    sub-tab was removed and the ratings lost their last reader. The write itself is unchanged.
    """
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_real_runner import make_real_hook

    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RADAR_RATINGS_ENABLED", True)
    conn = _conn()
    m.add_wallet(conn, "u1", "alice")
    ev = SimpleNamespace(
        side="buy", source_user_id="u1", source_handle="alice", mint="MINT", ticker="T",
        their_price=1.0, their_market_cap=1_000_000.0, their_size_usd=2_000.0,
        fomo_event_id="event-1", created_at="2026-07-16T10:00:00+00:00",
        received_at="2026-07-16T10:00:01+00:00",
        received_at_normalized="2026-07-16T10:00:01+00:00",
        clock_domain="observed_real_utc", signal_source="global_feed", network_id="solana",
    )
    hook = make_real_hook(
        None, {"solana": {}}, now_fn=lambda: "2026-07-16T10:00:01.100000+00:00",
        trade_size=100.0, max_age_s=60.0,
    )
    result = hook(conn, ev)
    assert result["status"] == "not_approved"
    row = conn.execute(
        "SELECT rating, reason_codes_json, upstream_event_id, signal_source "
        "FROM memecoin_radar_ratings",
    ).fetchone()
    assert row["rating"] == "yellow"
    assert "operator_not_approved" in json.loads(row["reason_codes_json"])
    assert row["upstream_event_id"] == "event-1"
    assert row["signal_source"] == "global_feed"


def test_the_flag_off_writes_no_rating_and_returns_none(monkeypatch):
    """The default since 2026-09-22. The funded runner wrote one rating per terminal attempt long
    after the radar crons stopped, because the writer never depended on them.

    The hook must still finalize the attempt: the rating is terminal telemetry, so turning it off
    changes what is RECORDED, never what the funded leg decides.
    """
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_real_runner import make_real_hook

    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RADAR_RATINGS_ENABLED", False)
    conn = _conn()
    m.add_wallet(conn, "u1", "alice")
    ev = SimpleNamespace(
        side="buy", source_user_id="u1", source_handle="alice", mint="MINT", ticker="T",
        their_price=1.0, their_market_cap=1_000_000.0, their_size_usd=2_000.0,
        fomo_event_id="event-1", created_at="2026-07-16T10:00:00+00:00",
        received_at="2026-07-16T10:00:01+00:00",
        received_at_normalized="2026-07-16T10:00:01+00:00",
        clock_domain="observed_real_utc", signal_source="global_feed", network_id="solana",
    )
    hook = make_real_hook(
        None, {"solana": {}}, now_fn=lambda: "2026-07-16T10:00:01.100000+00:00",
        trade_size=100.0, max_age_s=60.0,
    )
    result = hook(conn, ev)
    assert result["status"] == "not_approved", "the funded verdict must not depend on the flag"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_radar_ratings").fetchone()[0] == 0

    from sniper.services.memecoin_real_runner import _record_radar_best_effort
    assert _record_radar_best_effort(SimpleNamespace(conn=conn, attempt_id="attempt-1")) is None
