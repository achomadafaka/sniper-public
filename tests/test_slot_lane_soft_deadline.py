"""Soft deadline (operator, 2026-09-26): S/S+1 is a target, not a cutoff.

With the Lighthouse [S, S+1] guard gone, a lane buy that lands late is judged by the signed price
cap alone: a late landing that clears min_out is a fill, and a transaction whose outcome is still
unknown keeps its reservation (the fallback defers on it) instead of being released.
"""
import contextlib
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.execution import solana_real_seams as seams
from sniper.execution.solana_slot_executor import execute_prebuilt_entry
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_slot_lane as lane

S = 450_797_210
NOW = "2026-09-26T22:50:00+00:00"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _pending(conn):
    pid = m.reserve_pending(conn, mint="MintLate1", source_user_id="notdecu", strategy="alpha_b",
                            notional=175.0, reserved_at=NOW, network_id="solana",
                            origin_event_id="slotlane:SigLate", signal_source="slot_lane",
                            source_price=5.3e-6)
    return m.pending_row(conn, pid)


def test_a_landing_at_s_plus_3_that_clears_min_out_books_as_a_fill(conn):
    pending = _pending(conn)
    status = seams.ConfirmState("confirmed", landing_slot=S + 3)
    result = execute_prebuilt_entry(
        conn, pending=pending, signed_b64="AA==", sig="SigLate", validity=None,
        send_fn=lambda _b64: True, confirm_fn=lambda _sig: status,
        receipt_fill_fn=lambda _sig, _mint, _max: (174.2, 27_000_000.0, 6),
        now_iso=NOW, sleep_fn=lambda _s: None)
    assert result["status"] == "filled" and result["landing_slot"] == S + 3
    assert conn.execute("SELECT status FROM memecoin_pending_entries WHERE id=?",
                        (pending["id"],)).fetchone()[0] == "confirmed"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1").fetchone()[0] == 1
    assert lane.slot_offsets(SimpleNamespace(slot=S), result)["landing_minus_s"] == 3


@pytest.mark.parametrize("verdict", ["timeout", "unknown", "pending"])
def test_an_unconfirmed_send_keeps_its_reservation(conn, verdict):
    pending = _pending(conn)
    result = execute_prebuilt_entry(
        conn, pending=pending, signed_b64="AA==", sig="SigLate", validity=None,
        send_fn=lambda _b64: True, confirm_fn=lambda _sig: seams.ConfirmState(verdict),
        receipt_fill_fn=lambda *a: pytest.fail("no fill read before confirmation"),
        now_iso=NOW, sleep_fn=lambda _s: None)
    assert result["status"] == "unknown"
    assert conn.execute("SELECT status FROM memecoin_pending_entries WHERE id=?",
                        (pending["id"],)).fetchone()[0] == "reserved"
