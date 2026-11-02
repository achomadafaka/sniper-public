"""The frozen selection rule for `radar-realistic-v3`'s MAX_BASELINE_AGE_MS.

Every verdict this rule can return has a test, because the rule was frozen BEFORE any
qualifying window existed and the later commit is only allowed to substitute the integer.
If a branch here changes, the preregistration was broken.
"""
from scripts import radar_v3_baseline_age_preregistration as rule
from sniper.research.memecoin_opportunity.paper_adapter import (
    PaperFillRequest,
    append_paper_fill_receipt,
    append_paper_fill_request,
)
from sniper.research.memecoin_opportunity.paper_lifecycle import LIFECYCLE_VERSION
from sniper.research.memecoin_opportunity.paper_sampler import (
    DETAIL_VERSION,
    SOURCE_KEY,
)
from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
from sniper.research.memecoin_opportunity.store import connect

WINDOW_START = "2026-08-12T00:00:00Z"
WINDOW_END = "2026-08-13T00:00:00Z"


def _window(conn, *, complete=True, version=DETAIL_VERSION, key=SOURCE_KEY):
    upsert_runtime_status(
        conn,
        source_key=key,
        status="healthy",
        blocker_code=None,
        heartbeat_at=WINDOW_END,
        detail={
            "detail_version": version,
            "window_started_at": WINDOW_START,
            "window_ends_at": WINDOW_END,
            "window_complete": complete,
        },
    )


def _position(conn, index, *, baseline_latency_ms, receipt_at="2026-08-12T01:00:00Z"):
    """One filled Pump position, with or without its 5 s executable observation."""
    request_id = f"req-{index}"
    append_paper_fill_request(
        conn,
        PaperFillRequest(
            request_id=request_id,
            alert_id=f"alert-{index}",
            launch_id=f"launch-{index}",
            network_id="solana",
            venue_id="pumpfun_solana",
            token_id="TokenMint",
            creator_id=None,
            launch_mode="standard",
            state_fingerprint=f"state-{index}",
            rule_version="pump-confirmation-truth-v6",
            evaluator_version="alert-outcome-v4",
            adapter_version="radar-paper-adapter-v1",
            fill_model_version="route-floor-v1",
            clip_usd_micros=25_000_000,
            decision_at=receipt_at,
            decision_age_ms=10_000,
            intent_at=receipt_at,
            expires_at="2026-08-12T02:00:00Z",
            local_qty_atomic=1_100,
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
            "quote_out_asset": "TokenMint",
            "quote_out_decimals": 6,
            "expected_out_atomic": 1_100,
            "floor_out_atomic": 1_000,
            "slippage_bps": 300,
            "route_binding": {"route_plan": [{}]},
            "quote_payload": {"routePlan": [{}]},
        },
    )
    if baseline_latency_ms is None:
        return
    completed = rule._parse(receipt_at) + rule.timedelta(milliseconds=baseline_latency_ms)
    conn.execute(
        "INSERT INTO op_paper_exit_observations ("
        "observation_id,request_id,lifecycle_version,scheduled_age_ms,status,"
        "quote_completed_at,observed_at,return_bps,row_fingerprint,inserted_at"
        ") VALUES (?,?,?,?,'executable',?,?,?,?,?)",
        (
            f"obs-{index}",
            request_id,
            LIFECYCLE_VERSION,
            rule.BASELINE_AGE_MS,
            completed.isoformat().replace("+00:00", "Z"),
            receipt_at,
            -681,
            f"fp-{index}",
            receipt_at,
        ),
    )


def _ready(tmp_path, latencies, **window):
    conn = connect(tmp_path / "op.db")
    _window(conn, **window)
    for index, latency in enumerate(latencies):
        _position(conn, index, baseline_latency_ms=latency)
    return conn


def test_no_window_yet(tmp_path):
    conn = connect(tmp_path / "op.db")
    assert rule.evaluate(conn)["verdict"] == "NO WINDOW"


def test_a_sealed_v1_window_cannot_set_the_number(tmp_path):
    """v1 measured the load before v3 kept quoting decided positions."""
    conn = connect(tmp_path / "op.db")
    _window(conn, version="paper-sampler-v1", key="opportunity:paper_sampler")
    assert rule.evaluate(conn)["verdict"] == "NO WINDOW"
    _window(conn, version="paper-sampler-v1")
    assert rule.evaluate(conn)["verdict"] == "WRONG WINDOW"


def test_an_open_window_cannot_set_the_number(tmp_path):
    conn = _ready(tmp_path, [4_000] * 30, complete=False)
    assert rule.evaluate(conn)["verdict"] == "WINDOW OPEN"


def test_rule_5_needs_twenty_five_eligible_positions(tmp_path):
    conn = _ready(tmp_path, [4_000] * 24)
    result = rule.evaluate(conn)
    assert result["verdict"] == "TOO FEW POSITIONS"
    assert result["eligible"] == 24


def test_rule_4_drops_a_receipt_the_window_cut_short(tmp_path):
    """A position without a full 90 s of terminal age inside the window is not eligible."""
    conn = _ready(tmp_path, [4_000] * 25)
    _position(conn, 99, baseline_latency_ms=4_000, receipt_at="2026-08-12T23:59:59Z")
    assert rule.evaluate(conn)["eligible"] == 25


def test_rule_10_rejects_coverage_below_ninety_percent(tmp_path):
    conn = _ready(tmp_path, [4_000] * 26 + [None] * 4)
    result = rule.evaluate(conn)
    assert result["coverage_bps"] == 8_667
    assert result["verdict"] == "DO NOT ACTIVATE"
    assert "coverage" in result["reason"]


def test_coverage_of_exactly_ninety_percent_passes(tmp_path):
    conn = _ready(tmp_path, [4_000] * 27 + [None] * 3)
    result = rule.evaluate(conn)
    assert result["coverage_bps"] == 9_000
    assert result["verdict"] == "ACTIVATE"


def test_rule_7_counts_a_missing_baseline_as_infinite(tmp_path):
    """The missing baselines stay in the DENOMINATOR and push p90 up the tail.

    76 fast, 5 slow, 9 missing. Over all 90 positions p90 is the slow 9,000 ms. Drop the
    missing rows and p90 collapses to 1,000 ms -- the providers that answered, reported
    as if they were the coverage.
    """
    conn = _ready(tmp_path, [1_000] * 76 + [9_000] * 5 + [None] * 9)
    result = rule.evaluate(conn)
    assert result["coverage_bps"] == 9_000
    assert result["p90_ms"] == 9_000
    assert rule._p90([1_000.0] * 76 + [9_000.0] * 5) == 1_000
    assert result["verdict"] == "ACTIVATE"
    assert result["max_baseline_age_ms"] == 9_000


def test_rule_12_refuses_a_p90_above_the_hard_maximum(tmp_path):
    conn = _ready(tmp_path, [4_000] * 20 + [16_000] * 10)
    result = rule.evaluate(conn)
    assert result["max_baseline_age_ms"] == 16_000
    assert result["verdict"] == "DO NOT ACTIVATE"
    assert "hard maximum" in result["reason"]


def test_rule_9_rounds_up_to_the_next_whole_second(tmp_path):
    conn = _ready(tmp_path, [1_000] * 26 + [7_100] * 4)
    result = rule.evaluate(conn)
    assert result["p90_ms"] == 7_100
    assert result["max_baseline_age_ms"] == 8_000
    assert result["verdict"] == "ACTIVATE"


def test_a_qualifying_window_activates(tmp_path):
    conn = _ready(tmp_path, [2_000] * 30)
    result = rule.evaluate(conn)
    assert result["verdict"] == "ACTIVATE"
    assert result["max_baseline_age_ms"] == 2_000
    assert result["eligible"] == 30
    assert result["coverage_bps"] == 10_000


def test_the_hard_maximum_is_fifteen_seconds_and_the_floor_is_ninety_percent(tmp_path):
    """The frozen constants themselves, so a later edit cannot move them quietly."""
    assert rule.HARD_MAX_BASELINE_AGE_MS == 15_000
    assert rule.MIN_ELIGIBLE_POSITIONS == 25
    assert rule.MIN_COVERAGE_BPS == 9_000
    assert rule.ROUNDING_MS == 1_000
    assert rule.BASELINE_AGE_MS == 5_000
    assert rule.REQUIRED_SAMPLER_VERSION == "paper-sampler-v2"
