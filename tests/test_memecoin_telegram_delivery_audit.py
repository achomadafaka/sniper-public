import hashlib
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_telegram import (
    _TelegramDispatcher, _persist_delivery_audit, redact_telegram_text)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def test_delivery_audit_schema_and_insert_helper():
    conn = _conn()
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(memecoin_telegram_delivery_audit)")}
    assert {
        "message_kind", "source_id", "queued_at", "send_started_at", "completed_at",
        "send_ok", "send_ms", "rendered_text_redacted", "message_sha256", "error_class",
    } <= cols

    row_id = m.record_telegram_delivery(conn, {
        "message_kind": "operational",
        "source_id": "position:7",
        "queued_at": "2026-08-05T12:00:00+00:00",
        "send_started_at": "2026-08-05T12:00:01+00:00",
        "completed_at": "2026-08-05T12:00:02+00:00",
        "send_ok": True,
        "send_ms": 1000.0,
        "rendered_text_redacted": "safe",
        "message_sha256": "a" * 64,
        "error_class": None,
    })

    row = conn.execute(
        "SELECT * FROM memecoin_telegram_delivery_audit WHERE id=?", (row_id,)).fetchone()
    assert row["message_kind"] == "operational"
    assert row["send_ok"] == 1


def test_delivery_audit_attempt_migrates_and_records_resend():
    conn = _conn()
    conn.execute("ALTER TABLE memecoin_telegram_delivery_audit DROP COLUMN attempt")
    m.ensure_memecoin_tables(conn)
    row_id = m.record_telegram_delivery(conn, {
        "message_kind": "entry", "source_id": "same-source", "attempt": 2,
        "queued_at": "2026-10-26T16:00:00+00:00", "send_started_at": "2026-10-26T16:00:05+00:00",
        "completed_at": "2026-10-26T16:00:06+00:00", "send_ok": True, "send_ms": 1,
        "rendered_text_redacted": "RESENT (may be a duplicate)\nBOUGHT", "message_sha256": "a" * 64})
    assert conn.execute("SELECT attempt,source_id FROM memecoin_telegram_delivery_audit WHERE id=?", (row_id,)).fetchone()[:] == (2, "same-source")


def test_redactor_removes_tap_url_only():
    raw = "BUY\n⚡ tap: https://example.test/signed-secret\nmint: M"
    assert redact_telegram_text(raw) == "BUY\n⚡ tap: [redacted]\nmint: M"


def test_dispatcher_records_redacted_text_exact_hash_and_success():
    rows = []
    raw = "BUY\n⚡ tap: https://example.test/signed-secret\nmint: M"
    stamps = iter([
        "2026-08-05T12:00:00+00:00",
        "2026-08-05T12:00:01+00:00",
        "2026-08-05T12:00:02+00:00",
    ])
    dispatcher = _TelegramDispatcher(
        lambda _text: (True, 12.5), maxsize=2, audit_fn=rows.append,
        now_fn=lambda: next(stamps))

    assert dispatcher.enqueue(raw, kind="front_run", source_id="event:e1") is True
    dispatcher._queue.join()

    assert len(rows) == 1
    row = rows[0]
    assert row["message_kind"] == "front_run"
    assert row["source_id"] == "event:e1"
    assert row["send_ok"] is True and row["send_ms"] == 12.5
    assert row["message_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert row["rendered_text_redacted"] == "BUY\n⚡ tap: [redacted]\nmint: M"
    assert "signed-secret" not in row["rendered_text_redacted"]
    assert row["error_class"] is None


def test_dispatcher_records_sender_exception_and_continues():
    rows = []

    def fail(_text):
        raise RuntimeError("telegram down")

    dispatcher = _TelegramDispatcher(fail, maxsize=2, audit_fn=rows.append)
    assert dispatcher.enqueue("one") is True
    assert dispatcher.enqueue("two") is True
    dispatcher._queue.join()

    assert len(rows) == 2
    assert all(row["send_ok"] is False for row in rows)
    assert all(row["error_class"] == "RuntimeError" for row in rows)


@pytest.mark.parametrize("second_unknown", [False, True])
def test_dispatcher_resends_unknown_once_without_blocking_new_messages(second_unknown):
    import threading
    from sniper.services.memecoin_telegram import TelegramDeliveryUnknown
    sent, audits, results = [], [], []
    done = threading.Event()
    def send(text):
        sent.append(text)
        if text == "BOUGHT" or (second_unknown and text.startswith("RESENT")):
            raise TelegramDeliveryUnknown("timeout")
        return True, 1
    d = _TelegramDispatcher(send, maxsize=4, audit_fn=audits.append, retry_after_s=0.05)
    d.enqueue("BOUGHT", kind="entry", source_id="same", on_result=lambda ok: (results.append(ok), done.set()))
    d.enqueue("fresh", source_id="other")
    assert done.wait(2)
    d._queue.join()
    assert sent == ["BOUGHT", "fresh", "RESENT (may be a duplicate)\nBOUGHT"]
    own = [a for a in audits if a["source_id"] == "same"]
    assert [a["attempt"] for a in own] == [1, 2] and results == [not second_unknown]
    assert d._retries == []


def test_dispatcher_audit_exception_does_not_kill_sender():
    sent = []

    def audit_fail(_fields):
        raise sqlite3.OperationalError("audit unavailable")

    dispatcher = _TelegramDispatcher(
        lambda text: sent.append(text) or (True, 1.0), maxsize=2, audit_fn=audit_fail)
    assert dispatcher.enqueue("one") is True
    assert dispatcher.enqueue("two") is True
    dispatcher._queue.join()

    assert sent == ["one", "two"]


def test_front_run_latency_row_records_redacted_text_and_exact_hash():
    from sniper.services.memecoin_runner import _send_prepared

    conn = _conn()
    assert m.record_alert(conn, {
        "fomo_event_id": "e1",
        "source_user_id": "u1",
        "detected_at": "2026-08-05T12:00:00+00:00",
    }) is True
    raw = "BUY\n⚡ tap: https://example.test/signed-secret\nmint: M"

    assert _send_prepared(
        conn, "e1", raw, 4.0, lambda _text: (True, 12.0)) is True

    row = conn.execute(
        "SELECT * FROM memecoin_alert_latency WHERE fomo_event_id='e1'").fetchone()
    assert row["rendered_text_redacted"] == "BUY\n⚡ tap: [redacted]\nmint: M"
    assert row["message_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert "signed-secret" not in row["rendered_text_redacted"]


@pytest.mark.parametrize("db_path", [None, "sniper_tracker.db"])
def test_delivery_audit_defaults_to_state_root_without_an_absolute_db_path(
        monkeypatch, tmp_path, db_path):
    state = tmp_path / "state"
    release = tmp_path / "release"
    state.mkdir()
    release.mkdir()
    conn = sqlite3.connect(state / "sniper_tracker.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.close()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(state))
    if db_path is None:
        monkeypatch.delenv("DB_PATH", raising=False)
    else:
        monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.chdir(release)

    _persist_delivery_audit({
        "message_kind": "test",
        "source_id": "state-root",
        "queued_at": "2026-08-05T12:00:00+00:00",
        "send_started_at": "2026-08-05T12:00:01+00:00",
        "completed_at": "2026-08-05T12:00:02+00:00",
        "send_ok": True,
        "send_ms": 1.0,
        "rendered_text_redacted": "safe",
        "message_sha256": "a" * 64,
        "error_class": None,
    })

    conn = sqlite3.connect(state / "sniper_tracker.db")
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_telegram_delivery_audit WHERE source_id='state-root'"
    ).fetchone()[0] == 1
    assert not (release / "sniper_tracker.db").exists()


# --- the audit table must record DELIVERIES, not test runs (2026-08-06) -------------------
# The funded ledger held 567 test-written rows against 145 real ones. Cause: send_telegram fails
# closed on a missing token, but the dispatcher's `finally` wrote the audit row anyway, and
# conftest points DB_PATH at the real sniper_tracker.db so tests can read it.

def test_enqueue_is_a_noop_without_a_configured_channel(monkeypatch):
    """No token/chat -> no dispatcher, no thread, no audit row. This is the production fix:
    a message that was never attempted is not a delivery."""
    import sniper.services.memecoin_telegram as tg
    monkeypatch.setenv("MEMECOIN_TG_DISPATCH_ENABLED", "1")      # isolate from the conftest pin
    for var in ("MEMECOIN_TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN",
                "MEMECOIN_TG_CHAT_ID", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(tg, "_dispatcher", None)
    audited = []
    monkeypatch.setattr(tg, "_persist_delivery_audit", audited.append)
    assert tg.dispatch_enabled() is False
    assert tg.enqueue_telegram("x", kind="operational:TEST") is False
    assert tg._dispatcher is None, "an unconfigured channel must not start a dispatcher thread"
    assert audited == []


def test_dispatch_kill_switch_beats_a_fully_configured_channel(monkeypatch):
    """The hard off switch, for a process holding a valid token that must stay silent. Conftest
    sets it as defense in depth over the already-blanked test tokens."""
    import sniper.services.memecoin_telegram as tg
    monkeypatch.setenv("MEMECOIN_TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("MEMECOIN_TG_CHAT_ID", "123")
    monkeypatch.setenv("MEMECOIN_TG_DISPATCH_ENABLED", "1")
    assert tg.dispatch_enabled() is True                          # configured => would send
    monkeypatch.setenv("MEMECOIN_TG_DISPATCH_ENABLED", "0")
    monkeypatch.setattr(tg, "_dispatcher", None)
    assert tg.dispatch_enabled() is False
    assert tg.enqueue_telegram("x") is False
    assert tg._dispatcher is None


def test_the_suite_itself_runs_with_dispatch_disabled():
    """Guards the conftest pin: if someone drops _pin_telegram_dispatch_off(), this fails and the
    ledger starts collecting test rows again."""
    import sniper.services.memecoin_telegram as tg
    assert tg.dispatch_enabled() is False


# A SYNCHRONOUS SEND WRITES ITS OWN AUDIT ROW (Alpha 2026-10-16): the deploy's one-shot page has no
# dispatcher, and only the dispatcher wrote memecoin_telegram_delivery_audit, so the 19d910279 page
# left no row to check delivery against.

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from sniper.services import memecoin_telegram as tg
    monkeypatch.setattr(tg, "dispatch_enabled", lambda: True)      # a configured channel
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    m.ensure_memecoin_tables(conn)
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def _rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM memecoin_telegram_delivery_audit ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.parametrize("result, ok", [((True, 412.5), 1), ((False, 0.0), 0)])
def test_a_synchronous_send_writes_one_audit_row_with_its_result(ledger, result, ok):
    from sniper.services.memecoin_telegram import send_telegram_audited
    sent = []
    text = "🔴 memecoin deploy | landed with provider term(s): shyft_runtime_prepare_unready"
    got = send_telegram_audited(text, kind="operational:deploy", send_fn=lambda t: sent.append(t) or result)
    assert got == (bool(ok), result[1]) and sent == [text]
    [row] = _rows(ledger)
    assert (row["message_kind"], row["send_ok"], row["send_ms"], row["error_class"]) == (
        "operational:deploy", ok, result[1], None)
    assert row["message_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert row["rendered_text_redacted"] == text and row["queued_at"] == row["send_started_at"]


def test_a_send_that_raises_is_audited_as_failed_and_returns_not_ok(ledger):
    from sniper.services.memecoin_telegram import send_telegram_audited

    def boom(text):
        raise TimeoutError("telegram")
    assert send_telegram_audited("x", kind="operational:deploy", send_fn=boom)[0] is False
    [row] = _rows(ledger)
    assert row["send_ok"] == 0 and row["error_class"] == "TimeoutError"


def test_without_a_configured_channel_no_audit_row_is_written(tmp_path, monkeypatch):
    """Gamma LOW-1: the dispatcher's own rule; conftest blanks every Telegram key, so this is unconfigured."""
    from sniper.services import memecoin_telegram as tg
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    m.ensure_memecoin_tables(conn)
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    assert tg.dispatch_enabled() is False
    assert tg.send_telegram_audited("x", kind="operational:deploy", send_fn=lambda t: (False, 0.0)) == (False, 0.0)
    assert _rows(path) == []


def test_a_failed_audit_write_never_changes_the_send_result(monkeypatch):
    from sniper.services import memecoin_telegram as tg
    monkeypatch.setattr(tg, "dispatch_enabled", lambda: True)
    from sniper.services.memecoin_telegram import send_telegram_audited

    def broken(fields):
        raise sqlite3.OperationalError("database is locked")
    assert send_telegram_audited("x", kind="operational:deploy", send_fn=lambda t: (True, 5.0),
                                 audit_fn=broken) == (True, 5.0)



@pytest.mark.parametrize("raw, off", [("0", True), ("false", True), ("no", True), ("1", False), (None, False)])
def test_the_hard_off_switch_reads_one_way_for_the_dispatcher_and_the_deploy_page(monkeypatch, raw, off):
    from sniper.services import memecoin_telegram as tg
    if raw is None:
        monkeypatch.delenv("MEMECOIN_TG_DISPATCH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_TG_DISPATCH_ENABLED", raw)
    monkeypatch.setattr(tg, "_resolve_token", lambda: "t")
    monkeypatch.setattr(tg, "_resolve_chat", lambda: "c")
    assert tg.dispatch_off() is off and tg.dispatch_enabled() is (not off)
