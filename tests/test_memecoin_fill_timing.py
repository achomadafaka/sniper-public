"""The pure fill-timing module: S+n, the stages, and the why-not-S+1 cause (spec
docs/superpowers/specs/2026-10-02-memecoin-fill-timing-design.md, rev 2, section 2.5)."""
import json

import pytest

from sniper.services import memecoin_fill_timing as ft

T0 = "2026-10-02T10:00:00.000+00:00"


def _a(**kw):
    base = {"attempt_id": "a1", "network_id": "solana", "signal_source": "onchain_watch",
            "delivery_lane": None, "terminal_status": "filled", "terminal_reason": None,
            "observed_at": "2026-10-02T10:00:00.000+00:00",
            "decision_started_at": "2026-10-02T10:00:00.010+00:00",
            "transaction_submitted_at": "2026-10-02T10:00:00.400+00:00",
            "chain_confirmation_completed_at": "2026-10-02T10:00:01.100+00:00",
            "completed_at": "2026-10-02T10:00:01.200+00:00", "tx_sig": "OURS",
            "origin_event_id": "onchain:SRC", "trace_json": None}
    base.update(kw)
    return base


def _f(attempt=None, detection=None, slot=None, skip=None, fast=None, timing=None):
    return ft.facts(attempt or _a(), detection, slot, skip, fast, timing)


def _slot_trace(source, landing, head_at_recv=None):
    return json.dumps({
        "source_evidence": {"source_slot": source},
        "substage_events": [{"name": "slot_receipt", "source_slot": source, "head_at_recv": head_at_recv},
                            {"name": "slot_resend", "landing_slot": landing}],
        "submission_events": [{"stage": "chain_confirmation", "landing_slot": landing}]})


# --- slots and the gap -------------------------------------------------------------------------

def test_a_slot_lane_trace_gives_s_and_landing_and_s_plus_one_is_on_time():
    f = _f(_a(signal_source="slot_lane", delivery_lane="slot_lane_processed",
              trace_json=_slot_trace(100, 101, head_at_recv=100)))
    assert (f["source_slot"], f["landing_slot"], f["source_basis"]) == (100, 101, "slot_lane")
    assert ft.gap_of(f) == 1 and ft.classify(f)[0] == "on_time" and ft.landed_text(f) == "Landed S+1"


def test_s_plus_zero_is_a_real_zero():
    f = _f(_a(signal_source="slot_lane", trace_json=_slot_trace(100, 100)))
    assert ft.gap_of(f) == 0 and ft.landed_text(f) == "Landed S+0"


def test_a_missing_slot_is_none_never_zero():
    f = _f(_a(trace_json=json.dumps({"submission_events": [{"stage": "chain_confirmation",
                                                              "landing_slot": 101}]})))
    assert f["landing_slot"] == 101 and f["source_slot"] is None and ft.gap_of(f) is None
    assert ft.landed_text(f) == "Landed S+? (source slot resolving)"


def _onchain_trace(landing, head=None, head_age_ms=40.0):
    return json.dumps({"substage_events": [{"name": "attempt_row", "head_slot": head, "head_age_ms": head_age_ms}],
                       "submission_events": [{"stage": "chain_confirmation", "landing_slot": landing}]})


def test_an_onchain_fill_reads_the_detection_slot_and_the_runner_head():
    det = {"source_slot": 100, "block_time": 1790932881, "detected_at": T0}
    f = _f(_a(trace_json=_onchain_trace(106, head=103)), detection=det)
    assert (f["source_slot"], f["recv_slot"], f["source_basis"]) == (100, 103, "watcher")
    assert ft.gap_of(f) == 6 and ft.stages(f)["detect"] == 3


def test_the_resolver_row_fills_what_the_attempt_lacks():
    f = _f(timing={"source_slot": 100, "source_slot_basis": "rpc", "landing_slot": 104,
                   "landing_slot_basis": "rpc", "state": "complete"})
    assert ft.gap_of(f) == 4 and f["source_basis"] == "rpc"


_OBS_TX = "0x" + "a9" * 32
_OBS_ORIGIN = "swap:raybot:%s:%s:%s" % (_OBS_TX, "0x" + "ef" * 20, "0x" + "cd" * 20)


def _receipt_observation_trace(**changes):
    observation = {"network_id": "56", "source_tx": _OBS_TX, "source_block_number": 126956870}
    observation.update(changes)
    return json.dumps({"substage_events": [
        {"name": "evm_source_receipt_observation", "observation": observation},
        {"name": "evm.confirmation", "block": 126956875}]})


def test_receipt_observation_times_a_j7_fill_without_inventing_a_lane():
    f = _f(_a(network_id="56", origin_event_id=_OBS_ORIGIN, signal_source="follow_activity",
              delivery_lane="j7_wallets", trace_json=_receipt_observation_trace()))
    assert (f["source_slot"], f["source_basis"], ft.gap_of(f)) == (126956870, "receipt_observation", 5)
    assert f["fast"] is None and f["signal_source"] == "follow_activity" and f["delivery_lane"] == "j7_wallets"
    assert ft.classify(f) == ("fast_lane_skipped", "no fast-lane decision · largest: confirmation observed 700 ms")


@pytest.mark.parametrize("changes", [
    {"network_id": "4663"}, {"source_tx": "0x" + "ab" * 32},
    {"source_block_number": None}, {"source_block_number": 0},
    {"source_block_number": True}, {"source_block_number": 1.5},
])
def test_receipt_observation_needs_exact_chain_transaction_and_positive_block(changes):
    f = _f(_a(network_id="56", origin_event_id=_OBS_ORIGIN, trace_json=_receipt_observation_trace(**changes)))
    assert f["source_slot"] is None and ft.gap_of(f) is None


def test_receipt_observation_never_replaces_an_authoritative_detection():
    f = _f(_a(network_id="56", origin_event_id=_OBS_ORIGIN, trace_json=_receipt_observation_trace()),
           detection={"source_slot": 126956869})
    assert (f["source_slot"], f["source_basis"], ft.gap_of(f)) == (126956869, "watcher", 6)


def test_evm_gap_is_in_blocks_with_seconds_from_the_chain_clock():
    det = {"source_slot": 1000, "block_time": 1790932881, "detected_at": "2026-10-02T09:21:21.413+00:00",
           "detect_latency_s": 0.41}
    trace = json.dumps({"substage_events": [{"name": "evm.confirmation", "outcome": "confirmed",
                                             "block": 1019}]})
    f = _f(_a(network_id="4663", trace_json=trace), detection=det,
           timing={"landing_time": 1790932883.4, "source_time": 1790932881})
    assert ft.gap_of(f) == 19 and f["gap_s"] == pytest.approx(2.4)
    assert ft.landed_text(f).startswith("Landed +19 blocks (2.4 s)")


@pytest.mark.parametrize('network', ['4663', '56'])
def test_evm_detect_uses_the_chosen_event_receipt_not_a_later_watcher(network):
    source = 1790987355
    a = _a(network_id=network, signal_source='follow_activity',
           observed_at='2026-10-03T00:29:15.622383+00:00',
           decision_started_at='2026-10-03T00:29:40.822917+00:00')
    f = _f(a, detection={'block_time': source, 'detect_latency_s': 21.931278},
           timing={'source_time': source})
    assert ft.stages(f)['detect'] == pytest.approx(0.622383)


@pytest.mark.parametrize('received', ['garbage', '2026-10-03T00:29:15.622383',
                                      '2026-10-03T00:29:14+00:00'])
def test_evm_invalid_receipt_is_unknown_not_the_other_lanes_latency(received, monkeypatch):
    f = _f(_a(network_id='4663', observed_at=received),
           detection={'detect_latency_s': 21.931278}, timing={'source_time': 1790987355})
    assert ft.stages(f)['detect'] is None
    monkeypatch.setattr(ft, 'one', lambda _conn, sql, _args: {
        'source_time': 1790987355, 'stages_json': '{"detect":21.931278}'}
        if 'FROM memecoin_fill_timing WHERE' in sql else None)
    assert ft._details(None, _a(network_id='4663', observed_at=received),
                       object(), {}, True)['stages']['detect'] is None


# --- causes, first match wins ------------------------------------------------------------------

def test_on_time_per_chain():
    for chain, ok, late in (("8453", 1, 2), ("56", 2, 3), ("5042", 2, 3), ("4663", 8, 9)):
        on = _f(timing={"source_slot": 10, "landing_slot": 10 + ok}, attempt=_a(network_id=chain))
        off = _f(timing={"source_slot": 10, "landing_slot": 10 + late}, attempt=_a(network_id=chain))
        assert ft.classify(on)[0] == "on_time" and ft.classify(off)[0] != "on_time"


def test_a_price_failure():
    f = _f(_a(terminal_status="failed", terminal_reason="slippage_exceeded"),
           timing={"source_slot": 10, "landing_slot": 14})
    assert ft.classify(f)[0] == "price"


def test_not_landed():
    f = _f(_a(terminal_status="failed", terminal_reason="expired"), timing={"state": "not_landed"})
    assert ft.classify(f)[0] == "not_landed" and ft.landed_text(f).startswith("Not landed")


def test_a_fallback_after_a_failed_slot_attempt_names_its_s_plus_n_and_the_wait():
    slot = {"terminal_status": "failed", "terminal_reason": "slippage",
            "completed_at": "2026-10-02T09:59:59.620+00:00", "trace_json": _slot_trace(100, 102)}
    f = _f(_a(delivery_lane="onchain_fallback_after_slot_lane", decision_started_at=T0),
           slot=slot, timing={"source_slot": 100, "landing_slot": 106})
    cause, detail = ft.classify(f)
    assert cause == "slot_lane_failed" and "slot lane S+2" in detail and "slippage" in detail
    assert ft.stages(f)["fallback_wait_ms"] == pytest.approx(380.0)


@pytest.mark.parametrize("slot, skip, words", [
    ({"terminal_status": "refused", "terminal_reason": "over_ceiling", "trace_json": None}, None,
     "slot lane refused: over_ceiling"),
    (None, {"outcome": "handoff", "reason": "pool_unknown"}, "slot lane: handoff pool_unknown"),
    (None, None, "slot lane never saw it")])
def test_slot_lane_skipped(slot, skip, words):
    f = _f(slot=slot, skip=skip, timing={"source_slot": 100, "landing_slot": 105})
    cause, detail = ft.classify(f)
    assert cause == "slot_lane_skipped" and words in detail


def test_fast_lane_skipped_on_evm():
    f = _f(_a(network_id="4663"), fast={"lane": "rh_fast", "reason": "float_short"},
           timing={"source_slot": 10, "landing_slot": 40})
    assert ft.classify(f)[0] == "fast_lane_skipped" and "float_short" in ft.classify(f)[1]
    g = _f(_a(network_id="4663"), timing={"source_slot": 10, "landing_slot": 40})
    assert ft.classify(g)[0] == "fast_lane_skipped" and "no fast-lane decision" in ft.classify(g)[1]


def test_the_detail_always_ends_with_the_largest_stage():
    f = _f(_a(observed_at=T0, transaction_submitted_at="2026-10-02T10:00:02.000+00:00",
              chain_confirmation_completed_at="2026-10-02T10:00:02.700+00:00"),
           timing={"source_slot": 100, "landing_slot": 109})
    assert ft.classify(f)[1].endswith("largest: detect-to-submit 2000 ms")
    late = _f(slot=None, skip={"outcome": "handoff", "reason": "pool_unknown"},
              attempt=_a(trace_json=_onchain_trace(107, head=103)), detection={"source_slot": 100})
    # The head is three slots ahead; its distance cannot outrank measured elapsed time.
    assert ft.stages(late)["detect"] == 3
    assert ft.landed_text(late) == "Landed S+7 · slot lane: handoff pool_unknown · largest: confirmation observed 700 ms"


def test_failed_same_slot_with_a_lagging_head_ranks_observation_time():
    trace = json.loads(_slot_trace(100, 100, head_at_recv=98))
    trace["substage_events"].append({"name": "slot_pre_send", "head_slot": 98,
                                    "head_age_ms": 30, "head_fresh": True})
    f = _f(_a(signal_source="slot_lane", terminal_status="failed",
              terminal_reason="chain_error:BuySlippageBelowMinBaseAmountOut",
              transaction_submitted_at="2026-10-02T10:00:00.048+00:00",
              chain_confirmation_completed_at="2026-10-02T10:00:00.535+00:00",
              completed_at="2026-10-02T10:00:00.551+00:00", trace_json=json.dumps(trace)))
    st = ft.stages(f)
    assert (ft.gap_of(f), st["detect"], st["land_slots"]) == (0, -2, 2)
    assert st["land_ms"] == pytest.approx(487)
    assert ft.landed_text(f) == ("Landed S+0 · chain_error:BuySlippageBelowMinBaseAmountOut · "
                                 "largest: confirmation observed 487 ms")
    assert ft.classify(f)[0] == "price"


def test_solana_slot_distances_do_not_rank_when_elapsed_times_are_unknown():
    trace = json.loads(_slot_trace(100, 105, head_at_recv=103))
    trace["substage_events"].append({"name": "slot_pre_send", "head_slot": 104})
    f = _f(_a(signal_source="slot_lane", observed_at=None,
              transaction_submitted_at=None, chain_confirmation_completed_at=None,
              trace_json=json.dumps(trace)))
    st = ft.stages(f)
    assert st["detect"] == 3 and st["land_slots"] == 1
    assert st["decide_ms"] is None and st["land_ms"] is None
    assert ft.landed_text(f) == "Landed S+5 · no stage measured"


def test_solana_head_skew_does_not_hide_larger_detect_to_submit_time():
    trace = json.loads(_slot_trace(100, 105, head_at_recv=103))
    trace["substage_events"].append({"name": "slot_pre_send", "head_slot": 100})
    f = _f(_a(signal_source="slot_lane",
              chain_confirmation_completed_at="2026-10-02T10:00:00.700+00:00",
              trace_json=json.dumps(trace)))
    assert ft.stages(f)["land_slots"] == 5
    assert ft.landed_text(f) == "Landed S+5 · largest: detect-to-submit 400 ms"


def test_a_refusal_has_no_landed_text():
    assert ft.landed_text(_f(_a(terminal_status="refused"))) == ""


def test_open_readonly_never_creates_the_file(tmp_path):
    assert ft.open_readonly(tmp_path / "absent.db") is None and not (tmp_path / "absent.db").exists()
    conn = ft.connect(tmp_path / "t.db")
    conn.close()
    ro = ft.open_readonly(tmp_path / "t.db")
    with pytest.raises(Exception):
        ro.execute("INSERT INTO memecoin_slot_lane_skips (signature) VALUES ('x')")


def test_a_stale_runner_head_is_no_detect_measurement():
    f = _f(_a(trace_json=_onchain_trace(106, head=103, head_age_ms=5000.0)), detection={"source_slot": 100})
    assert f["recv_slot"] is None and ft.stages(f)["detect"] is None


def test_lookups_never_change_the_callers_row_factory():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a)")
    conn.execute("INSERT INTO t VALUES (1)")
    assert ft.one(conn, "SELECT a FROM t", ()) == {"a": 1}
    assert conn.row_factory is None and conn.execute("SELECT a FROM t").fetchone() == (1,)


def test_historical_handoff_json_preserves_known_source_and_receipt_slots():
    det = {'source_slot': None, 'source_valuation_json': json.dumps({
        'basis': 'slot_lane_handoff', 'source_slot': 100, 'recv_slot': 101})}
    f = _f(detection=det, timing={'source_slot': 99, 'source_slot_basis': 'rpc'})
    assert (f['source_slot'], f['source_basis'], f['recv_slot']) == (100, 'detection_json', 101)


@pytest.mark.parametrize('slot', [None, 0, -1, True, 'wrong', 10.5])
def test_invalid_handoff_slots_never_become_timing_evidence(slot):
    f = _f(detection={'source_valuation_json': json.dumps({'source_slot': slot, 'recv_slot': slot})})
    assert f['source_slot'] is None and f['recv_slot'] is None


@pytest.mark.parametrize('slot', [0, -1, True, 'wrong', 10.5])
def test_invalid_explicit_slot_does_not_displace_valid_historical_evidence(slot):
    f = _f(detection={'source_slot': slot, 'source_valuation_json': json.dumps({'source_slot': 100})})
    assert (f['source_slot'], f['source_basis']) == (100, 'detection_json')


def test_missing_evm_source_block_never_uses_solana_slot_units():
    f = _f(_a(network_id='8453', trace_json=_onchain_trace(101)))
    assert ft.landed_text(f).startswith('Landed +? blocks (source block resolving) · source block unresolved')
    f['landing_slot'] = None
    assert ft.landed_text(f).startswith('Landing block resolving · source block unresolved')


# --- R8 (Codex review 2026-10-06): the EVM fast lanes' own fills ---------------------------------
TX = "0x" + "a9" * 32
MINT56 = "0x7580d478f78d5a59c94cde67a4b4c3f3afa97777"


@pytest.mark.parametrize("origin,network,ref", [
    ("bscfast:56:%s:%s" % (TX, MINT56), "56", TX),
    ("rhfast:4663:%s:%s" % (TX.upper().replace("0X", "0x"), MINT56), "4663", TX),
    ("bscfast:56:%s:%s" % (TX, MINT56), "4663", None),            # chain mismatch
    ("bscfast:56:0xnothex:%s" % MINT56, "56", None),                # malformed hash
    ("bscfast:56:%s" % TX, "56", None),                             # no mint part
])
def test_source_ref_reads_the_fast_lane_origins(origin, network, ref):
    assert ft.source_ref({"origin_event_id": origin, "network_id": network}) == ref


def _fast_trace(source_block, landing, network="56"):
    return json.dumps({
        "source_evidence": {"network_id": network, "source_block_number": source_block},
        "substage_events": [
            {"name": "attempt_row", "completed_at": "2026-10-02T10:00:01.190+00:00"},
            {"name": "rh_fast_lane_send", "completed_at": "2026-10-02T10:00:02.367+00:00", "reply": "accepted"},
            {"name": "rh_fast_lane_receipt", "completed_at": "2026-10-02T10:00:03.428+00:00",
             "block": landing, "block_time": 1791292837}]})


def _fast(**kw):
    return _a(network_id="56", signal_source="bsc_fast_lane", delivery_lane="bsc_fast_lane",
              origin_event_id="bscfast:56:%s:%s" % (TX, MINT56), transaction_submitted_at=None,
              chain_confirmation_completed_at=None, **kw)


def test_a_fast_lane_fill_is_timed_from_its_own_trace_and_never_called_skipped():
    """Codex R8: BSC fast-lane fills read 'fast_lane_skipped · no stage measured' with a NULL
    source. The lane's own source block, send and receipt are in its trace."""
    f = _f(_fast(trace_json=_fast_trace(126067036, 126067043)))
    assert (f["source_slot"], f["source_basis"], f["landing_slot"]) == (126067036, "fast_lane", 126067043)
    st = ft.stages(f)
    assert st["decide_ms"] == pytest.approx(2367.0) and st["land_ms"] == pytest.approx(1061.0)
    cause, detail = ft.classify(f)
    assert cause == "decide_slow" and "fast_lane_skipped" not in detail
    assert ft.landed_text(f).startswith("Landed +7 blocks")


def test_a_fast_lane_source_block_on_another_network_is_not_evidence():
    f = _f(_fast(trace_json=_fast_trace(126067036, 126067043, network="4663")))
    assert f["source_slot"] is None


# --- Gamma INFO on R8: the fast lane's own live row (bound by our tx) fills what the trace lacks ------

def _fast_live(blocks_behind=1, safety_path="cache", safety_ms=None):
    return {"blocks_behind": blocks_behind, "safety_path": safety_path, "safety_ms": safety_ms}


def test_a_fast_lane_fill_without_its_source_block_takes_it_from_the_lanes_own_row():
    """52807e7b: no source_block_number in the trace; bsc_lane_live says blocks_behind=1."""
    trace = json.loads(_fast_trace(None, 125589805))
    f = ft.facts(_fast(trace_json=json.dumps(trace)), live=_fast_live(1))
    assert (f["source_slot"], f["source_basis"], ft.gap_of(f)) == (125589804, "fast_lane_live", 1)
    assert ft.classify(f)[0] == "on_time"


@pytest.mark.parametrize("bad", [None, -1, True, "7"])
def test_an_unusable_blocks_behind_is_no_source(bad):
    f = ft.facts(_fast(trace_json=_fast_trace(None, 125589805)), live=_fast_live(bad))
    assert f["source_slot"] is None


def test_the_trace_source_block_wins_and_a_slow_decide_names_the_lanes_safety_path():
    f = ft.facts(_fast(trace_json=_fast_trace(126067036, 126067043)),
                 live=_fast_live(9, "inline", 1152.8))
    assert (f["source_slot"], f["source_basis"]) == (126067036, "fast_lane")
    cause, detail = ft.classify(f)
    assert cause == "decide_slow" and detail.startswith("safety inline 1153 ms · largest: detect-to-submit")


def test_the_lanes_row_is_read_only_for_our_own_fill(tmp_path):
    import sqlite3
    db = tmp_path / "bsc.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE bsc_lane_live (id INTEGER PRIMARY KEY, source_tx TEXT, our_tx TEXT, "
              "blocks_behind INTEGER, safety_path TEXT, safety_ms REAL)")
    c.execute("INSERT INTO bsc_lane_live VALUES (1, ?, ?, 3, 'cache', NULL)", (TX, "0x" + "45" * 32))
    c.commit()
    lanes = {"bsc": c}
    assert ft.lane_live_of(lanes, "56", TX, "0x" + "45" * 32)["blocks_behind"] == 3
    assert ft.lane_live_of(lanes, "56", TX, "0x" + "46" * 32) is None
    assert ft.lane_live_of(lanes, "8453", TX, "0x" + "45" * 32) is None


def test_receipt_tables_are_additive_and_nullable_identity(tmp_path):
    from sniper.services import memecoin_fill_timing as ft
    conn = ft.connect(tmp_path / 'receipts.db')
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'memecoin_signal_receipts', 'memecoin_processed_receipts', 'memecoin_execution_coverage',
            'memecoin_fill_timing', 'memecoin_slot_lane_skips'} <= tables
    columns = {r[1]: r for r in conn.execute('PRAGMA table_info(memecoin_signal_receipts)')}
    assert columns['mint'][3] == columns['source_sig'][3] == 0
    assert columns['recv_monotonic'][2] == 'REAL'

def test_largest_detect_to_submit_is_not_labelled_decision_cpu():
    from sniper.services.memecoin_fill_timing import classify
    f = {"network_id": "8453", "observed_at": "2026-10-11T14:11:37Z",
         "submitted_at": "2026-10-11T14:11:44Z"}
    detail = classify(f)[1]
    assert detail.endswith("largest: detect-to-submit 7000 ms")


def _okt_attempt():
    boot = "linux:okt-boot"
    clock = {"version": 1, "producer_id": "j7:okt", "sequence": 23,
             "boot_id": boot, "monotonic_ns": 31_940_323_768_012,
             "raw_utc": "2026-10-12T17:40:27.765611+00:00",
             "corrected_utc": "2026-10-12T17:40:27.680738+00:00",
             "correction_s": .084873}
    trace = {"receipt_clock": clock, "receipt_clock_status": "boot_monotonic_v1",
             "submission_events": [
                 {"stage": "transaction_submitted", "attempt_no": 1, "boot_id": boot,
                  "monotonic_ns": 31_941_025_527_497},
                 {"stage": "chain_confirmation", "attempt_no": 1, "boot_id": boot,
                  "tx_sig": "OURS", "block": 127252459, "monotonic_ns": 31_941_480_796_002}],
             "substage_events": [
                 {"name": "evm.sender_dispatch", "attempt_no": 1, "boot_id": boot,
                  "tx_sig": "OURS", "monotonic_ns": 31_940_983_041_504}]}
    return _a(network_id="56", signal_source="follow_activity", delivery_lane="j7_wallets",
              observed_at="2026-10-12T17:40:27.680738+00:00",
              transaction_submitted_at="2026-10-12T17:40:28.382560+00:00",
              chain_confirmation_completed_at="2026-10-12T17:40:28.837829+00:00",
              trace_json=json.dumps(trace))


def test_okt_coarse_block_clock_cannot_hide_receipt_to_submit_bottleneck():
    f = _f(_okt_attempt(), fast={"lane": "bsc lane", "reason": "hook:skipped_mcap"},
           timing={"source_slot": 127252454, "landing_slot": 127252459,
                   "source_time": 1791826826.0})
    assert ft.gap_of(f) == 5
    assert ft.stages(f)["detect"] == pytest.approx(1.680738)
    line = ft.landed_text(f)
    assert "source block-to-receipt 1.7 s (coarse)" in line
    assert "receipt-to-dispatch 659 ms" in line
    assert "receipt-to-submit observed 702 ms" in line
    assert "receipt-to-confirmation observed 1157 ms" in line
    assert line.endswith("largest: receipt-to-submit 702 ms")


@pytest.mark.parametrize("bad", ["missing", "cross_boot", "missing_boot", "negative", "history_unknown"])
def test_unprovable_original_clock_never_becomes_receipt_execution_latency(bad):
    a = _okt_attempt()
    trace = json.loads(a["trace_json"])
    if bad == "missing":
        trace.pop("receipt_clock")
    elif bad == "history_unknown":
        trace["receipt_clock"]["history_unknown"] = True
    else:
        for event in trace["submission_events"] + trace["substage_events"]:
            if bad == "cross_boot":
                event["boot_id"] = "other-boot"
            elif bad == "missing_boot":
                event.pop("boot_id")
            else:
                event["monotonic_ns"] = 1
    a.update(trace_json=json.dumps(trace), observed_at=None,
             transaction_submitted_at=None, chain_confirmation_completed_at=None)
    f = _f(a, timing={"source_slot": 100, "landing_slot": 105, "source_time": 1791826826.0},
           detection={"detect_latency_s": 1.680738})
    line = ft.landed_text(f)
    assert "source block-to-receipt 1.7 s (coarse)" in line
    assert "receipt-to-dispatch" not in line and "receipt-to-submit" not in line
    assert "receipt-to-confirmation" not in line
    assert line.endswith("no stage measured")


def test_on_time_evm_fill_still_shows_original_receipt_execution_clock():
    a = _okt_attempt()
    t = json.loads(a["trace_json"])
    t["submission_events"][1]["block"] = 102
    a["trace_json"] = json.dumps(t)
    f = _f(a, timing={"source_slot": 100, "landing_slot": 102})
    assert ft.classify(f)[0] == "on_time"
    assert "receipt-to-submit observed 702 ms" in ft.landed_text(f)
    assert "receipt-to-confirmation observed 1157 ms" in ft.landed_text(f)


def test_evm_clock_reporting_does_not_change_solana_slot_stage_semantics():
    t = json.loads(_okt_attempt()["trace_json"])
    t["submission_events"][1].pop("block")
    a = _a(signal_source="slot_lane", trace_json=json.dumps(t))
    f = _f(a, timing={"source_slot": 100, "landing_slot": 101})
    assert ft.stages(f)["decide_ms"] == pytest.approx(400)
    assert ft.stages(f)["land_ms"] == pytest.approx(700)
    assert ft.landed_text(f) == "Landed S+1"


def _rejected_retry_attempt():
    a = _okt_attempt()
    t = json.loads(a["trace_json"])
    t["substage_events"][0].update(tx_sig="first", monotonic_ns=31_940_423_768_012)
    t["submission_events"][0].update(monotonic_ns=31_940_523_768_012)
    t["submission_events"][1].update(tx_sig="first", outcome="failed", monotonic_ns=31_941_123_768_012)
    t["substage_events"].append({"name": "evm.sender_dispatch", "attempt_no": 2,
                                "boot_id": "linux:okt-boot", "tx_sig": "second",
                                "monotonic_ns": 31_941_223_768_012})
    a.update(terminal_status="failed", terminal_reason="broadcast_rejected", tx_sig="second",
             trace_json=json.dumps(t))
    return a


def test_rejected_retry_cannot_inherit_previous_swap_submission_and_confirmation(monkeypatch):
    a = _rejected_retry_attempt()
    f = _f(a)
    assert f["receipt_dispatch_ms"] == 900
    assert f["receipt_submit_ms"] is None and f["receipt_confirmation_ms"] is None
    assert ft.stages(f)["decide_ms"] is None and ft.stages(f)["land_ms"] is None
    line = ft.landed_text(f)
    assert "receipt-to-dispatch 900 ms" in line
    assert "receipt-to-submit" not in line and "receipt-to-confirmation" not in line
    monkeypatch.setattr(ft, 'one', lambda _conn, sql, _args:
                        {"stages_json": '{"decide_ms":200,"land_ms":600}'}
                        if 'FROM memecoin_fill_timing WHERE' in sql else None)
    saved = ft._details(None, a, object(), {}, True)
    assert saved["stages"]["decide_ms"] is None and saved["stages"]["land_ms"] is None


def test_successful_retry_reports_only_the_final_swap_clock():
    a = _rejected_retry_attempt()
    t = json.loads(a["trace_json"])
    t["submission_events"].extend([
        {"stage": "transaction_submitted", "attempt_no": 2, "boot_id": "linux:okt-boot",
         "monotonic_ns": 31_941_273_768_012},
        {"stage": "chain_confirmation", "attempt_no": 2, "boot_id": "linux:okt-boot",
         "tx_sig": "second", "monotonic_ns": 31_941_523_768_012}])
    a.update(terminal_status="filled", terminal_reason="confirmed", trace_json=json.dumps(t))
    f = _f(a)
    assert (f["receipt_dispatch_ms"], f["receipt_submit_ms"], f["receipt_confirmation_ms"]) == (900, 950, 1200)
    assert ft.stages(f)["decide_ms"] == 950 and ft.stages(f)["land_ms"] == 250


@pytest.mark.parametrize("bad", ["missing_no", "bool_no", "missing_tx", "conflicting_tx"])
def test_unbound_subattempt_clock_is_unknown(bad):
    a = _okt_attempt()
    t = json.loads(a["trace_json"])
    events = t["substage_events"] + t["submission_events"]
    if bad in ("missing_no", "bool_no", "missing_tx"):
        for e in events:
            if bad == "missing_no":
                e.pop("attempt_no")
            elif bad == "bool_no":
                e["attempt_no"] = True
            else:
                e.pop("tx_sig", None)
    else:
        t["submission_events"][1]["tx_sig"] = "OTHER"
    a["trace_json"] = json.dumps(t)
    f = _f(a)
    assert all(f[key] is None for key in ("receipt_dispatch_ms", "receipt_submit_ms", "receipt_confirmation_ms"))
    assert ft.stages(f)["decide_ms"] is None and ft.stages(f)["land_ms"] is None


@pytest.mark.parametrize("bad", ["duplicate_clock", "unordered_clock"])
def test_conflicting_submission_clock_does_not_fabricate_an_ordered_span(bad):
    a = _okt_attempt()
    t = json.loads(a["trace_json"])
    if bad == "duplicate_clock":
        t["submission_events"].append({**t["submission_events"][0], "monotonic_ns": 31_941_200_000_000})
    else:
        t["submission_events"][0]["monotonic_ns"] = 31_940_523_768_012
    a["trace_json"] = json.dumps(t)
    f = _f(a)
    assert f["receipt_submit_ms"] is None
    assert ft.stages(f)["decide_ms"] is None and ft.stages(f)["land_ms"] is None


def test_rejected_retry_does_not_inherit_prior_confirmation_block_from_trace_or_cache():
    a = _rejected_retry_attempt()
    t = json.loads(a["trace_json"])
    t["submission_events"][1]["block"] = 501
    t["substage_events"].append({"name": "evm.confirmation", "attempt_no": 1,
                                "tx_sig": "first", "block": 501})
    a.update(trace_json=json.dumps(t), landing_slot=501)
    f = _f(a, timing={"source_slot": 500, "landing_slot": 501, "landing_time": 1791826827.0})
    assert f["landing_slot"] is None and ft.gap_of(f) is None
    assert f["landing_time"] is None
    assert ft.landed_text(f).startswith("Landing block resolving")


@pytest.mark.parametrize("landing_source", ["attempt", "resolver"])
def test_confirmed_terminal_swap_retains_resolver_landing_time_and_block(landing_source):
    a = _okt_attempt()
    t = json.loads(a["trace_json"])
    t["submission_events"][1].pop("block")
    a["trace_json"] = json.dumps(t)
    if landing_source == "attempt":
        a["landing_slot"] = 127252459
    f = _f(a, timing={"source_slot": 127252454, "landing_slot": 127252459,
                      "source_time": 1791826826.0, "landing_time": 1791826827.0})
    assert f["landing_slot"] == 127252459
    assert f["landing_basis"] == ("attempt" if landing_source == "attempt" else "rpc")
    assert ft.gap_of(f) == 5
    assert f["landing_time"] == 1791826827.0 and f["gap_s"] == 1.0
    assert f["receipt_confirmation_ms"] == pytest.approx(1157.027990)


@pytest.mark.parametrize("event_field", ["submission_events", "substage_events"])
@pytest.mark.parametrize("fast_lane", [False, True])
def test_modern_receipt_clock_ignores_non_dictionary_event_frames(event_field, fast_lane):
    a = _okt_attempt()
    t = json.loads(a["trace_json"])
    t[event_field][1:1] = [None, "malformed frame", 7, True]
    a["trace_json"] = json.dumps(t)
    if fast_lane:
        a["signal_source"] = "bsc_fast_lane"
    f = _f(a, timing={"source_slot": 127252454})
    assert f["landing_slot"] == 127252459 and ft.gap_of(f) == 5
    assert (f["receipt_dispatch_ms"], f["receipt_submit_ms"], f["receipt_confirmation_ms"]) == pytest.approx(
        (659.273492, 701.759485, 1157.027990))
