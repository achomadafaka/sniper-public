"""An expired signed slot window releases only the failed identity and keeps its receipt proof."""
import json
import time

import pytest

from sniper.execution import solana_real_seams as seams
from sniper.execution.solana_slot_executor import execute_prebuilt_entry
from sniper.execution.solana_slot_guard import LIGHTHOUSE_PROGRAM
from sniper.execution.solana_swap_executor import execute_entry
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_entry_trace import EntryAttemptTrace
from test_memecoin_entry_executor import _Deps
from test_memecoin_solana_slot_executor import conn  # noqa: F401


NOW = "2026-10-19T10:21:36+00:00"
GUARD = {"reason": "slot_window_expired", "source_slot": 461_033_147,
         "max_slot": 461_033_148, "landing_slot": 461_033_150, "instruction_index": 4,
         "failed_program": LIGHTHOUSE_PROGRAM, "program_error": "AssertionFailed",
         "fee_lamports": 5000, "no_tokens_bought": True}


class _Status(str):
    """The injected confirmation contract; receipt parsing is tested at its own boundary."""


@pytest.mark.parametrize("path", ["ordinary", "prebuilt"])
@pytest.mark.parametrize("verdict,guard", [("failed", GUARD), ("failed", None), ("unknown", None)])
def test_slot_guard_settlement_preserves_receipt_evidence_and_unknown(conn, monkeypatch, path,
                                                                    verdict, guard):
    def forbidden(*a, **kw):
        pytest.fail("settlement must not fetch a receipt, balance or RPC after failure")

    monkeypatch.setattr(seams, "_rpc_call", forbidden)
    trace = EntryAttemptTrace.start(conn, {"network_id": "solana", "mint": "MINT"}, NOW, time)
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10., reserved_at=NOW, network_id="solana",
                            attempt_id=trace.attempt_id)
    state = _Status(verdict)
    state.slot_guard = guard
    state.reason_code = "slot_window_expired" if guard else "chain_error:custom program error: 0x1771"
    state.landing_slot = GUARD["landing_slot"] if verdict == "failed" else None
    d = _Deps(confirm=state)
    common = dict(send_fn=d.send_fn, confirm_fn=d.confirm_fn, receipt_fill_fn=forbidden,
                  now_iso=NOW, trace=trace, sleep_fn=forbidden)
    if path == "prebuilt":
        result = execute_prebuilt_entry(conn, pending=m.pending_row(conn, pid), signed_b64="AA==",
                                        sig="SIG123", validity=None, **common)
    else:
        result = execute_entry(conn, pending_id=pid, mint="MINT", usd_amount=10., strategy="alpha_b",
            source_user_id="u1", quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
            sign_fn=d.sign_fn, usdc_fn=forbidden, token_fn=forbidden, receipt_first=True,
            mint_read_evidence={"network_id": "solana", "mint": "MINT", "decimals": 6,
                                "attempt_id": trace.attempt_id}, **common)

    reason = state.reason_code if verdict == "failed" else "unknown"
    assert result["status"] == verdict
    if verdict == "failed":
        assert result["reason"] == reason
    assert result.get("slot_guard") == guard
    assert d.calls.count("send") == d.calls.count("confirm") == 1
    pending = m.pending_row(conn, pid)
    assert pending["status"] == ("failed" if verdict == "failed" else "reserved")
    assert pending["resolution_reason"] == (reason if verdict == "failed" else None)
    assert pending["tx_sig"] == "SIG123"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_fills").fetchone()[0] == 0

    trace.finish(result["status"], reason)
    row = conn.execute("SELECT terminal_reason, trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                       (trace.attempt_id,)).fetchone()
    assert row["terminal_reason"] == reason
    events = [e for e in json.loads(row["trace_json"])["substage_events"]
              if e["name"] == "slot_guard_result"]
    assert len(events) == (1 if guard else 0)
    if guard:
        assert {key: events[0][key] for key in guard} == guard
        from sniper.services.memecoin_entry_view import entry_view
        from sniper.services.memecoin_telegram import render_entry_notification
        view = entry_view(conn, attempt_id=trace.attempt_id)
        assert view['status'] == 'transaction_failed' and view['slot_guard']
        message = render_entry_notification(view)
        for text in ('Slot window expired', str(guard['source_slot']), str(guard['max_slot']),
                     str(guard['landing_slot']), '0.000005000 SOL', 'no tokens bought'):
            assert text in message
