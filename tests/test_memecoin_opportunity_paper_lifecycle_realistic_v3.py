"""Acceptance tests for the `radar-realistic-v3` on-time first-quote stop reference.

The eight approved rules (operator decision 2026-08-11):

  1. a new lifecycle and a new activation row
  2. the baseline is ONLY the scheduled five-second executable quote
  3. the baseline quote must complete before a preregistered maximum age
  4. an absent or late baseline censors the position
  5. the +1,200 bps target stays relative to the cash entry
  6. the stop is `baseline_return_bps - 800`
  7. cash return and baseline-relative movement are stored separately
  8. v3 stays paper-only

v3 owns no quotes. It scores the observation series that `radar-realistic-v2` writes for
the identical positions, so every test drives v2 to produce quotes and then drives v3.
"""
import pytest

from sniper.research.memecoin_opportunity import paper_lifecycle_realistic_v3 as v3
from sniper.research.memecoin_opportunity.paper_adapter import (
    PaperFillRequest,
    append_paper_fill_receipt,
    append_paper_fill_request,
)
from sniper.research.memecoin_opportunity.paper_lifecycle import (
    QuoteDeferred,
    activate_paper_lifecycle,
    drive_paper_lifecycle,
)
from sniper.research.memecoin_opportunity.store import connect

RECEIPT_AT = "2026-08-12T00:00:00Z"
ENTRY_ATOMIC = 25_000_000
TOKEN_QTY = 1_100


@pytest.fixture(autouse=True)
def _preregistered(monkeypatch):
    """Every test but the fail-closed one runs with the baseline age preregistered."""
    monkeypatch.setattr(v3, "MAX_BASELINE_AGE_MS", 10_000)


def _ready(tmp_path, *, activate_v3=True):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso=RECEIPT_AT)
    if activate_v3:
        v3.activate_realistic_v3(conn, now_iso=RECEIPT_AT, authorised=True)
    return conn


def _entry(conn, request_id, *, receipt_at=RECEIPT_AT):
    append_paper_fill_request(
        conn,
        PaperFillRequest(
            request_id=request_id,
            alert_id=f"alert-{request_id}",
            launch_id=f"launch-{request_id}",
            network_id="solana",
            venue_id="pumpfun_solana",
            token_id="TokenMint",
            creator_id=None,
            launch_mode="standard",
            state_fingerprint=f"state-{request_id}",
            rule_version="pump-confirmation-truth-v6",
            evaluator_version="alert-outcome-v4",
            adapter_version="radar-paper-adapter-v1",
            fill_model_version="route-floor-v1",
            clip_usd_micros=25_000_000,
            decision_at=receipt_at,
            decision_age_ms=10_000,
            intent_at=receipt_at,
            expires_at="2026-08-12T00:10:00Z",
            local_qty_atomic=TOKEN_QTY,
        ),
    )
    append_paper_fill_receipt(
        conn,
        request_id=request_id,
        status="filled",
        now_iso=receipt_at,
        quote_started_at=receipt_at,
        quote_completed_at=receipt_at,
        economics={
            "quote_provider": "jupiter_quote_v3",
            "quote_in_asset": "So11111111111111111111111111111111111111112",
            "quote_in_atomic": ENTRY_ATOMIC,
            "quote_in_decimals": 6,
            "quote_out_asset": "TokenMint",
            "quote_out_decimals": 6,
            "expected_out_atomic": TOKEN_QTY,
            "floor_out_atomic": TOKEN_QTY - 100,
            "slippage_bps": 300,
            "route_binding": {"route_plan": [{}]},
            "quote_payload": {"routePlan": [{}]},
        },
    )


def _at(seconds):
    return f"2026-08-12T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def _quote(return_bps, *, completed_at=None):
    """A quote whose scored round trip lands on `return_bps` against the cash entry."""
    def run(request):
        scale = 10_000 - v3.EXECUTION_HAIRCUT_BPS
        target_scored = ENTRY_ATOMIC * (10_000 + return_bps) // 10_000
        expected = target_scored * 10_000 * 10_000 // (scale * scale) + 1
        return {
            "quote_provider": "jupiter_quote_v3",
            "quote_in_asset": request["token_id"],
            "quote_in_atomic": request["quantity_atomic"],
            "quote_in_decimals": 6,
            "quote_out_asset": request["quote_asset"],
            "quote_out_decimals": 6,
            "expected_out_atomic": expected,
            "floor_out_atomic": expected,
            "slippage_bps": 300,
            "route_binding": {"route_plan": [{}]},
            "quote_payload": {"routePlan": [{}]},
            "quote_started_at": request["now_iso"],
            "quote_completed_at": completed_at or request["now_iso"],
        }

    return run


def _tick(conn, quote_fn, second):
    """Collect the v2 observation for this bucket, then let v3 score the series."""
    drive_paper_lifecycle(conn, quote_fn=quote_fn, now_iso=_at(second))
    return v3.drive_realistic_v3(conn, now_iso=_at(second))


def _outcome(conn, request_id, lifecycle=v3.LIFECYCLE_VERSION):
    row = conn.execute(
        "SELECT status,result_code,return_bps,baseline_return_bps,baseline_move_bps,"
        "exit_reason_codes_json FROM op_paper_outcomes "
        "WHERE request_id=? AND lifecycle_version=?",
        (request_id, lifecycle),
    ).fetchone()
    return dict(row) if row is not None else None


# -- rule 1 -------------------------------------------------------------------

def test_activation_writes_its_own_row_beside_v2(tmp_path):
    conn = _ready(tmp_path)
    versions = {
        row[0]
        for row in conn.execute(
            "SELECT lifecycle_version FROM op_paper_lifecycle_activations"
        )
    }
    assert versions == {"radar-realistic-v2", "radar-realistic-v3"}


def test_activation_is_forward_only(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-11T23:00:00Z")
    _entry(conn, "old", receipt_at="2026-08-11T23:30:00Z")
    v3.activate_realistic_v3(conn, now_iso=RECEIPT_AT, authorised=True)
    assert v3.drive_realistic_v3(conn, now_iso=_at(30))["eligible"] == 0
    assert _outcome(conn, "old") is None


# -- rules 2, 3 and 4 ---------------------------------------------------------

def test_activation_fails_closed_until_the_sampler_sets_the_baseline_age(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(v3, "MAX_BASELINE_AGE_MS", None)
    conn = connect(tmp_path / "op.db")
    with pytest.raises(v3.BaselineAgeNotPreregistered):
        v3.activate_realistic_v3(conn, now_iso=RECEIPT_AT, authorised=True)


def test_a_late_baseline_censors_and_never_rebases(tmp_path):
    """A first quote past the maximum age must not erase a loss already taken."""
    conn = _ready(tmp_path)
    _entry(conn, "late")
    _tick(conn, _quote(-700, completed_at="2026-08-12T00:00:30Z"), 5)
    _tick(conn, _quote(-700), 95)
    outcome = _outcome(conn, "late")
    assert outcome["status"] == "censored"
    assert outcome["exit_reason_codes_json"] == '["baseline_late"]'
    assert outcome["return_bps"] is None


def test_an_absent_baseline_censors_after_the_terminal_age(tmp_path):
    conn = _ready(tmp_path)
    _entry(conn, "absent")

    def deferred(_request):
        raise QuoteDeferred

    _tick(conn, deferred, 5)
    assert _outcome(conn, "absent") is None
    _tick(conn, _quote(-300), 20)
    assert _outcome(conn, "absent") is None
    _tick(conn, _quote(-300), 95)
    outcome = _outcome(conn, "absent")
    assert outcome["status"] == "censored"
    assert outcome["exit_reason_codes_json"] == '["baseline_absent"]'


def test_only_the_five_second_bucket_can_be_the_baseline(tmp_path):
    """A 10 s quote is not a baseline even when no 5 s quote exists."""
    conn = _ready(tmp_path)
    _entry(conn, "tenonly")

    def only_after_five(request):
        if request["scheduled_age_ms"] == 5_000:
            raise QuoteDeferred
        return _quote(-2_000)(request)

    for second in (5, 10, 20, 95):
        _tick(conn, only_after_five, second)
    outcome = _outcome(conn, "tenonly")
    assert outcome["status"] == "censored"
    assert outcome["exit_reason_codes_json"] == '["baseline_absent"]'


# -- rules 5, 6 and 7 ---------------------------------------------------------

def test_the_cost_floor_alone_does_not_stop_the_position(tmp_path):
    """The defect v3 exists to fix: -850 bps of round-trip cost is not a -800 stop."""
    conn = _ready(tmp_path)
    _entry(conn, "floor")
    _tick(conn, _quote(-850), 5)
    assert _outcome(conn, "floor", "radar-realistic-v2")["result_code"] == "stop_first"
    assert _outcome(conn, "floor") is None
    _tick(conn, _quote(-850), 60)
    outcome = _outcome(conn, "floor")
    assert outcome["result_code"] == "time_exit"
    assert outcome["baseline_move_bps"] == pytest.approx(0, abs=3)


def test_the_stop_is_measured_from_the_baseline(tmp_path):
    conn = _ready(tmp_path)
    _entry(conn, "stopped")
    _tick(conn, _quote(-700), 5)
    _tick(conn, _quote(-1_450), 10)   # 750 bps below the baseline: still holding
    assert _outcome(conn, "stopped") is None
    _tick(conn, _quote(-1_550), 15)   # 850 bps below the baseline: stop
    outcome = _outcome(conn, "stopped")
    assert outcome["result_code"] == "stop_first"
    assert outcome["return_bps"] == pytest.approx(-1_550, abs=2)
    assert outcome["baseline_return_bps"] == pytest.approx(-700, abs=2)
    assert outcome["baseline_move_bps"] == pytest.approx(-850, abs=3)


def test_the_target_stays_relative_to_the_cash_entry(tmp_path):
    """A +1,200 move off a -700 baseline is +500 cash. That is not a target."""
    conn = _ready(tmp_path)
    _entry(conn, "target")
    _tick(conn, _quote(-700), 5)
    _tick(conn, _quote(500), 10)
    assert _outcome(conn, "target") is None
    _tick(conn, _quote(1_250), 15)
    outcome = _outcome(conn, "target")
    assert outcome["result_code"] == "target_first"
    assert outcome["return_bps"] == pytest.approx(1_250, abs=2)
    assert outcome["baseline_move_bps"] == pytest.approx(1_950, abs=3)


def test_cash_return_and_baseline_movement_are_stored_separately(tmp_path):
    conn = _ready(tmp_path)
    _entry(conn, "columns")
    _tick(conn, _quote(-600), 5)
    _tick(conn, _quote(-200), 60)
    outcome = _outcome(conn, "columns")
    assert outcome["result_code"] == "time_exit"
    assert outcome["return_bps"] == pytest.approx(-200, abs=2)
    assert outcome["baseline_move_bps"] == pytest.approx(400, abs=3)
    assert outcome["return_bps"] != outcome["baseline_move_bps"]


# -- shared contract ----------------------------------------------------------

def test_v3_adds_no_quote_load_and_writes_no_observations(tmp_path):
    conn = _ready(tmp_path)
    _entry(conn, "isolated")
    _tick(conn, _quote(-500), 5)
    versions = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT lifecycle_version FROM op_paper_exit_observations"
        )
    }
    assert versions == {"radar-realistic-v2"}


def test_v2_keeps_collecting_after_its_own_stop_so_v3_has_a_series(tmp_path):
    conn = _ready(tmp_path)
    _entry(conn, "keeps")
    _tick(conn, _quote(-900), 5)      # v2 stops here
    _tick(conn, _quote(-400), 10)
    _tick(conn, _quote(-300), 15)
    buckets = [
        row[0]
        for row in conn.execute(
            "SELECT scheduled_age_ms FROM op_paper_exit_observations "
            "WHERE request_id='keeps' ORDER BY scheduled_age_ms"
        )
    ]
    assert buckets == [5_000, 10_000, 15_000]
    v2_rows = conn.execute(
        "SELECT COUNT(*) FROM op_paper_outcomes "
        "WHERE request_id='keeps' AND lifecycle_version='radar-realistic-v2'"
    ).fetchone()[0]
    assert v2_rows == 1


def test_one_outcome_per_position_and_the_drive_is_idempotent(tmp_path):
    conn = _ready(tmp_path)
    _entry(conn, "once")
    _tick(conn, _quote(-500), 5)
    _tick(conn, _quote(-500), 60)
    _tick(conn, _quote(-500), 65)
    v3.drive_realistic_v3(conn, now_iso=_at(70))
    count = conn.execute(
        "SELECT COUNT(*) FROM op_paper_outcomes "
        "WHERE request_id='once' AND lifecycle_version=?",
        (v3.LIFECYCLE_VERSION,),
    ).fetchone()[0]
    assert count == 1


def test_the_activation_row_records_the_preregistered_baseline_age(tmp_path):
    conn = _ready(tmp_path)
    config = conn.execute(
        "SELECT config_json FROM op_paper_lifecycle_activations WHERE lifecycle_version=?",
        (v3.LIFECYCLE_VERSION,),
    ).fetchone()[0].replace(" ", "")
    assert '"max_baseline_age_ms":10000' in config
    assert '"stop_reference":"on_time_first_quote"' in config
    assert '"paper_only":true' in config


# --- activation authority and contract drift -------------------------------------------

def test_the_preregistered_integer_ships_without_opening_the_cohort(tmp_path):
    """Setting MAX_BASELINE_AGE_MS used to BE the activation.

    The worker attempts activation on every start, so shipping the measured integer
    opened an immutable cohort with no separate operator step. The control restores it.
    """
    from sniper.research.memecoin_opportunity import paper_lifecycle_realistic_v3 as v3
    from sniper.research.memecoin_opportunity.store import connect

    conn = connect(tmp_path / "op.db")
    # the autouse fixture pins a preregistered value for this file; the production
    # integer is pinned separately in tests/test_radar_v3_worker_wiring.py
    assert v3.MAX_BASELINE_AGE_MS is not None, "the integer is preregistered"
    assert v3.activation_authorised({}) is False, "the control is DEFAULT OFF"

    with pytest.raises(v3.ActivationNotAuthorised):
        v3.activate_realistic_v3(conn, now_iso="2026-08-19T20:00:00Z", authorised=False)
    assert conn.execute(
        "SELECT COUNT(*) FROM op_paper_lifecycle_activations "
        "WHERE lifecycle_version='radar-realistic-v3'"
    ).fetchone()[0] == 0, "no cohort row may exist while the control is off"


def test_activation_is_one_way_and_survives_the_control_going_back_off(tmp_path):
    from sniper.research.memecoin_opportunity import paper_lifecycle_realistic_v3 as v3
    from sniper.research.memecoin_opportunity.store import connect

    conn = connect(tmp_path / "op.db")
    v3.activate_realistic_v3(conn, now_iso="2026-08-19T20:00:00Z", authorised=True)
    # control back OFF: an existing cohort must keep running, not close or re-gate
    assert v3.activate_realistic_v3(
        conn, now_iso="2026-08-19T21:00:00Z", authorised=False
    ) == "radar-realistic-v3"
    assert conn.execute(
        "SELECT COUNT(*) FROM op_paper_lifecycle_activations "
        "WHERE lifecycle_version='radar-realistic-v3'"
    ).fetchone()[0] == 1, "activation is one-way and writes exactly one row"


def test_a_changed_constant_conflicts_with_the_stored_cohort_contract(tmp_path, monkeypatch):
    """The row is immutable; the module constants are not.

    Without this check the activation row could claim one contract while the scorer used
    another: stored max 10,000 against a runtime 15,000, with no conflict raised.
    """
    from sniper.research.memecoin_opportunity import paper_lifecycle_realistic_v3 as v3
    from sniper.research.memecoin_opportunity.store import ImmutableConflict, connect

    conn = connect(tmp_path / "op.db")
    v3.activate_realistic_v3(conn, now_iso="2026-08-19T20:00:00Z", authorised=True)
    stored = v3.stored_config(conn)
    opened_with = stored["max_baseline_age_ms"]

    # move the constant AFTER the cohort was opened
    monkeypatch.setattr(v3, "MAX_BASELINE_AGE_MS", opened_with + 5_000)
    with pytest.raises(ImmutableConflict, match="max_baseline_age_ms"):
        v3.activate_realistic_v3(conn, now_iso="2026-08-19T21:00:00Z", authorised=True)


def test_the_scorer_uses_the_stored_contract_not_the_current_constants(tmp_path, monkeypatch):
    """A live cohort must be scored by the thresholds it was opened with."""
    from sniper.research.memecoin_opportunity import paper_lifecycle_realistic_v3 as v3

    stored = {"exit_age_ms": 60_000, "target_bps": 1_200, "stop_move_bps": -800}
    # move the module constants far away; the stored contract must still decide
    monkeypatch.setattr(v3, "TARGET_BPS", 1)
    monkeypatch.setattr(v3, "STOP_MOVE_BPS", -1)
    monkeypatch.setattr(v3, "EXIT_AGE_MS", 1)

    assert v3._result_code(10_000, 500, -100, stored) is None, "no rail hit yet"
    assert v3._result_code(10_000, 1_200, -100, stored) == "target_first"
    assert v3._result_code(10_000, 0, -800, stored) == "stop_first"
    assert v3._result_code(60_000, 0, 0, stored) == "time_exit"
