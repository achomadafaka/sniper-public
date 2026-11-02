"""Crash points on the REAL Ink entry path, then a restarted process's recovery pass (plan Task 8).

The process dies (SystemExit, which no `except Exception` absorbs) at one point; a fresh executor
over the same chain, ledger and key then runs the runner's `recover_stale_reservations`. For every
point: at most one swap is ever signed, a swap that landed is booked exactly once, and a reservation
whose bytes could still land is never released.
"""
import pytest

from ink_constants import INK_TOKEN
from sniper.persistence import db_memecoin as db


def _enter(t):
    pid = t.reserve()
    try:
        t.ex["execute_fn"](t.conn, pid, INK_TOKEN, 10.0, "2026-09-29T09:00:01Z")
    except SystemExit:
        pass
    if t.conn.in_transaction:            # what a dead process leaves: nothing uncommitted survives
        t.conn.rollback()
    return pid


@pytest.mark.parametrize("crash, landed, reserved_after", [
    ("before_sign_swap", False, 0),      # nothing signed: provably never broadcast, released
    ("before_send_swap", False, 0),      # identity persisted, never admitted: released only by
                                         # the four-fact proof (see the next test), bytes destroyed
    ("after_send_swap", True, 0),        # landed: booked once from the canonical receipt
])
def test_a_crash_on_the_swap_then_restart(ink_real, crash, landed, reserved_after):
    t = ink_real()
    t.chain.crash = crash
    _enter(t)
    signed_before = t.acct.sign_calls
    restarted = t.build()
    t.recover(restarted)
    assert t.acct.sign_calls == signed_before, "recovery never signs"
    assert len(set(t.acct.swap_hashes)) == (0 if crash == "before_sign_swap" else 1)
    got = t.ledger()
    assert got["positions"] == got["entry_fills"] == (1 if landed else 0), got
    assert got["reserved"] == reserved_after, got


def test_an_unsent_signed_swap_is_held_until_the_never_admitted_proof(ink_real):
    """Died after the signed identity was persisted, before the send. Its bytes could still be
    broadcast, so recovery holds the reservation until evm_entry_never_admitted's four facts
    (nonce unspent, node holds nothing of ours, no coin, 300 s floor), then destroys the bytes
    with the release."""
    t = ink_real()
    t.chain.crash = "before_send_swap"
    pid = _enter(t)
    restarted = t.build()
    t.recover(restarted, now_iso="2026-09-29T09:02:00Z")          # 2 minutes: inside the floor
    assert t.ledger()["reserved"] == 1
    assert db.pending_row(t.conn, pid)["tx_sig"]
    t.recover(restarted, now_iso="2026-09-29T09:30:00Z")
    row = db.pending_row(t.conn, pid)
    assert t.ledger()["reserved"] == 0 and row["resolution_reason"].startswith("evm_entry_never_admitted")
    assert not row.get("signed_payload_b64"), "the rebroadcastable bytes die with the release"
    assert len(t.chain.sent) == 1, "only the approval ever reached the wire"


@pytest.mark.parametrize("dies_in", ["insert_fill", "confirm_pending"])
def test_a_crash_inside_the_fill_booking_books_once_after_restart(ink_real, monkeypatch, dies_in):
    """The swap confirmed; the process dies while writing the fill. Nothing half-written may
    survive, or recovery books the same swap a second time."""
    t = ink_real()
    real = getattr(db, dies_in)
    state = {"armed": True}

    def dying(*args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise SystemExit("process died while booking the fill")
        return real(*args, **kwargs)
    monkeypatch.setattr(db, dies_in, dying)
    _enter(t)
    t.recover(t.build())
    got = t.ledger()
    assert got["positions"] == 1 and got["entry_fills"] == 1, got
    assert got["reserved"] == 0, got
    assert len(set(t.acct.swap_hashes)) == 1


def test_a_crash_after_the_fill_commit_changes_nothing_on_restart(ink_real):
    t = ink_real()
    _enter(t)
    before = t.ledger()
    t.recover(t.build())
    assert t.ledger() == before == {"positions": 1, "reserved": 0, "entry_fills": 1}


def test_an_ambiguous_send_restart_signs_nothing_and_books_the_receipt_later(ink_real):
    """The plan's case: the swap send times out (it landed); its receipt appears only after the
    restart. One swap signature, one hash, one fill, no reservation left."""
    t = ink_real(send={"swap": "timeout"}, receipts={"swap": "none"})
    r = t.ex["execute_fn"](t.conn, t.reserve(), INK_TOKEN, 10.0, "2026-09-29T09:00:01Z")
    assert r["status"] == "unknown" and t.ledger()["reserved"] == 1
    t.chain.receipt_mode = "canonical"
    t.recover(t.build())
    assert len(t.acct.swap_hashes) == len(set(t.acct.swap_hashes)) == 1
    assert t.ledger() == {"positions": 1, "reserved": 0, "entry_fills": 1}
