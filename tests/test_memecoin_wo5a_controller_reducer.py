"""WO-5A — deterministic controller reducer + CAS persistence + byte-identical restart recon.

The reducer is PURE (no DB, no network). It maintains the persisted exit-decision hysteresis so a
runner restart reconstructs identical state by construction, and it advances break/route/flow
counters deterministically: break counts only on a NEW chain-grounded event identity (reprocessing
the same event or a repeated 30 s materialization does NOT double-count), route observations dedup
by quote identity, and the executable peak's price+time advance atomically. Ships INERT — nothing in
the live decision path reads the new columns until WO-5B wires sizing under WO-7 constants.
"""
import sqlite3

import pytest

from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
from sniper.analysis.exit_controller import (
    ControllerState, Observation, controller_state_from_row, event_identity,
    executable_no_progress, reduce, structure_cut_confirmed)
from sniper.persistence.db_memecoin import (
    CONTROLLER_STATE_COLS, ensure_memecoin_tables, persist_controller_state_cas,
    set_position_controller_state)
from sniper.persistence import db_memecoin as memedb
from sniper.services import memecoin_real_runner as runner

_NEW_COLS = ("controller_revision", "controller_last_event_id", "controller_last_event_block",
             "controller_last_event_log_index", "executable_peak_at", "route_quote_value",
             "route_quote_at")


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    return c


def _open(c):
    return c.execute(
        "INSERT INTO memecoin_positions(source_user_id, mint, entry_price, opened_at) "
        "VALUES('u','M',1.0,'t')").lastrowid


# --- schema ------------------------------------------------------------------------------------

def test_migration_adds_five_controller_columns_inert():
    c = _db()
    cols = {r["name"]: r["dflt_value"] for r in c.execute("PRAGMA table_info(memecoin_positions)")}
    for col in _NEW_COLS:
        assert col in cols, f"{col} missing from migration"
    pid = _open(c)
    row = c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row["controller_revision"] == 0
    for col in _NEW_COLS[1:]:
        assert row[col] is None


def test_controller_state_cols_includes_new_writable_columns():
    # controller_revision is the CAS guard, managed by persist_controller_state_cas — never a free
    # field. Every OTHER persisted controller column is writable through the allowlist.
    for col in ("controller_last_event_id", "controller_last_event_block",
                "controller_last_event_log_index", "executable_peak_at", "route_quote_value",
                "route_quote_at", "executable_peak_price"):
        assert col in CONTROLLER_STATE_COLS
    assert "controller_revision" not in CONTROLLER_STATE_COLS


# --- event-grounded break dedup ----------------------------------------------------------------

def test_event_identity_requires_tx_sig():
    # vault-delta-only rows (tx_sig=None) are not chain-grounded and cannot advance break counts.
    assert event_identity("solana", "P", None, 0) is None
    eid = event_identity("solana", "P", "SIG", 3)
    assert eid and event_identity("solana", "P", "SIG", 3) == eid          # deterministic
    assert event_identity("solana", "P", "SIG", 4) != eid                  # log_index disambiguates


def test_break_count_advances_once_per_event_identity():
    s0 = ControllerState()
    eid = event_identity("solana", "P", "SIG1", 0)
    obs = Observation(now="2026-07-25T00:00:00Z", event_id=eid, structure_break=True,
                      net_flow_negative=True)
    s1 = reduce(s0, obs)
    assert s1.consecutive_break_events == 1
    assert s1.structure_break_since == "2026-07-25T00:00:00Z"     # onset stamped once
    assert s1.controller_last_event_id == eid
    # reprocessing the SAME event id (repeated 30 s materialization / duplicate WS notification)
    s2 = reduce(s1, Observation(now="2026-07-25T00:00:30Z", event_id=eid, structure_break=True,
                                net_flow_negative=True))
    assert s2.consecutive_break_events == 1                       # did NOT double-count
    assert s2.structure_break_since == "2026-07-25T00:00:00Z"     # onset unchanged
    # a genuinely NEW event advances
    eid2 = event_identity("solana", "P", "SIG2", 0)
    s3 = reduce(s2, Observation(now="2026-07-25T00:01:00Z", event_id=eid2, structure_break=True,
                                net_flow_negative=True))
    assert s3.consecutive_break_events == 2


def test_break_count_saturates_at_confirmation_threshold_on_reordered_replay():
    a = event_identity("solana", "P", "SIG1", 0)
    b = event_identity("solana", "P", "SIG2", 0)
    state = ControllerState()
    for event_id in (a, b, a):
        state = reduce(state, Observation(
            now="2026-07-25T00:00:00Z", event_id=event_id, structure_break=True))
    assert state.consecutive_break_events == 2


def test_break_count_rejects_events_at_or_below_persisted_chain_highwater():
    state = ControllerState(
        controller_last_event_id="new", controller_last_event_block=20,
        controller_last_event_log_index=3, consecutive_break_events=1)
    older = reduce(state, Observation(
        now="t", event_id="old", event_block=19, event_log_index=9, structure_break=True))
    duplicate = reduce(state, Observation(
        now="t", event_id="new", event_block=20, event_log_index=3,
        structure_break=True))
    same_slot_distinct = reduce(state, Observation(
        now="t", event_id="same-block", event_block=20, event_log_index=3,
        structure_break=True))
    newer = reduce(state, Observation(
        now="t", event_id="newer", event_block=20, event_log_index=4,
        structure_break=True))
    assert older == state and duplicate == state
    assert same_slot_distinct.consecutive_break_events == 2
    assert newer.consecutive_break_events == 2
    assert newer.controller_last_event_block == 20
    assert newer.controller_last_event_log_index == 4


def test_order_metadata_promotion_does_not_recount_same_event():
    """The pull lane may enrich a hot event with block order after its identity was persisted."""
    event_id = event_identity("4663", "P", "0xabc", 2)
    unordered = reduce(ControllerState(), Observation(
        now="t0", event_id=event_id, structure_break=True))
    assert unordered.consecutive_break_events == 1

    ordered = reduce(unordered, Observation(
        now="t1", event_id=event_id, event_block=100, event_log_index=2,
        structure_break=True))

    assert ordered.consecutive_break_events == 1
    assert ordered.structure_break_since == "t0"
    assert ordered.controller_last_event_id == event_id
    assert ordered.controller_last_event_block == 100
    assert ordered.controller_last_event_log_index == 2

    next_event = reduce(ordered, Observation(
        now="t2", event_id=event_identity("4663", "P", "0xdef", 0),
        event_block=101, event_log_index=0, structure_break=True))
    assert next_event.consecutive_break_events == 2


def test_structure_recovery_resets_break_hysteresis():
    s = reduce(ControllerState(), Observation(
        now="t0", event_id=event_identity("c", "p", "s", 0), structure_break=True))
    assert s.consecutive_break_events == 1
    s = reduce(s, Observation(
        now="t1", event_id=event_identity("c", "p", "recovery", 0), structure_recovered=True))
    assert s.consecutive_break_events == 0 and s.structure_break_since is None


# --- negative flow onset -----------------------------------------------------------------------

def test_negative_flow_since_onset_and_clear():
    s = reduce(ControllerState(), Observation(now="a", net_flow_negative=True))
    assert s.negative_flow_since == "a"
    s = reduce(s, Observation(now="b", net_flow_negative=None))     # unknown -> hold
    assert s.negative_flow_since == "a"
    s = reduce(s, Observation(now="c", net_flow_negative=False))    # positive recovery -> clear
    assert s.negative_flow_since is None


# --- route observations dedup by quote identity ------------------------------------------------

def test_route_success_sample_dedups_by_quote_identity():
    s = reduce(ControllerState(route_failure_streak=2, infra_failure_streak=3), Observation(
        now="t0", route_quote_value=1.5, route_quote_at="q@0", route_degraded=False))
    assert s.route_quote_value == 1.5 and s.route_quote_at == "q@0"
    assert s.route_failure_streak == 0                    # a good fresh quote clears the streak
    assert s.infra_failure_streak == 0                    # success clears both failure classes
    assert s.route_degradation_since is None
    # the SAME quote observed again (same cached 3 s prewarm) must not re-sample
    before = s
    s = reduce(s, Observation(now="t1", route_quote_value=1.5, route_quote_at="q@0",
                              route_degraded=True))
    assert s.route_quote_value == before.route_quote_value
    assert s.route_degradation_since is None              # dedup: same quote, no new degradation


def test_route_degradation_onset_and_failure_streaks():
    s = reduce(ControllerState(), Observation(now="t0", route_quote_value=1.0, route_quote_at="q0",
                                              route_degraded=True))
    assert s.route_degradation_since == "q0"
    # a token/route failure (no quote) advances the route streak each attempt
    s = reduce(s, Observation(now="t1", route_failure=True))
    s = reduce(s, Observation(now="t2", route_failure=True))
    assert s.route_failure_streak == 2
    # infra failures advance a SEPARATE streak
    s = reduce(s, Observation(now="t3", infra_failure=True))
    assert s.infra_failure_streak == 1 and s.route_failure_streak == 2


# --- executable peak advances atomically -------------------------------------------------------

def test_executable_peak_price_and_time_advance_together():
    s = reduce(ControllerState(), Observation(now="p0", exec_price=2.0))
    assert s.executable_peak_price == 2.0 and s.executable_peak_at == "p0"
    s = reduce(s, Observation(now="p1", exec_price=1.5))      # lower -> no advance
    assert s.executable_peak_price == 2.0 and s.executable_peak_at == "p0"
    s = reduce(s, Observation(now="p2", exec_price=3.0))      # higher -> both advance together
    assert s.executable_peak_price == 3.0 and s.executable_peak_at == "p2"


def test_noop_observation_does_not_advance_controller_revision():
    state = ControllerState(controller_revision=7)
    assert reduce(state, Observation(now="t")) == state


def test_chain_onsets_use_event_time_while_quote_state_uses_quote_time():
    state = reduce(ControllerState(), Observation(
        now="2026-07-25T00:00:10Z", event_time="2026-07-25T00:00:01Z",
        event_id="c|p|s|0", event_block=1, event_log_index=0,
        structure_break=True, net_flow_negative=True, exec_price=2.0,
        route_quote_value=20.0, route_quote_at="2026-07-25T00:00:09Z",
        route_degraded=True))
    assert state.structure_break_since == "2026-07-25T00:00:01Z"
    assert state.negative_flow_since == "2026-07-25T00:00:01Z"
    assert state.route_degradation_since == "2026-07-25T00:00:09Z"
    assert state.executable_peak_at == "2026-07-25T00:00:09Z"


# --- predicates --------------------------------------------------------------------------------

def test_executable_no_progress_needs_peak_time_window_and_fresh_quote():
    s = ControllerState(executable_peak_at="2026-07-25T00:00:00Z", executable_peak_price=5.0)
    now = "2026-07-25T00:10:00Z"       # 600 s later
    # all present -> True
    assert executable_no_progress(s, now=now, no_progress_window_s=300, quote_fresh=True) is True
    # window not elapsed -> False
    assert executable_no_progress(s, now="2026-07-25T00:02:00Z", no_progress_window_s=300,
                                  quote_fresh=True) is False
    # stale quote -> False
    assert executable_no_progress(s, now=now, no_progress_window_s=300, quote_fresh=False) is False
    # window unregistered (None) -> False (degenerate: age alone never DERISKs)
    assert executable_no_progress(s, now=now, no_progress_window_s=None, quote_fresh=True) is False
    # no exec peak -> False
    assert executable_no_progress(ControllerState(), now=now, no_progress_window_s=300,
                                  quote_fresh=True) is False


def test_structure_cut_confirmed_two_events_or_window():
    # two decoded confirming events -> confirmed even with no registered window
    s2 = ControllerState(consecutive_break_events=2, structure_break_since="2026-07-25T00:00:00Z")
    assert structure_cut_confirmed(s2, now="2026-07-25T00:00:01Z", persistence_window_s=None) is True
    # one event, no window -> not confirmed (hysteresis holds)
    s1 = ControllerState(consecutive_break_events=1, structure_break_since="2026-07-25T00:00:00Z")
    assert structure_cut_confirmed(s1, now="2026-07-25T00:00:01Z", persistence_window_s=None) is False
    # one event but persistence window elapsed -> confirmed
    assert structure_cut_confirmed(s1, now="2026-07-25T00:05:00Z", persistence_window_s=120) is True


# --- CAS persistence ---------------------------------------------------------------------------

def test_cas_persist_winner_bumps_revision_and_writes():
    c = _db()
    pid = _open(c)
    won = persist_controller_state_cas(c, pid, 0, consecutive_break_events=3,
                                       structure_break_since="2026-07-25T00:00:00Z",
                                       executable_peak_price=9.0, executable_peak_at="pk")
    assert won is True
    row = c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row["controller_revision"] == 1
    assert row["consecutive_break_events"] == 3
    assert row["executable_peak_price"] == 9.0 and row["executable_peak_at"] == "pk"


def test_cas_persist_loser_leaves_row_untouched():
    c = _db()
    pid = _open(c)
    assert persist_controller_state_cas(c, pid, 0, route_failure_streak=1) is True   # rev -> 1
    # a stale writer that still believes rev==0 must LOSE and change nothing
    lost = persist_controller_state_cas(c, pid, 0, route_failure_streak=99)
    assert lost is False
    row = c.execute("SELECT controller_revision, route_failure_streak FROM memecoin_positions "
                    "WHERE id=?", (pid,)).fetchone()
    assert row["controller_revision"] == 1 and row["route_failure_streak"] == 1


def test_cas_persist_rejects_unknown_field():
    c = _db()
    pid = _open(c)
    with pytest.raises(ValueError, match="unknown controller-state"):
        persist_controller_state_cas(c, pid, 0, controller_revision=5)   # CAS-managed, not writable


# --- byte-identical restart reconstruction (both pull and hot contracts) ------------------------

def _drive(state, observations, *, persist=None):
    """Fold observations through the reducer. If `persist` (conn, pid) is given, round-trip the
    state through the DB after EACH step — modelling a restart between every tick. The result must
    be identical to the in-memory fold: reconstruction is byte-identical by construction."""
    for obs in observations:
        state = reduce(state, obs)
        if persist is not None:
            conn, pid = persist
            assert persist_controller_state_cas(conn, pid, state.controller_revision - 1,
                                                **state.to_fields())
            state = controller_state_from_row(
                conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    return state


def test_restart_reconstruction_is_byte_identical():
    eid = event_identity("solana", "P", "SIG", 0)
    obs = [
        Observation(now="t0", exec_price=2.0),
        Observation(now="t1", event_id=eid, structure_break=True, net_flow_negative=True),
        Observation(now="t2", route_quote_value=1.0, route_quote_at="q1", route_degraded=True),
        Observation(now="t3", exec_price=3.0),
    ]
    in_memory = _drive(ControllerState(), obs)
    c = _db()
    pid = _open(c)
    # persist controller_revision to match the in-memory fold's revision count
    with_restart = _drive(ControllerState(), obs, persist=(c, pid))
    # revision differs (DB writes bump it); every DECISION-relevant field is identical.
    assert in_memory.to_fields() == with_restart.to_fields()


def test_pull_and_hot_contracts_reconstruct_identically():
    # There is no lane branch in the reducer: the SAME observation sequence yields the SAME state
    # whether it was produced by the 30 s pull materializer or the sub-second hot lane.
    obs = [Observation(now="t0", exec_price=2.0),
           Observation(now="t1", event_id=event_identity("c", "p", "s", 0), structure_break=True)]
    pull = _drive(ControllerState(), obs)
    hot = _drive(ControllerState(), obs)
    assert pull == hot


def test_funded_sweep_controller_writer_persists_once_per_decoded_event_and_quote():
    c = _db()
    pid = _open(c)
    memedb.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0,
                       notional=10.0, fill_source="chain", fraction=1.0, created_at="t")
    pos = dict(c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    trusted = {name: Provenance("pool_ticks", quality="trusted")
               for name in ("price", "flow")}

    def state(event_id):
        return MarketState(
            chain_id="4663", mint="M", pool="p", position_id=pid,
            engine_version="exit-brain-v2", feature_version="v4",
            state=StateLabel.TRENDING, latest_event_id=event_id,
            source_event_time="2026-07-25T00:00:00Z",
            nominal_price=0.8, prior_low=1.0, net_flow_quote="-1",
            required_families=("price", "flow"), provenance=trusted)

    preview = {"expected_usdc": 12.0, "amount_atomic": 10,
               "quoted_at": "2026-07-25T00:00:00Z"}
    first = runner._reduce_engine_controller(
        c, pos, state("4663|p|tx1|0"), preview, "2026-07-25T00:00:01Z")
    assert first.controller_revision == 1
    assert first.consecutive_break_events == 1
    assert first.executable_peak_price == 1.2
    assert first.route_quote_value == 12.0

    # Same state + exact same quote is a no-op: revision stays stable, so an execution intent is not
    # continuously superseded by the 30-second sweep.
    again = runner._reduce_engine_controller(
        c, pos, state("4663|p|tx1|0"), preview, "2026-07-25T00:00:02Z")
    assert again.controller_revision == 1

    second = runner._reduce_engine_controller(
        c, pos, state("4663|p|tx2|0"), preview, "2026-07-25T00:00:03Z")
    assert second.controller_revision == 2
    assert second.consecutive_break_events == 2


def test_hot_controller_handoff_persists_same_slot_confirmations_for_funded_recompute():
    c = _db()
    pid = _open(c)
    hot = ControllerState(
        controller_revision=2, controller_last_event_id="solana|p|sigB|0",
        controller_last_event_block=20, controller_last_event_log_index=0,
        consecutive_break_events=2, structure_break_since="2026-07-25T00:00:01Z",
        negative_flow_since="2026-07-25T00:00:01Z")

    assert runner.persist_hot_controller_state(c, pid, hot) is True
    persisted = controller_state_from_row(memedb.get_position(c, pid))
    assert persisted.consecutive_break_events == 2
    assert persisted.controller_last_event_id == "solana|p|sigB|0"
    assert runner.persist_hot_controller_state(c, pid, hot) is False
    alternate = ControllerState(
        controller_revision=3, controller_last_event_id="solana|p|sigA|0",
        controller_last_event_block=20, controller_last_event_log_index=0,
        consecutive_break_events=2)
    assert runner.persist_hot_controller_state(c, pid, alternate) is False


def test_hot_controller_handoff_reloads_after_quote_only_cas_race(monkeypatch):
    c = _db()
    pid = _open(c)
    hot = ControllerState(
        controller_revision=2, controller_last_event_id="solana|p|sigB|0",
        controller_last_event_block=20, controller_last_event_log_index=0,
        consecutive_break_events=2, structure_break_since="2026-07-25T00:00:01Z",
        negative_flow_since="2026-07-25T00:00:01Z")
    original = memedb.persist_controller_state_cas
    calls = {"n": 0}

    def race_once(conn, position_id, expected_revision, **fields):
        calls["n"] += 1
        if calls["n"] == 1:
            assert original(
                conn, position_id, expected_revision,
                route_quote_value=17.0, route_quote_at="2026-07-25T00:00:02Z")
            return False
        return original(conn, position_id, expected_revision, **fields)

    monkeypatch.setattr(memedb, "persist_controller_state_cas", race_once)
    assert runner.persist_hot_controller_state(c, pid, hot) is True
    persisted = controller_state_from_row(memedb.get_position(c, pid))
    assert persisted.consecutive_break_events == 2
    assert persisted.controller_last_event_id == "solana|p|sigB|0"
    assert persisted.route_quote_value == 17.0
    assert calls["n"] == 2


def test_hot_controller_handoff_reports_exhausted_cas_contention(monkeypatch):
    c = _db()
    pid = _open(c)
    hot = ControllerState(
        controller_last_event_id="solana|p|sigB|0",
        controller_last_event_block=20, controller_last_event_log_index=0,
        consecutive_break_events=2)
    monkeypatch.setattr(memedb, "persist_controller_state_cas", lambda *_a, **_k: False)
    assert runner.persist_hot_controller_state(c, pid, hot) is None
