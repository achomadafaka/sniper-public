"""Entry alerts separate source age, local queue time, and measured dispatch."""
import json

import pytest

from sniper.services import memecoin_fill_timing as ft
from sniper.services.memecoin_telegram import render_entry_notification
from test_memecoin_fill_timing import _a, _slot_trace
from test_memecoin_notification_lifecycle import view as card_view


RECEIPT = "2026-10-20T06:48:17.200+00:00"
DECISION = "2026-10-20T06:48:17.228+00:00"
CHAIN = {"text": "chain 06:48:16 · +1.2s our detection",
         "anchor_at": "2026-10-20T06:48:16+00:00", "basis": "chain"}


def _times(**changes):
    card = card_view("transaction_failed", reason="chain_error", decision_started_at=DECISION,
                     chain_timing=CHAIN)
    card.update(observed_at=RECEIPT, source_created_at="2026-10-20T06:48:16+00:00",
                status_updated_at="2026-10-20T06:48:17.800+00:00")
    card.update(changes)
    return next(line for line in render_entry_notification(card).splitlines()
                if line.startswith("Times (UTC)"))


def test_chain_processing_is_local_queue_time_after_runner_receipt():
    line = _times(socket_received_at="2026-10-20T06:48:16.100+00:00")
    assert "+1.2s our detection · +0.0s processed" in line
    assert "+1.2s processed" not in line


@pytest.mark.parametrize("chain", [CHAIN, None])
@pytest.mark.parametrize("receipt", [None, "invalid", "2026-10-20T06:48:17.200"])
def test_unknown_runner_receipt_never_uses_source_or_separate_socket_clock(chain, receipt):
    line = _times(chain_timing=chain, observed_at=receipt,
                  socket_received_at="2026-10-20T06:48:16.100+00:00")
    assert "processed unknown" in line
    assert "+1.2s processed" not in line
    if chain is None:
        assert "s received" not in line


@pytest.mark.parametrize("decision", ["invalid", "2026-10-20T06:48:17.228"])
def test_invalid_processing_stamp_does_not_invent_a_duration(decision):
    assert "processed unknown" in _times(decision_started_at=decision)


def test_reversed_local_stamps_name_the_local_clock_disagreement():
    line = _times(decision_started_at="2026-10-20T06:48:17.000+00:00")
    assert "processed unknown (receipt/processed stamps disagree by 0.2s, order unknown)" in line


def test_sub_resolution_local_reversal_keeps_existing_zero_presentation():
    line = _times(decision_started_at="2026-10-20T06:48:17.198+00:00")
    assert "+0.0s processed" in line and "stamps disagree" not in line


def _solana_attempt(dispatch, *, status="filled", source=100, landing=101,
                    event="slot_transport_dispatch"):
    trace = json.loads(_slot_trace(source, landing))
    trace["substage_events"].append({"name": event, "receipt_to_dispatch_ms": dispatch})
    return _a(signal_source="slot_lane", terminal_status=status,
              terminal_reason="chain_error:BuySlippageBelowMinTokensOut" if status == "failed" else None,
              trace_json=json.dumps(trace))


@pytest.mark.parametrize("status,source,landing", [
    ("filled", 100, 101), ("filled", 100, 104), ("failed", 100, 101),
    ("filled", None, 101), ("failed", 100, None),
])
@pytest.mark.parametrize("event", ["slot_transport_dispatch", "slot_pre_send"])
def test_explicit_solana_dispatch_reaches_live_and_resolver_lines(status, source, landing, event):
    facts = ft.facts(_solana_attempt(28, status=status, source=source, landing=landing, event=event))
    cause, detail = ft.classify(facts)
    line = ft.landed_text(facts)
    assert "receipt-to-dispatch 28 ms" in line
    assert ft.line_from_row(dict(network_id="solana", gap=ft.gap_of(facts),
                                landing_slot=landing, cause=cause, cause_detail=detail,
                                source_sig=facts["source_ref"])) == line


def test_zero_dispatch_is_a_valid_measurement():
    assert "receipt-to-dispatch 0 ms" in ft.landed_text(ft.facts(_solana_attempt(0)))


@pytest.mark.parametrize("dispatch", [None, -1, True, "28", float("nan"), float("inf")])
def test_missing_or_invalid_dispatch_never_falls_back_to_wall_clock(dispatch):
    assert ft.landed_text(ft.facts(_solana_attempt(dispatch))) == "Landed S+1"


def test_evm_line_does_not_pick_up_a_solana_dispatch_event():
    attempt = _solana_attempt(28)
    attempt.update(network_id="4663", source_slot=100, landing_slot=101)
    original = dict(attempt, trace_json=None)
    assert ft.landed_text(ft.facts(attempt)) == ft.landed_text(ft.facts(original))
