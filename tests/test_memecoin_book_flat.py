"""The deploy waiter fires only on a flat funded book: no open real position, no reserved entry,
and (with --dip-watches) no live dip-retry watch.

scripts/memecoin_wait_flat_then_deploy.sh and scripts/memecoin_deploy_pinned.sh gate on this exit
code. A false 0 restarts the runner mid-position or mid-reservation. Runs the script as the waiter
does, against the real memecoin schema in a tmp_path DB.
"""
import contextlib
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.persistence.db_memecoin import ensure_memecoin_tables

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_book_flat.py"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.db"
    with contextlib.closing(sqlite3.connect(path)) as conn:
        ensure_memecoin_tables(conn)
        conn.commit()
    return path


def _insert(path, sql, params):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute(sql, params)
        conn.commit()


def _run(path, *flags):
    return subprocess.run([sys.executable, str(SCRIPT), *flags, str(path)],
                          capture_output=True, text=True)


def _exit_code(path, *flags):
    return _run(path, *flags).returncode


def _position(path, mode, is_open):
    _insert(path, "INSERT INTO memecoin_positions (mint, execution_mode, is_open) VALUES (?,?,?)",
            ("MintA", mode, is_open))


def _pending(path, status):
    _insert(path, "INSERT INTO memecoin_pending_entries (mint, status) VALUES (?,?)",
            ("MintB", status))


def test_empty_book_is_flat_and_non_matching_rows_do_not_block(db):
    assert _exit_code(db) == 0
    _position(db, "paper", 1)       # paper opens do not hold funded money
    _position(db, "real", 0)        # a closed funded position
    _pending(db, "confirmed")       # a settled reservation
    assert _exit_code(db) == 0


def test_one_open_real_position_is_not_flat(db):
    _position(db, "real", 1)
    assert _exit_code(db) == 1


def test_one_reserved_pending_entry_is_not_flat(db):
    _pending(db, "reserved")
    assert _exit_code(db) == 1


def test_unreadable_ledger_is_not_flat(tmp_path):
    assert _exit_code(tmp_path / "missing.db") != 0


# --- live dip-retry watches (Alpha 2026-10-01: the 15:29Z deploy cut a live Degen watch) ---

def _dip_row(path, monkeypatch, *, signal_age_s, fid="e1"):
    """One dip row written by the PRODUCTION builder (memecoin_dip_retry.maybe_enqueue), the way the
    runner writes it after a scoped chase refusal, its signal `signal_age_s` before now."""
    from sniper.feeds.fomo_feed import CopyEvent
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_dip_retry as D
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_WALLETS", ("degencapitalllc",))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana",))
    created = datetime.fromtimestamp(time.time() - signal_age_s, timezone.utc).isoformat()
    ev = CopyEvent(source_user_id="fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb",
                   source_handle="DegenCapitalLLC", mint="MintDip", ticker="", side="buy",
                   their_price=1e-5, their_market_cap=50000.0, their_size_usd=500.0,
                   is_first_buy=True, fomo_event_id=fid, created_at=created, received_at=created,
                   lag_s=0.5, signal_source="follow_activity", network_id="solana")
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        pid = m.reserve_pending(conn, mint=ev.mint, source_user_id=ev.source_user_id,
                                strategy="alpha_b", notional=100.0, reserved_at=created,
                                attempt_id="A-" + fid, network_id="solana")
        conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, mint, network_id, pending_id, "
                     "decision_started_at) VALUES (?,?,?,?,?)",
                     ("A-" + fid, ev.mint, "solana", pid, created))
        m.release_pending(conn, pid, "chase_quote")
        res = {"status": "skipped_chase", "attempt_id": "A-" + fid,
               "reason": "exact quote is +40% above source price (cap 30%)",
               "quote_chase_pct": 40.0, "quote_price": 1.4e-5}
        assert D.maybe_enqueue(conn, ev, res, created, event_json=json.dumps(asdict(ev)))
        conn.commit()


def test_a_live_dip_watch_keeps_the_book_not_flat(db, monkeypatch):
    _dip_row(db, monkeypatch, signal_age_s=5)          # deadline 25 s ahead: it could still sign
    out = _run(db, "--dip-watches")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "dip watches 1" in out.stdout


def test_without_the_flag_a_dip_watch_does_not_block(db, monkeypatch):
    # The deploy's own --require-flat check runs AFTER its kill switch, when a dip hit is refused
    # anyway; it must keep today's meaning or a live row would fail a deploy mid-cutover.
    _dip_row(db, monkeypatch, signal_age_s=5)
    assert _exit_code(db) == 0


def test_a_dip_row_past_its_deadline_and_margin_reports_and_does_not_block(db, monkeypatch):
    # run_hits ends a row past its deadline with no decision, so it cannot sign; a row left behind
    # (runner down, lane stuck) must not hold every deploy, including the one that fixes it.
    _dip_row(db, monkeypatch, signal_age_s=30 + 31)
    out = _run(db, "--dip-watches")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "dip watches 0" in out.stdout and "stale dip row e1" in out.stdout


def test_a_dip_row_inside_the_margin_still_blocks(db, monkeypatch):
    # past the deadline but inside the margin: a decision begun at the deadline may still be running
    _dip_row(db, monkeypatch, signal_age_s=30 + 10)
    out = _run(db, "--dip-watches")
    assert out.returncode == 1 and "dip watches 1" in out.stdout, out.stdout + out.stderr


def test_a_blind_retry_row_is_not_a_dip_watch(db):
    _insert(db, "INSERT INTO memecoin_entry_retry_queue (fomo_event_id, mint, network_id, "
                "first_queued_at, next_attempt_at, attempts, event_json) VALUES (?,?,?,?,?,0,?)",
            ("b1", "MintC", "solana", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", "{}"))
    assert _exit_code(db, "--dip-watches") == 0


def test_an_unreadable_dip_deadline_is_not_flat(db):
    _insert(db, "INSERT INTO memecoin_entry_retry_queue (fomo_event_id, mint, network_id, "
                "first_queued_at, next_attempt_at, attempts, event_json, dip_json) "
                "VALUES (?,?,?,?,?,0,?,?)",
            ("d1", "MintD", "solana", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", "{}",
             '{"signal_at": 1}'))
    out = _run(db, "--dip-watches")
    assert out.returncode != 0 and "d1" in out.stderr, out.stdout + out.stderr


def test_the_waiter_and_the_pinned_deploy_ask_for_dip_watches_and_the_cutover_does_not():
    scripts = SCRIPT.parent
    for name in ("memecoin_wait_flat_then_deploy.sh", "memecoin_deploy_pinned.sh"):
        calls = [ln for ln in (scripts / name).read_text().splitlines()
                 if "memecoin_book_flat.py" in ln and not ln.lstrip().startswith("#")]
        assert calls and all("--dip-watches" in ln for ln in calls), name
    deploy = (scripts / "deploy_memecoin_runtime.sh").read_text()
    assert "memecoin_book_flat.py" in deploy and "--dip-watches" not in deploy


def test_a_future_dated_signal_is_stale_not_live(db, monkeypatch):
    # Gamma LOW on 67fab47ff: the deadline is the source/J7 created_at + window. A future-dated
    # stamp (or one in milliseconds, which reads as far future) would hold the waiter for 2 h.
    _dip_row(db, monkeypatch, signal_age_s=-3600)
    out = _run(db, "--dip-watches")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "dip watches 0" in out.stdout and "stale dip row e1" in out.stdout


def test_a_signal_a_few_seconds_ahead_of_the_host_clock_still_blocks(db, monkeypatch):
    # J7/source stamps can lead the host clock by seconds: inside the 30 s tolerance the row is live
    # (Gamma on 94cbd85f1: a mutant dropping the tolerance survived without this).
    _dip_row(db, monkeypatch, signal_age_s=-10)
    out = _run(db, "--dip-watches")
    assert out.returncode == 1 and "dip watches 1" in out.stdout, out.stdout + out.stderr
