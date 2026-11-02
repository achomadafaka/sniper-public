"""Chart-truth collector: the spec's invariants, each as a test that can fail.

Design: docs/superpowers/specs/2026-08-27-memecoin-chart-truth-at-source-buy-design.md
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.persistence import db_memecoin_chart_truth as store
from sniper.services import memecoin_chart_truth as ct

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
BUY = datetime(2026, 8, 28, 11, 30, 17, tzinfo=timezone.utc)     # :17 seconds, mid-minute


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ledger_tables(c)
    store.ensure_tables(c)
    return c


def _ledger_tables(c):
    """The ledger side discovery reads: EVENTS for the work (admission-independent) and
    quote_observations for the pool (zero provider calls)."""
    c.execute("CREATE TABLE memecoin_position_marks(source TEXT, sweep_ts TEXT)")
    c.execute("CREATE TABLE memecoin_entry_snapshots(upstream_event_id TEXT, features_json TEXT,"
              " decided_at TEXT)")
    c.execute("CREATE TABLE memecoin_events(fomo_event_id TEXT, mint TEXT, network_id TEXT,"
              " side TEXT, created_at TEXT)")
    c.execute("CREATE TABLE memecoin_quote_observations_v2(mint TEXT, pair_address TEXT,"
              " observed_at TEXT)")


def _seed(c, *, pool="POOL1", net="solana", buy=BUY, eid="e1", mint=None, with_pool=True):
    mint = mint or ("MINT_" + eid)
    c.execute("INSERT INTO memecoin_events VALUES(?,?,?, 'buy', ?)",
              (eid, mint, net, buy.isoformat()))
    if with_pool:
        c.execute("INSERT INTO memecoin_quote_observations_v2 VALUES(?,?,?)",
                  (mint, pool, ct._minute_iso(buy)))
    c.commit()


def _page(minutes, price=1.0):
    return {int(datetime.fromisoformat(m).timestamp()): (price, price * 1.02, price * 0.99, price,
                                                         100.0)
            for m in minutes}


def test_the_minute_comes_from_the_event_not_the_clock(conn):
    """The whole measurement inverts if `buy_minute` is the FETCH minute."""
    _seed(conn)
    items, _, _ = ct.discover(conn, since_iso=ct._minute_iso(NOW - timedelta(hours=2)),
                           now_iso=ct._minute_iso(NOW))
    minutes = {m for _n, _p, m in items}
    assert ct._minute_iso(BUY) in minutes, "the buy minute must be present"
    assert ct._minute_iso(NOW) not in minutes, "the fetch minute must NOT be the key"


def test_a_three_minute_window_is_requested(conn):
    """`created_at` is a foreign clock with ~+-27s spread; a point key lands wrong ~21% of the time."""
    _seed(conn)
    items, _, _ = ct.discover(conn, since_iso=ct._minute_iso(NOW - timedelta(hours=2)),
                           now_iso=ct._minute_iso(NOW))
    assert sorted(m for _n, _p, m in items) == sorted([
        ct._minute_iso(BUY - timedelta(minutes=1)),
        ct._minute_iso(BUY),
        ct._minute_iso(BUY + timedelta(minutes=1))])


def test_the_key_carries_no_measurement_and_a_revision_updates(conn):
    got = store.save_candles(conn, "solana", "P", {ct._minute_iso(BUY): (1, 2, 1, 1.5, 9)},
                             now_iso="T1")
    assert got == 1
    store.save_candles(conn, "solana", "P", {ct._minute_iso(BUY): (1, 3, 1, 2.0, 9)}, now_iso="T2")
    rows = conn.execute("SELECT close_price, revision_count, price_space, minute_range "
                        "FROM memecoin_chart_truth").fetchall()
    assert len(rows) == 1, "a revision must UPDATE, never insert a second row"
    assert rows[0]["close_price"] == 2.0 and rows[0]["revision_count"] == 1
    assert rows[0]["price_space"] == "chart"
    assert rows[0]["minute_range"] == pytest.approx(3.0)


def test_it_yields_to_the_funded_lane_and_fetches_nothing(conn):
    _seed(conn)
    conn.executemany("INSERT INTO memecoin_position_marks VALUES('geckoterminal', ?)",
                     [(ct._minute_iso(NOW),) for _ in range(200)])
    conn.commit()
    called = []
    out = ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW),
                      fetch_fn=lambda *a, **k: called.append(1) or {}, sleep_fn=lambda s: None)
    assert out["skipped_cycle"] == 1 and out["fetched"] == 0
    assert not called, "a yielded cycle must issue no request at all"


def test_a_yielded_cycle_still_QUEUES_the_work(conn):
    """A yield defers the FETCH, never the work.

    `run_once` returned before discovery, so a yielded cycle queued NOTHING — and the lane is
    forward-only with a bounded lookback, so those events were gone rather than delayed. Reproduced
    at 51 funded marks with one new event: zero rows queued. This is the precise rule f0c28426
    exists to enforce, broken in the function it was written for.
    """
    _seed(conn)
    conn.executemany("INSERT INTO memecoin_position_marks VALUES('geckoterminal', ?)",
                     [(ct._minute_iso(NOW),) for _ in range(200)])
    conn.commit()
    called = []
    out = ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW),
                      fetch_fn=lambda *a, **k: called.append(1) or {}, sleep_fn=lambda s: None)
    assert out["skipped_cycle"] == 1 and not called, "a yield must issue no request"
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_chart_truth_queue").fetchone()[0] == 3, (
        "the three window minutes must be QUEUED even though the fetch yielded")


def test_the_deadline_follows_the_source_window_not_the_fetch_clock(conn):
    """A page reaches PAGE_MINUTES back from NOW, so an item's deadline is a property of ITS OWN
    buy minute. Deriving it from fetch time expired fresh rows up to an hour early."""
    from datetime import datetime as _dt
    _seed(conn)
    ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW), fetch_fn=lambda *a, **k: None,
                sleep_fn=lambda s: None)
    rows = conn.execute("SELECT buy_minute, deadline_at FROM memecoin_chart_truth_queue").fetchall()
    assert rows
    for r in rows:
        gap = (_dt.fromisoformat(r["deadline_at"]) - _dt.fromisoformat(r["buy_minute"]))
        assert gap == timedelta(minutes=ct.PAGE_MINUTES), (
            "the deadline must be the buy minute plus the page span")


def test_the_db_path_is_state_rooted_not_module_rooted():
    """A pinned deploy runs from .runtime/.../releases/<sha>/; a __file__-derived path writes the
    queue inside the release tree and the next deploy strands every deferred item."""
    assert ".runtime" not in str(store.DB_PATH), "the queue must not live inside a release tree"
    assert store.DB_PATH.name == "memecoin_chart_truth.db"


def test_a_yielded_pool_is_DEFERRED_not_dropped(conn):
    """Busy funded and busy tape share a driver, so dropping on yield loses the busiest tape.
    Deferral is safe only because the source is historical."""
    _seed(conn)
    ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW), fetch_fn=lambda *a, **k: None,
                sleep_fn=lambda s: None)
    left = conn.execute("SELECT COUNT(*) FROM memecoin_chart_truth_queue").fetchone()[0]
    assert left == 3, "the three window minutes stay queued when the provider gives nothing"


def test_the_first_429_stops_the_cycle(conn):
    from sniper.execution.geckoterminal_quote import GtRateLimited
    for i in range(4):
        _seed(conn, pool=f"P{i}", eid=f"e{i}")
    calls = []

    def fetch(net, pool, *a, **k):
        calls.append(pool)
        raise GtRateLimited("429")

    out = ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW), fetch_fn=fetch,
                      sleep_fn=lambda s: None)
    assert out["rate_limited"] == 1
    assert len(calls) == 1, "no retry, and no second pool after a 429"


def test_a_missing_minute_writes_no_row(conn):
    """An absent row means unknown. A substituted neighbour would be a fabricated observation."""
    _seed(conn)
    only_middle = _page([ct._minute_iso(BUY)])
    out = ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW),
                      fetch_fn=lambda *a, **k: only_middle, sleep_fn=lambda s: None)
    assert out["rows"] == 1 and out["no_candle"] == 2
    assert conn.execute("SELECT COUNT(*) FROM memecoin_chart_truth").fetchone()[0] == 1


def test_a_missing_pool_is_skipped_never_guessed(conn):
    _seed(conn, eid="e9", with_pool=False)
    items, skipped, _ = ct.discover(conn, since_iso=ct._minute_iso(NOW - timedelta(hours=2)),
                                 now_iso=ct._minute_iso(NOW))
    assert items == [] and skipped == 1


def test_an_age_out_is_counted_not_silent(conn):
    store.enqueue(conn, [("solana", "OLD", ct._minute_iso(BUY))],
                  now_iso=ct._minute_iso(NOW),
                  deadline_at=ct._minute_iso(NOW - timedelta(hours=1)))
    out = ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW), fetch_fn=lambda *a, **k: None,
                      sleep_fn=lambda s: None)
    assert out["aged_out"] == 1, "real data loss must be visible in the counters"


def test_the_iso_minute_uses_a_T_separator(conn):
    """SQLite `datetime('now')` renders a space, and ' ' sorts before 'T' — a 24h window silently
    reached ~40h back on 2026-08-23."""
    assert "T" in ct._minute_iso(BUY) and " " not in ct._minute_iso(BUY)
    assert ct._minute_iso(BUY).endswith("+00:00")


def test_an_unmapped_chain_is_out_of_scope_not_queued_work(conn):
    """GT_OHLCV_NETWORK is an allowlist of live-verified slugs, so an unmapped chain can never be
    fetched. Queuing it would park an item that must age out, and the age-out counter is supposed
    to mean REAL DATA LOSS. Caught by running discovery against the live ledger, which returned
    8453 and 56 pools alongside solana.

    The example chain is 56, not 8453: the `base` slug was probed live on 2026-09-06 (HTTP 200,
    100 candles) and 8453 is now MAPPED, so it no longer demonstrates the unmapped case."""
    _seed(conn, eid="e8", net="56", pool="0xdead")
    items, no_pool, no_chain = ct.discover(
        conn, since_iso=ct._minute_iso(NOW - timedelta(hours=2)), now_iso=ct._minute_iso(NOW))
    assert items == [] and no_chain == 1 and no_pool == 0


def test_a_real_429_raises_rather_than_returning_none():
    """`urlopen` RAISES on 4xx, so a status-based 429 check is dead code.

    Before this was fixed the collector saw a rate limit as an ordinary provider error and walked
    on to the next pool — retrying into the limit, on a shared IP the funded lane uses. Found by
    running the real client against the live API.
    """
    import urllib.error
    from sniper.execution.geckoterminal_quote import GtRateLimited, gt_pool_ohlcv_minute

    def raising(code):
        def http(method, url, headers=None, body=None):
            raise urllib.error.HTTPError(url, code, "boom", {}, None)
        return http

    with pytest.raises(GtRateLimited):
        gt_pool_ohlcv_minute("solana", "P", http=raising(429))
    # everything else degrades to None rather than exploding
    assert gt_pool_ohlcv_minute("solana", "P", http=raising(404)) is None
    assert gt_pool_ohlcv_minute("solana", "P", http=raising(500)) is None


def test_an_unmapped_chain_never_calls_the_provider():
    """GT_OHLCV_NETWORK is an allowlist; a guessed slug would price a coin against the wrong chain."""
    from sniper.execution.geckoterminal_quote import gt_pool_ohlcv_minute
    called = []

    def http(method, url, headers=None, body=None):
        called.append(url)
        return 200, {}, None

    assert gt_pool_ohlcv_minute("56", "0xdead", http=http) is None      # 8453 is mapped since 09-06
    assert called == []


def test_the_lane_writes_its_own_file_and_never_the_funded_ledger(tmp_path):
    """A measurement lane gets NO writer on sniper_tracker.db. The ledger supplies the work and the
    yield signal read-only; every write lands in this lane's own database.

    The write-amplification finding is a day old — an hourly collector has no business adding pages
    to the file the funded runner and the web contend on.
    """
    ledger = sqlite3.connect(str(tmp_path / "ledger.db"))
    ledger.row_factory = sqlite3.Row
    _ledger_tables(ledger)
    _seed(ledger)
    before = {r[0] for r in ledger.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    lane = store.connect(tmp_path / "chart_truth.db")
    page = _page([ct._minute_iso(BUY)])
    out = ct.run_once(lane, ledger, now_fn=lambda: ct._minute_iso(NOW),
                      fetch_fn=lambda *a, **k: page, sleep_fn=lambda s: None)

    assert out["rows"] == 1
    after = {r[0] for r in ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert after == before, f"the lane created tables on the ledger: {after - before}"
    assert lane.execute("SELECT COUNT(*) FROM memecoin_chart_truth").fetchone()[0] == 1
    assert store.DB_PATH.name == "memecoin_chart_truth.db"


def test_the_yield_signal_is_read_from_the_ledger_not_the_lane(tmp_path):
    """The signal lives in memecoin_position_marks, which is the ledger's table — reading it from
    the lane's own database would silently always return zero and the yield would never fire."""
    ledger = sqlite3.connect(str(tmp_path / "l.db"))
    ledger.row_factory = sqlite3.Row
    _ledger_tables(ledger)
    _seed(ledger)
    ledger.executemany("INSERT INTO memecoin_position_marks VALUES('geckoterminal', ?)",
                       [(ct._minute_iso(NOW),) for _ in range(200)])
    ledger.commit()
    lane = store.connect(tmp_path / "c.db")
    called = []
    out = ct.run_once(lane, ledger, now_fn=lambda: ct._minute_iso(NOW),
                      fetch_fn=lambda *a, **k: called.append(1) or {}, sleep_fn=lambda s: None)
    assert out["skipped_cycle"] == 1 and out["funded_marks"] == 200 and not called


def test_discovery_does_not_depend_on_a_paper_ADMISSION_decision(conn):
    """A collector must not learn that a trade happened from an admission decision.

    Discovery read `memecoin_entry_snapshots`, which is written inside the paper-arm admission path,
    so the empty-arms guard starved it: 355 buy events against 60 snapshots on 2026-08-28. Events
    are written at the ingest seam, upstream of arms, and `copy_event_to_arms` never touches them.
    """
    _seed(conn, eid="e1")
    # an event with NO entry snapshot at all — the shape a dead paper lane produces
    assert conn.execute("SELECT COUNT(*) FROM memecoin_entry_snapshots").fetchone()[0] == 0
    items, no_pool, no_chain = ct.discover(
        conn, since_iso=ct._minute_iso(NOW - timedelta(hours=2)), now_iso=ct._minute_iso(NOW))
    assert len(items) == 3, "the window must be discovered from the EVENT alone"


def test_discovery_makes_no_provider_call(conn, monkeypatch):
    """Pool resolution is a lookup, not a request — discovery runs BEFORE the yield check, so a
    request here would escape the budget and the yield rules entirely."""
    called = []
    monkeypatch.setattr(ct, "gt_pool_ohlcv_minute", lambda *a, **k: called.append(1))
    _seed(conn)
    ct.discover(conn, since_iso=ct._minute_iso(NOW - timedelta(hours=2)),
                now_iso=ct._minute_iso(NOW))
    assert not called


def test_queued_counts_real_inserts_and_is_always_reported(conn):
    """`INSERT OR IGNORE` drops duplicates silently, so returning the input length reported every
    re-seen item as newly queued — a counter that can only agree with its input is not a
    measurement."""
    _seed(conn)
    first = ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW),
                        fetch_fn=lambda *a, **k: None, sleep_fn=lambda s: None)
    assert first["queued"] == 3
    second = ct.run_once(conn, now_fn=lambda: ct._minute_iso(NOW),
                         fetch_fn=lambda *a, **k: None, sleep_fn=lambda s: None)
    assert "queued" in second, "the field must be emitted on every pass, not only on a yield"
    assert second["queued"] == 0, "re-seeing the same work is not new work"
