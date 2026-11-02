"""The cockpit's "Release pause" button (2026-09-18).

The operator watches the system by hand and wants the drawdown pause out of the way while he does,
without disarming it for the hours he is away. The knob group can turn a rail OFF; this releases the
rail's CURRENT hold and leaves it armed, exactly like Clear on the kill switch.

The defect this file pins: a latch is a row, and a row outlives the count that produced it. Both the
drawdown replay and the daily latch had to learn about the reset, or the button reports success
while entries stay blocked.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.persistence import db_memecoin as m

DAY = "2026-09-18"
RESET = "/api/memecoin/real/risk-reset"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_positions (is_open INT, execution_mode TEXT, "
              "realized_pnl REAL, closed_at TEXT)")
    c.execute("CREATE TABLE memecoin_daily_loss_trips (day TEXT PRIMARY KEY, tripped_at TEXT)")
    return c


def close(conn, at, pnl):
    conn.execute("INSERT INTO memecoin_positions VALUES (0,'real',?,?)", (pnl, at))


def test_a_reset_releases_an_active_drawdown_pause(conn):
    close(conn, f"{DAY}T10:00:00+00:00", +50.0)
    close(conn, f"{DAY}T10:05:00+00:00", -120.0)      # $170 off the peak -> pause
    now = f"{DAY}T10:10:00+00:00"
    assert m.drawdown_pause_until(conn, DAY, 100, 2640, now) is not None
    m.set_risk_reset(conn, DAY, actor="test", reason="operator watching", now_iso=now)
    assert m.drawdown_pause_until(conn, DAY, 100, 2640, now) is None


def test_the_rail_stays_armed_and_the_next_trigger_pauses_again(conn):
    close(conn, f"{DAY}T10:00:00+00:00", +50.0)
    close(conn, f"{DAY}T10:05:00+00:00", -120.0)
    m.set_risk_reset(conn, DAY, actor="test", reason="", now_iso=f"{DAY}T10:10:00+00:00")
    assert m.drawdown_pause_until(conn, DAY, 100, 2640, f"{DAY}T10:11:00+00:00") is None
    # a NEW drop after the release must pause again — this is a release, not a disable
    close(conn, f"{DAY}T10:20:00+00:00", +80.0)
    close(conn, f"{DAY}T10:25:00+00:00", -150.0)
    assert m.drawdown_pause_until(conn, DAY, 100, 2640, f"{DAY}T10:30:00+00:00") is not None


def test_a_reset_releases_the_daily_latch(conn):
    conn.execute("INSERT INTO memecoin_daily_loss_trips VALUES (?,?)", (DAY, f"{DAY}T09:00:00+00:00"))
    assert m.is_daily_loss_tripped(conn, DAY) is True
    m.set_risk_reset(conn, DAY, actor="test", reason="", now_iso=f"{DAY}T10:00:00+00:00")
    assert m.is_daily_loss_tripped(conn, DAY) is False


def test_a_latch_that_fires_after_the_reset_still_blocks(conn):
    m.set_risk_reset(conn, DAY, actor="test", reason="", now_iso=f"{DAY}T10:00:00+00:00")
    conn.execute("INSERT INTO memecoin_daily_loss_trips VALUES (?,?)", (DAY, f"{DAY}T11:00:00+00:00"))
    assert m.is_daily_loss_tripped(conn, DAY) is True


def test_the_release_expires_at_midnight(conn):
    """The row is keyed by day, so tomorrow counts from its own start with no carried release."""
    m.set_risk_reset(conn, DAY, actor="test", reason="", now_iso=f"{DAY}T23:59:00+00:00")
    assert m.risk_reset_at(conn, DAY) != ""
    assert m.risk_reset_at(conn, "2026-09-19") == ""
    conn.execute("INSERT INTO memecoin_daily_loss_trips VALUES (?,?)",
                 ("2026-09-19", "2026-09-19T00:30:00+00:00"))
    assert m.is_daily_loss_tripped(conn, "2026-09-19") is True


def test_the_risk_pnl_reader_counts_from_the_release(conn):
    close(conn, f"{DAY}T09:00:00+00:00", -200.0)
    close(conn, f"{DAY}T11:00:00+00:00", +30.0)
    m.set_risk_reset(conn, DAY, actor="test", reason="", now_iso=f"{DAY}T10:00:00+00:00")
    assert m.daily_risk_realized_pnl(conn, DAY) == pytest.approx(30.0)
    # accounting is untouched: the full day still sums to -170
    assert m.daily_real_realized_pnl(conn, DAY) == pytest.approx(-170.0)


def test_no_reset_row_means_no_release(conn):
    conn.execute("INSERT INTO memecoin_daily_loss_trips VALUES (?,?)", (DAY, f"{DAY}T09:00:00+00:00"))
    assert m.risk_reset_at(conn, DAY) == ""
    assert m.is_daily_loss_tripped(conn, DAY) is True


def test_an_unreadable_reset_table_never_invents_a_release():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_daily_loss_trips (day TEXT PRIMARY KEY, tripped_at TEXT)")
    c.execute("INSERT INTO memecoin_daily_loss_trips VALUES (?,?)", (DAY, f"{DAY}T09:00:00+00:00"))
    assert m.risk_reset_at(c, DAY) == ""          # table absent
    assert m.is_daily_loss_tripped(c, DAY) is True


def test_risk_day_since_without_a_connection_is_the_environment_alone():
    assert m.risk_day_since(DAY) == DAY


def test_the_later_of_the_two_bounds_wins(conn, monkeypatch):
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RISK_DAY_START_AT", f"{DAY}T08:00:00+00:00", raising=False)
    assert m.risk_day_since(DAY, conn) == f"{DAY}T08:00:00+00:00"
    m.set_risk_reset(conn, DAY, actor="test", reason="", now_iso=f"{DAY}T12:00:00+00:00")
    assert m.risk_day_since(DAY, conn) == f"{DAY}T12:00:00+00:00"


def test_the_button_and_its_handler_are_wired():
    """A handler with no button, or a button with no route, is a control that does nothing."""
    app = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "app.js")).read()
    assert 'data-action="rriskreset"' in app
    assert "a === 'rriskreset'" in app
    assert "_riskReset()" in app
    assert "/api/memecoin/real/risk-reset" in app


def test_the_release_endpoint_reads_the_overlay_not_this_process_settings(monkeypatch):
    """THE WEB PROCESS NEVER APPLIES THE OVERLAY (Beta, 2026-09-18).

    `apply_overrides` is called only by the funded runner's main cycle, so the web server's `cfg` is
    fixed at its start-up `.env` value for the life of that process. `235bc132` resolved that for
    /real/status and did not reach this endpoint.

    This drives the REAL route. The first version of this test grepped the function's source for a
    `cfg.` read, which is the same weak-guard class as the two B3 tests replaced in this very
    commit — Beta caught the double standard. A source match cannot fail when the resolution is
    wrong, only when the spelling changes.
    """
    import sqlite3
    from flask import Flask
    from sniper.persistence import db_memecoin as dbm
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    import sniper.settings.memecoin as cfg

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    dbm.ensure_memecoin_tables(conn)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn}))
    client = app.test_client()
    token = (client.get("/api/memecoin/entry-knobs").get_json() or {}).get("csrf_token")

    # This PROCESS is left believing the breaker is armed at $100. The ledger overrides below are
    # what the runner actually reads, and the final assertion turns this process's belief into the
    # WRONG answer, so a regression that reads `cfg` fails rather than coincidentally agreeing.
    monkeypatch.setattr(cfg, "MEMECOIN_DRAWDOWN_PAUSE_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DRAWDOWN_PAUSE_S", 2640.0)
    # A drop the armed breaker would pause on: +50 then -120 leaves the day $170 under its high.
    # The closes sit MINUTES ago, not at midnight: a 2,640-second pause started at 00:20 has
    # expired by the time this runs, which is how the first draft of this test fooled itself.
    from datetime import datetime, timedelta
    now = datetime.fromisoformat(dbm.real_utc_now_iso())
    day = now.date().isoformat()
    for pnl, ago_s in ((50.0, 600), (-120.0, 300)):
        pid = dbm.open_position(conn, source_user_id="u", source_handle="h", mint="M%s" % pnl,
                                ticker="T", entry_price=1.0, entry_notional=175.0,
                                entry_fill_source="chain", entry_lag_s=0.1, their_size_usd=0.0,
                                opened_at=day + "T00:00:00+00:00", execution_mode="real")
        dbm.close_position(conn, pid, (now - timedelta(seconds=ago_s)).isoformat(),
                           realized_pnl=pnl)
    if (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() < 900:
        pytest.skip("too close to UTC midnight for a same-day drawdown replay")

    # `effective()` caches the pristine .env defaults on first use, so a monkeypatch of cfg cannot
    # move them. Write the armed value as an OVERRIDE, which is what the runner would read anyway.
    dbm.set_entry_knob_override(conn, "MEMECOIN_DRAWDOWN_PAUSE_USD", 100, active=True, actor="test",
                                reason="audit armed", now_iso=dbm.real_utc_now_iso())
    dbm.set_entry_knob_override(conn, "MEMECOIN_DRAWDOWN_PAUSE_S", 2640, active=True, actor="test",
                                reason="audit armed", now_iso=dbm.real_utc_now_iso())

    # CONTROL: with the armed numbers this day really is paused. Read the helper directly, because
    # POSTing once to prove it would write a reset and hide the case the next assertion needs.
    assert dbm.drawdown_pause_until(conn, day, 100.0, 2640.0, dbm.real_utc_now_iso()) is not None, \
        "the fixture does not reproduce a live pause; the real assertion below would pass vacuously"

    # Now the LEDGER says the breaker is off while this process still says $100. A regression that
    # reads `cfg` sees the armed value and reports the pause; the overlay says there is none.
    dbm.set_entry_knob_override(conn, "MEMECOIN_DRAWDOWN_PAUSE_USD", 0, active=True, actor="test",
                                reason="audit off", now_iso=dbm.real_utc_now_iso())
    off = client.post(RESET, json={"reason": "off"}, headers={"X-CSRF-Token": token})
    assert off.status_code == 200, off.get_data(as_text=True)
    assert off.get_json()["before"]["paused_until"] is None, \
        "the endpoint reported a pause the runner does not enforce — it read its own frozen cfg"
