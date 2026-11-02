"""4663 fast lane, cached-only (spec 2026-10-14 rh-fast-cached-only §3.1): a safety cache miss is
refused at once with the precheck's reason. No inline read, no hook, no attempt row, and the lane's
hold is released so the ordinary path judges the buy."""
import time

import pytest

from test_memecoin_rh_fast_entry import MINT, _go, _payload, _setup, _stats  # noqa: F401


@pytest.fixture
def miss(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", "https://own-4663.example.test/key")
    built = _setup(monkeypatch, tmp_path, "live")
    built[0]["cache"]._verdicts.pop(MINT, None)
    yield built
    built[0]["live"]._safety_pool.shutdown(wait=True)


def _knob(monkeypatch, on):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", on, raising=False)


def _spy(live):
    calls = []
    live.inline_safety_fn = lambda m, usd=None: calls.append("safety") or (True, None)
    live.inline_facts_fn = lambda m: calls.append("facts") or (18, 10 ** 27)
    live.stats_fn = lambda m: calls.append("stats") or _stats()
    return calls


def _attempts(ledger):
    return ledger.execute("SELECT count(*) FROM memecoin_entry_attempts").fetchone()[0]


@pytest.mark.parametrize("stale, reason", [(False, "no_safety_verdict"), (True, "safety_stale")])
def test_a_cache_miss_refuses_without_any_inline_read(miss, monkeypatch, stale, reason):
    from sniper.services import memecoin_rh_fast_lane as lane
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    if stale:
        built["cache"]._verdicts[MINT] = (True, None, time.time() - 3600, 0.0)
    calls = _spy(built["live"])
    p = _payload()
    rec = _go(built, p)
    assert rec["reason"] == reason and calls == [] and not sender.raws, rec
    assert _attempts(ledger) == 0
    assert not lane.LANE_HOLDS.holds(p["tx_hash"], MINT)     # the ordinary path is free


def test_with_the_knob_off_a_miss_reads_inline_as_before(miss, monkeypatch):
    _knob(monkeypatch, False)
    built, sender, net, ledger = miss
    calls = _spy(built["live"])
    rec = _go(built, _payload())
    assert "safety" in calls and sender.raws, (rec, calls)


def test_a_cached_pass_still_sends_with_the_knob_on(miss, monkeypatch):
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    built["cache"]._verdicts[MINT] = (True, None, time.time(), 0.0)
    calls = _spy(built["live"])
    rec = _go(built, _payload())
    assert sender.raws and calls == [], (rec, calls)


def test_a_cached_pass_without_stats_refuses_without_an_inline_read(miss, monkeypatch):
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    built["cache"]._verdicts[MINT] = (True, None, time.time(), 0.0)
    built["cache"]._stats.pop(MINT, None)
    calls = _spy(built["live"])
    rec = _go(built, _payload())
    assert rec["reason"] == "no_market_stats" and calls == [] and not sender.raws, rec


def _last_live(built, cols):
    import sqlite3
    return sqlite3.connect(built["db_path"]).execute(
        "SELECT %s FROM rh_fast_lane_live ORDER BY id DESC LIMIT 1" % cols).fetchone()


def test_a_send_records_feed_to_send_accepted_by_and_state_class(miss, monkeypatch):
    from sniper.util.receipt_clock import ReceiptClock
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    built["cache"]._verdicts[MINT] = (True, None, time.time(), 0.0)
    p = _payload()
    p["receipt_clock"] = ReceiptClock("test-feed").capture()
    _go(built, p)
    assert sender.raws
    got, by, cls = _last_live(built, "feed_to_send_ms, accepted_by, state_class")
    assert got is not None and 0 <= got < 5000 and cls == "exact", (got, cls)
    assert by is None                              # the test's injected sender is the legacy seam


def test_an_absent_receipt_clock_is_null_never_zero(miss, monkeypatch):
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    built["cache"]._verdicts[MINT] = (True, None, time.time(), 0.0)
    p = _payload()
    p["receipt_clock"] = None
    _go(built, p)
    assert sender.raws and _last_live(built, "feed_to_send_ms") == (None,)


def test_state_class_names_the_decides_state():
    from sniper.services.memecoin_rh_fast_lane import state_class
    assert [state_class({"state_kind": k}) for k in ("tape_post", "pre", "receipt_leg", None)] == [
        "exact", "fresh", None, None]
    assert state_class(None) is None


def test_send_counters_summarise_a_days_sends_by_class(tmp_path):
    import sqlite3
    from sniper.services import memecoin_rh_fast_lane as lane
    from sniper.util.clock import real_utc_now_iso
    db = sqlite3.connect(str(tmp_path / "l.db"))
    db.executescript(lane._LIVE)
    lane.add_missing_columns(db, "rh_fast_lane_live", lane.LIVE_COLUMNS)
    now = real_utc_now_iso()
    for cls, reason, ms in (("exact", "filled", 20.0), ("exact", "reverted", 30.0), ("fresh", "filled", None)):
        db.execute("INSERT INTO rh_fast_lane_live (reason, send_reply, state_class, feed_to_send_ms, "
                   "written_at) VALUES (?,?,?,?,?)", (reason, "accepted", cls, ms, now))
    db.execute("INSERT INTO rh_fast_lane_live (reason, send_reply, written_at) VALUES "
               "('no_safety_verdict', NULL, ?)", (now,))                # not a send
    got = lane.send_counters(db)
    assert got["by_class"] == {"exact": {"sends": 2, "reverted": 1}, "fresh": {"sends": 1, "reverted": 0}}
    assert got["feed_to_send_ms"] == {"n": 2, "p50": 30.0, "p90": 30.0}


def test_the_coverage_report_counts_live_signals_by_cache_state(tmp_path):
    import sqlite3
    from scripts.research.rh_fast_cache_coverage import coverage
    db = sqlite3.connect(str(tmp_path / "l.db"))
    db.execute("CREATE TABLE rh_fast_lane_events (mode TEXT, reason TEXT, safety_path TEXT, written_at TEXT)")
    db.executemany("INSERT INTO rh_fast_lane_events VALUES (?,?,?,?)", [
        ("live", "filled", "cache", "2026-10-15T01:00:00+00:00"),
        ("live", "no_safety_verdict", None, "2026-10-15T02:00:00+00:00"),
        ("live", "safety_stale", "cache", "2026-10-15T03:00:00+00:00"),   # the precheck marks cache first
        ("live", "hook:filled:", "inline", "2026-10-15T04:00:00+00:00"),
        ("live", "relay_origin_unproved", None, "2026-10-15T05:00:00+00:00"),  # never reached safety
        ("live", "snapshot_stale", "cache", "2026-10-15T05:30:00+00:00"),       # refused before safety
        ("live", "source_band:source buy $50 under absolute floor $197", "cache",
         "2026-10-15T05:40:00+00:00"),                                          # a miss the band refused
        ("runner_shadow", "filled", "cache", "2026-10-15T06:00:00+00:00")])
    assert coverage(db) == {"2026-10-15": {"cache": 1, "inline": 1, "no_safety_verdict": 1,
                                           "safety_stale": 1, "band_miss": 1}}
