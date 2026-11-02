"""Shared BSC delivery labels retain watcher identity without claiming a skipped lane."""
import copy
import json

import pytest

from sniper.services import memecoin_fill_timing as ft


TX = "0x" + "ab" * 32
MINT = "0x" + "cd" * 20


def _attempt(delivery_lane, *, network="56", signal_source="onchain_watch",
             source_block=100, evidence_network="56"):
    return {
        "attempt_id": "shared-bsc", "network_id": network, "signal_source": signal_source,
        "delivery_lane": delivery_lane, "terminal_status": "filled", "terminal_reason": None,
        "origin_event_id": f"onchain:{network}:{TX}:{MINT}", "tx_sig": "0x" + "ef" * 32,
        "observed_at": "2026-10-10T12:00:00.000+00:00",
        "decision_started_at": "2026-10-10T12:00:00.010+00:00",
        "transaction_submitted_at": "2026-10-10T12:00:00.300+00:00",
        "chain_confirmation_completed_at": "2026-10-10T12:00:00.450+00:00",
        "trace_json": json.dumps({
            "source_evidence": {"network_id": evidence_network, "source_block_number": source_block},
            "submission_events": [{"stage": "chain_confirmation", "block": 104}],
        }),
    }


@pytest.mark.parametrize("delivery_lane", ["bsc_v3_fast_lane", "bsc_flap_fast_lane"])
def test_successful_shared_delivery_uses_its_bound_source_block_and_real_stages(delivery_lane):
    attempt = _attempt(delivery_lane)
    f = ft.facts(attempt, fast={"lane": "bsc lane", "reason": "hook:filled"})
    assert (f["source_slot"], f["source_basis"], f["landing_slot"]) == (100, "fast_lane", 104)
    assert ft.gap_of(f) == 4
    assert ft.stages(f) == {
        "detect": None, "fallback_wait_ms": None, "decide_ms": pytest.approx(300),
        "land_slots": None, "land_ms": pytest.approx(150),
    }
    assert ft.classify(f) == ("decide_slow", "largest: detect-to-submit 300 ms")
    assert ft.landed_text(f) == "Landed +4 blocks · largest: detect-to-submit 300 ms"
    assert f["signal_source"] == "onchain_watch" and f["source_ref"] == TX


@pytest.mark.parametrize("delivery_lane", ["bsc_v3_fast_lane", "bsc_flap_fast_lane"])
def test_known_shared_delivery_is_not_a_skip_when_its_source_slot_is_already_explicit(delivery_lane):
    attempt = _attempt(delivery_lane)
    attempt["source_slot"] = 100
    f = ft.facts(attempt, fast={"lane": "bsc lane", "reason": "hook:filled"})
    assert ft.classify(f) == ("decide_slow", "largest: detect-to-submit 300 ms")


@pytest.mark.parametrize("network,signal_source,delivery_lane", [
    ("4663", "onchain_watch", "bsc_v3_fast_lane"),
    ("8453", "onchain_watch", "bsc_flap_fast_lane"),
    ("56", "follow_activity", "bsc_v3_fast_lane"),
    ("56", None, "bsc_flap_fast_lane"),
    ("56", "onchain_watch", None),
    ("56", "onchain_watch", "bsc_fast_shared"),
    ("56", "onchain_watch", "bsc_v3_fast_lane_shadow"),
    ("56", "onchain_watch", "prefix:bsc_flap_fast_lane"),
])
def test_unrecognized_delivery_keeps_the_ordinary_watcher_skip(network, signal_source, delivery_lane):
    attempt = _attempt(delivery_lane, network=network, signal_source=signal_source,
                       evidence_network=network)
    f = ft.facts(attempt, fast={"lane": "bsc watcher", "reason": "post:no_venue_leg"})
    assert f["source_slot"] is None and f["source_basis"] is None
    assert ft.gap_of(f) is None
    assert ft.classify(f) == (
        "fast_lane_skipped", "bsc watcher: post:no_venue_leg · largest: detect-to-submit 300 ms")


@pytest.mark.parametrize("source_block,evidence_network", [
    (None, "56"), (0, "56"), (-1, "56"), (True, "56"), (100.5, "56"),
    ("unknown", "56"), (100, "4663"), (100, None),
])
@pytest.mark.parametrize("delivery_lane", ["bsc_v3_fast_lane", "bsc_flap_fast_lane"])
def test_unknown_or_foreign_source_evidence_never_invents_a_block(delivery_lane, source_block,
                                                               evidence_network):
    f = ft.facts(_attempt(delivery_lane, source_block=source_block,
                         evidence_network=evidence_network))
    assert f["source_slot"] is None and f["source_basis"] is None and ft.gap_of(f) is None
    assert ft.classify(f) == ("source_unknown", "source block unresolved · largest: detect-to-submit 300 ms")
    assert ft.landed_text(f) == (
        "Landed +? blocks (source block resolving) · source block unresolved · largest: detect-to-submit 300 ms")
    assert ft.stages(f)["detect"] is None and ft.stages(f)["fallback_wait_ms"] is None


@pytest.mark.parametrize("signal_source,network", [("bsc_fast_lane", "56"), ("rh_fast_lane", "4663")])
def test_existing_native_fast_sources_keep_their_source_and_classification(signal_source, network):
    attempt = _attempt(None, network=network, signal_source=signal_source, evidence_network=network)
    attempt["landing_slot"] = 112
    f = ft.facts(attempt)
    assert (f["source_slot"], f["source_basis"], ft.gap_of(f)) == (100, "fast_lane", 12)
    assert ft.classify(f) == ("decide_slow", "largest: detect-to-submit 300 ms")


@pytest.mark.parametrize("delivery_lane", ["bsc_v3_fast_lane", "bsc_flap_fast_lane"])
def test_shared_identity_does_not_replace_explicit_slots_clocks_or_stored_history(delivery_lane):
    attempt = _attempt(delivery_lane)
    attempt["source_slot"] = 99
    trace = json.loads(attempt["trace_json"])
    trace["substage_events"] = [
        {"name": "rh_fast_lane_send", "completed_at": "2026-10-10T12:00:00.200+00:00"},
        {"name": "rh_fast_lane_receipt", "completed_at": "2026-10-10T12:00:00.600+00:00"},
    ]
    attempt["trace_json"] = json.dumps(trace)
    history = {"network_id": "56", "source_slot": 98, "landing_slot": 103, "gap": 5,
               "cause": "fast_lane_skipped", "cause_detail": "bsc lane: hook:filled", "state": "complete"}
    before = copy.deepcopy((attempt, history))
    f = ft.facts(attempt, timing=history)
    assert (f["source_slot"], f["source_basis"], f["landing_slot"], ft.gap_of(f)) == (99, "attempt", 104, 5)
    assert f["submitted_at"] == "2026-10-10T12:00:00.300+00:00"
    assert f["confirmed_at"] == "2026-10-10T12:00:00.450+00:00"
    assert ft.stages(f)["decide_ms"] == pytest.approx(300)
    assert ft.stages(f)["land_ms"] == pytest.approx(150)
    assert (attempt, history) == before
    assert ft.line_from_row(history) == "Landed +5 blocks · bsc lane: hook:filled"
