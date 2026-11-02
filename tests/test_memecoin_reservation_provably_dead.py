"""A provably-dead reservation releases on the blockhash proof, not a 20-minute proxy.

2026-08-14, reservation 434: a buy broadcast at 17:57:17 whose blockhash expired 423 blocks before
the check, signature absent from full history, zero tokens delivered. It blocked its mint, an
exposure slot, and two funded deploys — with `not_found_checks=0`, because the Solana release rule
is 20 minutes + three spaced reads. The de-risk and full-close paths already replaced that proxy
with the exact fact (`sig_provably_dead`): past `submission_last_valid_height` the transaction can
never land. The entry side records the same binding and never read it. It took an operator UPDATE.

The proof here is (1) chain height past the reservation's own last_valid_height AND (2) a SECOND
spaced not_found read — the spacing closes the lagging-node hole the same way the de-risk path's
balance check does. Either fact alone keeps the row reserved, and rows without a height binding or
a height probe keep the old proxy untouched.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

LVH = 417324765
NOW = "2026-08-14T18:05:00+00:00"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "resv.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    return c


def _reserve(conn, *, reserved_at="2026-08-14T17:57:16+00:00", lvh=LVH, sig="SigResv111"):
    pid = m.reserve_pending(conn, mint="MintResv", source_user_id="u1", strategy="alpha_b",
                            notional=45.0, reserved_at=reserved_at, network_id="solana")
    conn.execute(
        "UPDATE memecoin_pending_entries SET tx_sig=?, broadcast_at=?, "
        "submission_last_valid_height=? WHERE id=?",
        (sig, reserved_at, lvh, pid))
    conn.commit()
    return pid


def _row(conn, pid):
    return dict(conn.execute("SELECT * FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone())


def _recover(conn, *, height, spacing_ok=True):
    first = "2026-08-14T18:00:00+00:00" if spacing_ok else NOW
    def run(now_iso):
        return R.recover_stale_reservations(
            conn, confirm_for=lambda _n, _s: "not_found", now_iso=now_iso,
            max_age_s=60.0, height_for=lambda _n: height)
    out = run(first)
    out += run(NOW)
    return out


def test_expired_blockhash_plus_second_read_releases(conn):
    pid = _reserve(conn)
    out = _recover(conn, height=LVH + 423)
    assert ("released_dropped" in dict(out).get(pid, "")) or (pid, "released_dropped") in out, out
    row = _row(conn, pid)
    assert row["status"] == "failed"
    assert "blockhash_expired" in row["resolution_reason"]


def test_one_read_is_never_enough(conn):
    """The spaced second read closes the lagging-node hole; a single not_found must hold."""
    pid = _reserve(conn)
    out = R.recover_stale_reservations(
        conn, confirm_for=lambda _n, _s: "not_found", now_iso=NOW,
        max_age_s=60.0, height_for=lambda _n: LVH + 423)
    assert _row(conn, pid)["status"] == "reserved", out


def test_inside_the_blockhash_window_holds(conn):
    pid = _reserve(conn)
    _recover(conn, height=LVH - 1)
    assert _row(conn, pid)["status"] == "reserved"


def test_no_height_binding_keeps_the_old_proxy(conn):
    pid = _reserve(conn, lvh=None)
    _recover(conn, height=LVH + 423)
    assert _row(conn, pid)["status"] == "reserved", "no binding -> the 20-minute proxy still owns it"


def test_no_height_probe_keeps_the_old_proxy(conn):
    pid = _reserve(conn)
    for now in ("2026-08-14T18:00:00+00:00", NOW):
        R.recover_stale_reservations(
            conn, confirm_for=lambda _n, _s: "not_found", now_iso=now, max_age_s=60.0)
    assert _row(conn, pid)["status"] == "reserved"


def test_height_probe_failure_holds(conn):
    def boom(_n):
        raise RuntimeError("rpc down")
    pid = _reserve(conn)
    for now in ("2026-08-14T18:00:00+00:00", NOW):
        R.recover_stale_reservations(
            conn, confirm_for=lambda _n, _s: "not_found", now_iso=now,
            max_age_s=60.0, height_for=boom)
    assert _row(conn, pid)["status"] == "reserved"


def test_a_confirmed_tx_still_orphans_never_releases(conn):
    pid = _reserve(conn)
    R.recover_stale_reservations(
        conn, confirm_for=lambda _n, _s: "confirmed", now_iso=NOW,
        max_age_s=60.0, height_for=lambda _n: LVH + 423)
    row = _row(conn, pid)
    assert row["status"] == "reserved" and row["recovery_status"] == "orphaned"
