"""Refusal notices collapse to one per coin per window, and the EVALUATING card is muted.

Evidence for both: docs/audits/2026-09-22-research-lanes-and-alerts-review.md -- 1,927 of 3,027
Telegram messages in seven days were entry REFUSED notices over 630 coins (one coin 38 times), and
50 were EVALUATING cards with no outcome attached to any of them.
"""
import sqlite3

import pytest

from sniper.settings import memecoin as cfg


def view(status="refused", **fields):
    base = dict(origin_event_id="e1", attempt_id="a1", network_id="solana", mint="M",
                ticker="TEST", source_handle="alice", source_user_id="u1", side="buy",
                source_created_at="2026-09-22T12:00:01+00:00",
                observed_at="2026-09-22T12:00:02+00:00",
                completed_at="2026-09-22T12:00:03+00:00",
                status_updated_at="2026-09-22T12:00:03+00:00",
                status=status, reason="roundtrip_cost", authority={"status": "armed"},
                requested_usd=175)
    base.update(fields)
    return base


def worker(now="2026-09-22T12:00:04+00:00"):
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sent, clock = [], {"now": now}
    life = NotificationLifecycle(conn, now_fn=lambda: clock["now"],
        read_view=lambda **kw: current[kw.get("origin_event_id")],
        send_fn=lambda text: sent.append(text) or (True, 1),
        render_fn=lambda v: "%s:%s" % (v["status"], v.get("mint")), eligible_fn=lambda v: True)
    current = {}
    life.read_view = lambda **kw: current[kw.get("origin_event_id")]
    life.activate("2026-09-22T12:00:00+00:00")
    return life, conn, current, sent, clock


def states(conn):
    return [r[0] for r in conn.execute(
        "SELECT delivery_state FROM memecoin_notification_transitions "
        "WHERE message_kind='entry' ORDER BY rowid")]


def _refuse(life, current, clock, *, event_id, at, mint="M", **fields):
    clock["now"] = at
    v = view(origin_event_id=event_id, attempt_id="a-" + event_id, mint=mint,
             status_updated_at=at, completed_at=at, observed_at=at, **fields)
    current[event_id] = v
    life.observe(v)
    life.deliver()
    return v


def test_second_refusal_for_the_same_coin_inside_the_window_is_silent_but_recorded(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 3600.0)
    life, conn, current, sent, clock = worker()
    _refuse(life, current, clock, event_id="e1", at="2026-09-22T12:00:04+00:00")
    _refuse(life, current, clock, event_id="e2", at="2026-09-22T12:20:00+00:00")
    assert sent == ["refused:M"]
    assert states(conn) == ["delivered", "baseline"]


def test_a_refusal_after_the_window_pages_again(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 3600.0)
    life, conn, current, sent, clock = worker()
    _refuse(life, current, clock, event_id="e1", at="2026-09-22T12:00:04+00:00")
    _refuse(life, current, clock, event_id="e2", at="2026-09-22T13:00:05+00:00")
    assert sent == ["refused:M", "refused:M"]


def test_a_different_coin_inside_the_window_pages(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 3600.0)
    life, conn, current, sent, clock = worker()
    _refuse(life, current, clock, event_id="e1", at="2026-09-22T12:00:04+00:00")
    _refuse(life, current, clock, event_id="e2", at="2026-09-22T12:20:00+00:00", mint="N")
    assert sent == ["refused:M", "refused:N"]


def test_a_refusal_after_money_moved_is_never_collapsed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 3600.0)
    life, conn, current, sent, clock = worker()
    _refuse(life, current, clock, event_id="e1", at="2026-09-22T12:00:04+00:00")
    _refuse(life, current, clock, event_id="e2", at="2026-09-22T12:20:00+00:00", tx_sig="sig1")
    assert sent == ["refused:M", "refused:M"]


def test_a_filled_entry_inside_the_window_is_never_collapsed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 3600.0)
    life, conn, current, sent, clock = worker()
    _refuse(life, current, clock, event_id="e1", at="2026-09-22T12:00:04+00:00")
    _refuse(life, current, clock, event_id="e2", at="2026-09-22T12:20:00+00:00", status="filled")
    assert sent == ["refused:M", "filled:M"]


def test_the_knob_at_zero_restores_every_refusal(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 0.0)
    life, conn, current, sent, clock = worker()
    _refuse(life, current, clock, event_id="e1", at="2026-09-22T12:00:04+00:00")
    _refuse(life, current, clock, event_id="e2", at="2026-09-22T12:00:30+00:00")
    assert sent == ["refused:M", "refused:M"]


def test_a_collapsed_refusal_does_not_silence_the_coin_it_never_paged_for(monkeypatch):
    """Only a refusal the operator could have SEEN starts a window."""
    monkeypatch.setattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 3600.0)
    life, conn, current, sent, clock = worker()
    life.eligible_fn = lambda v: False       # nothing pages at all
    _refuse(life, current, clock, event_id="e1", at="2026-09-22T12:00:04+00:00")
    life.eligible_fn = lambda v: True
    _refuse(life, current, clock, event_id="e2", at="2026-09-22T12:20:00+00:00")
    assert sent == ["refused:M"]
    assert states(conn) == ["baseline", "delivered"]


def test_evaluating_card_is_muted_by_default_and_the_result_still_pages(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", False)
    life, conn, current, sent, clock = worker()
    current["e1"] = view("evaluating")
    life.observe(current["e1"])
    life.deliver()
    assert sent == []
    current["e1"] = view("refused")
    life.observe(current["e1"])
    life.deliver()
    assert sent == ["refused:M"]


def test_the_knob_restores_the_evaluating_card(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", True)
    life, conn, current, sent, clock = worker()
    current["e1"] = view("evaluating")
    life.observe(current["e1"])
    clock["now"] = "2026-09-22T12:00:30+00:00"    # past MEMECOIN_TG_SOURCE_HOLD_S
    life.deliver()
    assert sent == ["evaluating:M"]


def test_a_muted_card_is_still_rendered_so_the_alert_instrument_keeps_its_population(monkeypatch):
    """The renderer writes the alert-intent row; muting the send must not skip it."""
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", False)
    rendered = []
    life, conn, current, sent, clock = worker()
    life.render_fn = lambda v: rendered.append(v["status"]) or "text"
    current["e1"] = view("evaluating")
    life.observe(current["e1"])
    clock["now"] = "2026-09-22T12:00:30+00:00"        # past MEMECOIN_TG_SOURCE_HOLD_S
    life.deliver()
    assert sent == [], "nothing reaches Telegram"
    assert rendered == ["evaluating"], "the intent writer still runs"
    assert [r[0] for r in conn.execute(
        "SELECT delivery_state FROM memecoin_notification_transitions "
        "WHERE message_kind='source'")] == ["baseline"]


def test_a_card_superseded_inside_the_hold_records_nothing_muted_or_not(monkeypatch):
    """The muted population must be the cards that WOULD have sent, not every observed signal."""
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", False)
    rendered = []
    life, conn, current, sent, clock = worker()
    life.render_fn = lambda v: rendered.append(v["status"]) or "text"
    current["e1"] = view("evaluating")
    life.observe(current["e1"])
    life.deliver()                                   # inside the 4 s hold: no decision yet
    current["e1"] = view("refused")
    life.observe(current["e1"])
    life.deliver()
    assert rendered == ["refused"], "the card never rendered, so it recorded no alert intent"
    assert sent == ["text"]


def test_the_muted_card_writes_its_alert_latency_row_on_a_real_ledger(tmp_path, monkeypatch):
    """The guard for the coupling above: the row must land in memecoin_alert_latency."""
    import sqlite3 as sq
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_telegram as tg
    from sniper.feeds.fomo_feed import CopyEvent
    path = tmp_path / "ledger.db"
    conn = sq.connect(path)
    conn.row_factory = sq.Row
    m.ensure_memecoin_tables(conn)
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    now = ["2026-09-22T12:00:00+00:00"]
    monkeypatch.setattr(tg, "_real_utc_now_iso", lambda: now[0])
    sent = []
    monkeypatch.setattr(tg, "send_telegram", lambda text: sent.append(text) or (True, 1))
    w = tg._NotificationWorker()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", False)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "tg_min_buy_usd", lambda mc: 100)
    monkeypatch.setattr(cfg, "tg_mcap_ok", lambda mc: True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_HOLD_S", 0.0)   # no decision will come here
    m.add_wallet(w.write_conn, "u1", "alice")
    w.write_conn.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='4663' "
                         "WHERE source_user_id='u1'")
    w.write_conn.commit()
    now[0] = "2026-09-22T12:00:04+00:00"
    ev = CopyEvent("u1", "alice", "0x" + "a" * 40, "T", "buy", .1, None, 700, True,
                   "e1", "2026-09-22T12:00:01+00:00", "2026-09-22T12:00:02+00:00", 1,
                   network_id="4663")
    m.insert_event(w.write_conn, vars(ev))
    w.cycle({"event": ev})
    assert sent == [], "the card is muted"
    rows = w.write_conn.execute(
        "SELECT fomo_event_id, mint, detected_at, alert_sent_at FROM memecoin_alert_latency").fetchall()
    assert [r["fomo_event_id"] for r in rows] == ["e1"], "the instrument kept the signal"
    assert rows[0]["mint"] == "0x" + "a" * 40
    assert rows[0]["detected_at"], "detected_at is the anchor a consumer must use"
    assert rows[0]["alert_sent_at"] is None, "nothing was sent, so the send time stays null"
