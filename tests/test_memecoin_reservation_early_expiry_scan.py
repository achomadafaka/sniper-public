"""A provably-dead reservation must not wait out the staleness clock before it can be released.

2026-08-25, reservation 855 (happyseb): broadcast at 17:00:39 with `submission_last_valid_height`
418723842 recorded by the write-ahead. The signature never reached the chain and the wallet held
none of the mint. By 17:06 the chain was 1302 blocks past that binding, so the transaction was
already provably dead — but `MEMECOIN_RESERVATION_STALE_S` is 600s, so recovery had not even LOOKED
at the row yet. It held its mint and an exposure slot for the full ten minutes.

`expiry_proof_s` admits such a row to the scan early. The RELEASE conditions are unchanged (chain
height past the binding, two spaced not_found reads, 60s span); only the clock that admits the row
moved. A row admitted early may take that release and nothing else, and it must carry both a tx_sig
and a height binding to be admitted at all — otherwise the scan could race an executor that has not
broadcast yet.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

LVH = 418723842
RESERVED = "2026-08-25T17:00:38+00:00"
STALE_S = 600.0


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "early.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    return c


def _reserve(conn, *, sig="SigEarly1", lvh=LVH, broadcast=True):
    pid = m.reserve_pending(conn, mint="MintEarly", source_user_id="u1", strategy="alpha_b",
                            notional=23.0, reserved_at=RESERVED, network_id="solana")
    conn.execute(
        "UPDATE memecoin_pending_entries SET tx_sig=?, broadcast_at=?, "
        "submission_last_valid_height=? WHERE id=?",
        (sig, (RESERVED if broadcast else None), lvh, pid))
    conn.commit()
    return pid


def _status(conn, pid):
    return conn.execute(
        "SELECT status FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()[0]


def _run(conn, now_iso, *, height=LVH + 1302, confirm="not_found", proof_s=90.0):
    return R.recover_stale_reservations(
        conn, confirm_for=lambda _n, _s: confirm, now_iso=now_iso,
        max_age_s=STALE_S, height_for=lambda _n: height, expiry_proof_s=proof_s)


def test_dead_reservation_releases_minutes_before_the_staleness_clock():
    """The happyseb case end to end: two spaced reads inside the 600s window free the slot."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = _reserve(c)
    _run(c, "2026-08-25T17:02:30+00:00")                      # t+112s: first not_found read
    assert _status(c, pid) == "reserved", "one read must not be enough"
    out = _run(c, "2026-08-25T17:04:30+00:00")                # t+232s: second, 120s later
    assert _status(c, pid) == "failed"
    assert (pid, "released_dropped") in out
    # and that is well inside the window the old behaviour would still have been waiting out
    assert 232 < STALE_S


def test_early_scan_off_reproduces_the_old_behaviour(conn):
    """proof_s=0 must leave a young row completely untouched."""
    pid = _reserve(conn)
    assert _run(conn, "2026-08-25T17:04:30+00:00", proof_s=0.0) == []
    assert _status(conn, pid) == "reserved"
    r = dict(conn.execute("SELECT * FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone())
    assert not r["not_found_checks"], "an off scan must not even record a probe"


def test_early_row_without_a_height_binding_is_left_alone(conn):
    """No binding means the exact proof can never fire, so the row waits for the proxy."""
    pid = _reserve(conn, lvh=None)
    assert _run(conn, "2026-08-25T17:04:30+00:00") == []
    assert _status(conn, pid) == "reserved"


def test_early_row_with_no_signature_is_never_released_as_never_broadcast(conn):
    """The never-broadcast release can race a live executor, so it must stay on the old clock."""
    pid = m.reserve_pending(conn, mint="MintNoSig", source_user_id="u1", strategy="alpha_b",
                            notional=23.0, reserved_at=RESERVED, network_id="solana")
    conn.commit()
    assert _run(conn, "2026-08-25T17:04:30+00:00") == []
    assert _status(conn, pid) == "reserved"


def test_early_row_whose_transaction_landed_is_not_touched_early(conn):
    """A confirmed tx means funds are at risk; that alert and tag keep the original clock."""
    pid = _reserve(conn)
    assert _run(conn, "2026-08-25T17:04:30+00:00", confirm="confirmed") == []
    r = dict(conn.execute("SELECT * FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone())
    assert r["status"] == "reserved" and r["recovery_status"] != "orphaned"


def test_early_release_still_requires_the_chain_to_be_past_the_binding(conn):
    """Height not past last_valid_height: the bytes can still land, so the slot stays held."""
    pid = _reserve(conn)
    _run(conn, "2026-08-25T17:02:30+00:00", height=LVH - 1)
    _run(conn, "2026-08-25T17:04:30+00:00", height=LVH - 1)
    assert _status(conn, pid) == "reserved"


def test_early_scan_never_reaches_the_twenty_minute_proxy(conn):
    """The proxy is time-based, not evidence-based. Without a height probe nothing releases early."""
    pid = _reserve(conn)
    for now in ("2026-08-25T17:02:30+00:00", "2026-08-25T17:04:30+00:00",
                "2026-08-25T17:06:30+00:00", "2026-08-25T17:08:30+00:00"):
        R.recover_stale_reservations(
            conn, confirm_for=lambda _n, _s: "not_found", now_iso=now,
            max_age_s=STALE_S, height_for=None, expiry_proof_s=90.0)
    assert _status(conn, pid) == "reserved"


def test_a_row_past_the_staleness_clock_keeps_every_old_branch(conn):
    """Beyond max_age_s the row is not 'early' and the never-broadcast release works as before."""
    pid = m.reserve_pending(conn, mint="MintOld", source_user_id="u1", strategy="alpha_b",
                            notional=23.0, reserved_at=RESERVED, network_id="solana")
    conn.commit()
    out = _run(conn, "2026-08-25T17:20:00+00:00")             # t+1162s, past 600s
    assert (pid, "released_never_broadcast") in out
    assert _status(conn, pid) == "failed"


@pytest.mark.parametrize("confirm", ["confirmed", "pending", "unavailable"])
def test_early_release_needs_an_explicit_not_found_answer(conn, confirm):
    """The safety argument does NOT rest on the executor being finished.

    `bind_entry_submission` persists the sig and the height BEFORE network I/O, so a row carrying
    both may still have a live executor between bind and send. What makes the early release safe is
    the release condition itself: the chain is already past the binding, so the blockhash has expired
    and a late send can never land. A transaction that DID land answers confirmed or pending, never
    not_found, and an unreadable provider answers neither. None of them may release early.
    """
    pid = _reserve(conn)
    for now in ("2026-08-25T17:02:30+00:00", "2026-08-25T17:04:30+00:00"):
        assert _run(conn, now, confirm=confirm) == []
    assert _status(conn, pid) == "reserved"


def test_the_true_release_reason_survives_a_late_executor(conn):
    """Peer review 2026-08-25 (F5): release_pending was an unconditional UPDATE.

    Recovery can release a row while its executor is still in flight. That executor then reaches its
    own release with a reason describing what IT saw, and the audit column silently became the later,
    less true one. First writer wins: the caller that actually freed the slot owns the reason.
    """
    pid = _reserve(conn)
    _run(conn, "2026-08-25T17:02:30+00:00")
    _run(conn, "2026-08-25T17:04:30+00:00")
    assert _status(conn, pid) == "failed"
    m.release_pending(conn, pid, "broadcast_rejected")      # the late executor, still honest
    reason = conn.execute(
        "SELECT resolution_reason FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()[0]
    assert reason == "dropped_blockhash_expired_proven", reason
