"""P0 improvements: wallets/stats rollup, recent-events feed with per-event action,
health (runner heartbeat + feed activity). Style mirrors tests/test_memecoin_api.py."""
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_runner as run


def _client(ctx_extra=None):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    ctx = {"conn": c}
    if ctx_extra:
        ctx.update(ctx_extra)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return app.test_client(), c


def _event(c, eid, uid="u1", handle="alice", mint="MINT", lag=0.4, recv="2999-01-01T00:00:00+00:00"):
    m.insert_event(c, {
        "fomo_event_id": eid, "source_user_id": uid, "source_handle": handle,
        "mint": mint, "ticker": "T", "side": "buy", "their_price": 0.005,
        "their_market_cap": 1e6, "their_size_usd": 3000.0, "is_first_buy": True,
        "created_at": recv, "received_at": recv, "lag_s": lag,
    })


def _raw_event(c, eid, *, source, observed, lag):
    c.execute(
        "INSERT INTO memecoin_feed_observations_v2"
        "(upstream_event_id,raw_type,source_ts,observed_at,lag_s,raw_json) "
        "VALUES (?,'large_buy',?,?,?,?)",
        (eid, source, observed, lag, json.dumps({"id": eid})),
    )


# ── wallets/stats rollup ────────────────────────────────────────────────────
def test_wallets_stats_rollup_math():
    cl, c = _client()
    m.add_wallet(c, "u1", "alice")
    # one open, two closed (one win, one loss)
    m.open_position(c, source_user_id="u1", source_handle="alice", mint="A", ticker="A",
                    entry_price=0.01, entry_notional=100.0, entry_fill_source="jupiter",
                    entry_lag_s=0.3, their_size_usd=1000.0, opened_at="t")
    p2 = m.open_position(c, source_user_id="u1", source_handle="alice", mint="B", ticker="B",
                         entry_price=0.01, entry_notional=100.0, entry_fill_source="jupiter",
                         entry_lag_s=0.3, their_size_usd=1000.0, opened_at="t")
    p3 = m.open_position(c, source_user_id="u1", source_handle="alice", mint="C", ticker="C",
                         entry_price=0.01, entry_notional=100.0, entry_fill_source="jupiter",
                         entry_lag_s=0.3, their_size_usd=1000.0, opened_at="t")
    m.close_position(c, p2, "t2", realized_pnl=50.0)
    m.close_position(c, p3, "t2", realized_pnl=-20.0)
    st = cl.get("/api/memecoin/wallets/stats").get_json()["stats"]["u1"]
    assert st["open_count"] == 1
    assert st["closed_count"] == 2
    assert st["realized_pnl"] == 30.0
    assert st["win_rate"] == 0.5


def test_wallets_stats_no_closed_is_null_wr():
    cl, c = _client()
    m.add_wallet(c, "u1", "alice")
    st = cl.get("/api/memecoin/wallets/stats").get_json()["stats"]["u1"]
    assert st["closed_count"] == 0 and st["win_rate"] is None and st["realized_pnl"] == 0.0


# ── recent events feed + action ─────────────────────────────────────────────
def test_events_recent_shape_and_order():
    cl, c = _client()
    _event(c, "e1")
    _event(c, "e2")
    m.set_event_action(c, "e2", "opened")
    body = cl.get("/api/memecoin/events/recent?limit=10").get_json()
    evs = body["events"]
    assert [e["fomo_event_id"] for e in evs] == ["e2", "e1"]  # newest first
    assert evs[0]["action"] == "opened"
    assert evs[1]["action"] is None


def test_events_recent_limit_clamped():
    cl, c = _client()
    for i in range(5):
        _event(c, f"e{i}")
    assert len(cl.get("/api/memecoin/events/recent?limit=2").get_json()["events"]) == 2
    # bad limit falls back to default, does not 500
    assert cl.get("/api/memecoin/events/recent?limit=abc").status_code == 200


# ── run_once records the per-event outcome ──────────────────────────────────
def test_run_once_records_action():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice")
    feed = {"responseObject": {"feed": [
        {"id": "e1", "type": "large_buy", "userId": "u1", "tokenAddress": "MINT",
         "createdAt": "2026-07-05T10:00:00Z",
         "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                  "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True}},
    ]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 05 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    run.run_once(c, _Auth(), size_usd=100.0, http=http,
                 fill_fn=lambda *a, **k: (0.005, "jupiter"))
    row = c.execute("SELECT action FROM memecoin_events WHERE fomo_event_id='e1'").fetchone()
    assert row["action"] == "opened"


# ── health: heartbeat freshness + feed activity ─────────────────────────────
def test_health_runner_alive_when_heartbeat_fresh(tmp_path):
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time(), "auth_ok": True, "events": 2, "opened": 1}))
    cl, c = _client({"heartbeat_path": str(hb)})
    m.add_wallet(c, "u1", "alice")
    _event(c, "e1")
    h = cl.get("/api/memecoin/health").get_json()
    assert h["runner_alive"] is True
    assert h["auth_ok"] is True
    assert h["curated_count"] == 1
    assert h["events_last_hour"] >= 0
    for k in ("last_event_at", "last_event_age_s", "open_count", "feed_lag_median_s", "runner_age_s"):
        assert k in h


def test_health_publishes_fastlane_status(tmp_path):
    hb = tmp_path / "hb.json"
    fastlane = {"status": "failed", "failure": "RuntimeError", "fallback_active": True}
    hb.write_text(json.dumps({
        "ts": time.time(), "auth_ok": True, "fastlane": fastlane,
    }))
    cl, _c = _client({"heartbeat_path": str(hb)})

    assert cl.get("/api/memecoin/health").get_json()["fastlane"] == fastlane


def test_health_separates_recent_lag_from_startup_backlog(tmp_path, monkeypatch):
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    started_at = "2026-08-19T11:00:00+00:00"
    monkeypatch.setattr("sniper.util.clock.real_utc_now", lambda *_args: now.timestamp())
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({
        "ts": time.time(), "auth_ok": True, "started_at": started_at,
    }))
    cl, c = _client({"heartbeat_path": str(hb)})
    _raw_event(c, "recent-1", source="2026-08-19T11:58:58+00:00",
               observed="2026-08-19T11:59:00+00:00", lag=2.0)
    _raw_event(c, "recent-2", source="2026-08-19T11:57:56+00:00",
               observed="2026-08-19T11:58:00+00:00", lag=4.0)
    _raw_event(c, "hour", source="2026-08-19T11:29:50+00:00",
               observed="2026-08-19T11:30:00+00:00", lag=10.0)
    _raw_event(c, "startup-1", source="2026-08-19T10:59:00+00:00",
               observed="2026-08-19T11:00:30+00:00", lag=90.0)
    _raw_event(c, "startup-2", source="2026-08-19T10:59:00+00:00",
               observed="2026-08-19T11:01:00+00:00", lag=120.0)
    _raw_event(c, "not-backlog", source="2026-08-19T11:00:30+00:00",
               observed="2026-08-19T11:01:00+00:00", lag=30.0)

    health = cl.get("/api/memecoin/health").get_json()
    assert health["events_last_5m"] == 2
    assert health["feed_lag_5m_sample_count"] == 2
    assert health["feed_lag_5m_median_s"] == 3.0
    assert health["startup_backlog_count"] == 2
    assert health["startup_backlog_complete"] is True


def test_health_marks_lag_untrusted_when_the_source_clock_is_ahead(tmp_path, monkeypatch):
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("sniper.util.clock.real_utc_now", lambda *_args: now.timestamp())
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time(), "auth_ok": True}))
    cl, c = _client({"heartbeat_path": str(hb)})
    _raw_event(c, "past", source="2026-08-19T11:58:42+00:00",
               observed="2026-08-19T11:59:00+00:00", lag=18.0)
    # SIX future-dated samples, not one (2026-09-17). A single bad clock used to void the reading,
    # and on a quiet five-minute window (~16 events) that suppressed a 0.30 s median every time.
    for i in range(6):
        _raw_event(c, "future%d" % i, source="2026-08-19T11:59:12+00:00",
                   observed="2026-08-19T11:59:00+00:00", lag=-12.0)

    health = cl.get("/api/memecoin/health").get_json()
    assert health["feed_lag_status"] == "untrusted_source_clock"
    assert health["feed_lag_5m_sample_count"] == 7
    assert health["feed_lag_5m_future_count"] == 6
    assert health["feed_lag_5m_median_s"] is None


def test_health_ignores_the_missing_timestamp_sentinel(tmp_path, monkeypatch):
    """Inherited review finding O2. A normaliser writes lag_s=-1.0 when the source carried no
    timestamp (sniper/feeds/fomo_feed.py). That row proves nothing about any clock, so it must
    be neither a future-dated sample nor a member of the lag population; a REAL negative lag on
    a row that has a source stamp still counts."""
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("sniper.util.clock.real_utc_now", lambda *_args: now.timestamp())
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time(), "auth_ok": True}))
    cl, c = _client({"heartbeat_path": str(hb)})
    _raw_event(c, "sane", source="2026-08-19T11:58:58+00:00",
               observed="2026-08-19T11:59:00+00:00", lag=2.0)
    for i in range(20):
        _raw_event(c, "nosrc%d" % i, source=None,
                   observed="2026-08-19T11:59:00+00:00", lag=-1.0)
    _event(c, "ev-sane", lag=0.4, recv="2026-08-19T11:50:00+00:00")
    for i in range(20):
        m.insert_event(c, {
            "fomo_event_id": "ev-nosrc%d" % i, "source_user_id": "u1", "source_handle": "a",
            "mint": "M", "ticker": "T", "side": "buy", "their_price": 0.005,
            "their_market_cap": 1e6, "their_size_usd": 3000.0, "is_first_buy": True,
            "created_at": None, "received_at": "2026-08-19T11:50:00+00:00", "lag_s": -1.0,
        })
    health = cl.get("/api/memecoin/health").get_json()
    assert health["feed_lag_status"] == "ok"
    assert health["feed_lag_5m_sample_count"] == 1
    assert health["feed_lag_5m_future_count"] == 0
    assert health["feed_lag_5m_no_source_ts_count"] == 20
    assert health["feed_lag_sample_count"] == 1
    assert health["feed_lag_future_count"] == 0
    assert health["feed_lag_no_source_ts_count"] == 20
    assert health["feed_lag_lead_lane"] is None


def test_health_hides_startup_backlog_until_window_is_complete(tmp_path, monkeypatch):
    now = datetime(2026, 8, 19, 11, 3, tzinfo=timezone.utc)
    started_at = "2026-08-19T11:00:00+00:00"
    monkeypatch.setattr("sniper.util.clock.real_utc_now", lambda *_args: now.timestamp())
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({
        "ts": time.time(), "auth_ok": True, "started_at": started_at,
    }))
    cl, c = _client({"heartbeat_path": str(hb)})
    _raw_event(c, "startup", source="2026-08-19T10:59:00+00:00",
               observed="2026-08-19T11:01:00+00:00", lag=120.0)

    health = cl.get("/api/memecoin/health").get_json()
    assert health["startup_backlog_count"] is None
    assert health["startup_backlog_complete"] is False


def test_raw_feed_health_window_uses_observed_at_index():
    _cl, c = _client()
    plan = c.execute(
        "EXPLAIN QUERY PLAN SELECT lag_s FROM memecoin_feed_observations_v2 "
        "WHERE observed_at > ?", ("2026-08-19T11:55:00+00:00",),
    ).fetchall()
    assert any("idx_mc_feed_v2_observed_at" in str(row[3]) for row in plan)


def test_health_runner_dead_when_heartbeat_stale(tmp_path):
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time() - 9999, "auth_ok": False}))
    cl, c = _client({"heartbeat_path": str(hb)})
    h = cl.get("/api/memecoin/health").get_json()
    assert h["runner_alive"] is False
    assert h["auth_ok"] is False


def test_health_no_heartbeat_file():
    cl, c = _client({"heartbeat_path": "/nonexistent/path/hb.json"})
    h = cl.get("/api/memecoin/health").get_json()
    assert h["runner_alive"] is False
    assert h["auth_ok"] is None


def test_a_few_future_dated_samples_still_report_the_median(tmp_path, monkeypatch):
    """2026-09-17: the cockpit read "source clock untrusted" while the median lag was 0.30 s over
    2,771 samples, 10 of them (0.36%) future-dated. One bad clock voided the whole reading."""
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("sniper.util.clock.real_utc_now", lambda *_args: now.timestamp())
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time(), "auth_ok": True}))
    cl, c = _client({"heartbeat_path": str(hb)})
    for i in range(40):
        _raw_event(c, "ok%d" % i, source="2026-08-19T11:58:59+00:00",
                   observed="2026-08-19T11:59:00+00:00", lag=0.30)
    _raw_event(c, "future", source="2026-08-19T11:59:10+00:00",
               observed="2026-08-19T11:59:00+00:00", lag=-10.41)

    health = cl.get("/api/memecoin/health").get_json()

    assert health["feed_lag_status"] == "ok"
    assert health["feed_lag_5m_median_s"] == 0.30
    assert health["feed_lag_5m_future_count"] == 1
    assert round(health["feed_lag_5m_future_share"], 4) == round(1 / 41, 4)


def test_a_feed_whose_clock_is_mostly_ahead_stays_untrusted(tmp_path, monkeypatch):
    """A real clock fault still suppresses the number rather than reporting a flattering median."""
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("sniper.util.clock.real_utc_now", lambda *_args: now.timestamp())
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time(), "auth_ok": True}))
    cl, c = _client({"heartbeat_path": str(hb)})
    for i in range(6):        # six future-dated samples clear the minimum count
        _raw_event(c, "future%d" % i, source="2026-08-19T11:59:08+00:00",
                   observed="2026-08-19T11:59:00+00:00", lag=-8.0)
    for i in range(4):
        _raw_event(c, "ok%d" % i, source="2026-08-19T11:58:59+00:00",
                   observed="2026-08-19T11:59:00+00:00", lag=0.5)

    health = cl.get("/api/memecoin/health").get_json()

    assert health["feed_lag_status"] == "untrusted_source_clock"
    assert health["feed_lag_5m_median_s"] is None


def test_a_quiet_window_with_two_bad_clocks_still_reports(tmp_path, monkeypatch):
    """Live 2026-09-17 21:31Z: 16 events in five minutes, two future-dated (12.5%), while the base
    rate over 2,790 samples was 0.43%. A share alone must not condemn the feed."""
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("sniper.util.clock.real_utc_now", lambda *_args: now.timestamp())
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time(), "auth_ok": True}))
    cl, c = _client({"heartbeat_path": str(hb)})
    for i in range(14):
        _raw_event(c, "ok%d" % i, source="2026-08-19T11:58:59+00:00",
                   observed="2026-08-19T11:59:00+00:00", lag=0.4)
    for i in range(2):
        _raw_event(c, "future%d" % i, source="2026-08-19T11:59:09+00:00",
                   observed="2026-08-19T11:59:00+00:00", lag=-9.0)

    health = cl.get("/api/memecoin/health").get_json()

    assert health["feed_lag_status"] == "ok"
    assert health["feed_lag_5m_median_s"] == 0.4
    assert health["feed_lag_5m_future_count"] == 2


def test_a_leading_clock_is_measured_from_the_leading_samples_alone():
    """THE FEED IS A MIXTURE, NOT A SHIFTED DISTRIBUTION (2026-09-18, second attempt).

    The first version assumed one global offset, took the 5th percentile of ALL samples as it, and
    subtracted that from the median. Live, 1,279 of 1,489 samples were sane at +0.19 s and 210 were
    future-dated at -12.4 s, so it printed "source clock 19.0s ahead, lag ~19.23s" — two numbers
    that cannot both be true. Only a future-dated sample proves a bad clock, because a delivery
    delay cannot be negative.
    """
    client, c = _client()
    recv = "2999-01-01T00:00:00+00:00"
    # 30 sane sources near 1 s, and 10 whose clocks lead by about 12 s.
    for i in range(30):
        _event(c, "sane%d" % i, mint="S%d" % i, lag=0.8 + i * 0.02, recv=recv)
        _raw_event(c, "sane%d" % i, source=recv, observed=recv, lag=0.8 + i * 0.02)
    for i in range(10):
        _event(c, "lead%d" % i, mint="L%d" % i, lag=-12.0 - i * 0.1, recv=recv)
        _raw_event(c, "lead%d" % i, source=recv, observed=recv, lag=-12.0 - i * 0.1)
    c.commit()
    body = client.get("/api/memecoin/health").get_json()
    assert body["feed_lag_status"] == "untrusted_source_clock"
    assert body["feed_lag_clock_lead_s"] == pytest.approx(12.45, abs=0.4), \
        "the lead must come from the future-dated samples alone"
    # The lag itself is the SANE median and survives the untrusted verdict: suppressing it left the
    # operator with no number while three quarters of the samples were good.
    assert body["feed_lag_median_s"] == pytest.approx(1.1, abs=0.3)
    assert body["feed_lag_future_count"] == 10


def test_the_lane_behind_the_leading_clocks_is_named():
    """N EVENTS ON ONE LANE, NOT N BROKEN CLOCKS (2026-09-19).

    The card said "205 clocks ~13s ahead". 205 was the EVENT count, drawn from 37 distinct wallets
    that all arrive on one relay lane — one clock stamping everything it forwards, not 205 of them.
    """
    client, c = _client()
    recv = "2999-01-01T00:00:00+00:00"
    for i in range(30):
        _event(c, "s%d" % i, mint="S%d" % i, lag=1.0, recv=recv)
        _raw_event(c, "s%d" % i, source=recv, observed=recv, lag=1.0)
    # ten future-dated events from TWO different wallets on the SAME lane
    for i in range(10):
        c.execute("INSERT INTO memecoin_events(fomo_event_id, mint, side, network_id, created_at,"
                  " received_at, lag_s, signal_source, source_handle, delivery_lane)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  ("l%d" % i, "L%d" % i, "buy", "solana", recv, recv, -12.0 - i * 0.1,
                   "follow_activity", "w%d" % (i % 2), "j7_wallets"))
        _raw_event(c, "l%d" % i, source=recv, observed=recv, lag=-12.0 - i * 0.1)
    c.commit()
    body = client.get("/api/memecoin/health").get_json()
    assert body["feed_lag_status"] == "untrusted_source_clock"
    assert body["feed_lag_lead_lane"] == "j7_wallets", "the card must name the lane, not the count"
    assert body["feed_lag_future_count"] == 10
    assert body["feed_lag_sample_count"] == 40, "the denominator the card shows N out of"


def test_a_sane_clock_reports_no_lead():
    """On a healthy feed there is no leading subgroup to describe."""
    client, c = _client()
    recv = "2999-01-01T00:00:00+00:00"
    for i in range(40):
        _event(c, "ok%d" % i, mint="M%d" % i, lag=1.0 + i * 0.01, recv=recv)
        _raw_event(c, "ok%d" % i, source=recv, observed=recv, lag=1.0 + i * 0.01)
    c.commit()
    body = client.get("/api/memecoin/health").get_json()
    assert body["feed_lag_status"] == "ok"
    assert body["feed_lag_clock_lead_s"] is None
    assert body["feed_lag_lead_lane"] is None
    assert body["feed_lag_median_s"] == pytest.approx(1.2, abs=0.3)


def test_too_few_bad_clocks_to_estimate_a_lead():
    """Four future-dated samples cannot support a median; the card falls back to naming the count."""
    client, c = _client()
    recv = "2999-01-01T00:00:00+00:00"
    for i in range(8):
        _event(c, "s%d" % i, mint="S%d" % i, lag=1.0, recv=recv)
        _raw_event(c, "s%d" % i, source=recv, observed=recv, lag=1.0)
    for i in range(4):
        _event(c, "f%d" % i, mint="F%d" % i, lag=-9.0, recv=recv)
        _raw_event(c, "f%d" % i, source=recv, observed=recv, lag=-9.0)
    c.commit()
    body = client.get("/api/memecoin/health").get_json()
    assert body["feed_lag_clock_lead_s"] is None


@pytest.mark.parametrize("states,age,expected", [
    (["connect_failed", "auth_error"], 0, "down"),
    (["connected", "auth_error"], 0, "degraded"),
    (["connected"], 0, "connected"),
    (["connected", "connected"], 0, "connected"),
    (["connected"], 180, "stale"),
    (["unknown"], 0, "unknown"),
    ([], 0, "unknown"),
])
def test_relay_source_health_is_separate_from_runner_auth(tmp_path, monkeypatch, states, age, expected):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SIGNAL_SOURCE", "j7")
    monkeypatch.setattr(run, "read_feed_snapshot_status", lambda: {
        "events": [], "snapshot_at": "2026-10-18T03:17:12+00:00", "error": None})
    accounts = [{"status": state, "last_error": "Invalid token private-jwt" if state == "auth_error" else None,
                 "tracked_count": None, "pump_tracked_count": None, "token": "private-token"}
                for state in states]
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time() - age, "auth_ok": True,
                             "j7_feed": {"accounts": accounts, "last_event_at": None,
                                         "token": "private-token"}}))
    client, _ = _client({"heartbeat_path": str(hb)})

    health = client.get("/api/memecoin/health").get_json()
    feed = client.get("/api/memecoin/feed-live").get_json()

    assert health["auth_ok"] is True
    source = health["source_health"]
    assert feed["source_health"] == source
    assert source["source"] == "j7" and source["status"] == expected
    assert source["last_event_at"] is None
    assert [account["status"] for account in source["accounts"]] == states
    assert all(account["tracked_count"] is None for account in source["accounts"])
    assert "private-token" not in json.dumps(source)
    assert "private-jwt" not in json.dumps(source)
    assert all(account["last_error"] == ("Invalid token" if state == "auth_error" else None)
               for state, account in zip(states, source["accounts"]))
    assert feed["events"] == [] and feed["error"] is None


@pytest.mark.parametrize("last_event,want", [
    (None, None), (17, None), ("bad-stamp", None),
    ("2026-10-18T03:17:12+00:00", "2026-10-18T03:17:12+00:00"),
])
def test_relay_source_keeps_an_unknown_receipt_unknown(tmp_path, monkeypatch, last_event, want):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SIGNAL_SOURCE", "j7")
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time(), "auth_ok": True,
                             "j7_feed": {"status": "connected", "last_event_at": last_event}}))
    client, _ = _client({"heartbeat_path": str(hb)})

    source = client.get("/api/memecoin/health").get_json()["source_health"]

    assert source["status"] == "connected"
    assert source["last_event_at"] == want
    assert len(source["accounts"]) == 1


def test_relay_source_without_a_heartbeat_is_unknown(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SIGNAL_SOURCE", "j7")
    client, _ = _client({"heartbeat_path": str(tmp_path / "absent")})

    source = client.get("/api/memecoin/health").get_json()["source_health"]

    assert source == {"source": "j7", "status": "unknown", "last_event_at": None, "accounts": []}


def test_relay_source_malformed_account_cannot_hide_partial_coverage(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SIGNAL_SOURCE", "j7")
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.time(), "j7_feed": {"accounts": [
        {"status": "connected"}, {"status": [], "last_error": {"token": "private-token"}}]}}))
    client, _ = _client({"heartbeat_path": str(hb)})

    response = client.get("/api/memecoin/feed-live")
    assert response.status_code == 200
    source = response.get_json()["source_health"]

    assert source["status"] == "degraded"
    assert [a["status"] for a in source["accounts"]] == ["connected", "unknown"]
    assert source["accounts"][1]["last_error"] is None
