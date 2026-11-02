"""Original ingress clocks survive decoding, drain and native/shared handoff."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from sniper.feeds import j7_feed
from sniper.feeds.fomo_feed import normalize_feed_observation
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_entry_trace import EntryAttemptTrace
from sniper.util import receipt_clock as rc


RAW = "2026-10-11T12:00:00.050000+00:00"
CORRECTED = "2026-10-11T12:00:00+00:00"
DRAIN = "2026-10-11T12:00:01+00:00"


def stamp(**overrides):
    return dict(version=1, raw_utc=RAW, corrected_utc=CORRECTED, correction_s=0.05,
                monotonic_ns=1_000_000_000, boot_id="host:boot", producer_id="watcher:run1",
                sequence=1, **overrides)


class Clock:
    ns = 1_020_000_000

    def perf_counter(self):
        return self.ns / 1_000_000_000

    def monotonic_ns(self):
        return self.ns


def test_raw_clock_50ms_ahead_has_positive_original_ingress_interval(monkeypatch):
    monkeypatch.setattr(rc, "clock_domain", lambda: "host:boot")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_tables(conn)
    clock = Clock()
    event = SimpleNamespace(network_id="56", mint="mint", fomo_event_id="tx1",
                            received_at=RAW, received_at_normalized=RAW,
                            receipt_clock=stamp())
    trace = EntryAttemptTrace.start(conn, event, "2026-10-11T12:00:00.020000+00:00", clock)
    # A later UTC correction must not change either original T0 or monotonic elapsed.
    clock.ns = 1_035_000_000
    trace.mark_substage("physical_send")
    trace.finish("refused", "fixture")
    row = dict(conn.execute("SELECT observed_at, trace_json FROM memecoin_entry_attempts").fetchone())
    payload = json.loads(row["trace_json"])
    assert row["observed_at"] == CORRECTED
    assert payload["decision_receipt_ms"] == 20.0
    assert payload["receipt_clock"]["raw_utc"] == RAW
    assert payload["substage_events"][0]["receipt_elapsed_ms"] == 35.0
    assert payload["substage_events"][0]["monotonic_ns"] == 1_035_000_000
    conn.close()


def test_capture_uses_cached_correction_and_boot_without_ingress_reads(monkeypatch):
    monkeypatch.setattr(rc, "clock_domain", lambda: "host:boot")
    from sniper.util import clock
    monkeypatch.setattr(clock, "offset_s", lambda: 0.05)
    producer = rc.ReceiptClock("watcher", raw_time_fn=lambda: 1791720000.05)
    monkeypatch.setattr(clock, "offset_s", lambda: pytest.fail("ingress read correction file"))
    anchor = producer.capture(monotonic_ns=1_000_000_000)
    assert anchor["corrected_utc"] == CORRECTED
    assert anchor["boot_id"] == "host:boot"
    assert anchor["sequence"] == 1
    with pytest.raises(TypeError):
        anchor["monotonic_ns"] = 5
    with pytest.raises(TypeError):
        anchor.update(monotonic_ns=5)
    assert json.loads(json.dumps(anchor))["monotonic_ns"] == 1_000_000_000


def test_correction_refresh_changes_future_stamps_without_rewriting_t0(monkeypatch):
    from sniper.util import clock
    offset = [0.05]
    monkeypatch.setattr(clock, "offset_s", lambda: offset[0])
    producer = rc.ReceiptClock("watcher", raw_time_fn=lambda: 1791720000.05)
    first = producer.capture(monotonic_ns=1_000_000_000)
    offset[0] = .15
    producer.refresh_correction()
    later = producer.capture(monotonic_ns=1_020_000_000)
    assert first["corrected_utc"] == CORRECTED
    assert later["corrected_utc"] == "2026-10-11T11:59:59.900000+00:00"
    assert rc.elapsed_receipt_ms(first, later["monotonic_ns"], boot_id=first["boot_id"]) == 20.0


@pytest.mark.parametrize("value", [None, {}, {**stamp(), "boot_id": "other:boot"},
                                   {**stamp(), "monotonic_ns": -1},
                                   {**stamp(), "monotonic_ns": True}])
def test_foreign_or_missing_anchor_is_unknown(value):
    assert rc.elapsed_receipt_ms(value, 1_020_000_000, boot_id="host:boot") is None


def relay(event_id="opaque-1", tx=None):
    data = dict(id=event_id, userId="u1", side="buy", usdAmount=100.,
                timestamp=CORRECTED, token={"address": "MINT", "networkId": "56"})
    if tx:
        data["transactionHash"] = tx
    return dict(kind="trade", data=data)


def test_j7_drain_preserves_ingress_clock_without_changing_source_age_policy(monkeypatch):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    feed = j7_feed.J7WalletsFeed("token")
    feed._mono_fn = lambda: 1_000_000_000
    feed.observe(relay())
    raw = feed.drain()[0][0]
    observation = normalize_feed_observation(raw, DRAIN, None)
    event = observation.event
    assert event.receipt_clock["monotonic_ns"] == 1_000_000_000
    assert event.receipt_clock["corrected_utc"] == CORRECTED
    assert event.received_at == DRAIN
    assert event.received_at_normalized == DRAIN
    assert observation.observed_at == DRAIN
    # Native -> shared metadata and JSON IPC retain one T0, without a new capture.
    carried = rc.freeze_receipt_clock(json.loads(json.dumps(event.receipt_clock)))
    assert carried == event.receipt_clock


def test_duplicate_exact_transaction_keeps_first_stamp_but_opaque_ids_stay_separate(monkeypatch):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    feed = j7_feed.J7WalletsFeed("token")
    ticks = iter([1_000_000_000, 1_050_000_000, 1_060_000_000, 1_070_000_000])
    feed._mono_fn = lambda: next(ticks)
    tx = "0x" + "ab" * 32
    feed.observe(relay("exact-1", tx))
    feed.observe(relay("exact-2", tx))
    feed.observe(relay("opaque-1"))
    feed.observe(relay("opaque-2"))
    items = feed.drain()[0]
    assert [item["receipt_clock"]["monotonic_ns"] for item in items] == [
        1_000_000_000, 1_000_000_000, 1_060_000_000, 1_070_000_000]
    assert items[2]["receipt_identity_status"] == "source_identity_unknown"
    assert items[3]["receipt_identity_status"] == "source_identity_unknown"


def test_runner_normalization_keeps_j7_t0_on_the_shared_copy_event(monkeypatch):
    from sniper.services import memecoin_runner as runner
    from sniper.persistence import db_memecoin
    from sniper.settings import memecoin as settings
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    feed = j7_feed.J7WalletsFeed("token")
    feed._mono_fn = lambda: 1_000_000_000
    feed.observe(relay())
    monkeypatch.setattr(j7_feed, "j7_feed_or_none", lambda **kwargs: feed)
    monkeypatch.setattr(settings, "MEMECOIN_FIREHOSE_ENABLED", False)
    monkeypatch.setattr(runner, "write_feed_snapshot", lambda events: None)
    monkeypatch.setattr(runner, "_flag_new_networks", lambda *args: None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_tables(conn)
    monkeypatch.setattr(db_memecoin, "curated_user_ids", lambda conn: {"u1"})
    captured = []
    normalize = runner.normalize_feed_observation
    def capture(raw, observed, epoch):
        observation = normalize(raw, observed, epoch)
        captured.append(observation.event)
        return observation
    monkeypatch.setattr(runner, "normalize_feed_observation", capture)
    runner.run_once(conn, auth=None, size_usd=10, now_fn=lambda: DRAIN,
                    stats_fn=lambda *args, **kwargs: {})
    assert len(captured) == 1
    assert captured[0].receipt_clock["monotonic_ns"] == 1_000_000_000
    assert captured[0].receipt_clock["corrected_utc"] == CORRECTED
    assert captured[0].received_at_normalized == DRAIN
    conn.close()


def test_remember_uses_earliest_same_boot_capture_not_first_lock_winner():
    producer = rc.ReceiptClock("reordered")
    early = producer.capture(monotonic_ns=100)
    later = producer.capture(monotonic_ns=200)
    producer.remember("exact-tx", later)
    assert producer.remember("exact-tx", early)["monotonic_ns"] == 100


def test_evicted_receipt_history_is_unknown_for_redelivery_and_genuinely_new_keys():
    producer = rc.ReceiptClock("bounded", cap=1)
    a = producer.remember("A", producer.capture(monotonic_ns=100))
    b = producer.remember("B", producer.capture(monotonic_ns=200))
    assert rc.freeze_receipt_clock(a) is a
    assert rc.freeze_receipt_clock(b) is b
    assert producer.evicted == 1
    # The retained original key remains usable after a different key was evicted.
    retained = producer.remember("B", producer.capture(monotonic_ns=250))
    assert retained["monotonic_ns"] == 200
    assert rc.freeze_receipt_clock(retained) is retained
    repeated = producer.remember("A", producer.capture(monotonic_ns=300))
    assert repeated["monotonic_ns"] == 300
    assert repeated["history_unknown"] is True
    assert rc.freeze_receipt_clock(repeated) is None
    assert rc.freeze_receipt_clock(json.loads(json.dumps(repeated))) is None
    assert rc.elapsed_receipt_ms(repeated, 350, boot_id=repeated["boot_id"]) is None
    # An absent key can be new or forgotten; bounded memory cannot distinguish the two.
    new = producer.remember("genuinely-new", producer.capture(monotonic_ns=400))
    assert new["history_unknown"] is True
    assert rc.freeze_receipt_clock(new) is None
    with pytest.raises(TypeError):
        new["history_unknown"] = False


def test_known_and_remember_cannot_strip_unknown_history_from_either_input():
    producer = rc.ReceiptClock("bounded", cap=1)
    producer.remember("A", producer.capture(monotonic_ns=100))
    producer.remember("B", producer.capture(monotonic_ns=200))
    unknown = producer.known("A", producer.capture(monotonic_ns=300))
    assert rc.freeze_receipt_clock(unknown) is None
    destination = rc.ReceiptClock("destination")
    destination.remember("A", destination.capture(monotonic_ns=250))
    assert rc.freeze_receipt_clock(destination.remember("A", unknown)) is None
    # An earlier clean raw stamp must not resurrect a history-censored retained key.
    assert rc.freeze_receipt_clock(destination.known("A", destination.capture(monotonic_ns=50))) is None


@pytest.mark.parametrize("accounts", [False, True])
def test_j7_evicted_redelivery_normalizes_to_unknown_without_resetting_admission(monkeypatch, accounts):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    owner = (j7_feed.J7AccountsFeed(["first", "second"]) if accounts
             else j7_feed.J7WalletsFeed("token"))
    feed = owner._feeds[0] if accounts else owner
    owner._receipt_anchors = rc.ReceiptClock("bounded", cap=1)
    ticks = iter([100, 200, 250, 300, 400])
    feed._mono_fn = lambda: next(ticks)
    a, b, c = ("0x" + pair * 32 for pair in ("ab", "cd", "ef"))
    for event_id, tx, expected in [("A-original", a, 100), ("B-original", b, 200),
                                   ("B-retained", b, 200), ("A-redelivery", a, None),
                                   ("C-new", c, None)]:
        feed.observe(relay(event_id, tx))
        item = owner.drain()[0][0]
        assert item["id"] == event_id
        event = normalize_feed_observation(item, DRAIN, None).event
        assert event.received_at == event.received_at_normalized == DRAIN
        if expected is None:
            assert item["receipt_clock"] is None
            assert item["receipt_identity_status"] == "receipt_history_unknown"
            assert event.receipt_clock is None
        else:
            assert item["receipt_clock"]["monotonic_ns"] == expected
            assert event.receipt_clock["monotonic_ns"] == expected


def test_asynchronous_j7_callbacks_keep_earlier_capture_after_reordered_parsing(monkeypatch):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    feed = j7_feed.J7WalletsFeed("token")
    early_thread = []
    feed._mono_fn = lambda: 100 if threading.get_ident() in early_thread else 200
    entered, release = threading.Event(), threading.Event()
    original = j7_feed.fomo_event_to_feed_item
    def decode(data):
        if data["data"]["id"] == "early":
            entered.set()
            assert release.wait(2)
        return original(data)
    monkeypatch.setattr(j7_feed, "fomo_event_to_feed_item", decode)
    tx = "0x" + "ab" * 32
    def observe_early():
        early_thread.append(threading.get_ident())
        feed.observe(relay("early", tx))
    thread = threading.Thread(target=observe_early)
    thread.start()
    try:
        assert entered.wait(1)
        feed.observe(relay("later", tx))
    finally:
        release.set()
        thread.join(2)
    items = feed.drain()[0]
    assert [item["id"] for item in items] == ["later", "early"]
    assert [item["received_mono_ns"] for item in items] == [200, 100]
    assert [item["receipt_clock"]["monotonic_ns"] for item in items] == [100, 100]


def test_two_j7_accounts_reconcile_earliest_t0_without_changing_account_drain_order(monkeypatch):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    accounts = j7_feed.J7AccountsFeed(["first", "second"])
    first, second = accounts._feeds
    first._mono_fn, second._mono_fn = lambda: 200, lambda: 100
    tx = "0x" + "ab" * 32
    second.observe(relay("early-account2", tx))
    first.observe(relay("later-account1", tx))
    items = accounts.drain()[0]
    assert [item["id"] for item in items] == ["later-account1", "early-account2"]
    assert [item["received_mono_ns"] for item in items] == [200, 100]
    assert [item["receipt_clock"]["monotonic_ns"] for item in items] == [100, 100]
    # An account delivered in a later batch still keeps the known earliest exact transaction.
    first._mono_fn = lambda: 300
    first.observe(relay("late-redelivery", tx))
    assert accounts.drain()[0][0]["receipt_clock"]["monotonic_ns"] == 100


def test_account_callback_between_child_drains_updates_the_whole_published_batch(monkeypatch):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    accounts = j7_feed.J7AccountsFeed(["first", "second"])
    first, second = accounts._feeds
    first._mono_fn, second._mono_fn = lambda: 200, lambda: 100
    tx = "0x" + "ab" * 32
    first.observe(relay("later-account1", tx))
    drain = first.drain
    def between():
        batch = drain()
        second.observe(relay("early-account2", tx))
        return batch
    monkeypatch.setattr(first, "drain", between)
    items = accounts.drain()[0]
    assert [item["id"] for item in items] == ["later-account1", "early-account2"]
    assert [item["receipt_clock"]["monotonic_ns"] for item in items] == [100, 100]


@pytest.mark.parametrize("accounts", [False, True])
@pytest.mark.parametrize("blocked", ["decode", "utc"])
def test_drain_before_earlier_callback_completes_keeps_t0_unknown(monkeypatch, accounts, blocked):
    owner = (j7_feed.J7AccountsFeed(["first", "second"]) if accounts
             else j7_feed.J7WalletsFeed("token"))
    early, later = (owner._feeds[1], owner._feeds[0]) if accounts else (owner, owner)
    early_thread = []
    early._mono_fn = lambda: 100 if threading.get_ident() in early_thread else 200
    if accounts:
        later._mono_fn = lambda: 200
    entered, release = threading.Event(), threading.Event()
    original = j7_feed.fomo_event_to_feed_item
    def wait_early():
        if threading.get_ident() in early_thread:
            entered.set()
            assert release.wait(2)
    def decode(data):
        if blocked == "decode":
            wait_early()
        return original(data)
    def utc():
        if blocked == "utc":
            wait_early()
        return CORRECTED
    monkeypatch.setattr(j7_feed, "fomo_event_to_feed_item", decode)
    monkeypatch.setattr(j7_feed, "_now_iso", utc)
    tx = "0x" + "ab" * 32
    def observe_early():
        early_thread.append(threading.get_ident())
        early.observe(relay("early-unresolved", tx))
    thread = threading.Thread(target=observe_early)
    thread.start()
    try:
        assert entered.wait(1)
        later.observe(relay("later-complete", tx))
        items = owner.drain()[0]
        assert [item["id"] for item in items] == ["later-complete"]
        assert items[0]["received_mono_ns"] == 200
        assert items[0]["receipt_clock"] is None
        assert items[0]["receipt_identity_status"] == "earlier_receipt_unresolved"
        event = normalize_feed_observation(items[0], DRAIN, None).event
        assert event.receipt_clock is None
        assert event.received_at == event.received_at_normalized == DRAIN
        conn = sqlite3.connect(":memory:")
        ensure_memecoin_tables(conn)
        trace = EntryAttemptTrace.start(conn, event, DRAIN, Clock())
        trace.mark_substage("physical_send")
        trace.finish("refused", "fixture")
        payload = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])
        assert payload.get("receipt_clock") is None
        assert payload.get("decision_receipt_ms") is None
        assert payload["substage_events"][0].get("receipt_elapsed_ms") is None
        conn.close()
    finally:
        release.set()
        thread.join(2)
    completed = owner.drain()[0]
    assert [item["id"] for item in completed] == ["early-unresolved"]
    assert completed[0]["receipt_clock"]["monotonic_ns"] == 100


def test_unresolved_different_exact_transaction_does_not_censor_t0(monkeypatch):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    feed = j7_feed.J7WalletsFeed("token")
    early_thread = []
    feed._mono_fn = lambda: 100 if threading.get_ident() in early_thread else 200
    entered, release = threading.Event(), threading.Event()
    original = j7_feed.fomo_event_to_feed_item
    def decode(data):
        if data["data"]["id"] == "early-other-tx":
            entered.set()
            assert release.wait(2)
        return original(data)
    monkeypatch.setattr(j7_feed, "fomo_event_to_feed_item", decode)
    def observe_early():
        early_thread.append(threading.get_ident())
        feed.observe(relay("early-other-tx", "0x" + "ab" * 32))
    thread = threading.Thread(target=observe_early)
    thread.start()
    try:
        assert entered.wait(1)
        feed.observe(relay("later-tx", "0x" + "cd" * 32))
        assert feed.drain()[0][0]["receipt_clock"]["monotonic_ns"] == 200
    finally:
        release.set()
        thread.join(2)


@pytest.mark.parametrize("accounts", [False, True])
def test_prelock_callback_capture_cannot_publish_falsely_later_t0(monkeypatch, accounts):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    owner = (j7_feed.J7AccountsFeed(["first", "second"]) if accounts
             else j7_feed.J7WalletsFeed("token"))
    early, later = (owner._feeds[1], owner._feeds[0]) if accounts else (owner, owner)
    early_thread = []
    early._mono_fn = lambda: 100 if threading.get_ident() in early_thread else 200
    if accounts:
        later._mono_fn = lambda: 200
    entered, release = threading.Event(), threading.Event()
    lock = early._lock
    class ExistingLock:
        paused = False
        def __enter__(self):
            if threading.get_ident() in early_thread and not self.paused:
                self.paused = True
                entered.set()
                assert release.wait(2)
            return lock.__enter__()
        def __exit__(self, *args):
            return lock.__exit__(*args)
    early._lock = ExistingLock()
    tx = "0x" + "ab" * 32
    def observe_early():
        early_thread.append(threading.get_ident())
        early.observe(relay("early-prelock", tx))
    thread = threading.Thread(target=observe_early)
    thread.start()
    try:
        assert entered.wait(1)
        later.observe(relay("later-complete", tx))
        items = owner.drain()[0]
        assert [item["id"] for item in items] == ["later-complete"]
        assert items[0]["received_mono_ns"] == 200
        assert items[0]["receipt_clock"] is None
        assert items[0]["receipt_identity_status"] == "earlier_receipt_unresolved"
    finally:
        release.set()
        thread.join(2)
    assert owner.drain()[0][0]["receipt_clock"]["monotonic_ns"] == 100


def test_bounded_prelock_marker_saturation_stays_censored_and_counted(monkeypatch):
    monkeypatch.setattr(j7_feed, "_now_iso", lambda: CORRECTED)
    feed = j7_feed.J7WalletsFeed("token", receipt_buffer=2)
    early_thread = []
    feed._mono_fn = lambda: 100 if threading.get_ident() in early_thread else 200
    entered, release = threading.Event(), threading.Event()
    lock = feed._lock
    class ExistingLock:
        paused = False
        def __enter__(self):
            if threading.get_ident() in early_thread and not self.paused:
                self.paused = True
                entered.set()
                assert release.wait(2)
            return lock.__enter__()
        def __exit__(self, *args):
            return lock.__exit__(*args)
    feed._lock = ExistingLock()
    tx = "0x" + "ab" * 32
    def observe_early():
        early_thread.append(threading.get_ident())
        feed.observe(relay("early-prelock", tx))
    thread = threading.Thread(target=observe_early)
    thread.start()
    try:
        assert entered.wait(1)
        feed.observe(relay("at-capacity", tx))
        assert feed.drain()[0][0]["receipt_clock"] is None
        assert len(feed._receipt_entering) <= 2
    finally:
        release.set()
        thread.join(2)
    # Conservative saturation cannot turn into a valid clock after markers have vanished.
    assert feed.drain()[0][0]["receipt_clock"] is None
    feed.observe(relay("after-saturation", tx))
    assert feed.drain()[0][0]["receipt_clock"] is None
    assert feed.status()["receipt_ingress_overflow"] == 1
    assert feed.status()["receipt_ingress_pending"] == 0
    assert not feed._receipt_entering


@pytest.mark.parametrize("field", ["monotonic_ns", "boot_id", "receipt_elapsed_ms"])
def test_submission_metadata_cannot_override_bound_clock_fields(field):
    conn = sqlite3.connect(":memory:")
    ensure_memecoin_tables(conn)
    event = SimpleNamespace(network_id="56", fomo_event_id="tx", receipt_clock=stamp())
    trace = EntryAttemptTrace.start(conn, event, CORRECTED, Clock())
    with pytest.raises(ValueError, match="reserved fields"):
        trace.mark_submission("transaction_submitted", 1, **{field: None})
    conn.close()
