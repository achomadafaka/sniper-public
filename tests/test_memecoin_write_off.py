"""Operator write-off of a stuck funded bag (2026-09-29, operator via Gamma/Alpha).

A write-off closes an open funded position at $0 proceeds through the ledger, not the chain: no
quote, no signature, the tokens stay in the wallet (Other Holdings lists them from then on). The
rules pinned here:
  * an exit in flight (a close or de-risk claim, a signature, a submitted or unknown intent)
    refuses, because a write-off racing an ambiguous sell would book the bag twice;
  * only an open execution_mode='real' position, and only with a reason;
  * realized = de-risk proceeds already booked - entry cost (the canonical close formula at 0);
  * the slot and exposure are released; a re-run changes nothing;
  * a later sell of the leftover tokens cannot reach the closed position.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m

ROOT = Path(__file__).resolve().parents[1]
MINT = "0x37551729e5dce871e07ac1be9e5d93c115817d9e"


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _open(conn, *, mode="real", entry=13.93, paused=None, network="4663"):
    cur = conn.execute(
        "INSERT INTO memecoin_positions (source_user_id, mint, ticker, entry_price, entry_notional, "
        "is_open, opened_at, execution_mode, network_id, strategy, tracking_paused_at, stuck_bag) "
        "VALUES ('w1', ?, 'AICAT', 0.0000057, ?, 1, '2026-09-29T12:09:41+00:00', ?, ?, 'alpha_b', ?, 1)",
        (MINT, entry, mode, network, paused))
    conn.commit()
    return cur.lastrowid


def _write_off(conn, pid, **kw):
    kw.setdefault("actor", "operator")
    kw.setdefault("reason", "sells revert on chain")
    kw.setdefault("balance_text", "unknown: no address given")
    kw.setdefault("at", "2026-09-29T21:00:00+00:00")
    return m.write_off_real_position(conn, pid, **kw)


def _row(conn, pid):
    return dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())


@pytest.mark.parametrize("column, value", [
    ("close_claim_id", "c1"), ("close_sig", "0xsig"), ("close_pending_at", "2026-09-29T20:59:00Z"),
    ("derisk_claim_id", "d1"), ("derisk_sig", "0xdsig"), ("derisk_pending_at", "2026-09-29T20:59:00Z"),
])
def test_an_exit_in_flight_refuses(column, value):
    conn = _ledger()
    pid = _open(conn)
    conn.execute(f"UPDATE memecoin_positions SET {column}=? WHERE id=?", (value, pid))
    conn.commit()
    r = _write_off(conn, pid)
    assert r["status"] == "refused" and "in flight" in r["reason"]
    assert _row(conn, pid)["is_open"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_position_write_offs").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["submitted", "submitted_unknown"])
def test_an_ambiguous_sell_intent_refuses(status):
    conn = _ledger()
    pid = _open(conn)
    conn.execute("INSERT INTO memecoin_exit_intents (claim_id, position_id, action, intent_json, status) "
                 "VALUES ('c9', ?, 'close', '{}', ?)", (pid, status))
    conn.commit()
    r = _write_off(conn, pid)
    assert r["status"] == "refused" and status in r["reason"]
    assert _row(conn, pid)["is_open"] == 1


def test_finished_intents_do_not_block():
    conn = _ledger()
    pid = _open(conn)
    for i, status in enumerate(("failed", "dropped", "confirmed", "quoted")):
        conn.execute("INSERT INTO memecoin_exit_intents (claim_id, position_id, action, intent_json, "
                     "status) VALUES (?, ?, 'close', '{}', ?)", ("c%d" % i, pid, status))
    conn.commit()
    assert _write_off(conn, pid)["status"] == "written_off"


def test_the_canonical_close_fields():
    conn = _ledger()
    pid = _open(conn, paused="2026-09-29T12:22:10+00:00")          # a Long Hold bag
    conn.execute("INSERT INTO memecoin_fills (position_id, kind, price, qty, notional, created_at) "
                 "VALUES (?, 'derisk', 1.0, 1.0, 5.0, '2026-09-29T12:15:00Z')", (pid,))
    conn.commit()
    r = _write_off(conn, pid, balance_text="raw=2419446409488970000000000 decimals=18",
                   balance_source="https://robinhood-rpc.publicnode.com")
    assert r["status"] == "written_off"
    row = _row(conn, pid)
    assert row["is_open"] == 0 and row["closed_at"] == "2026-09-29T21:00:00+00:00"
    assert row["exit_reason"] == "written_off" and row["stuck_bag"] == 0
    assert row["realized_pnl"] == pytest.approx(5.0 - 13.93)
    assert row["close_sig"] is None and row["close_claim_id"] is None
    audit = dict(conn.execute("SELECT * FROM memecoin_position_write_offs").fetchone())
    assert (audit["position_id"], audit["actor"], audit["reason"]) == (pid, "operator", "sells revert on chain")
    assert audit["balance_at_write_off"].startswith("raw=2419446")
    assert audit["realized_pnl"] == pytest.approx(-8.93) and audit["derisk_proceeds"] == 5.0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind!='derisk'",
                        (pid,)).fetchone()[0] == 0, "no sale happened, so no exit fill is invented"


def test_the_slot_and_exposure_are_released():
    conn = _ledger()
    pid = _open(conn)
    assert m.real_open_snapshot(conn, network="4663")["count"] == 1
    _write_off(conn, pid)
    snap = m.real_open_snapshot(conn, network="4663")
    assert snap["count"] == 0


def test_a_rerun_changes_nothing():
    conn = _ledger()
    pid = _open(conn)
    first = _write_off(conn, pid)
    before = _row(conn, pid)
    again = _write_off(conn, pid, reason="second try", at="2026-09-29T22:00:00+00:00")
    assert first["status"] == "written_off" and again["status"] == "already_written_off"
    assert _row(conn, pid) == before
    assert conn.execute("SELECT COUNT(*) FROM memecoin_position_write_offs").fetchone()[0] == 1


def test_a_later_sell_cannot_reach_the_written_off_position():
    """The funded exit path claims before it signs; the claim needs an open row, so it is refused
    and a confirmed-fill booking against the closed row is a no-op, never an exception."""
    conn = _ledger()
    pid = _open(conn)
    _write_off(conn, pid)
    assert m.claim_position_close(conn, pid, "2026-09-29T22:00:00Z", "manual") is None
    assert m.book_full_exit_and_finalize(
        conn, position_id=pid, claim_id="c1", sig="0xsig", closed_at="2026-09-29T22:00:00Z",
        realized_pnl=1.0, exit_reason="manual", fill_kind="manual_close", price=1.0, qty=1.0,
        notional=1.0) is False
    assert _row(conn, pid)["realized_pnl"] == pytest.approx(-13.93)


@pytest.mark.parametrize("setup, reason", [
    (lambda c: _open(c, mode="paper"), "execution_mode"),
    (lambda c: 999, "no such position"),
    (lambda c: _open(c, entry=None), "entry cost"),
])
def test_what_cannot_be_written_off(setup, reason):
    conn = _ledger()
    pid = setup(conn)
    r = _write_off(conn, pid)
    assert r["status"] == "refused" and reason in r["reason"]


def test_a_closed_position_is_refused():
    conn = _ledger()
    pid = _open(conn)
    conn.execute("UPDATE memecoin_positions SET is_open=0, closed_at='2026-09-29T13:00:00Z', "
                 "exit_reason='bracket_exit', realized_pnl=-1.0 WHERE id=?", (pid,))
    conn.commit()
    r = _write_off(conn, pid)
    assert r["status"] == "refused" and "not open" in r["reason"]


def test_a_reason_is_required():
    conn = _ledger()
    pid = _open(conn)
    with pytest.raises(ValueError):
        _write_off(conn, pid, reason="  ")


# ---------------------------------------------------------------------------- the script

def _script():
    spec = importlib.util.spec_from_file_location("wo", ROOT / "scripts/memecoin_write_off_position.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_script_is_a_dry_run_unless_confirmed(tmp_path, capsys):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = _open(conn)
    conn.close()
    wo = _script()
    assert wo.main([str(pid), "--reason", "sells revert", "--db", str(path)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    check = sqlite3.connect(path)
    assert check.execute("SELECT is_open FROM memecoin_positions WHERE id=?", (pid,)).fetchone()[0] == 1
    assert wo.main([str(pid), "--reason", "sells revert", "--db", str(path), "--confirm"]) == 0
    assert check.execute("SELECT is_open, exit_reason FROM memecoin_positions WHERE id=?",
                         (pid,)).fetchone() == (0, "written_off")


def test_the_script_refusal_exits_nonzero(tmp_path):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = _open(conn)
    conn.execute("UPDATE memecoin_positions SET close_sig='0xs' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    assert _script().main([str(pid), "--reason", "x", "--db", str(path), "--confirm"]) == 2


def test_no_address_reads_as_unknown_never_zero():
    wo = _script()
    text, source = wo.balance_at_write_off({"network_id": "4663", "mint": MINT}, address=None)
    assert text.startswith("unknown") and source is None
    text, _ = wo.balance_at_write_off({"network_id": "solana", "mint": "So1"}, address="x")
    assert text.startswith("unknown")


def test_a_failed_balance_read_reads_as_unknown(monkeypatch):
    wo = _script()

    def boom(*_a, **_k):
        raise OSError("timeout")
    monkeypatch.setattr(wo, "_eth_call", boom)
    text, source = wo.balance_at_write_off({"network_id": "4663", "mint": MINT},
                                           address="0x" + "1" * 40)
    assert text.startswith("unknown") and "timeout" in text and source


@pytest.mark.parametrize("column, value", [("close_claim_id", "c1"), ("derisk_sig", "0xd")])
def test_the_update_itself_refuses_a_claim_that_raced_the_read(monkeypatch, column, value):
    """A runner claim can land between the refusal read and the write. The UPDATE repeats every
    refusal as its predicate; the read is bypassed here to prove the predicate alone holds."""
    conn = _ledger()
    pid = _open(conn)
    conn.execute(f"UPDATE memecoin_positions SET {column}=? WHERE id=?", (value, pid))
    conn.commit()
    pos = _row(conn, pid)
    monkeypatch.setattr(m, "write_off_refusal", lambda _c, _p: (pos, None))
    r = _write_off(conn, pid)
    assert r["status"] == "refused" and "changed" in r["reason"]
    assert _row(conn, pid)["is_open"] == 1


def test_the_update_itself_refuses_an_ambiguous_intent_that_raced_the_read(monkeypatch):
    conn = _ledger()
    pid = _open(conn)
    conn.execute("INSERT INTO memecoin_exit_intents (claim_id, position_id, action, intent_json, status) "
                 "VALUES ('c9', ?, 'close', '{}', 'submitted_unknown')", (pid,))
    conn.commit()
    pos = _row(conn, pid)
    monkeypatch.setattr(m, "write_off_refusal", lambda _c, _p: (pos, None))
    assert _write_off(conn, pid)["status"] == "refused"
    assert _row(conn, pid)["is_open"] == 1
