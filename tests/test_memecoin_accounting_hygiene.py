"""Accounting hygiene after 73882 / 73884 (Alpha GO 2026-10-15).

(1) An exit attempt whose confirm read 'unknown' keeps that status: it is the executor's truth at the
    time. memecoin_exit_attempt_outcomes adds what reconciliation later proved: landed (a fill on the
    same position carries the attempt's signature), not_landed (that signature's exit intent ended
    failed or dropped) or unresolved. 9 of 12 'unknown' exit attempts in the 7 days to 10-15 had
    landed (73882's rows 3081 and 3083 among them).
(3) The confirmed-entry recovery opened 73884 with source_handle '(real)' although its attempt named
    probablyinsider; it now takes the attempt's handle and links the attempt to the position.
(4) The board read "Landed Unknown" for 73884: its attempt ended anomaly/receipt_unreadable, and the
    timing view gave a line only for filled or failed attempts. An attempt whose position holds the
    entry fill on the attempt's own signature now counts as filled there.
(5) The closed card names each sale's share of the position: "sold 75%" for the partial and "sold
    remaining 25%" for the close.
"""
import json
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.persistence import db_memecoin_notifications as notes
from sniper.services import memecoin_real_runner as R

NOW = "2026-07-09T01:00:00Z"
OLD = "2026-07-09T00:00:00Z"


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _position(c, *, mint="HhCj9Mag", attempt_id=None, entry_qty=3478462.672284):
    pid = m.open_position(c, source_user_id="u1", source_handle="probablyinsider", mint=mint, ticker="HH",
                          entry_price=2.28e-05, entry_notional=79.26, entry_fill_source="chain",
                          entry_lag_s=0, their_size_usd=0.0, opened_at="2026-10-15T13:15:44+00:00",
                          strategy="alpha_b", execution_mode="real", network_id="solana",
                          attempt_id=attempt_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=2.28e-05, qty=entry_qty, notional=79.26,
                  fill_source="chain", fraction=1.0, created_at="2026-10-15T13:15:44+00:00",
                  tx_sig="ENTRY_SIG", attempt_id=attempt_id)
    return pid


def _attempt(c, pid, status, sig, reason="real_tp", at="2026-10-15T13:20:30+00:00"):
    m.record_exit_execution(c, position_id=pid, network_id="solana", exit_reason=reason,
                                    attempted_at=at, status=status, sig=sig, total_ms=500.0)
    return c.execute("SELECT MAX(id) FROM memecoin_exit_execution_attempts").fetchone()[0]


def _outcomes(c):
    return {r["id"]: (r["status"], r["outcome"]) for r in c.execute(
        "SELECT id, status, outcome FROM memecoin_exit_attempt_outcomes")}


# --- (1) the outcomes view ---

def test_an_unknown_exit_whose_sale_was_booked_reads_landed_and_keeps_its_status():
    c = _c(); pid = _position(c)
    derisk = _attempt(c, pid, "unknown", "PARTIAL_SIG", reason="derisk")
    close = _attempt(c, pid, "unknown", "CLOSE_SIG")
    m.insert_fill(c, position_id=pid, kind="derisk", price=None, qty=None, notional=None,
                  fill_source="chain_confirmed_unpriced", fraction=0.75, created_at="t", tx_sig="PARTIAL_SIG")
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=2.56e-05, qty=869615.0, notional=22.3,
                  fill_source="chain", fraction=1.0, created_at="t", tx_sig="CLOSE_SIG")
    assert _outcomes(c) == {derisk: ("unknown", "landed"), close: ("unknown", "landed")}


def test_a_failed_or_dropped_intent_reads_not_landed_and_silence_reads_unresolved():
    c = _c(); pid = _position(c)
    failed = _attempt(c, pid, "unknown", "DEAD_SIG", reason="derisk")
    open_ = _attempt(c, pid, "unknown", "OPEN_SIG")
    unsigned = _attempt(c, pid, "stuck", None)
    c.execute("INSERT INTO memecoin_exit_intents(claim_id, position_id, action, intent_json, status) "
              "VALUES('C1', ?, 'derisk', ?, 'failed')", (pid, json.dumps({"tx_identity": "DEAD_SIG"})))
    c.commit()
    assert _outcomes(c) == {failed: ("unknown", "not_landed"), open_: ("unknown", "unresolved"),
                            unsigned: ("stuck", None)}


def test_a_fill_on_another_position_or_a_case_changed_solana_sig_is_not_a_landing():
    c = _c(); pid = _position(c); other = _position(c, mint="Other")
    mine = _attempt(c, pid, "unknown", "CaseSig")
    m.insert_fill(c, position_id=other, kind="bracket_exit", price=1.0, qty=1.0, notional=1.0,
                  fill_source="chain", fraction=1.0, created_at="t", tx_sig="CaseSig")
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=1.0, qty=1.0, notional=1.0,
                  fill_source="chain", fraction=1.0, created_at="t", tx_sig="casesig")
    assert _outcomes(c)[mine] == ("unknown", "unresolved")


def test_an_evm_hash_matches_whatever_its_case():
    c = _c(); pid = _position(c)
    a = _attempt(c, pid, "unknown", "0xABCDEF")
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=1.0, qty=1.0, notional=1.0,
                  fill_source="chain", fraction=1.0, created_at="t", tx_sig="0xabcdef")
    assert _outcomes(c)[a] == ("unknown", "landed")


# --- (3) + (4) the confirmed-entry recovery ---

def _recover(c, *, handle="probablyinsider", terminal=("anomaly", "receipt_unreadable")):
    from sniper.execution.solana_swap_executor import recover_confirmed_entry
    from sniper.execution.solana_wallet import USDC_MINT
    attempt_id = "attempt-73884"
    m.start_entry_attempt(c, attempt_id=attempt_id, decision_started_at=OLD, network_id="solana",
                          mint="M_RECOVERED", source_user_id="u", source_handle=handle,
                          delivery_lane="j7_verified_backup")
    pid = m.reserve_pending(c, mint="M_RECOVERED", source_user_id="u", strategy="alpha_b", notional=10.0,
                            reserved_at="2026-07-09T00:55:00Z", network_id="solana", attempt_id=attempt_id,
                            source_price=1.5)
    m.set_pending_broadcast(c, pid, "SIG_RECOVERED", None, OLD)
    m.finish_entry_attempt(c, attempt_id, terminal[0], terminal[1], OLD, "{}", tx_sig="SIG_RECOVERED")
    m.mark_pending_orphaned(c, pid)
    owner = "CustodyWallet"
    receipt = {"blockTime": 1788204959,
               "transaction": {"signatures": ["SIG_RECOVERED"],
                               "message": {"accountKeys": [{"pubkey": owner, "signer": True}]}},
               "meta": {"err": None,
                        "preTokenBalances": [{"accountIndex": 1, "mint": USDC_MINT, "owner": owner,
                                              "uiTokenAmount": {"amount": "20000000", "decimals": 6}}],
                        "postTokenBalances": [
                            {"accountIndex": 1, "mint": USDC_MINT, "owner": owner,
                             "uiTokenAmount": {"amount": "10000000", "decimals": 6}},
                            {"accountIndex": 2, "mint": "M_RECOVERED", "owner": owner,
                             "uiTokenAmount": {"amount": "5000000", "decimals": 6}}]}}
    out = R.recover_stale_reservations(
        c, confirm_for=lambda _n, _s: "confirmed", now_iso=NOW, expiry_proof_s=90,
        confirmed_entry_for=lambda conn, row, now: recover_confirmed_entry(conn, row, receipt, owner=owner,
                                                                         now_iso=now))
    assert out == [(pid, "recovered_confirmed")]
    position = c.execute("SELECT * FROM memecoin_positions WHERE attempt_id=?", (attempt_id,)).fetchone()
    attempt = c.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    return dict(position), dict(attempt)


def test_the_recovered_position_takes_the_attempts_handle_and_the_attempt_links_to_it():
    c = _c()
    position, attempt = _recover(c)
    assert position["source_handle"] == "probablyinsider"
    assert attempt["position_id"] == position["id"]
    assert attempt["terminal_status"] == "anomaly"                 # its history is never rewritten


def test_an_attempt_without_a_handle_still_reads_real():
    c = _c()
    position, _attempt_row = _recover(c, handle=None)
    assert position["source_handle"] == "(real)"


def test_the_timing_view_gives_a_recovered_attempt_its_landed_line(monkeypatch):
    from sniper.services import memecoin_fill_timing as T
    c = _c()
    _position_row, attempt = _recover(c)
    seen = []
    monkeypatch.setattr(T, "open_readonly", lambda: None)
    monkeypatch.setattr(T, "_details", lambda ledger, a, timing, lanes, include: seen.append(
        a["terminal_status"]) or {"line": "Landed S+1", "stages": {}})
    assert T.view_lines(c, [attempt]) == {attempt["attempt_id"]: "Landed S+1"}
    assert seen == ["filled"]


def test_an_anomaly_attempt_without_its_own_entry_fill_still_has_no_line(monkeypatch):
    from sniper.services import memecoin_fill_timing as T
    c = _c()
    m.start_entry_attempt(c, attempt_id="lonely", decision_started_at=OLD, network_id="solana", mint="M")
    m.finish_entry_attempt(c, "lonely", "anomaly", "receipt_unreadable", OLD, "{}", tx_sig="NOT_BOOKED")
    attempt = dict(c.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id='lonely'").fetchone())
    monkeypatch.setattr(T, "open_readonly", lambda: None)
    monkeypatch.setattr(T, "_details", lambda *a: {"line": "never", "stages": {}})
    assert T.view_lines(c, [attempt]) == {"lonely": ""}


# --- (5) the closed card names each sale's share ---

def test_the_partial_and_the_close_name_their_share_of_the_position():
    from sniper.services.memecoin_telegram import render_entry_notification as format_notification
    c = _c(); pid = _position(c)
    m.insert_fill(c, position_id=pid, kind="derisk", price=2.64e-05, qty=2608847.004213, notional=68.85,
                  fill_source="chain_confirmed_receipt_reconciled", fraction=0.75, created_at="t1",
                  tx_sig="PARTIAL_SIG")
    close = m.insert_fill(c, position_id=pid, kind="bracket_exit", price=2.56e-05, qty=869615.668071,
                          notional=22.30, fill_source="chain", fraction=1.0, created_at="t2", tx_sig="CLOSE_SIG")
    partial = close - 1
    assert notes.notification_exit_view(c, partial)["sold_share"] == {"share": 0.75, "remaining": False}
    assert notes.notification_exit_view(c, close)["sold_share"] == {"share": 0.25, "remaining": True}
    assert "sold 75%" in format_notification(notes.notification_exit_view(c, partial))
    assert "sold remaining 25%" in format_notification(notes.notification_exit_view(c, close))


def test_a_single_full_exit_says_no_share():
    from sniper.services.memecoin_telegram import render_entry_notification as format_notification
    c = _c(); pid = _position(c)
    fid = m.insert_fill(c, position_id=pid, kind="bracket_exit", price=2.56e-05, qty=3478462.672284,
                        notional=88.0, fill_source="chain", fraction=1.0, created_at="t", tx_sig="CLOSE_SIG")
    view = notes.notification_exit_view(c, fid)
    assert view["sold_share"] is None and "sold " not in format_notification(view).split("Our sale")[1].split("\n")[0][:20]


def test_an_unknown_quantity_says_no_share():
    c = _c(); pid = _position(c)
    fid = m.insert_fill(c, position_id=pid, kind="derisk", price=None, qty=None, notional=None,
                        fill_source="chain_confirmed_unpriced", fraction=0.75, created_at="t", tx_sig="P")
    assert notes.notification_exit_view(c, fid)["sold_share"] is None


def test_only_the_entry_fill_proves_an_entry_landed(monkeypatch):
    from sniper.services import memecoin_fill_timing as T
    c = _c()
    m.start_entry_attempt(c, attempt_id="odd", decision_started_at=OLD, network_id="solana", mint="M")
    m.finish_entry_attempt(c, "odd", "anomaly", "receipt_unreadable", OLD, "{}", tx_sig="SHARED_SIG")
    pid = _position(c, mint="M", attempt_id="odd")
    c.execute("UPDATE memecoin_fills SET tx_sig='OTHER' WHERE position_id=? AND kind='entry'", (pid,))
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=1.0, qty=1.0, notional=1.0,
                  fill_source="chain", fraction=1.0, created_at="t", tx_sig="SHARED_SIG")
    attempt = dict(c.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id='odd'").fetchone())
    monkeypatch.setattr(T, "open_readonly", lambda: None)
    monkeypatch.setattr(T, "_details", lambda *a: {"line": "never", "stages": {}})
    assert T.view_lines(c, [attempt]) == {"odd": ""}


def test_a_linked_attempt_is_never_re_linked():
    """Gamma LOW on 61d22e12a: an attempt that already names a position keeps it."""
    c = _c()
    m.start_entry_attempt(c, attempt_id="linked", decision_started_at=OLD, network_id="solana", mint="M")
    assert m.link_entry_attempt_position(c, "linked", 101) is True
    assert m.link_entry_attempt_position(c, "linked", 202) is False
    assert m.link_entry_attempt_position(c, "missing", 303) is False
    row = c.execute("SELECT position_id FROM memecoin_entry_attempts WHERE attempt_id='linked'").fetchone()
    assert row[0] == 101
