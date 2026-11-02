import sqlite3

import pytest

from sniper.research.memecoin_opportunity.paper_adapter import (
    PaperFillRequest,
    append_paper_fill_receipt,
    append_paper_fill_request,
)
from sniper.research.memecoin_opportunity.paper_lifecycle import (
    EXECUTION_HAIRCUT_BPS,
    LIFECYCLE_VERSION,
    QuoteDeferred,
    QuoteRejected,
    activate_paper_lifecycle,
    drive_paper_lifecycle,
    realistic_result_summary,
    _append_observation,
)
from sniper.research.memecoin_opportunity import paper_lifecycle
from sniper.research.memecoin_opportunity.paper_completion import PaperQuoteCompletion
from sniper.research.memecoin_opportunity.store import connect


def _entry(conn, request_id, *, receipt_at, token="TokenMint",
           rule_version="pump-confirmation-truth-v6",
           network_id="solana", venue_id="pumpfun_solana",
           token_quantity=1_100):
    append_paper_fill_request(
        conn,
        PaperFillRequest(
            request_id=request_id,
            alert_id=f"alert-{request_id}",
            launch_id=f"launch-{request_id}",
            network_id=network_id,
            venue_id=venue_id,
            token_id=token,
            creator_id=None,
            launch_mode="standard",
            state_fingerprint=f"state-{request_id}",
            rule_version=rule_version,
            evaluator_version="alert-outcome-v4",
            adapter_version="radar-paper-adapter-v1",
            fill_model_version="route-floor-v1",
            clip_usd_micros=25_000_000,
            decision_at=receipt_at,
            decision_age_ms=10_000,
            intent_at=receipt_at,
            expires_at="2026-08-06T00:10:00Z",
            local_qty_atomic=token_quantity,
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
            "quote_in_atomic": 25_000_000,
            "quote_in_decimals": 6,
            "quote_out_asset": token,
            "quote_out_decimals": 6,
            "expected_out_atomic": token_quantity,
            "floor_out_atomic": max(1, token_quantity - 100),
            "slippage_bps": 300,
            "route_binding": {"route_plan": [{}]},
            "quote_payload": {"routePlan": [{}]},
        },
    )


def _quote(value):
    def run(request):
        return {
            "quote_provider": "jupiter_quote_v3",
            "quote_in_asset": request["token_id"],
            "quote_in_atomic": request["quantity_atomic"],
            "quote_in_decimals": 6,
            "quote_out_asset": request["quote_asset"],
            "quote_out_decimals": 6,
            "expected_out_atomic": value,
            "floor_out_atomic": value,
            "slippage_bps": 300,
            "route_binding": {"route_plan": [{}]},
            "quote_payload": {"routePlan": [{}]},
            "quote_started_at": request["now_iso"],
            "quote_completed_at": request["now_iso"],
        }

    return run


def test_activation_is_forward_only_and_does_not_backfill_old_entries(tmp_path):
    conn = connect(tmp_path / "op.db")
    _entry(conn, "old", receipt_at="2026-08-06T00:00:00Z")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:01:00Z")

    assert drive_paper_lifecycle(
        conn, quote_fn=_quote(30_000_000), now_iso="2026-08-06T00:02:00Z"
    ) == {"eligible": 0, "quoted": 0, "completed": 0, "censored": 0, "extra": 0}
    assert realistic_result_summary(conn)["valid_results"] == 0


def test_exit_quote_accepts_a_token_quantity_above_sqlite_integer_range(tmp_path):
    conn = connect(tmp_path / "op.db")
    quantity = 2**80
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(
        conn,
        "large-token",
        receipt_at="2026-08-06T00:00:01Z",
        token_quantity=quantity,
    )

    counters = drive_paper_lifecycle(
        conn,
        quote_fn=_quote(25_000_000),
        now_iso="2026-08-06T00:00:06Z",
    )

    row = conn.execute(
        "SELECT quote_in_atomic,typeof(quote_in_atomic) "
        "FROM op_paper_exit_observations"
    ).fetchone()
    assert counters == {"eligible": 1, "quoted": 1, "completed": 0, "censored": 0, "extra": 0}
    assert tuple(row) == (str(quantity), "text")


def test_first_executable_target_floor_closes_the_result(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "new", receipt_at="2026-08-06T00:00:01Z")

    counters = drive_paper_lifecycle(
        conn, quote_fn=_quote(30_000_000), now_iso="2026-08-06T00:00:06Z"
    )

    assert counters == {"eligible": 1, "quoted": 1, "completed": 1, "censored": 0, "extra": 0}
    assert dict(conn.execute(
        "SELECT result_code,return_bps FROM op_paper_outcomes"
    ).fetchone()) == {"result_code": "target_first", "return_bps": 1708}
    assert realistic_result_summary(conn) == {
        "lifecycle_version": LIFECYCLE_VERSION,
        "activated_at": "2026-08-06T00:00:00Z",
        "valid_results": 1,
        "target_first": 1,
        "stop_first": 0,
        "time_exits": 0,
        "censored": 0,
        "open": 0,
        "execution_haircut_bps_per_leg": EXECUTION_HAIRCUT_BPS,
        "by_arm": {
            "pump-confirmation-truth-v6": {
                "valid_results": 1,
                "average_net_return_bps": 1708,
                "target_first": 1,
                "stop_first": 0,
                "time_exits": 0,
                "censored": 0,
            }
        },
        "by_venue": {
            "pumpfun_solana": {
                "valid_results": 1,
                "average_net_return_bps": 1708,
                "target_first": 1,
                "stop_first": 0,
                "time_exits": 0,
                "censored": 0,
                "open": 0,
            },
            "pons_v1_4663": {
                "valid_results": 0,
                "average_net_return_bps": None,
                "target_first": 0,
                "stop_first": 0,
                "time_exits": 0,
                "censored": 0,
                "open": 0,
            },
        },
    }


def test_quote_after_60_seconds_is_always_a_time_exit(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "late", receipt_at="2026-08-06T00:00:01Z")

    drive_paper_lifecycle(
        conn, quote_fn=_quote(30_000_000), now_iso="2026-08-06T00:01:01Z"
    )

    row = conn.execute(
        "SELECT result_code,return_bps FROM op_paper_outcomes"
    ).fetchone()
    assert tuple(row) == ("time_exit", 1708)


def test_route_failures_are_observations_then_become_censored_at_90_seconds(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "route", receipt_at="2026-08-06T00:00:01Z")

    def unavailable(_request):
        raise QuoteRejected("no_route")

    drive_paper_lifecycle(
        conn, quote_fn=unavailable, now_iso="2026-08-06T00:00:06Z"
    )
    assert tuple(conn.execute(
        "SELECT status,failure_reason FROM op_paper_exit_observations"
    ).fetchone()) == ("unavailable", "no_route")

    counters = drive_paper_lifecycle(
        conn, quote_fn=unavailable, now_iso="2026-08-06T00:01:31Z"
    )
    assert counters["censored"] == 1
    assert tuple(conn.execute(
        "SELECT status,result_code,return_bps FROM op_paper_outcomes"
    ).fetchone()) == ("censored", "route_censored", None)


def test_pending_terminal_quote_is_censored_at_90_seconds(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "pending", receipt_at="2026-08-06T00:00:01Z")

    def pending(_request):
        raise QuoteDeferred

    counters = drive_paper_lifecycle(
        conn, quote_fn=pending, now_iso="2026-08-06T00:01:31Z"
    )

    assert counters["censored"] == 1
    assert conn.execute("SELECT result_code FROM op_paper_outcomes").fetchone()[0] \
        == "route_censored"


def test_late_completion_writes_requested_bucket_without_retroactive_outcome(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "late-bucket", receipt_at="2026-08-06T00:00:01Z")
    requests = []

    def pending(request):
        requests.append(dict(request))
        raise QuoteDeferred

    drive_paper_lifecycle(
        conn,
        quote_fn=pending,
        now_iso="2026-08-06T00:00:06Z",
    )
    request = requests[0]
    completion = PaperQuoteCompletion(
        observation_id=request["observation_id"],
        venue_id="pumpfun_solana",
        status="filled",
        economics=_quote(20_000_000)(request),
        failure_reason=None,
        requested_at="2026-08-06T00:00:06Z",
        completed_at="2026-08-06T00:00:11Z",
    )

    assert paper_lifecycle.append_bound_paper_completion(
        conn,
        request=request,
        completion=completion,
    ) is True

    row = conn.execute(
        "SELECT observation_id,scheduled_age_ms,return_bps "
        "FROM op_paper_exit_observations"
    ).fetchone()
    assert tuple(row) == (request["observation_id"], 5_000, -2_195)
    assert conn.execute("SELECT COUNT(*) FROM op_paper_outcomes").fetchone()[0] == 0


def test_bound_current_bucket_completion_keeps_v2_outcome_rule(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "current-bucket", receipt_at="2026-08-06T00:00:01Z")
    requests = []

    def pending(request):
        requests.append(dict(request))
        raise QuoteDeferred

    drive_paper_lifecycle(
        conn,
        quote_fn=pending,
        now_iso="2026-08-06T00:00:11Z",
    )
    request = requests[0]
    completion = PaperQuoteCompletion(
        observation_id=request["observation_id"],
        venue_id="pumpfun_solana",
        status="filled",
        economics=_quote(30_000_000)(request),
        failure_reason=None,
        requested_at="2026-08-06T00:00:11Z",
        completed_at="2026-08-06T00:00:11.500000Z",
    )
    paper_lifecycle.append_bound_paper_completion(
        conn,
        request=request,
        completion=completion,
    )

    counters = drive_paper_lifecycle(
        conn,
        quote_fn=lambda _request: (_ for _ in ()).throw(
            AssertionError("the stored current bucket must not quote again")
        ),
        now_iso="2026-08-06T00:00:11.600000Z",
    )

    assert counters["completed"] == 1
    assert conn.execute("SELECT result_code FROM op_paper_outcomes").fetchone()[0] \
        == "target_first"


def test_a_completion_that_lands_after_its_own_bucket_still_decides(tmp_path):
    """Regression: 63 of 63 `pons_v1_4663` positions censored while the route worked.

    Every `pons_v1_4663` quote completed after its own 5 s bucket rolled over, a median
    of 46.8 s late. The lifecycle read only the bucket that was current at tick time, so
    it never saw them, and the position rode to the 90 s censor while it held 19
    executable quotes. `route_censored` named the route for a lane latency.
    """
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "late-lands", receipt_at="2026-08-06T00:00:01Z")
    requests = []

    def pending(request):
        requests.append(dict(request))
        raise QuoteDeferred

    drive_paper_lifecycle(conn, quote_fn=pending, now_iso="2026-08-06T00:01:01Z")
    request = requests[0]
    assert request["scheduled_age_ms"] == 60_000
    paper_lifecycle.append_bound_paper_completion(
        conn,
        request=request,
        completion=PaperQuoteCompletion(
            observation_id=request["observation_id"],
            venue_id="pumpfun_solana",
            status="filled",
            economics=_quote(26_000_000)(request),
            failure_reason=None,
            requested_at="2026-08-06T00:01:01Z",
            completed_at="2026-08-06T00:01:47Z",
        ),
    )

    counters = drive_paper_lifecycle(
        conn, quote_fn=pending, now_iso="2026-08-06T00:01:31Z"
    )

    assert counters["censored"] == 0
    assert tuple(conn.execute(
        "SELECT status,result_code FROM op_paper_outcomes"
    ).fetchone()) == ("valid", "time_exit")


def test_the_earliest_crossing_decides_not_the_current_bucket(tmp_path):
    """Regression: 6 of 207 valid v2 rows scored a later bucket, all one direction.

    One position recorded `stop_first` at -8,427 bps after it had already crossed the
    +1,200 target at 10 s.
    """
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "ordering", receipt_at="2026-08-06T00:00:01Z")
    entry = conn.execute(
        "SELECT r.request_id,r.token_id,f.quote_out_decimals,f.expected_out_atomic,"
        "f.quote_in_asset,f.quote_in_atomic,f.quote_in_decimals,f.receipt_at "
        "FROM op_paper_fill_requests r JOIN op_paper_fills f USING(request_id)"
    ).fetchone()
    for bucket, value in ((10_000, 30_000_000), (15_000, 10_000_000)):
        _append_observation(
            conn,
            entry=entry,
            scheduled_age_ms=bucket,
            observed_at="2026-08-06T00:00:17Z",
            status="executable",
            economics=_quote(value)({
                "token_id": "TokenMint",
                "quantity_atomic": 1_000,
                "quote_asset": entry["quote_in_asset"],
                "now_iso": "2026-08-06T00:00:17Z",
            }),
        )

    drive_paper_lifecycle(
        conn,
        quote_fn=lambda _request: (_ for _ in ()).throw(
            AssertionError("the stored current bucket must not quote again")
        ),
        now_iso="2026-08-06T00:00:17Z",
    )

    assert tuple(conn.execute(
        "SELECT result_code,return_bps FROM op_paper_outcomes"
    ).fetchone()) == ("target_first", 1708)


def test_an_in_flight_earlier_bucket_holds_the_decision_until_terminal(tmp_path):
    """A later bucket must never decide ahead of an earlier one that has not answered.

    The 5 s quote crosses nothing, the 10 s quote is still in flight, and the 15 s quote
    crosses the stop. An early `stop_first` here would repeat the same one-directional
    error the ordering fix removes, so the scan waits at the gap. At the terminal age
    nothing else can land and the stop is the honest answer.
    """
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "gap", receipt_at="2026-08-06T00:00:01Z")
    entry = conn.execute(
        "SELECT r.request_id,r.token_id,f.quote_out_decimals,f.expected_out_atomic,"
        "f.quote_in_asset,f.quote_in_atomic,f.quote_in_decimals,f.receipt_at "
        "FROM op_paper_fill_requests r JOIN op_paper_fills f USING(request_id)"
    ).fetchone()
    for bucket, value in ((5_000, 25_600_000), (15_000, 10_000_000)):
        _append_observation(
            conn,
            entry=entry,
            scheduled_age_ms=bucket,
            observed_at="2026-08-06T00:00:17Z",
            status="executable",
            economics=_quote(value)({
                "token_id": "TokenMint",
                "quantity_atomic": 1_000,
                "quote_asset": entry["quote_in_asset"],
                "now_iso": "2026-08-06T00:00:17Z",
            }),
        )

    def pending(_request):
        raise QuoteDeferred

    held = drive_paper_lifecycle(conn, quote_fn=pending, now_iso="2026-08-06T00:00:17Z")
    assert held["completed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM op_paper_outcomes").fetchone()[0] == 0

    counters = drive_paper_lifecycle(
        conn, quote_fn=pending, now_iso="2026-08-06T00:01:31Z"
    )

    assert counters["completed"] == 1
    assert conn.execute(
        "SELECT result_code FROM op_paper_outcomes"
    ).fetchone()[0] == "stop_first"


def test_bound_rejection_is_idempotent_and_keeps_requested_bucket(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "rejected-bucket", receipt_at="2026-08-06T00:00:01Z")
    requests = []

    def pending(request):
        requests.append(dict(request))
        raise QuoteDeferred

    drive_paper_lifecycle(
        conn,
        quote_fn=pending,
        now_iso="2026-08-06T00:00:06Z",
    )
    request = requests[0]
    completion = PaperQuoteCompletion(
        observation_id=request["observation_id"],
        venue_id="pumpfun_solana",
        status="rejected",
        economics=None,
        failure_reason="no_route",
        requested_at="2026-08-06T00:00:06Z",
        completed_at="2026-08-06T00:00:11Z",
    )

    assert paper_lifecycle.append_bound_paper_completion(
        conn, request=request, completion=completion,
    ) is True
    assert paper_lifecycle.append_bound_paper_completion(
        conn, request=request, completion=completion,
    ) is False
    row = conn.execute(
        "SELECT scheduled_age_ms,status,failure_reason "
        "FROM op_paper_exit_observations"
    ).fetchone()
    assert tuple(row) == (5_000, "unavailable", "no_route")


def test_restart_recovers_an_executable_observation_without_an_outcome(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "crash", receipt_at="2026-08-06T00:00:01Z")
    entry = conn.execute(
        "SELECT r.request_id,r.token_id,f.quote_out_decimals,f.expected_out_atomic,"
        "f.quote_in_asset,f.quote_in_atomic,f.quote_in_decimals,f.receipt_at "
        "FROM op_paper_fill_requests r JOIN op_paper_fills f USING(request_id)"
    ).fetchone()
    _append_observation(
        conn,
        entry=entry,
        scheduled_age_ms=60_000,
        observed_at="2026-08-06T00:01:01Z",
        status="executable",
        economics=_quote(26_000_000)({
            "token_id": "TokenMint",
            "quantity_atomic": 1_000,
            "quote_asset": entry["quote_in_asset"],
            "now_iso": "2026-08-06T00:01:01Z",
        }),
    )

    counters = drive_paper_lifecycle(
        conn,
        quote_fn=lambda _request: (_ for _ in ()).throw(AssertionError("quoted twice")),
        now_iso="2026-08-06T00:01:01Z",
    )

    assert counters["completed"] == 1
    assert conn.execute("SELECT result_code FROM op_paper_outcomes").fetchone()[0] \
        == "time_exit"


def test_lifecycle_tables_reject_updates_and_deletes(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE op_paper_lifecycle_activations SET activated_at=?",
            ("2026-08-06T00:00:01Z",),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM op_paper_lifecycle_activations")


def test_score_ignores_the_provider_slippage_floor(tmp_path):
    """v1 scored `expected * 0.97` on both legs, so `slippageBps` set the result.

    Two runs of the same route, identical expected amounts, different provider
    thresholds, must produce the same return. A number that moves with a caller-set
    tolerance and not with the world is not a measurement.
    """
    returns = []
    for index, floor_fraction in enumerate((0.97, 0.50)):
        conn = connect(tmp_path / f"op-{index}.db")
        activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
        _entry(conn, f"floor-{index}", receipt_at="2026-08-06T00:00:01Z")

        def quote(request, fraction=floor_fraction):
            economics = _quote(30_000_000)(request)
            economics["floor_out_atomic"] = int(30_000_000 * fraction)
            economics["slippage_bps"] = int((1 - fraction) * 10_000)
            return economics

        drive_paper_lifecycle(conn, quote_fn=quote, now_iso="2026-08-06T00:00:06Z")
        returns.append(
            conn.execute("SELECT return_bps FROM op_paper_outcomes").fetchone()[0]
        )
    assert returns == [1708, 1708]


def test_exit_sells_the_expected_entry_quantity(tmp_path):
    """The selector quoted the expected quantity; the result must sell the same one."""
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "qty", receipt_at="2026-08-06T00:00:01Z")
    sold = []

    def quote(request):
        sold.append(request["quantity_atomic"])
        return _quote(30_000_000)(request)

    drive_paper_lifecycle(conn, quote_fn=quote, now_iso="2026-08-06T00:00:06Z")
    assert sold == [1_100]  # expected_out_atomic, not the 1_000 floor


def test_matched_control_runs_the_same_lifecycle_and_reports_separately(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(conn, "arm", receipt_at="2026-08-06T00:00:01Z")
    _entry(
        conn, "control", receipt_at="2026-08-06T00:00:01Z", token="OtherMint",
        rule_version="route-control-v2",
    )

    counters = drive_paper_lifecycle(
        conn, quote_fn=_quote(30_000_000), now_iso="2026-08-06T00:00:06Z"
    )

    assert counters == {"eligible": 2, "quoted": 2, "completed": 2, "censored": 0, "extra": 0}
    by_arm = realistic_result_summary(conn)["by_arm"]
    assert set(by_arm) == {"pump-confirmation-truth-v6", "route-control-v2"}
    assert by_arm["route-control-v2"]["valid_results"] == 1


def test_retired_arms_do_not_join_the_forward_cohort(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    _entry(
        conn, "v5", receipt_at="2026-08-06T00:00:01Z",
        rule_version="pump-confirmation-truth-v5",
    )

    assert drive_paper_lifecycle(
        conn, quote_fn=_quote(30_000_000), now_iso="2026-08-06T00:00:06Z"
    ) == {"eligible": 0, "quoted": 0, "completed": 0, "censored": 0, "extra": 0}


def test_lifecycle_scores_and_reports_each_launchpad_separately(tmp_path):
    conn = connect(tmp_path / "op.db")
    activate_paper_lifecycle(conn, now_iso="2026-08-06T00:00:00Z")
    # bags_robinhood_4663 retired 2026-08-19 (HISTORICAL_ONLY): it scores nothing now
    for venue, rule in (
        ("pumpfun_solana", "pump-confirmation-truth-v6"),
        ("pons_v1_4663", "pons-confirmation-v1"),
    ):
        _entry(
            conn,
            venue,
            receipt_at="2026-08-06T00:00:01Z",
            token=f"Token-{venue}",
            rule_version=rule,
            network_id="solana" if venue == "pumpfun_solana" else "4663",
            venue_id=venue,
        )

    counters = drive_paper_lifecycle(
        conn,
        quote_fn=_quote(30_000_000),
        now_iso="2026-08-06T00:00:06Z",
    )
    summary = realistic_result_summary(conn)

    assert counters["completed"] == 2
    assert set(summary["by_venue"]) == {
        "pumpfun_solana",
        "pons_v1_4663",
    }
    assert summary["by_venue"]["pumpfun_solana"]["valid_results"] == 1
    assert summary["by_venue"]["pons_v1_4663"]["target_first"] == 1
