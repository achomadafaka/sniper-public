"""The notification worker owns durable transition delivery."""
import sqlite3
from types import SimpleNamespace

import pytest


def view(status="evaluating", **fields):
    return dict(origin_event_id="e1", attempt_id="a1", network_id="solana", mint="M",
                ticker="TEST", source_handle="alice", source_user_id="u1", side="buy",
                source_created_at="2026-09-11T12:00:01+00:00",
                observed_at="2026-09-11T12:00:02+00:00",
                completed_at="2026-09-11T12:00:03+00:00", status_updated_at="2026-09-11T12:00:03+00:00",
                status=status, authority={"status": "armed"}, requested_usd=175, **fields)


def worker(send=None, render=None):
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sent = []
    current = {}
    life = NotificationLifecycle(conn, now_fn=lambda: "2026-09-11T12:00:04+00:00",
        read_view=lambda **kw: current[kw["origin_event_id"]],
        send_fn=send or (lambda text: sent.append(text) or (True, 1)),
        render_fn=render or (lambda v: v["status"]), eligible_fn=lambda v: True)
    life.activate("2026-09-11T12:00:00+00:00")
    return life, conn, current, sent


def _source_cards(monkeypatch):
    """The EVALUATING card is muted by default since 2026-09-22; these tests cover it switched on.
    The muted default is covered in tests/test_memecoin_telegram_refusal_collapse.py."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", True)


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM memecoin_notification_transitions ORDER BY created_at, rowid")]


def test_activation_is_durable_and_does_not_replay_history():
    life, conn, current, sent = worker()
    old = view("refused")
    old.update(observed_at="2026-09-10T12:00:00+00:00", completed_at="2026-09-10T12:00:00+00:00", status_updated_at="2026-09-10T12:00:00+00:00")
    current["e1"] = old
    life.observe(old)
    life.deliver()
    assert sent == []
    assert rows(conn)[0]["delivery_state"] == "baseline"
    assert life.activate("2026-09-12T00:00:00+00:00") == "2026-09-11T12:00:00+00:00"


def test_funding_handoff_stays_on_dashboard_and_only_the_fill_pages():
    life, conn, current, sent = worker()
    current['e1'] = view('funding_handoff')
    life.observe(current['e1'])
    life.deliver()
    assert sent == []
    current['e1'] = view('filled', position_id=1, fill_id=1, tx_sig='signed')
    life.observe(current['e1'])
    life.deliver()
    assert sent == ['filled']


def test_processed_receipt_handoff_does_not_page_before_the_confirmed_buy():
    from test_memecoin_entry_view import attempt, fill, view as project
    from sniper.persistence import db_memecoin as m
    life, conn, current, sent = worker()
    m.ensure_memecoin_tables(conn)
    attempt(conn, origin="e1", status="source_initiation_unknown", signal_source="slot_lane",
            delivery_lane="slot_lane_processed", terminal_reason="missing_receipt_proof")
    current["e1"] = project(conn, attempt_id="a1")
    life.observe(current["e1"])
    life.deliver()
    assert sent == []
    attempt(conn, aid="confirmed", origin="onchain:signature", status="filled",
            signal_source="onchain_watch", delivery_lane="onchain_fallback_after_slot_lane")
    fill(conn, aid="confirmed", origin="onchain:signature")
    current["onchain:signature"] = project(conn, attempt_id="confirmed")
    life.observe(current["onchain:signature"])
    life.deliver()
    assert sent == ["filled"]


def test_current_result_supersedes_unsent_source_and_repeat_is_deduplicated(monkeypatch):
    _source_cards(monkeypatch)
    life, conn, current, sent = worker()
    life.observe(view())
    current["e1"] = view("refused", reason="roundtrip_cost")
    life.deliver()
    life.observe(current["e1"])
    life.deliver()
    assert sent == ["refused"]
    assert {r["delivery_state"] for r in rows(conn)} == {"superseded", "delivered"}


def test_final_retry_refusal_is_delivered_after_prior_notice_even_if_filter_changes():
    life, conn, current, sent = worker()
    current["e1"] = view("retry_pending")
    life.observe(current["e1"])
    life.deliver()
    life.eligible_fn = lambda v: False
    current["e1"] = view("refused", reason="retry window expired")
    life.observe(current["e1"])
    life.deliver()
    # retries are silent since 2026-09-11; the silent retry still authorizes the final result
    assert sent == ["refused"]


def test_unknown_then_recovered_fill_has_separate_stable_key(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_UNRESOLVED_GRACE_S", 0.0)  # key contract, not the grace
    life, conn, current, sent = worker()
    current["e1"] = view("submitted_unknown", tx_sig="SIG")
    life.observe(current["e1"])
    life.deliver()
    current["e1"] = view("filled", tx_sig="SIG", fill_id=3, position_id=2)
    life.observe(current["e1"])
    life.observe(current["e1"])
    life.deliver()
    assert sent == ["submitted_unknown", "filled"]
    assert len(rows(conn)) == 2
    assert rows(conn)[1]["fill_id"] == "3"


def test_send_timeout_is_unknown_and_is_not_retried_before_delay():
    from sniper.services.memecoin_telegram import TelegramDeliveryUnknown
    calls = []
    def send(text):
        calls.append(text)
        raise TelegramDeliveryUnknown("timeout")
    life, conn, current, sent = worker(send)
    current["e1"] = view("filled")
    life.observe(current["e1"])
    life.deliver()
    life.observe(current["e1"])
    life.deliver()
    assert calls == ["filled"]
    assert rows(conn)[0]["delivery_state"] == "delivery_unknown"


@pytest.mark.parametrize("error", ["unknown", "transport", "transient", "http500"])
def test_ambiguous_delivery_has_one_marked_resend_after_five_seconds(error):
    import urllib.error
    from sniper.services import memecoin_telegram as tg
    calls, audits = [], []
    errors = {"unknown": tg.TelegramDeliveryUnknown("timeout"), "transport": OSError("reset"),
              "transient": tg.TelegramTransientError("dns"),
              "http500": urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, None)}
    def send(text):
        calls.append(text)
        if len(calls) == 1:
            raise errors[error]
        return True, 2.0
    life, conn, current, _ = worker(send)
    life.audit_fn = audits.append
    now = ["2026-09-11T12:00:04+00:00"]
    life.now_fn = lambda: now[0]
    current["e1"] = view("filled")
    life.observe(current["e1"])
    life.deliver()
    now[0] = "2026-09-11T12:00:08.999+00:00"
    life.deliver()
    assert calls == ["filled"]
    now[0] = "2026-09-11T12:00:09+00:00"
    life.deliver()
    assert calls == ["filled", "RESENT (may be a duplicate)\nfilled"]
    assert [a["attempt"] for a in audits] == [1, 2]
    assert [a["source_id"] for a in audits] == ["e1", "e1"]
    assert [a["send_ok"] for a in audits] == [False, True]
    now[0] = "2026-09-11T12:01:00+00:00"
    life.deliver()
    assert len(calls) == 2 and rows(conn)[0]["delivery_state"] == "delivered"


@pytest.mark.parametrize("result", ["ok", "false", "http403"])
def test_success_or_definite_rejection_is_not_resent(result):
    import urllib.error
    calls = []
    def send(text):
        calls.append(text)
        if result == "http403":
            raise urllib.error.HTTPError("https://example.invalid", 403, "blocked", {}, None)
        return result == "ok", 1
    life, conn, current, _ = worker(send)
    current["e1"] = view("filled")
    life.observe(current["e1"])
    life.deliver()
    life.now_fn = lambda: "2026-09-11T12:01:00+00:00"
    life.deliver()
    assert calls == ["filled"]


def test_second_unknown_is_terminal_across_restart():
    from sniper.services import memecoin_telegram as tg
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    calls = []
    def send(text):
        calls.append(text)
        raise tg.TelegramDeliveryUnknown("timeout")
    life, conn, current, _ = worker(send)
    current["e1"] = view("filled")
    life.observe(current["e1"])
    life.deliver()
    life = NotificationLifecycle(conn, now_fn=lambda: "2026-09-11T12:00:10+00:00",
        read_view=lambda **kw: current["e1"], send_fn=send, render_fn=lambda v: v["status"], eligible_fn=lambda v: True)
    life.recover_interrupted()
    life.deliver()
    life.now_fn = lambda: "2026-09-11T12:01:00+00:00"
    life.deliver()
    assert len(calls) == 2 and rows(conn)[0]["delivery_state"] == "delivery_unknown"


@pytest.mark.parametrize("attempt", [None, 1, 2])
def test_restart_recovers_only_new_first_attempt_and_caps_interrupted_resend(attempt):
    import json
    life, conn, current, sent = worker()
    current["e1"] = view("filled")
    key = life.observe(current["e1"])
    evidence = {} if attempt is None else {"delivery_attempt": attempt}
    conn.execute("UPDATE memecoin_notification_transitions SET delivery_state='sending',evidence_json=?",
                 (json.dumps(evidence),))
    conn.commit()
    life.recover_interrupted()
    life.now_fn = lambda: "2026-09-11T12:00:09+00:00"
    life.deliver()
    life.recover_interrupted()
    life.now_fn = lambda: "2026-09-11T12:01:00+00:00"
    life.deliver()
    assert sent == (["RESENT (may be a duplicate)\nfilled"] if attempt == 1 else [])
    if attempt == 1:
        assert json.loads(rows(conn)[0]["evidence_json"])["delivery_attempt"] == 2


def test_historical_unknown_without_retry_evidence_stays_silent():
    life, conn, current, sent = worker()
    current["e1"] = view("filled")
    life.observe(current["e1"])
    conn.execute("UPDATE memecoin_notification_transitions SET delivery_state='delivery_unknown'")
    conn.commit()
    life.recover_interrupted()
    life.now_fn = lambda: "2026-09-11T12:01:00+00:00"
    life.deliver()
    assert sent == []
    assert rows(conn)[0]["delivery_state"] == "delivery_unknown"


def test_refusal_card_reads_only_same_chain_open_real_position(monkeypatch):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_telegram as tg
    from test_memecoin_entry_view import fill
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    w = tg._NotificationWorker.__new__(tg._NotificationWorker)
    w.read_conn = w.write_conn = conn
    v = view("refused", reason="first_source_buy_missed")
    v["mint"] = "MINT"
    assert "already holding" not in w._render(v)
    fill(conn, aid="paper", mode="paper")
    fill(conn, aid="other-chain", chain="4663")
    assert "already holding" not in w._render(v)
    pid = fill(conn, aid="held-real")
    text = w._render(v)
    assert "add-on refused: already holding" in text and f"pos {pid}" in text and "bought " in text
    assert v["reason"] == "first_source_buy_missed" and v.get("position_id") is None
    conn.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=?", (pid,))
    conn.commit()
    assert "already holding" not in w._render(v)


def test_restart_changes_interrupted_send_to_unknown():
    life, conn, current, sent = worker()
    life.observe(view("filled"))
    conn.execute("UPDATE memecoin_notification_transitions SET delivery_state='sending'")
    conn.commit()
    life.recover_interrupted()
    assert rows(conn)[0]["delivery_state"] == "delivery_unknown"


@pytest.mark.parametrize("mode", ["format", "send"])
def test_preparation_and_definite_send_failure_never_mark_delivered(mode):
    def fail(v):
        raise ValueError("formatter")
    life, conn, current, sent = worker(send=(lambda text: (False, 0)) if mode == "send" else None,
                                     render=fail if mode == "format" else None)
    current["e1"] = view("refused")
    life.observe(current["e1"])
    life.deliver()
    assert rows(conn)[0]["delivery_state"] == "failed"


def test_old_missed_result_produces_one_summary_and_old_unresolved_still_pages():
    life, conn, current, sent = worker()
    life.now_fn = lambda: "2026-09-11T13:00:00+00:00"
    current["e1"] = view("refused")
    life.observe(current["e1"])
    life.deliver()
    life.observe(current["e1"])
    life.deliver()
    assert len(sent) == 1 and "missed" in sent[0].lower()
    assert rows(conn)[0]["delivery_state"] == "missed"
    current["e1"] = view("submitted_unknown")
    life.observe(current["e1"])
    life.deliver()
    assert sent[-1] == "submitted_unknown"


def test_delivery_evidence_is_redacted_and_contains_exact_identity():
    life, conn, current, sent = worker(render=lambda v: "FILL\ntap: https://secret.test/key")
    current["e1"] = view("filled", position_id=12, fill_id=13, tx_sig="sig")
    life.observe(current["e1"])
    life.deliver()
    row = rows(conn)[0]
    assert row["origin_event_id"] == "e1" and row["attempt_id"] == "a1"
    assert row["network_id"] == "solana" and row["position_id"] == "12"
    assert "secret.test" not in str(row)
    assert "[redacted]" in row["rendered_text_redacted"]


def test_raw_http_timeout_makes_one_attempt():
    from sniper.services.memecoin_telegram import TelegramDeliveryUnknown, send_telegram
    calls = []
    def http(*args):
        calls.append(args)
        raise TimeoutError("response lost")
    with pytest.raises(TelegramDeliveryUnknown):
        send_telegram("text", token="test", chat_id="1", http=http, min_gap_s=0)
    assert len(calls) == 1


def test_feed_snapshot_preserves_identity_receipt_and_reports_error(tmp_path):
    from sniper.services.memecoin_runner import write_feed_snapshot, read_feed_snapshot_status
    path = str(tmp_path / "feed.json")
    ev = SimpleNamespace(fomo_event_id="e1", source_handle="alice", source_user_id="u1", side="buy",
        ticker="T", mint="M", network_id="143", their_size_usd=10, their_market_cap=None,
        their_price=0.1, created_at="source", received_at="receipt")
    write_feed_snapshot([ev], path)
    status = read_feed_snapshot_status(path)
    assert status["snapshot_at"] and status["error"] is None
    assert status["events"][0]["origin_event_id"] == "e1"
    assert status["events"][0]["observed_at"] == "receipt"
    (tmp_path / "feed.json").write_text("{")
    assert read_feed_snapshot_status(path)["error"]


def test_source_intent_does_not_prepare_or_write_in_funded_thread(monkeypatch):
    from sniper.services import memecoin_runner as r
    jobs = []
    q = SimpleNamespace(enqueue_source=lambda ev: jobs.append(ev) or True)
    monkeypatch.setattr(r, "_prepare_alert", lambda *a: pytest.fail("hot-path preparation"))
    ev = SimpleNamespace(fomo_event_id="e1")
    assert r._dispatch_front_run_alert(None, ev, None, q) == (1, 0)
    assert jobs == [ev]


def test_projection_formatter_has_explicit_status_times_counts_and_safe_links(monkeypatch):
    from sniper.services.memecoin_telegram import render_entry_notification
    monkeypatch.setenv("MEMECOIN_ONE_TAP_BASE_URL", "https://desk.example")
    v = view("submitted_unknown", reason="confirmation unknown", tx_sig="SIG")
    v["links"] = {"entry": "/?mc_origin=e1", "trader": "https://fomo.family/profile/alice"}
    v["wallet_evidence"] = {"paper_n": 18, "paper_coins": 7, "paper_pnl": -61, "gate_ok": False}
    text = render_entry_notification(v)
    assert "SUBMITTED, CONFIRMATION UNKNOWN" in text
    assert "Our size: $175" in text and "alice bought" in text
    assert "Times (UTC): source" in text and "s processed" in text
    assert "18 closes" not in text and "7 coins" not in text
    assert ">Trader</a>" in text
    assert len(text.splitlines()) <= 12, text
    assert "https://desk.example/?mc_origin=e1" in text
    monkeypatch.setenv("MEMECOIN_ONE_TAP_BASE_URL", "http://localhost:5000")
    assert "localhost" not in render_entry_notification(v)


def test_failed_transaction_does_not_claim_reservation_release():
    from sniper.services.memecoin_alerts import alert_failed_tx
    messages = []
    alert_failed_tx("M", "SIG", "reverted", send_fn=lambda text: messages.append(text))
    assert "reservation released" not in messages[0]


def test_canonical_chain_aliases_deduplicate():
    from sniper.persistence.db_memecoin_notifications import transition_key
    a = view("filled")
    a["network_id"] = "4663"
    b = dict(a, network_id="robinhood")
    assert transition_key(a) == transition_key(b)


def test_schema_rejects_invalid_delivery_state():
    life, conn, current, sent = worker()
    life.observe(view("filled"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE memecoin_notification_transitions SET delivery_state='success'")


def test_two_workers_claim_one_delivery(tmp_path):
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    path = tmp_path / "notifications.db"
    conns = [sqlite3.connect(path), sqlite3.connect(path)]
    for conn in conns:
        conn.row_factory = sqlite3.Row
    current = view("filled")
    sent = []
    args = dict(read_view=lambda **kw: current, send_fn=lambda text: sent.append(text) or (True, 1),
                render_fn=lambda v: "fill", eligible_fn=lambda v: True,
                now_fn=lambda: "2026-09-11T12:00:04+00:00")
    workers = [NotificationLifecycle(conn, **args) for conn in conns]
    workers[0].activate("2026-09-11T12:00:00+00:00")
    key = workers[0].observe(current)
    assert workers[0].claim(key, {"rendered_text_redacted": "fill"})
    assert not workers[1].claim(key, {"rendered_text_redacted": "fill"})


def test_scan_cursor_preserves_burst_and_refreshes_late_attempt():
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_memecoin_notifications import scan_notification_views
    life, conn, current, sent = worker()
    m.ensure_memecoin_tables(conn)
    scan_notification_views(life, conn, lambda **kw: current.get(kw.get("origin_event_id")), limit=2)
    for n in range(5):
        origin = f"e{n}"
        current[origin] = dict(view("refused"), origin_event_id=origin)
        conn.execute("INSERT INTO memecoin_events(fomo_event_id) VALUES(?)", (origin,))
    conn.commit()
    for _ in range(3):
        scan_notification_views(life, conn, lambda **kw: current.get(kw.get("origin_event_id")), limit=2)
    assert {r["origin_event_id"] for r in rows(conn)} == {f"e{n}" for n in range(5)}


def test_dispatcher_source_job_uses_lifecycle_owner():
    import threading
    from sniper.services.memecoin_telegram import _TelegramDispatcher
    called = threading.Event()
    jobs = []
    class Lifecycle:
        def cycle(self, job=None):
            if job:
                jobs.append(job)
                called.set()
    dispatcher = _TelegramDispatcher(lambda text: pytest.fail("raw send"), maxsize=2,
                                      lifecycle_factory=Lifecycle)
    ev = SimpleNamespace(fomo_event_id="e1", network_id="143")
    assert dispatcher.enqueue_source(ev)
    assert called.wait(2)
    dispatcher._queue.join()
    assert jobs[0]["event"] is ev


@pytest.mark.parametrize("reason,tested,basis", [
    ("m5_turnover_over_ceiling", 3000, "raw_volume_m5"),
    ("m5_turnover_under_floor", 2300, "source_adjusted_volume_m5"),
])
def test_volume_evidence_uses_actual_ceiling_and_correct_volume_basis(reason, tested, basis):
    from sniper.services.memecoin_real_runner import _volume_notification_evidence
    result = _volume_notification_evidence(volume_m5=3000, signal_usd=700, mcap=10000,
                                          effective_ceiling=.5, reason=reason, need=5000)
    assert result["effective_ceiling"] == .5
    assert result["organic_volume_m5"] == 2300
    assert result["tested_volume_m5"] == tested
    assert result["measurement_basis"] == basis
    assert result["threshold"] == 5000


def test_source_queue_rejection_is_not_delivery():
    from sniper.services import memecoin_runner as r
    q = SimpleNamespace(enqueue_source=lambda ev: False)
    assert r._dispatch_front_run_alert(None, object(), None, q) == (0, 1)


def test_real_entry_alert_defers_to_current_projection(monkeypatch):
    from sniper.services import memecoin_real_runner as r
    from sniper.services import memecoin_telegram as tg
    monkeypatch.setattr(tg, "notification_lifecycle_active", lambda: True)
    monkeypatch.setattr(r, "_safe_alert", lambda *a, **kw: pytest.fail("raw result alert"))
    r._alert_entry_result("M", 175, {"status": "filled", "sig": "SIG"})


def test_missed_summary_pages_past_prior_summaries():
    life, conn, current, sent = worker()
    life.now_fn = lambda: "2026-09-11T13:00:00+00:00"
    for n in range(205):
        life.observe(dict(view("refused"), origin_event_id=f"old{n}"))
    for _ in range(4):
        life.deliver(limit=20)
    count = sum(__import__("json").loads(row["evidence_json"])["missed_count"]
                for row in rows(conn) if row["message_kind"] == "missed_summary")
    assert count == 205


def test_preexisting_unresolved_attempt_is_seeded_as_current_notice():
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_memecoin_notifications import scan_unresolved_notification_views
    life, conn, current, sent = worker()
    m.ensure_memecoin_tables(conn)
    current["e1"] = view("transaction_unresolved")
    current["e1"].update(observed_at="2026-01-01T00:00:00+00:00", status_updated_at="2026-01-01T00:00:00+00:00")
    conn.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,decision_started_at,terminal_status,terminal_reason,completed_at) "
                 "VALUES('a1','e1','2026-01-01','unknown','timeout','2026-01-01')")
    conn.commit()
    scan_unresolved_notification_views(life, conn, lambda **kw: current["e1"], limit=2)
    life.deliver()
    assert sent == ["transaction_unresolved"]
    assert rows(conn)[0]["message_kind"] == "entry"


def test_new_entry_fill_recovers_preexisting_untracked_attempt():
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_memecoin_notifications import notification_fill_identity
    life, conn, current, sent = worker()
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_positions(id,mint,network_id,execution_mode,origin_event_id) VALUES(7,'M','solana','real','e1')")
    conn.execute("INSERT INTO memecoin_fills(id,position_id,kind,attempt_id,origin_event_id) VALUES(8,7,'entry','a1','e1')")
    conn.commit()
    assert notification_fill_identity(conn, 8) == {"origin_event_id": "e1", "attempt_id": "a1"}


def test_worker_uses_real_projection_and_retains_completed_source_audit(tmp_path, monkeypatch):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_telegram as tg
    from sniper.feeds.fomo_feed import CopyEvent
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    now = ["2026-09-11T12:00:00+00:00"]
    monkeypatch.setattr(tg, "_real_utc_now_iso", lambda: now[0])
    sent = []
    monkeypatch.setattr(tg, "send_telegram", lambda text: sent.append(text) or (True, 1))
    worker = tg._NotificationWorker()
    from sniper.settings import memecoin as cfg
    _source_cards(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "tg_min_buy_usd", lambda mc: 100)
    monkeypatch.setattr(cfg, "tg_mcap_ok", lambda mc: True)
    m.add_wallet(worker.write_conn, "u1", "alice")
    # armed on the signal's chain: since 2026-09-11 an unarmed wallet never pages
    worker.write_conn.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='4663' WHERE source_user_id='u1'")
    worker.write_conn.commit()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_HOLD_S", 0.0)   # no decision will come here
    now[0] = "2026-09-11T12:00:04+00:00"
    ev = CopyEvent("u1", "alice", "0x" + "a" * 40, "T", "buy", .1, None, 700, True,
                   "e1", "2026-09-11T12:00:01+00:00", "2026-09-11T12:00:02+00:00", 1,
                   network_id="4663")
    m.insert_event(worker.write_conn, vars(ev))
    worker.cycle({"event": ev})
    assert len(sent) == 1 and "Robinhood" in sent[0]
    row = worker.read_conn.execute("SELECT * FROM memecoin_alert_latency WHERE fomo_event_id='e1'").fetchone()
    assert row["detected_at"] == ev.received_at
    assert row["send_ok"] == 1
    assert worker.read_conn.execute("SELECT source_id FROM memecoin_telegram_delivery_audit").fetchone()[0] == "e1"


def test_exit_view_uses_frozen_position_policy_and_full_fill_identity():
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_memecoin_notifications import notification_exit_view
    import json
    life, conn, current, sent = worker()
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_positions(id,mint,network_id,execution_mode,origin_event_id,funded_contract_json) "
                 "VALUES(7,'M','solana','real','e1',?)", (json.dumps({"policy": {"policy_id": "frozen-policy"}}),))
    conn.execute("INSERT INTO memecoin_fills(id,position_id,kind,tx_sig,created_at,notional,price) "
                 "VALUES(8,7,'manual_close','FULL_SIGNATURE','2026-09-11T12:00:03+00:00',150,1)")
    conn.commit()
    result = notification_exit_view(conn, 8)
    assert result["fill_id"] == 8 and result["position_id"] == 7
    assert result["tx_sig"] == "FULL_SIGNATURE" and result["position_policy_id"] == "frozen-policy"


def _mirror_sale(*details):
    """A closed funded position sold by the mirror, after one audit row per source sell."""
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_memecoin_notifications import notification_exit_view
    import json
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_positions(id,mint,ticker,network_id,execution_mode,exit_reason,is_open) "
                 "VALUES(7,'M','TEST','solana','real','mirror',0)")
    conn.execute("INSERT INTO memecoin_fills(id,position_id,kind,tx_sig,created_at,notional,price) "
                 "VALUES(8,7,'mirror_exit','SIG','2026-09-15T12:05:00+00:00',150,1)")
    for minute, detail in enumerate(details):
        m.record_exit_trigger_audit(
            conn, position_id=7, reason="mirror", display_price=None, preview_usdc=None,
            allowed=detail.get("code") != "size_hold", detail=json.dumps(detail),
            checked_at=f"2026-09-15T12:0{minute}:00+00:00", lane="mirror_on_sell")
    return notification_exit_view(conn, 8)


def _sale_line(view):
    from sniper.services.memecoin_telegram import render_entry_notification
    return next(line for line in render_entry_notification(view).splitlines() if line.startswith("Our sale"))


def test_mirror_exit_names_the_share_the_trader_sold():
    """Alpha, 2026-09-15: the mirror page says how much of its bag the trader sold, from the
    latest decision for that position, not the bare word."""
    sale = _sale_line(_mirror_sale({"code": "size_hold", "sold_fraction": 0.18},
                                   {"code": "size_mirror", "sold_fraction": 0.4, "min_fraction": 0.3}))
    assert "trader sold 40% of their bag" in sale
    assert "· mirror" not in sale


def test_mirror_exit_without_a_measured_share_keeps_the_bare_reason():
    sale = _sale_line(_mirror_sale({"code": "mirror_allowed"}))
    assert sale.endswith("· mirror") and "trader sold" not in sale


def test_resolved_origins_leave_active_refresh_without_losing_delivery_history():
    life, conn, current, sent = worker()
    current["e1"] = view("retry_pending")
    life.observe(current["e1"])
    life.deliver()
    current["e1"] = view("filled", fill_id=4)
    life.observe(current["e1"])
    life.deliver()
    life.read_view = lambda **kw: pytest.fail("resolved origin was refreshed")
    life.refresh_tracked()
    assert [r["delivery_state"] for r in rows(conn)] == ["baseline", "delivered"]


def test_final_retry_change_uses_observation_time_but_keeps_result_time():
    life, conn, current, sent = worker()
    current["e1"] = view("retry_pending")
    life.observe(current["e1"])
    life.deliver()
    life.now_fn = lambda: "2026-09-11T13:00:00+00:00"
    current["e1"] = view("refused")
    life.observe(current["e1"])
    life.deliver()
    assert sent == ["refused"], "a late final result still pages once after a silent retry"
    assert __import__("json").loads(rows(conn)[1]["evidence_json"])["completed_at"] == current["e1"]["completed_at"]


def test_real_partial_fill_has_one_notification_owner(monkeypatch):
    from sniper.services import memecoin_real_runner as r
    from sniper.services import memecoin_telegram as tg
    monkeypatch.setattr(tg, "notification_lifecycle_active", lambda: True)
    monkeypatch.setattr(r, "_safe_alert", lambda *a, **kw: pytest.fail("duplicate partial-fill alert"))
    r._alert_exit_result("M", 7, "derisk", {"status": "derisked", "sig": "S"})


def test_real_roundtrip_metadata_is_displayed():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("refused", gate_evidence=[{"name": "roundtrip_cap", "cost_pct": 10.19,
            "cap_pct": 10, "basis": "independent_pre_entry_quotes", "scaled_sell_usd": 157.17}])
    text = render_entry_notification(v)
    assert "10.19%" in text and "10.00%" in text
    assert "$157.17" in text and "independent_pre_entry_quotes" in text


def test_delivery_view_is_exact_chain_and_unknown_before_activation():
    from sniper.persistence.db_memecoin_notifications import notification_delivery_view
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert notification_delivery_view(conn, network_id="solana", origin_event_id="e1")["available"] is False
    life, conn, current, sent = worker()
    current["e1"] = view("refused")
    life.observe(current["e1"])
    result = notification_delivery_view(conn, network_id="solana", origin_event_id="e1")
    assert result["result"]["delivery_state"] == "queued"
    assert notification_delivery_view(conn, network_id="4663", origin_event_id="e1")["result"] is None
    life.deliver()
    assert notification_delivery_view(conn, network_id="solana", origin_event_id="e1")["result"]["delivery_state"] == "delivered"


def test_source_toggle_does_not_disable_funded_results(monkeypatch):
    from sniper.services.memecoin_telegram import _NotificationWorker
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", False)
    monkeypatch.setattr(m, "real_approved_user_ids", lambda *a, **kw: {"u1"})
    worker = object.__new__(_NotificationWorker)
    worker.read_conn = None
    assert not worker._eligible(view("source_received"))
    assert worker._eligible(view("refused"))


def test_historical_authority_and_current_authority_have_separate_labels():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("refused")
    v["authority"] = {"state": "not_armed", "basis": "historical_evidence"}
    v["current_authority"] = {"state": "armed"}
    text = render_entry_notification(v)
    assert "armed on Solana (was not armed at decision)" in text


def test_originless_manual_attempts_have_distinct_keys_and_recover_after_scan(monkeypatch):
    _source_cards(monkeypatch)
    life, conn, current, sent = worker()
    a = dict(view("evaluating"), origin_event_id=None, attempt_id="manual-a")
    b = dict(a, attempt_id="manual-b")
    assert life.observe(a) != life.observe(b)
    reads = []
    def read(**kw):
        reads.append(kw)
        return dict(a if kw["attempt_id"] == "manual-a" else b, status="refused")
    life.read_view = read
    life.refresh_tracked()
    life.deliver()
    assert sent == ["refused", "refused"]
    assert {r["attempt_id"] for r in rows(conn) if r["is_current"]} == {"manual-a", "manual-b"}


def test_worker_rolls_back_failed_cycle_before_next_send():
    from sniper.services.memecoin_telegram import _NotificationWorker
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE probe(value TEXT)")
    worker = object.__new__(_NotificationWorker)
    worker.write_conn = conn
    def fail(job=None):
        conn.execute("INSERT INTO probe VALUES('partial')")
        raise sqlite3.OperationalError("disk unavailable")
    worker._cycle = fail
    with pytest.raises(sqlite3.OperationalError):
        worker.cycle()
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0


def test_silent_evaluation_remains_tracked_for_late_terminal_result():
    life, conn, current, sent = worker()
    life.eligible_fn = lambda v: v["status"] == "refused"
    current["e1"] = view("evaluating")
    life.observe(current["e1"])
    life.deliver()
    assert sent == []
    current["e1"] = view("refused")
    life.refresh_tracked()
    life.deliver()
    assert sent == ["refused"]


def test_delivery_revision_changes_without_execution_ledger_change():
    from sniper.persistence.db_memecoin_notifications import notification_delivery_revision
    life, conn, current, sent = worker()
    current["e1"] = view("filled")
    life.observe(current["e1"])
    before = notification_delivery_revision(conn)
    life.deliver()
    assert notification_delivery_revision(conn) != before


def test_source_candidate_defers_consensus_database_read(monkeypatch):
    from sniper.services import memecoin_runner as r
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_CONSENSUS_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_CONSENSUS_MAX_MCAP", 2_000_000)
    monkeypatch.setattr(cfg, "tg_min_buy_usd", lambda mc: 100)
    monkeypatch.setattr(cfg, "tg_mcap_ok", lambda mc: True)
    monkeypatch.setattr(r.m, "prior_buyers_60m", lambda *a, **kw: pytest.fail("notification DB read before funded hook"))
    ev = SimpleNamespace(source_user_id="other", side="buy", their_size_usd=500, their_market_cap=10000)
    q = SimpleNamespace(enqueue_source=lambda ev: True)
    assert r._want_source_intent(None, ev, set(), q)


def test_source_scan_preserves_size_filter_even_when_wallet_is_armed(monkeypatch):
    from sniper.services.memecoin_telegram import _NotificationWorker
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "tg_min_buy_usd", lambda mc: 500)
    monkeypatch.setattr(m, "curated_user_ids", lambda *a: {"u1"})
    monkeypatch.setattr(m, "real_approved_user_ids", lambda *a, **kw: {"u1"})
    worker = object.__new__(_NotificationWorker)
    worker.read_conn = None
    v = view("source_received", source_size_usd=10)
    assert not worker._eligible(v)


def test_ineligible_old_attempt_does_not_create_missed_delivery_summary():
    life, conn, current, sent = worker()
    life.eligible_fn = lambda view: False
    life.now_fn = lambda: "2026-09-11T13:00:00+00:00"
    life.observe(view("refused"))
    life.deliver()
    assert sent == []
    assert rows(conn)[0]["delivery_state"] == "baseline"


def test_exit_delivery_does_not_replace_entry_delivery_state():
    from sniper.persistence.db_memecoin_notifications import notification_delivery_view
    life, conn, current, sent = worker()
    life.observe(view("filled", fill_id=4))
    life.observe(view("sold", message_kind="exit", fill_id=5))
    result = notification_delivery_view(conn, network_id="solana", origin_event_id="e1")
    assert result["result"]["execution_state"] == "filled"
    assert any(row["message_kind"] == "exit" for row in result["history"])


def test_no_reserved_size_is_omitted_and_unknown_source_amount_is_explicit():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("refused")
    v["requested_usd"] = None
    text = render_entry_notification(v)
    assert "Our size:" not in text
    assert "alice bought unknown size" in text


@pytest.mark.parametrize("terminal", ["no_fill", "refused", "failed_before_submission", "transaction_failed", "filled"])
def test_restart_does_not_redate_historical_needs_review_correction(terminal):
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    life, conn, current, sent = worker()
    old = dict(view("needs_review"), observed_at="2026-09-10T12:00:00+00:00",
               completed_at="2026-09-10T12:00:01+00:00", status_updated_at="2026-09-10T12:00:01+00:00")
    current["e1"] = old
    life.observe(old)
    life.deliver()
    assert sent == ["needs_review"]
    current["e1"] = dict(old, status=terminal)
    restarted = NotificationLifecycle(conn, read_view=lambda **kw: current["e1"],
        send_fn=lambda text: sent.append(text) or (True, 1), render_fn=lambda v: v["status"],
        eligible_fn=lambda v: True, now_fn=lambda: "2026-09-11T13:00:00+00:00")
    restarted.activate()
    restarted.refresh_tracked()
    restarted.deliver()
    assert sent == ["needs_review"]
    assert rows(conn)[-1]["delivery_state"] == "baseline"
    assert rows(conn)[-1]["event_at"] == old["status_updated_at"]
    assert "transition_observed_at" not in __import__("json").loads(rows(conn)[-1]["evidence_json"])


@pytest.mark.parametrize("prior_status", ["transaction_unresolved", "submitted_unknown"])
def test_old_actual_unresolved_transaction_still_notifies_new_resolution(prior_status):
    life, conn, current, sent = worker()
    old = dict(view(prior_status, tx_sig="SIG"), observed_at="2026-09-10T12:00:00+00:00",
               completed_at="2026-09-10T12:00:01+00:00", status_updated_at="2026-09-10T12:00:01+00:00")
    current["e1"] = old
    life.observe(old)
    life.deliver()
    life.now_fn = lambda: "2026-09-11T13:00:00+00:00"
    current["e1"] = dict(old, status="no_fill")
    life.refresh_tracked()
    life.deliver()
    assert sent == [prior_status, "no_fill"]


@pytest.mark.parametrize("cap", [100000, 1000000, 100])
def test_disabled_roundtrip_cap_has_no_negative_required_amount(cap):
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("no_fill", gate_evidence=[{"name": "roundtrip_cap", "cost_pct": 4.2,
             "cap_pct": cap, "basis": "independent_pre_entry_quotes", "scaled_sell_usd": 167.65}])
    text = render_entry_notification(v)
    assert "Round trip costs 4.20% · cap disabled" in text
    assert "Required:" not in text
    assert "$-" not in text


def test_historical_review_still_delivers_a_new_durable_completion():
    life, conn, current, sent = worker()
    old = dict(view("needs_review"), observed_at="2026-09-10T12:00:00+00:00",
               completed_at=None, status_updated_at="2026-09-10T12:00:01+00:00")
    current["e1"] = old
    life.observe(old)
    life.deliver()
    current["e1"] = dict(old, status="no_fill", completed_at="2026-09-11T12:00:03+00:00",
                         status_updated_at="2026-09-11T12:00:03+00:00")
    life.refresh_tracked()
    life.deliver()
    assert sent == ["needs_review", "no_fill"]


# ---- 2026-09-11 Alpha: age gate, one message per signal, compact layout ---------------------

def _old(status, **fields):
    v = view(status, **fields)
    v.update(observed_at="2026-07-17T01:47:19+00:00", completed_at="2026-07-17T02:08:42+00:00",
             status_updated_at="2026-07-17T02:08:42+00:00", source_created_at="2026-07-17T01:46:39+00:00")
    return v


def test_old_unresolved_view_over_a_recorded_terminal_reservation_does_not_page():
    """The 13:06Z burst shape: a July row the view could not classify, reservation long failed."""
    life, conn, current, sent = worker()
    current["e1"] = _old("needs_review", reservation_status="failed")
    life.observe(current["e1"], eligible=True)
    life.deliver()
    assert sent == []
    assert rows(conn)[0]["delivery_state"] == "baseline"


@pytest.mark.parametrize("reservation", ["reserved", None])
def test_old_unresolved_view_pages_while_the_reservation_is_open_or_unknown(reservation):
    """Money may still be reserved; an unknown reservation state is not a closed one."""
    life, conn, current, sent = worker()
    current["e1"] = _old("transaction_unresolved", reservation_status=reservation)
    life.observe(current["e1"], eligible=True)
    life.deliver()
    assert sent == ["transaction_unresolved"]


def _clocked_worker(clock):
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sent, current = [], {}
    life = NotificationLifecycle(conn, now_fn=lambda: clock[0],
        read_view=lambda **kw: current[kw["origin_event_id"]],
        send_fn=lambda text: sent.append(text) or (True, 1),
        render_fn=lambda v: v["status"], eligible_fn=lambda v: True)
    life.activate("2026-09-11T12:00:00+00:00")
    return life, conn, current, sent


def test_a_funded_signal_pages_once_when_the_decision_lands_inside_the_hold(monkeypatch):
    _source_cards(monkeypatch)
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_HOLD_S", 4.0)
    clock = ["2026-09-11T12:00:02.500000+00:00"]
    life, conn, current, sent = _clocked_worker(clock)
    current["e1"] = view("evaluating")
    life.observe(current["e1"])
    life.deliver()
    assert sent == [], "undecided source must wait for its decision"
    clock[0] = "2026-09-11T12:00:03.500000+00:00"
    current["e1"] = view("refused", reason="not_approved")
    life.deliver()
    assert sent == ["refused"], "only the result goes out"


def test_a_source_still_undecided_after_the_hold_is_sent(monkeypatch):
    _source_cards(monkeypatch)
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_HOLD_S", 4.0)
    clock = ["2026-09-11T12:00:03+00:00"]
    life, conn, current, sent = _clocked_worker(clock)
    current["e1"] = view("evaluating")
    life.observe(current["e1"])
    life.deliver()
    assert sent == []
    clock[0] = "2026-09-11T12:00:07+00:00"
    life.deliver()
    assert sent == ["evaluating"]


def test_an_unfunded_chain_signal_is_not_held(monkeypatch):
    _source_cards(monkeypatch)
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_HOLD_S", 4.0)
    clock = ["2026-09-11T12:00:03+00:00"]
    life, conn, current, sent = _clocked_worker(clock)
    current["e1"] = view("source_received")
    current["e1"]["network_id"] = "143"
    life.observe(current["e1"])
    life.deliver()
    assert sent == ["source_received"]


def test_compact_result_message_reads_in_words_and_stays_short():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("refused", reason="not_approved", source_size_usd=994,
             current_authority={"state": "not_armed"})
    v.update(network_id="4663", mint="0x" + "9" * 40, origin_event_id="swap:raybot:" + "ab" * 60)
    text = render_entry_notification(v)
    lines = text.splitlines()
    assert lines[0].startswith("🟠 REFUSED · TEST · ")
    assert "Why: wallet not armed on this chain" in text
    assert "processed unknown · 1.0s processed to result" in text
    assert "Source event" not in text and "ab" * 60 not in text, "internal identities stay off the phone"
    assert lines[-1] == "ref a1"
    assert len(lines) <= 10, text



def test_a_wallet_not_armed_on_the_chain_never_pages(tmp_path, monkeypatch):
    """Operator 2026-09-11: "Don't send notif for disarmed wallets." Not its buy, not its refusal."""
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_telegram as tg
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn); conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(tg, "_real_utc_now_iso", lambda: "2026-09-11T12:00:04+00:00")
    worker = tg._NotificationWorker()
    m.add_wallet(worker.write_conn, "u1", "alice")          # curated, armed nowhere
    for status, extra in (("source_received", {}), ("refused", {"reason": "not_approved"})):
        v = view(status, **extra)
        v.update(network_id="solana", requested_usd=None)
        assert worker._eligible(v) is False, status
    v = view("filled")                                       # anything we actually did still pages
    v.update(network_id="solana")
    assert worker._eligible(v) is True



def test_the_message_is_safe_telegram_html():
    """Dynamic text is escaped; the only tags are the named links."""
    import re
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("refused", reason="gate <b>& \"quote\" 1<2", source_size_usd=900)
    v.update(ticker="<script>", network_id="solana", mint="So11111111111111111111111111111111111111112")
    text = render_entry_notification(v)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "&lt;b&gt;&amp;" in text
    assert set(re.findall(r"</?([a-z]+)", text)) <= {"a"}



def test_a_retrying_decision_pages_once_with_its_outcome():
    """EPI 17:37Z: three RETRY SCHEDULED pages in 20 s, then REFUSED. Now: one page."""
    life, conn, current, sent = worker()
    for attempt in ("a1", "a2", "a3"):
        current["e1"] = view("retry_pending", attempt_id=attempt) if False else dict(view("retry_pending"), attempt_id=attempt)
        life.observe(current["e1"])
        life.deliver()
    current["e1"] = dict(view("refused"), attempt_id="a4")
    life.observe(current["e1"])
    life.deliver()
    assert sent == ["refused"]


def test_an_old_retry_record_does_not_authorize_a_historical_page():
    """A retry row from before activation must not turn its old result into a live page."""
    life, conn, current, sent = worker()
    old = dict(view("retry_pending"), observed_at="2026-09-10T12:00:00+00:00",
               status_updated_at="2026-09-10T12:00:00+00:00", completed_at="2026-09-10T12:00:00+00:00")
    life.observe(old)
    life.eligible_fn = lambda v: False
    final = dict(view("refused"), attempt_id="a9", observed_at="2026-09-10T12:00:00+00:00",
                 status_updated_at="2026-09-10T12:01:00+00:00", completed_at="2026-09-10T12:01:00+00:00")
    life.observe(final)
    life.deliver()
    assert sent == []



def test_a_buy_mid_confirmation_pages_only_its_outcome(monkeypatch):
    """All 19 'SUBMITTED, CONFIRMATION UNKNOWN' pages on 2026-09-11 preceded BOUGHT by seconds."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_UNRESOLVED_GRACE_S", 30.0)
    clock = ["2026-09-11T12:00:05+00:00"]
    life, conn, current, sent = _clocked_worker(clock)
    current["e1"] = view("submitted_unknown", tx_sig="SIG")
    life.observe(current["e1"])
    life.deliver()
    assert sent == []
    clock[0] = "2026-09-11T12:00:07+00:00"
    current["e1"] = view("filled", tx_sig="SIG", fill_id=3, position_id=2)
    life.deliver()
    assert sent == ["filled"]


def test_a_transaction_still_unresolved_after_the_grace_pages(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_UNRESOLVED_GRACE_S", 30.0)
    clock = ["2026-09-11T12:00:05+00:00"]
    life, conn, current, sent = _clocked_worker(clock)
    current["e1"] = view("submitted_unknown", tx_sig="SIG")
    life.observe(current["e1"])
    life.deliver()
    clock[0] = "2026-09-11T12:00:40+00:00"
    life.deliver()
    assert sent == ["submitted_unknown"]


def test_a_round_trip_that_returns_more_than_paid_reads_as_a_gain():
    """cost_pct is (1 - back/paid) x 100. PENIS (2026-09-12) printed "-4.09% · sells back $182.16"
    on a $175 clip, which read as a loss."""
    from sniper.services.memecoin_telegram import render_entry_notification
    view = {"status": "failed_before_submission", "ticker": "T", "network_id": "solana",
            "origin_event_id": "o", "source_handle": "a", "source_size_usd": 100.0,
            "reason": "route build refused",
            "gate_evidence": [{"name": "roundtrip_cap", "cost_pct": -4.09, "cap_pct": 10.0,
                               "scaled_sell_usd": 182.16, "basis": "independent_pre_entry_quotes"}]}
    text = render_entry_notification(view)
    text = text.get("text") if isinstance(text, dict) else text
    assert "Round trip gains 4.09% vs cap 10.00% · sells back $182.16" in str(text)


def test_a_size_refusal_says_the_transaction_was_too_large():
    """2026-09-14 ("human" ref 7b9a3454): the alert said a bare `broadcast_rejected` for a buy
    the send step refused because the signed swap was over the Solana packet cap."""
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("no_fill", reason="broadcast_rejected", gate_evidence=[
        {"name": "oversize_retry", "pre_bytes": 1473, "limit": 1232},
        {"name": "oversize_retry_rebuild", "post_bytes": 1442, "limit": 1232, "fits": False}])
    assert "Why: transaction too large (1,442 bytes, cap 1,232)" in render_entry_notification(v)
    v["gate_evidence"] = []
    assert "Why: broadcast_rejected" in render_entry_notification(v)   # no size evidence: as before


@pytest.mark.parametrize('prior_source', [False, True])
def test_mcap_ceiling_refusal_stays_silent_after_source(prior_source):
    life, conn, current, sent = worker()
    if prior_source:
        current['e1'] = view('evaluating')
        life.observe(current['e1'])
    current['e1'] = view('refused', reason='mcap $11,013,731 over ceiling $250,000 (over_ceiling)')
    life.observe(current['e1'])
    life.deliver()
    assert sent == []
    assert rows(conn)[-1]['delivery_state'] == 'baseline'


def test_queued_mcap_refusal_is_silent_at_delivery():
    life, conn, current, sent = worker()
    current['e1'] = view('refused', reason='over_ceiling')
    life.observe(current['e1'])
    conn.execute("UPDATE memecoin_notification_transitions SET delivery_state='queued'")
    conn.commit()
    life.deliver()
    assert sent == []
    assert rows(conn)[-1]['delivery_state'] == 'baseline'


@pytest.mark.parametrize('status,reason,extra', [
    ('filled', 'over_ceiling', {}),
    ('submitted_unknown', 'over_ceiling', {}),
    ('transaction_failed', 'over_ceiling', {}),
    ('refused', 'm5_turnover_over_ceiling', {}),
    ('refused', 'under_floor', {}),
    ('refused', 'over_ceiling', {'tx_sig': 'sig'}),
    ('refused', 'over_ceiling', {'source_user_id': None}),
    ('refused', 'over_ceiling', {'message_kind': 'exit'}),
    ('refused', 'insufficient quote balance', {'network_id': '5042'}),
])
def test_mcap_mute_preserves_other_results(status, reason, extra):
    life, conn, current, sent = worker()
    v = view(status, reason=reason)
    v.update(extra)
    current['e1'] = v
    life.read_view = lambda **kw: v
    life.observe(v)
    life.now_fn = lambda: '2026-09-11T12:00:40+00:00'
    life.deliver()
    assert sent == [status]


def test_arc_source_alert_uses_its_chain_approval(monkeypatch):
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_telegram import _NotificationWorker
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_TG_ALERTS', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_TG_MIN_USD', 249)
    monkeypatch.setattr(cfg, 'MEMECOIN_TG_MAX_MCAP_USD', 100_000)
    monkeypatch.setattr(m, 'real_approved_user_ids', lambda conn, network_id: {'u1'} if network_id == '5042' else set())
    monkeypatch.setattr(m, 'curated_user_ids', lambda conn: {'u1'})
    w = object.__new__(_NotificationWorker); w.read_conn = None
    v = view('source_received')
    v.update(network_id='5042', source_market_cap=12_000, source_size_usd=500)
    assert w._eligible(v)


def test_refusal_reports_receipt_processing_and_prior_failures():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view('refused', reason='entry older than 60s',
             socket_received_at='2026-09-11T12:00:01.500000+00:00',
             source_timestamp_trust='claimed_untrusted',
             wallet_evidence={'paper_n': 0, 'paper_pnl': None, 'gate_ok': False},
             decision_started_at='2026-09-11T12:00:02.900000+00:00',
             attempts=[{'attempt_id': 'older', 'terminal_status': 'aborted',
                        'terminal_reason': 'provider_poolkey_read_failed',
                        'completed_at': '2026-09-11T12:00:02.500000+00:00'},
                       {'attempt_id': 'a1', 'terminal_status': 'refused',
                        'terminal_reason': 'entry older than 60s'},
                       {'attempt_id': 'future', 'terminal_status': 'aborted',
                        'terminal_reason': 'not_yet',
                        'completed_at': '2026-09-11T12:00:04+00:00'}])
    text = render_entry_notification(v)
    assert '+1.0s received' in text and '+0.9s processed' in text
    assert '1.0s receipt to result' in text
    assert 'source (unverified)' in text
    assert 'Earlier failures: 1' in text and 'provider_poolkey_read_failed' in text
    assert 'not_yet' not in text and 'gate ✗' not in text
    assert 'Paper αb' not in text


def test_missing_receipt_does_not_claim_socket_arrival():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view('refused', decision_started_at='2026-09-11T12:00:02+00:00')
    v['observed_at'] = None
    text = render_entry_notification(v)
    assert 's received' not in text
    assert 'processed unknown' in text and '1.0s processed to result' in text


def test_a_delivered_evaluating_card_always_gets_its_result_even_a_ceiling_refusal(monkeypatch):
    """GCAT (BSC, 2026-09-17 11:49Z): the decision started 4.2 s after the signal, so the source hold
    expired and EVALUATING went out. The result was an over-ceiling refusal, which is muted, so the
    operator saw EVALUATING and nothing after it. Once EVALUATING is delivered, the result pages."""
    _source_cards(monkeypatch)
    life, conn, current, sent = worker()
    current['e1'] = view('evaluating')
    life.observe(current['e1'])
    life.now_fn = lambda: '2026-09-11T12:00:10+00:00'       # past the 4 s source hold
    life.deliver()
    assert sent == ['evaluating']
    current['e1'] = view('refused', reason='mcap $1,412,896 over ceiling $250,000 (over_ceiling)')
    life.observe(current['e1'])
    life.deliver()
    assert sent == ['evaluating', 'refused']


def test_a_ceiling_refusal_without_a_delivered_card_stays_silent():
    life, conn, current, sent = worker()
    current['e1'] = view('refused', reason='mcap $1,412,896 over ceiling $250,000 (over_ceiling)')
    life.observe(current['e1'])
    life.now_fn = lambda: '2026-09-11T12:00:10+00:00'
    life.deliver()
    assert sent == []


def test_source_delivery_lookup_does_not_scan_other_signals():
    from sniper.persistence.db_memecoin_notifications import _source_card_delivered
    _life, conn, _current, _sent = worker()
    conn.executemany(
        "INSERT INTO memecoin_notification_transitions "
        "(transition_key, network_id, origin_event_id, attempt_id, message_kind, execution_state, "
        "event_at, created_at, updated_at, delivery_state) VALUES (?, 'solana', ?, ?, 'source', "
        "'evaluating', 't', 't', 't', 'delivered')",
        [(str(i), 'other-' + str(i), 'other-' + str(i)) for i in range(1000)])
    steps = []
    conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
    try:
        assert not _source_card_delivered(conn, view())
    finally:
        conn.set_progress_handler(None, 0)
    assert len(steps) < 10, 'The lookup must use the signal identity index.'


def test_a_passing_round_trip_on_another_refusal_is_labelled_passed():
    """SEND (2026-09-17): the card refused on the route premium and also printed
    "Round trip costs 4.85% vs cap 10.00%", which read like a second refusal."""
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("refused", reason="route quote is 1.79x the source price (cap 1.50x)", gate_evidence=[
        {"name": "roundtrip_cap", "cost_pct": 4.85, "cap_pct": 10.0, "scaled_sell_usd": 166.52,
         "basis": "independent_pre_entry_quotes"}])
    text = render_entry_notification(v)
    text = text.get("text") if isinstance(text, dict) else text
    assert "Round trip costs 4.85% vs cap 10.00% · sells back $166.52 (independent_pre_entry_quotes) · passed" in str(text)


@pytest.mark.parametrize("reason,bound,need,tested,basis", [
    ("m5_volume_under_abs_floor", None, 5000, 4000, "source_adjusted_volume_m5"),
    ("m5_turnover_under_floor", None, 5000, 4000, "source_adjusted_volume_m5"),
    ("m5_turnover_under_floor", 8000, 10000, 8000, "onchain_v4_lower_bound"),
    ("m5_turnover_under_floor", float("nan"), 5000, 4000, "source_adjusted_volume_m5"),
])
def test_volume_evidence_records_the_final_gate_input(reason, bound, need, tested, basis):
    from sniper.services.memecoin_real_runner import _volume_notification_evidence
    result = _volume_notification_evidence(
        volume_m5=4000, signal_usd=3500, mcap=150000, effective_ceiling=0,
        reason=reason, need=need, floor_lower_bound=bound, source_age_s=1, index_lag_s=90)
    assert result["tested_volume_m5"] == tested
    assert result["decided_organic_volume_m5"] == tested
    assert result["threshold"] == need
    assert result["measurement_basis"] == basis


def test_entry_card_uses_own_observation_clock_and_drops_legacy_paper_money():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("shadow", reason="Pre-entry checks passed; reservation and quote checks not run.",
             decision_started_at="2026-09-11T12:00:02.002+00:00",
             socket_received_at="2026-09-11T12:00:02.005+00:00",
             wallet_evidence={"paper_n": 38, "paper_pnl": 159},
             fast_lane_results=[{"attempt_id":"fast", "status":"refused",
                                 "reason":"re-entry cooldown", "signal_source":"onchain_watch"}])
    text = render_entry_notification(v)
    assert "SHADOW" in text and "NEEDS REVIEW" not in text
    assert "Fast lane: refused · re-entry cooldown" in text
    assert "Paper" not in text and "$159" not in text
    assert "stamps disagree" not in text


def test_monotonic_decision_elapsed_survives_utc_offset_jump():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view("refused", decision_started_at="2026-09-11T12:00:01.950+00:00",
             decision_elapsed_ms=839.2)
    text = render_entry_notification(v)
    assert "0.839s attempt duration" in text
    assert "1.0s receipt to result" not in text


def test_shadow_scan_is_silent_but_signed_unknown_still_pages(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_UNRESOLVED_GRACE_S", 0.0)
    life, conn, current, sent = worker()
    current["e1"] = view("shadow")
    life.observe(current["e1"]); life.deliver()
    assert sent == []
    current["e1"] = view("submitted_unknown", tx_sig="SIG")
    life.observe(current["e1"]); life.deliver()
    assert sent == ["submitted_unknown"]


# AUTON f139fd7a (2026-10-06 20:49:02Z): a j7_shadow attempt refused native_fast_lane_unavailable
# paged "REFUSED · Why: native_fast_lane_unavailable" while the coin's real problem was its mcap.
# Built from that row's real shape through the real writers and entry_view, never a hand-made view.
_AUTON_ORIGIN = ("swap:raybot:4PwMJ8uuRt5tifKW1Zmiz7FuLRMb9sR1uSTD1tDSQJYvATYvrZF9DYGQMzMXo4jSJjCL6Dz"
                 "JSYJQnJ6gdRd7wm7f:f5mybjeatqfd6rxwds2zxzehbguuhsgvjkuhlfacr4hv:"
                 "39ahtl8ynze4amh26j29c93pa5172v3ft9uuucqqs8fz")


def _real_refusal_lifecycle(delivery_lane, reason, *, signal_source="follow_activity"):
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    from sniper.services.memecoin_entry_view import entry_view
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    started = "2026-09-11T12:00:02.065037+00:00"
    m.start_entry_attempt(conn, attempt_id="f139fd7ab1364c6692ca384e76c7c520",
                          origin_event_id=_AUTON_ORIGIN, decision_started_at=started,
                          signal_source=signal_source, delivery_lane=delivery_lane,
                          network_id="solana", mint="39ahtL8ynzE4amH26J29C93PA5172V3ft9UuUcqQS8fz",
                          source_user_id="uid-omakase", source_handle="OmakaseOnly",
                          ticker="AUTON", side="buy", observed_at=started,
                          clock_domain="observed_real_utc")
    m.finish_entry_attempt(conn, "f139fd7ab1364c6692ca384e76c7c520", "refused", reason,
                           "2026-09-11T12:00:02.068+00:00", "{}")
    sent = []
    life = NotificationLifecycle(conn, now_fn=lambda: "2026-09-11T12:00:04+00:00",
                                 read_view=lambda **kw: entry_view(conn, **kw),
                                 send_fn=lambda text: sent.append(text) or (True, 1),
                                 render_fn=lambda v: "%s|%s" % (v["status"], v["reason"]),
                                 eligible_fn=lambda v: True)
    life.activate("2026-09-11T12:00:00+00:00")
    life.observe(entry_view(conn, origin_event_id=_AUTON_ORIGIN))
    life.deliver()
    return sent


@pytest.mark.parametrize("delivery_lane", ["j7_shadow", "j7_wallets"])
def test_a_j7_fast_lane_only_refusal_from_the_real_attempt_shape_never_pages(delivery_lane):
    assert _real_refusal_lifecycle(delivery_lane, "native_fast_lane_unavailable") == []


def test_the_same_real_shape_with_a_coin_reason_still_pages():
    sent = _real_refusal_lifecycle("j7_wallets", "re-entry cooldown: a real position closed")
    assert sent == ["refused|re-entry cooldown: a real position closed"]


# ---- 8453 PICNIC (2026-10-06, Gamma/Alpha): a swap stopped behind an unseen unwrap spent nothing ----

_UNWRAP_WORDS = "Why: unwrap not yet visible to the swap node (no buy made, gas only)"


def test_a_swap_held_for_an_unseen_unwrap_says_so_and_no_landing_line():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = dict(view("unknown", reason="native pre-transaction ambiguous",
                  fill_timing_line="Landing block resolving",
                  gate_evidence=[{"name": "evm.native_visible", "visible": False, "wait_ms": 6000.0}]),
             network_id="8453")
    text = render_entry_notification(v)
    assert _UNWRAP_WORDS in text and "Landing block resolving" not in text


def test_a_swap_rejected_after_an_unwrap_never_proven_visible_says_the_same():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = dict(view("no_fill", reason="broadcast_rejected", fill_timing_line="Landing block resolving",
                  gate_evidence=[{"name": "evm.native_visible", "wait_ms": 412.0}]),
             network_id="8453")
    text = render_entry_notification(v)
    assert _UNWRAP_WORDS in text and "Why: broadcast_rejected" not in text
    assert "Landing block resolving" not in text


def test_a_rejection_after_every_node_saw_the_unwrap_keeps_its_own_words():
    # Gamma LOW on bec6db20d: visible=True means the unwrap was seen; the refusal is something else.
    from sniper.services.memecoin_telegram import render_entry_notification
    v = dict(view("no_fill", reason="broadcast_rejected",
                  gate_evidence=[{"name": "evm.native_visible", "visible": True, "wait_ms": 412.0}]),
             network_id="8453")
    assert "Why: broadcast_rejected" in render_entry_notification(v)


def test_a_rejection_with_no_unwrap_keeps_its_own_words():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = dict(view("no_fill", reason="broadcast_rejected", gate_evidence=[]), network_id="8453")
    assert "Why: broadcast_rejected" in render_entry_notification(v)


def test_the_entry_view_keeps_the_native_visible_evidence():
    import json
    from sniper.services.memecoin_entry_view import _gate_evidence
    trace = {"substage_events": [{"name": "evm.native_visible", "visible": False, "wait_ms": 6000.0,
                                  "need_wei": 10 ** 18, "attempt_no": 1}]}
    rows = _gate_evidence({"trace_json": json.dumps(trace)})
    assert rows == [{"name": "evm.native_visible", "visible": False, "wait_ms": 6000.0}]

@pytest.mark.parametrize('status,allowed,active,shown', [
    ('failed_before_submission', True, False, False),
    ('filled', True, False, False),
    ('refused', False, True, True),
    ('refused', False, False, True),
])
def test_volume_card_only_shows_enforced_refusal(status, allowed, active, shown):
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view(status, reason='float_short', gate_evidence=[dict(name='volume_gate',
        allowed=allowed, active=active, tested_volume_m5=99.4, threshold=3064.987,
        measurement_basis='source_adjusted_volume_m5')])
    assert ('Volume m5:' in render_entry_notification(v)) is shown


def test_float_short_names_native_funding_buffer():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = view('failed_before_submission', reason='float_short')
    v['network_id'] = '56'
    assert 'native BNB funding buffer cannot cover approved input plus gas' in render_entry_notification(v)


@pytest.mark.parametrize('source_sent', [False, True])
def test_native_owned_secondary_is_silent_even_after_source_card(monkeypatch, source_sent):
    _source_cards(monkeypatch)
    life, conn, current, sent = worker()
    if source_sent:
        current['e1'] = view()
        life.observe(current['e1'])
        # Advance past the source-card hold.
        life.now_fn = lambda: '2026-09-11T12:00:08+00:00'
        life.deliver()
        assert sent == ['evaluating']
    current['e1'] = view('refused', reason='missing_receipt_proof',
        duplicate_same_source=True, native_owned_secondary_refusal=True)
    life.observe(current['e1'])
    life.deliver()
    assert sent == (['evaluating'] if source_sent else [])
    assert rows(conn)[-1]['delivery_state'] == 'baseline'


def test_deliver_rechecks_new_native_ownership_after_queueing():
    life, conn, current, sent = worker()
    current['e1'] = view('refused', reason='missing_receipt_proof')
    life.observe(current['e1'])
    current['e1'].update(duplicate_same_source=True, native_owned_secondary_refusal=True)
    life.deliver()
    assert sent == []
    assert rows(conn)[-1]['delivery_state'] == 'baseline'


def terminal_review_fixture():
    life, conn, current, sent = worker()
    old = view('needs_review', tx_sig='SIGNED', reservation_status='failed',
               reservation_resolution_reason='rh_fast_reverted')
    current['e1'] = old
    life.observe(old)
    life.deliver()
    assert sent == ['needs_review']
    life.now_fn = lambda: '2026-09-14T08:04:11+00:00'
    return life, conn, current, sent, old


def test_unchanged_terminal_projection_correction_retains_execution_stamp():
    life, conn, current, sent, old = terminal_review_fixture()
    current['e1'] = dict(old, status='transaction_failed')
    life.refresh_tracked()
    life.deliver()
    result = rows(conn)[-1]
    assert result['event_at'] == old['status_updated_at']
    assert result['delivery_state'] == 'baseline'
    assert sent == ['needs_review']
    assert not any(r['message_kind'] == 'missed_summary' for r in rows(conn))


@pytest.mark.parametrize('field,value', [
    ('completed_at', '2026-09-14T08:04:10+00:00'),
    ('status_updated_at', '2026-09-14T08:04:10+00:00'),
    ('tx_sig', 'DIFFERENT'),
    ('reservation_resolution_reason', 'new_receipt_failure'),
])
def test_changed_durable_terminal_facts_still_notify(field, value):
    life, conn, current, sent, old = terminal_review_fixture()
    current['e1'] = dict(old, status='transaction_failed', **{field:value})
    life.refresh_tracked()
    life.deliver()
    assert sent == ['needs_review', 'transaction_failed']


def test_legacy_terminal_label_correction_is_explicit_when_proof_is_missing():
    from sniper.services.memecoin_telegram import render_entry_notification
    life, conn, current, sent, old = terminal_review_fixture()
    # A transition recorded by the previous release has timestamps but no terminal ledger proof.
    conn.execute("UPDATE memecoin_notification_transitions SET evidence_json='{}'")
    conn.commit()
    current['e1'] = dict(old, status='transaction_failed')
    life.render_fn = render_entry_notification
    life.refresh_tracked()
    life.deliver()
    assert len(sent) == 2 and 'Historical result correction' in sent[-1]
