"""A deferred unpriced close stays owed until it is published (2026-10-08 review C05).

The scan defers a young unpriced close, but the fill cursor still moves past it, and the re-check
only re-read the newest 500 fills inside six hours. A seven-hour scanner gap, or 501 newer fills,
dropped the close for good. Each deferred fill id is now kept in the scan state (the same row as
the cursors) and re-read until it is observed, or dropped with a named terminal reason.
"""
import sqlite3

from sniper.persistence import db_memecoin_notifications as n
from test_memecoin_exit_card_unpriced import _fill, _setup


def _scan(life, read, view, times=1):
    for _ in range(times):
        n.scan_notification_views(life, read, view)


def _state(life):
    import json
    return json.loads(life.conn.execute(
        "SELECT scan_cursors_json FROM memecoin_notification_state WHERE singleton=1").fetchone()[0])


def test_a_deferred_close_survives_a_seven_hour_gap():
    life, read, clock, seen, view = _setup()
    _fill(read, 9001, priced=False)
    _scan(life, read, view)
    assert seen == []
    clock[0] = "2026-10-08T02:51:00+00:00"            # 7 h later
    _scan(life, read, view)
    assert seen == [(9001, None)]


def test_a_deferred_close_survives_501_newer_fills():
    life, read, clock, seen, view = _setup()
    _fill(read, 9001, priced=False)
    _scan(life, read, view)
    read.executemany("INSERT INTO memecoin_fills(id,position_id,kind,created_at,qty,price,notional,fill_source) "
                     "VALUES(?,71670,'entry','2026-10-07T19:50:30+00:00',1,0.1,0.1,'chain_confirmed')",
                     [(10_000 + i,) for i in range(501)])
    read.commit()
    clock[0] = "2026-10-07T19:53:00+00:00"            # past the 120 s wait
    _scan(life, read, view, times=7)                  # the cursor drains 100 a scan
    assert (9001, None) in seen


def test_a_restart_while_waiting_keeps_the_owed_close():
    life, read, clock, seen, view = _setup()
    _fill(read, 9001, priced=False)
    _scan(life, read, view)
    fresh = n.NotificationLifecycle(life.conn, now_fn=lambda: clock[0], read_view=lambda **kw: None,
                                    send_fn=lambda text: (True, 1), render_fn=lambda v: "x",
                                    eligible_fn=lambda v: True)
    clock[0] = "2026-10-08T03:00:00+00:00"
    _scan(fresh, read, view)
    assert seen == [(9001, None)]


def test_a_price_that_lands_late_still_pages_priced_and_once():
    life, read, clock, seen, view = _setup()
    _fill(read, 9001, priced=False)
    _scan(life, read, view)
    clock[0] = "2026-10-07T19:51:00+00:00"            # inside the wait
    read.execute("UPDATE memecoin_fills SET price=0.17,notional=17.36,"
                 "fill_source='chain_confirmed_receipt_reconciled' WHERE id=9001")
    read.commit()
    clock[0] = "2026-10-08T03:00:00+00:00"
    _scan(life, read, view, times=3)
    assert seen == [(9001, 17.36)]
    assert 9001 not in _state(life).get("deferred_fills", [])


def test_a_deferred_close_whose_position_vanished_ends_with_a_reason(caplog):
    life, read, clock, seen, view = _setup()
    _fill(read, 9001, priced=False)
    _scan(life, read, view)
    read.execute("DELETE FROM memecoin_positions WHERE id=71670")
    read.commit()
    clock[0] = "2026-10-08T03:00:00+00:00"
    _scan(life, read, view)
    state = _state(life)
    assert seen == [] and 9001 not in state.get("deferred_fills", [])
    assert state["deferred_terminal"]["9001"] == "view_unavailable"
    assert "deferred close fill=9001 ended view_unavailable" in caplog.text
