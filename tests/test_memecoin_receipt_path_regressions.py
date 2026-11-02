"""A confirmed Solana purchase becomes one tracked position (2026-09-27, operator order).

"A confirmed purchase must become a tracked position with working exits." The slot lane books a
fill from the transaction's own receipt, native-SOL funded or USDC funded; a confirmed buy whose
receipt cannot be read stays reserved and the stale-reservation recovery books it from the receipt
later. Pinned here, per path: an empty first read, a first read that raises, a receipt that never
arrives, and a crash between send and receipt followed by recovery that runs twice.
"""
import contextlib
import sqlite3

import pytest

from sniper.execution import solana_slot_executor as slot
from sniper.execution.solana_swap_executor import recover_confirmed_entry
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

NATIVE_FILL = {"sol_spent_lamports": 499_000_000, "got": 10.0, "decimals": 6,
               "fee_lamports": 100_000, "rent_lamports": 2_039_280}
USDC_FILL = (19.9, 150000.0, 6)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _reserve(conn, *, native, attempt_id=None, reserved_at="2026-09-27T15:00:00Z"):
    funding = ({"funding_asset": "sol", "funding_amount_atomic": 500_000_000,
                "funding_sol_usd": 100.0} if native else {})
    pid = m.reserve_pending(conn, mint="coin", source_user_id="wallet", strategy="alpha_b",
                            notional=50.0 if native else 20.0, reserved_at=reserved_at,
                            network_id="solana", signal_source="slot_lane",
                            attempt_id=attempt_id, **funding)
    return m.pending_row(conn, pid)


class _Status(str):
    pass


def _book(conn, pending, reads):
    """Run the slot executor on a confirmed send; `reads` answers each receipt read in turn."""
    seen = []

    def receipt_fill_fn(_sig, _mint, _max):
        answer = reads[min(len(seen), len(reads) - 1)]
        seen.append(answer)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    native = pending.get("funding_asset") == "sol"
    result = slot.execute_prebuilt_entry(
        conn, pending=pending, signed_b64="AA==", sig="sig",
        validity=({"funding_asset": "sol", "funding_amount_atomic": 500_000_000}
                  if native else None),
        send_fn=lambda _b64: True, confirm_fn=lambda _sig: _Status("confirmed"),
        receipt_fill_fn=receipt_fill_fn, now_iso="2026-09-27T15:00:01Z",
        sleep_fn=lambda _s: None)
    return result, seen


def _counts(conn):
    return (conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM memecoin_fills WHERE kind='entry'").fetchone()[0])


@pytest.mark.parametrize("native,fill", [(True, NATIVE_FILL), (False, USDC_FILL)])
def test_an_empty_first_receipt_read_books_once_on_the_second(conn, native, fill):
    pending = _reserve(conn, native=native)
    result, seen = _book(conn, pending, [None, fill])
    assert result["status"] == "filled", result
    assert len(seen) == 2
    assert _counts(conn) == (1, 1)
    assert m.pending_row(conn, pending["id"])["status"] == "confirmed"


@pytest.mark.parametrize("native,fill", [(True, NATIVE_FILL), (False, USDC_FILL)])
def test_a_first_receipt_read_that_times_out_books_on_the_next(conn, native, fill):
    """A read that raises is a read that saw nothing. It must be retried like an empty one, not
    thrown out of the executor, which left the confirmed buy with no position until recovery."""
    pending = _reserve(conn, native=native)
    result, seen = _book(conn, pending, [TimeoutError("getTransaction timed out"), fill])
    assert result["status"] == "filled", result
    assert len(seen) == 2
    assert _counts(conn) == (1, 1)


@pytest.mark.parametrize("native", [True, False])
def test_a_receipt_that_never_arrives_stays_reserved_for_recovery(conn, native):
    pending = _reserve(conn, native=native)
    result, seen = _book(conn, pending, [None])
    assert result["status"] == "anomaly" and result["reason"] == "receipt_unreadable"
    assert len(seen) == slot.RECEIPT_READS
    assert _counts(conn) == (0, 0)
    assert m.pending_row(conn, pending["id"])["status"] == "reserved"


def _native_receipt(sig):
    return {
        "blockTime": 1790521200,
        "transaction": {"signatures": [sig], "message": {"accountKeys": [
            {"pubkey": "owner", "signer": True}, {"pubkey": "coin-ata", "signer": False}]}},
        "meta": {"err": None, "fee": 100_000,
                 "preBalances": [2_000_000_000, 0],
                 "postBalances": [1_600_000_000, 2_039_280],
                 "preTokenBalances": [],
                 "postTokenBalances": [{"accountIndex": 1, "mint": "coin", "owner": "owner",
                                        "uiTokenAmount": {"amount": "1000000", "decimals": 6}}]},
    }


def test_a_crash_between_send_and_receipt_is_booked_once_by_recovery_and_is_exit_managed(conn):
    """Native-SOL slot buy: the signature is written, the runner dies before the receipt. Recovery
    runs on two consecutive reconcile passes; it books one position and one fill from the receipt,
    confirms the reservation, and the position is in the open funded book the exit sweep reads."""
    m.start_entry_attempt(conn, attempt_id="a1", decision_started_at="2026-09-27T15:00:00Z",
                          network_id="solana", mint="coin")
    pending = _reserve(conn, native=True, attempt_id="a1")
    m.set_pending_broadcast(conn, pending["id"], "sig", None, "2026-09-27T15:00:01Z")
    passes = []
    for now in ("2026-09-27T15:02:00Z", "2026-09-27T15:04:00Z"):
        passes.append(R.recover_stale_reservations(
            conn, confirm_for=lambda _network, _sig: "confirmed", now_iso=now, expiry_proof_s=90,
            confirmed_entry_for=lambda c, row, t: recover_confirmed_entry(
                c, row, _native_receipt(row["tx_sig"]), owner="owner", now_iso=t)))
    assert passes[0] == [(pending["id"], "recovered_confirmed")], passes
    assert passes[1] == [], passes
    assert _counts(conn) == (1, 1)
    assert m.pending_row(conn, pending["id"])["status"] == "confirmed"
    book = m.list_open_positions(conn, mode="real", include_paused=False)
    assert [(p["mint"], p["network_id"]) for p in book] == [("coin", "solana")]
    assert book[0]["entry_notional"] == pytest.approx(0.39786072 * 100.0)


def test_the_general_path_receipt_fallback_that_raises_leaves_the_buy_reserved():
    """execute_entry falls back to the receipt when the balance delta shows no spend. A receipt
    read that raises must read as unreadable (anomaly, still reserved for recovery), not escape
    the executor after a confirmed send."""
    from sniper.execution import solana_swap_executor as ex
    from test_memecoin_entry_roundtrip_cap import _Deps

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    d = _Deps(quote_impact=0.01, usd_back=9.9)

    def raising_receipt(_sig, _mint, _maximum):
        raise TimeoutError("getTransaction timed out")

    try:
        result = ex.execute_entry(
            c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
            source_user_id="u1", now_iso="2026-09-27T17:20:00Z",
            quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn,
            send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=lambda: 100.0,
            token_fn=lambda _mint: 0.0, impact_cap_pct=15.0, sleep_fn=lambda _s: None,
            receipt_fill_fn=raising_receipt)
        status = m.pending_row(c, pid)["status"]
    finally:
        c.close()
    assert result["status"] == "anomaly", result
    assert status == "reserved"


def _general_entry(usdc_reads, receipt=(9.9, 500.0, 6)):
    """A confirmed general-path entry. `usdc_reads` answers the USDC reads in order (the first is
    the pre-balance); an Exception entry raises. The token delta is a real 500."""
    from sniper.execution import solana_swap_executor as ex
    from test_memecoin_entry_roundtrip_cap import _Deps

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    d = _Deps(quote_impact=0.01, usd_back=9.9)
    reads = list(usdc_reads)

    def usdc_fn():
        answer = reads.pop(0) if len(reads) > 1 else reads[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    try:
        result = ex.execute_entry(
            c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
            source_user_id="u1", now_iso="2026-09-27T17:20:00Z",
            quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn,
            send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=usdc_fn,
            token_fn=d.token_fn, impact_cap_pct=15.0, sleep_fn=lambda _s: None,
            receipt_fill_fn=lambda _sig, _mint, _maximum: receipt)
        notional = c.execute("SELECT entry_notional FROM memecoin_positions").fetchone()
    finally:
        c.close()
    return result, (notional[0] if notional else None)


@pytest.mark.parametrize("post", [0.0, ConnectionError("rpc transport lost")])
def test_a_failed_post_send_usdc_read_books_the_receipt_not_the_wallet(post):
    """solana_wallet.usdc_balance fails CLOSED to 0.0, so a failed post-send read made the delta
    usdc_pre - 0 = the whole wallet ($100 here, for a $10 clip). A delta above the reservation is
    not our fill: re-read, then book the receipt, which is bounded by the reservation."""
    result, notional = _general_entry([100.0, post])
    assert result["status"] == "filled", result
    assert notional == pytest.approx(9.9)


def test_a_plausible_delta_still_books_the_delta():
    result, notional = _general_entry([100.0, 90.0], receipt=None)
    assert result["status"] == "filled" and notional == pytest.approx(10.0)


def test_an_implausible_delta_with_no_receipt_is_an_anomaly_not_a_fill():
    result, notional = _general_entry([100.0, 0.0], receipt=None)
    assert result["status"] == "anomaly" and notional is None


@pytest.mark.parametrize("answer,expect", [
    (ConnectionError("rpc transport lost"), "ConnectionError"),
    ((503, {}, None), "status=503"),
    ((200, {"error": {"code": -32602, "message": "commitment below confirmed"}}, None), "-32602"),
])
def test_an_unreadable_entry_receipt_says_why_in_the_log(caplog, answer, expect):
    """_entry_receipt answered None for a transport error, a non-200 and a JSON-RPC error alike, and
    logged nothing, so an RPC outage read as 'not served yet'. Each now logs one stamped line."""
    import logging
    from types import SimpleNamespace
    from sniper.execution import solana_real_seams as seams

    def http(_method, _url, body=None):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    executor = seams.build_real_solana_executor(
        keypair=SimpleNamespace(pubkey=lambda: "offline-owner"), rpc_url="offline://rpc", http=http)
    pending = {"network_id": "solana", "attempt_id": "a1", "tx_sig": "SIGX", "id": 1}
    with caplog.at_level(logging.WARNING, logger="sniper.execution.solana_real_seams"):
        assert executor["recover_confirmed_entry_fn"](None, pending, "2026-09-27T17:30:00Z") is None
    lines = [r.getMessage() for r in caplog.records if "SIGX" in r.getMessage()]
    assert len(lines) == 1 and expect in lines[0] and lines[0][:4] == "2026", lines
