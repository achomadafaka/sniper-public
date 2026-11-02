import pytest

from sniper.research.memecoin_opportunity.paper_adapter import (
    PaperFillRequest,
    append_paper_fill_receipt,
    append_paper_fill_request,
)
from sniper.research.memecoin_opportunity.paper_lifecycle_v3 import (
    ADAPTIVE_ARMS,
    LIFECYCLE_V3,
    activate_adaptive_lifecycle,
    adaptive_result_summary,
    drive_adaptive_lifecycle,
)
from sniper.research.memecoin_opportunity.store import connect


@pytest.fixture
def radar_conn(tmp_path):
    return connect(tmp_path / "radar.db")


def _entry(
    conn,
    request_id,
    *,
    receipt_at="2026-08-11T00:00:01Z",
    rule_version="pump-confirmation-truth-v6",
    venue_id="pumpfun_solana",
    network_id="solana",
    token_quantity=1_100,
):
    append_paper_fill_request(
        conn,
        PaperFillRequest(
            request_id=request_id,
            alert_id=f"alert-{request_id}",
            launch_id=f"launch-{request_id}",
            network_id=network_id,
            venue_id=venue_id,
            token_id=f"token-{request_id}",
            creator_id=None,
            launch_mode="standard",
            state_fingerprint=f"state-{request_id}",
            rule_version=rule_version,
            evaluator_version="alert-outcome-v4",
            adapter_version="radar-paper-adapter-v1",
            fill_model_version="route-floor-v1",
            clip_usd_micros=25_000_000,
            decision_at=receipt_at,
            decision_age_ms=20_000,
            intent_at=receipt_at,
            expires_at="2026-08-11T00:10:00Z",
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
            "quote_provider": "test",
            "quote_in_asset": "USDC",
            "quote_in_atomic": 25_000_000,
            "quote_in_decimals": 6,
            "quote_out_asset": f"token-{request_id}",
            "quote_out_decimals": 6,
            "expected_out_atomic": token_quantity,
            "floor_out_atomic": token_quantity,
            "slippage_bps": 300,
            "route_binding": {"pool": "pool-1"},
            "quote_payload": {"route": "pool-1"},
        },
    )
    return request_id


@pytest.fixture
def filled_entry(radar_conn):
    return _entry(radar_conn, "entry-1")


def _quote_at(full_out):
    def quote(request):
        quantity = int(request["quantity_atomic"])
        expected = full_out * quantity // 1_100
        return {
            "quote_provider": "test",
            "quote_in_asset": request["token_id"],
            "quote_in_atomic": quantity,
            "quote_in_decimals": 6,
            "quote_out_asset": "USDC",
            "quote_out_decimals": 6,
            "expected_out_atomic": expected,
            "floor_out_atomic": expected,
            "slippage_bps": 300,
            "route_binding": {"pool": "pool-1"},
            "quote_payload": {"route": "pool-1"},
            "quote_started_at": request["now_iso"],
            "quote_completed_at": request["now_iso"],
        }

    return quote


@pytest.fixture
def rising_quotes():
    return _quote_at(30_000_000)


@pytest.fixture
def two_tranche_quotes():
    return _quote_at(30_000_000)


@pytest.fixture
def stop_quotes():
    return _quote_at(20_000_000)


@pytest.fixture
def followup_quotes():
    return _quote_at(22_000_000)


@pytest.fixture
def v2_outcome(radar_conn):
    outcome_id = "v2-outcome"
    radar_conn.execute(
        "INSERT INTO op_paper_outcomes ("
        "outcome_id,request_id,lifecycle_version,status,result_code,"
        "entry_quote_atomic,return_bps,outcome_at,row_fingerprint,inserted_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            outcome_id,
            "v2-request",
            "radar-realistic-v2",
            "valid",
            "stop_first",
            "25000000",
            -800,
            "2026-08-11T00:00:00Z",
            "v2-fingerprint",
            "2026-08-11T00:00:00Z",
        ),
    )
    return outcome_id


def test_the_arms_are_the_v3_rules_and_the_baseline_control():
    assert "pump-adaptive-v3" in ADAPTIVE_ARMS
    assert "pump-confirmation-truth-v6" in ADAPTIVE_ARMS


def test_activation_is_idempotent(radar_conn):
    first = activate_adaptive_lifecycle(
        radar_conn, now_iso="2026-08-11T00:00:00Z",
    )
    second = activate_adaptive_lifecycle(
        radar_conn, now_iso="2026-08-11T01:00:00Z",
    )
    assert first == second == LIFECYCLE_V3
    assert radar_conn.execute(
        "SELECT COUNT(*) FROM op_paper_lifecycle_activations "
        "WHERE lifecycle_version=?",
        (LIFECYCLE_V3,),
    ).fetchone()[0] == 1


def test_activation_does_not_backfill_an_old_fill(radar_conn):
    old = _entry(
        radar_conn, "old-entry", receipt_at="2026-08-10T23:59:59Z",
    )
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    drive_adaptive_lifecycle(
        radar_conn,
        quote_fn=_quote_at(30_000_000),
        observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:10Z",
    )
    assert radar_conn.execute(
        "SELECT COUNT(*) FROM op_paper_position_states WHERE request_id=?", (old,),
    ).fetchone()[0] == 0


def test_a_take_profit_writes_one_action_row_and_leaves_the_position_open(
    radar_conn, filled_entry, rising_quotes,
):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    drive_adaptive_lifecycle(
        radar_conn,
        quote_fn=rising_quotes,
        observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:10Z",
    )
    actions = radar_conn.execute(
        "SELECT action,fraction_ppm FROM op_paper_position_actions "
        "WHERE request_id=?",
        (filled_entry,),
    ).fetchall()
    assert [tuple(row) for row in actions] == [("DERISK", 500_000)]
    assert radar_conn.execute(
        "SELECT COUNT(*) FROM op_paper_outcomes WHERE request_id=? "
        "AND lifecycle_version=?",
        (filled_entry, LIFECYCLE_V3),
    ).fetchone()[0] == 0


def test_the_outcome_sums_every_tranche(
    radar_conn, filled_entry, two_tranche_quotes,
):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    for tick in ("00:00:10", "00:02:30"):
        drive_adaptive_lifecycle(
            radar_conn,
            quote_fn=two_tranche_quotes,
            observation_fn=lambda *_, **__: {},
            now_iso=f"2026-08-11T{tick}Z",
        )
    row = radar_conn.execute(
        "SELECT status,result_code,return_bps,origin_kind,policy_hash "
        "FROM op_paper_outcomes WHERE request_id=? AND lifecycle_version=?",
        (filled_entry, LIFECYCLE_V3),
    ).fetchone()
    total = radar_conn.execute(
        "SELECT SUM(CAST(scored_out_atomic AS INTEGER)) "
        "FROM op_paper_position_actions WHERE request_id=?",
        (filled_entry,),
    ).fetchone()[0]
    entry_atomic = radar_conn.execute(
        "SELECT CAST(quote_in_atomic AS INTEGER) FROM op_paper_fills "
        "WHERE request_id=?",
        (filled_entry,),
    ).fetchone()[0]
    assert row["status"] == "valid"
    assert row["return_bps"] == (total - entry_atomic) * 10_000 // entry_atomic
    assert row["origin_kind"] == "radar_pump"
    assert row["policy_hash"]


def test_the_realistic_v2_rows_are_untouched(radar_conn, v2_outcome):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    drive_adaptive_lifecycle(
        radar_conn,
        quote_fn=lambda *_: None,
        observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:10Z",
    )
    assert radar_conn.execute(
        "SELECT return_bps FROM op_paper_outcomes WHERE outcome_id=?", (v2_outcome,),
    ).fetchone()[0] == -800


def test_the_summary_never_pools_two_venues(radar_conn):
    summary = adaptive_result_summary(radar_conn)
    # bags_robinhood_4663 retired 2026-08-19: a retired venue holds no result block
    assert set(summary["by_venue"]) == {
        "pumpfun_solana",
        "pons_v1_4663",
    }
    assert "average_net_return_bps" not in summary
    assert summary.get("pooled") is None


def test_a_terminal_exit_schedules_four_follow_up_horizons(
    radar_conn, filled_entry, stop_quotes,
):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    for tick in ("00:00:10", "00:00:12"):
        drive_adaptive_lifecycle(
            radar_conn,
            quote_fn=stop_quotes,
            observation_fn=lambda *_, **__: {},
            now_iso=f"2026-08-11T{tick}Z",
        )
    horizons = [
        row[0]
        for row in radar_conn.execute(
            "SELECT horizon_s FROM op_paper_exit_followups WHERE request_id=? "
            "ORDER BY horizon_s",
            (filled_entry,),
        )
    ]
    assert horizons == [30, 120, 300, 900]


def test_a_follow_up_quote_changes_no_state_and_no_return(
    radar_conn, filled_entry, stop_quotes, followup_quotes,
):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    for tick in ("00:00:10", "00:00:12"):
        drive_adaptive_lifecycle(
            radar_conn,
            quote_fn=stop_quotes,
            observation_fn=lambda *_, **__: {},
            now_iso=f"2026-08-11T{tick}Z",
        )

    def snapshot():
        return (
            radar_conn.execute(
                "SELECT return_bps FROM op_paper_outcomes WHERE request_id=? "
                "AND lifecycle_version=?",
                (filled_entry, LIFECYCLE_V3),
            ).fetchone()[0],
            tuple(radar_conn.execute(
                "SELECT COUNT(*),MAX(sequence) FROM op_paper_position_states "
                "WHERE request_id=?",
                (filled_entry,),
            ).fetchone()[0:2]),
            radar_conn.execute(
                "SELECT COUNT(*) FROM op_paper_position_actions WHERE request_id=?",
                (filled_entry,),
            ).fetchone()[0],
        )

    before = snapshot()
    drive_adaptive_lifecycle(
        radar_conn,
        quote_fn=followup_quotes,
        observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:16:00Z",
    )
    assert snapshot() == before


def test_an_open_position_uses_a_two_second_quote_cadence(
    radar_conn, filled_entry, rising_quotes,
):
    activate_adaptive_lifecycle(radar_conn, now_iso="2026-08-11T00:00:00Z")
    calls = 0

    def counted_quote(request):
        nonlocal calls
        calls += 1
        return rising_quotes(request)

    first = drive_adaptive_lifecycle(
        radar_conn,
        quote_fn=counted_quote,
        observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:10Z",
    )
    second = drive_adaptive_lifecycle(
        radar_conn,
        quote_fn=counted_quote,
        observation_fn=lambda *_, **__: {},
        now_iso="2026-08-11T00:00:11Z",
    )

    assert first["derisked"] == 1
    assert second["skipped"] == 1
    assert calls == 2
