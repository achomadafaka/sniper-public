"""An unpriced full-close card waits for its receipt reconciliation (2026-10-07, Alpha).

Base manual exits 71670 and 71561 paged '💰 SOLD ... Our sale: Unknown @ Unknown'. Recovery booked
the confirmed close without USD proceeds (fill_source chain_confirmed_unpriced, price and notional
NULL), the notification scan observed that fill row once and sent it, and the next sweep's
'unpriced close reconciled from receipt' priced the SAME row in place (71670 +$7.36) after the card
had gone. The scan now defers such a fill until it is priced, or until
MEMECOIN_EXIT_CARD_UNPRICED_WAIT_S passes (then it sends what it has). Other fills are unchanged.
"""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.persistence import db_memecoin_notifications as n


def _setup():
    life_conn = sqlite3.connect(":memory:")
    life_conn.row_factory = sqlite3.Row
    clock = ["2026-10-07T19:50:00+00:00"]
    seen = []
    life = n.NotificationLifecycle(life_conn, now_fn=lambda: clock[0], read_view=lambda **kw: None,
                                   send_fn=lambda text: (True, 1), render_fn=lambda v: "x",
                                   eligible_fn=lambda v: True)
    life.activate("2026-10-07T19:00:00+00:00")
    read = sqlite3.connect(":memory:")
    read.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(read)
    n.scan_notification_views(life, read, lambda **kw: None)     # seeds the cursors
    read.execute("INSERT INTO memecoin_positions(id,mint,network_id,execution_mode,is_open) "
                 "VALUES(71670,'0xkarp','8453','real',0)")

    def view(**kw):
        current = n.notification_exit_view(read, kw.get("fill_id"))
        if current and not life.conn.execute(
                "SELECT 1 FROM memecoin_notification_transitions WHERE fill_id=?",
                (str(kw.get("fill_id")),)).fetchone():
            seen.append((kw.get("fill_id"), current["filled_notional"]))   # what the card says
        return current
    return life, read, clock, seen, view


def _fill(read, fid, *, priced):
    read.execute("INSERT INTO memecoin_fills(id,position_id,kind,tx_sig,created_at,qty,price,notional,fill_source) "
                 "VALUES(?,71670,'manual_close','0xsig%s','2026-10-07T19:50:00+00:00',100,?,?,?)",
                 (fid, 0.1 if priced else None, 10.0 if priced else None,
                  "chain_confirmed" if priced else "chain_confirmed_unpriced"))
    read.commit()


def test_an_unpriced_close_is_observed_once_it_is_priced():
    """THE regression: the card went before the reconciliation priced the row."""
    life, read, clock, seen, view = _setup()
    _fill(read, 9001, priced=False)
    n.scan_notification_views(life, read, view)
    assert seen == [], "deferred while unpriced"
    read.execute("UPDATE memecoin_fills SET price=0.17,notional=17.36,fill_source='chain_confirmed_receipt_reconciled' "
                 "WHERE id=9001")
    read.commit()
    n.scan_notification_views(life, read, view)
    assert seen == [(9001, 17.36)]
    n.scan_notification_views(life, read, view)
    assert seen == [(9001, 17.36)], "paged once"
    assert life.conn.execute("SELECT COUNT(*) FROM memecoin_notification_transitions WHERE fill_id='9001'"
                             ).fetchone()[0] == 1


def test_an_unpriced_close_that_never_reconciles_is_sent_after_the_wait(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EXIT_CARD_UNPRICED_WAIT_S", "120")
    life, read, clock, seen, view = _setup()
    _fill(read, 9002, priced=False)
    n.scan_notification_views(life, read, view)
    clock[0] = "2026-10-07T19:51:30+00:00"
    n.scan_notification_views(life, read, view)
    assert seen == []
    clock[0] = "2026-10-07T19:52:01+00:00"
    n.scan_notification_views(life, read, view)
    assert seen == [(9002, None)]


def test_a_priced_fill_is_observed_at_once_and_later_fills_are_not_held():
    life, read, clock, seen, view = _setup()
    _fill(read, 9003, priced=False)
    _fill(read, 9004, priced=True)
    n.scan_notification_views(life, read, view)
    assert seen == [(9004, 10.0)]


def test_an_unpriced_close_first_seen_after_the_wait_pages_at_once():
    """Gamma LOW, case A: the worker was down past the wait; it must not defer, then forget."""
    life, read, clock, seen, view = _setup()
    _fill(read, 9005, priced=False)
    clock[0] = "2026-10-07T20:10:00+00:00"            # 20 min after the close
    n.scan_notification_views(life, read, view)
    assert seen == [(9005, None)]


def test_a_deferred_close_survives_a_long_scanner_gap():
    """Gamma LOW, case B: deferred at age 0, next scan 8 minutes later."""
    life, read, clock, seen, view = _setup()
    _fill(read, 9006, priced=False)
    n.scan_notification_views(life, read, view)
    clock[0] = "2026-10-07T19:58:00+00:00"
    n.scan_notification_views(life, read, view)
    assert seen == [(9006, None)]


def test_an_unpriced_close_first_seen_past_the_recheck_window_still_pages():
    """Gamma: the main-loop age check alone covers a close first seen after the 6 h re-check."""
    life, read, clock, seen, view = _setup()
    _fill(read, 9007, priced=False)
    clock[0] = "2026-10-08T02:50:00+00:00"            # 7 h after the close
    n.scan_notification_views(life, read, view)
    assert seen == [(9007, None)]
