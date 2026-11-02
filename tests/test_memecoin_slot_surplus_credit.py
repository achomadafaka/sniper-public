"""The slot lane's SOL surplus credit (operator order 2026-09-26): bounded, and unknown is not zero.

Beta's review: a misparse must not credit a large share of the spend, and a receipt that does not
carry the owner's balances must read as unknown, not as a true zero surplus.
"""
import contextlib
import sqlite3

import pytest

from sniper.execution.solana_slot_executor import (
    MAX_SURPLUS_CREDIT_FRACTION, execute_prebuilt_entry, receipt_owner_sol_surplus)
from sniper.persistence import db_memecoin as m


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


class _Status(str):
    pass


def _book(conn, surplus):
    pid = m.reserve_pending(conn, mint="MintSlot1", source_user_id="wallet", strategy="alpha_b",
                            notional=20.0, reserved_at="2026-09-26T20:00:00+00:00",
                            network_id="solana", origin_event_id="slot:sig:1",
                            signal_source="slot_lane", source_price=0.0001)
    return execute_prebuilt_entry(
        conn, pending=m.pending_row(conn, pid), signed_b64="AA==", sig="S1", validity=None,
        send_fn=lambda _b: True, confirm_fn=lambda _s: _Status("confirmed"),
        receipt_fill_fn=lambda *_a: (20.0, 150000.0, 6), sol_surplus_fn=lambda _s: surplus,
        sol_usd=150.0, now_iso="2026-09-26T20:00:01+00:00", sleep_fn=lambda _s: None)


def test_a_credit_over_the_cap_books_the_plain_usdc_delta(conn):
    # 20.0 * 5% = $1.00 cap; 0.01 SOL at $150 = $1.50 is over it, so no credit.
    assert MAX_SURPLUS_CREDIT_FRACTION == 0.05
    result = _book(conn, surplus=10_000_000)
    assert result["sol_surplus_credit_usd"] == 0.0
    assert conn.execute("SELECT entry_notional FROM memecoin_positions").fetchone()[0] \
        == pytest.approx(20.0)


def test_a_credit_under_the_cap_is_applied(conn):
    result = _book(conn, surplus=4_000_000)     # $0.60 <= $1.00
    assert result["sol_surplus_credit_usd"] == pytest.approx(0.60)


def test_an_unknown_surplus_books_the_plain_usdc_delta(conn):
    assert _book(conn, surplus=None)["sol_surplus_credit_usd"] == 0.0


def test_a_receipt_without_the_owner_or_balances_is_unknown_not_zero():
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "Other"}]}},
          "meta": {"preBalances": [1], "postBalances": [2]}}
    assert receipt_owner_sol_surplus(tx, "Owner") is None
    tx["transaction"]["message"]["accountKeys"] = [{"pubkey": "Owner"}]
    del tx["meta"]["postBalances"]
    assert receipt_owner_sol_surplus(tx, "Owner") is None
