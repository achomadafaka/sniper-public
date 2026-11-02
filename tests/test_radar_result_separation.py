from flask import Flask

from sniper.research.memecoin_opportunity.paper_adapter import (
    ADAPTER_VERSION,
    FILL_MODEL_VERSION,
    PaperFillRequest,
    append_paper_fill_receipt,
    append_paper_fill_request,
)
from sniper.research.memecoin_opportunity.paper_lifecycle import (
    activate_paper_lifecycle,
)
from sniper.research.memecoin_opportunity.paper_lifecycle_v3 import (
    activate_adaptive_lifecycle,
)
from sniper.research.memecoin_opportunity.store import connect
from sniper.routes.api_memecoin_opportunity import (
    create_api_memecoin_opportunity_blueprint,
)


def _entry(conn, request_id, *, venue, rule, lifecycle, result_code, return_bps):
    network = "solana" if venue == "pumpfun_solana" else "4663"
    append_paper_fill_request(
        conn,
        PaperFillRequest(
            request_id=request_id,
            alert_id=f"alert-{request_id}",
            launch_id=f"launch-{request_id}",
            network_id=network,
            venue_id=venue,
            token_id=f"token-{request_id}",
            creator_id=None,
            launch_mode="standard",
            state_fingerprint=f"state-{request_id}",
            rule_version=rule,
            evaluator_version="alert-outcome-v4",
            adapter_version=ADAPTER_VERSION,
            fill_model_version=FILL_MODEL_VERSION,
            clip_usd_micros=25_000_000,
            decision_at="2026-08-11T00:00:01Z",
            decision_age_ms=20_000,
            intent_at="2026-08-11T00:00:01Z",
            expires_at="2026-08-11T00:01:00Z",
            local_qty_atomic=1_000,
        ),
    )
    append_paper_fill_receipt(
        conn,
        request_id=request_id,
        status="filled",
        now_iso="2026-08-11T00:00:01Z",
        quote_started_at="2026-08-11T00:00:01Z",
        quote_completed_at="2026-08-11T00:00:01Z",
        economics={
            "quote_provider": "test",
            "quote_in_asset": "USDC",
            "quote_in_atomic": 25_000_000,
            "quote_in_decimals": 6,
            "quote_out_asset": f"token-{request_id}",
            "quote_out_decimals": 6,
            "expected_out_atomic": 1_000,
            "floor_out_atomic": 1_000,
            "slippage_bps": 300,
            "route_binding": {"pool": "pool-1"},
            "quote_payload": {"route": "pool-1"},
        },
    )
    conn.execute(
        "INSERT INTO op_paper_outcomes ("
        "outcome_id,request_id,lifecycle_version,status,result_code,"
        "entry_quote_atomic,exit_floor_atomic,return_bps,outcome_at,"
        "origin_kind,policy_hash,row_fingerprint,inserted_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"outcome-{request_id}",
            request_id,
            lifecycle,
            "valid",
            result_code,
            "25000000",
            "26000000",
            return_bps,
            "2026-08-11T00:01:00Z",
            "radar_" + venue.split("_", 1)[0],
            "policy-v3" if lifecycle == "radar-adaptive-v3" else None,
            f"fingerprint-{request_id}",
            "2026-08-11T00:01:00Z",
        ),
    )


def _payload(tmp_path):
    path = tmp_path / "radar.db"
    conn = connect(path)
    activate_paper_lifecycle(conn, now_iso="2026-08-11T00:00:00Z")
    activate_adaptive_lifecycle(conn, now_iso="2026-08-11T00:00:00Z")
    _entry(
        conn,
        "v2-pump",
        venue="pumpfun_solana",
        rule="pump-confirmation-truth-v6",
        lifecycle="radar-realistic-v2",
        result_code="target_first",
        return_bps=100,
    )
    _entry(
        conn,
        "v2-bags",
        venue="bags_robinhood_4663",
        rule="bags-confirmation-v1",
        lifecycle="radar-realistic-v2",
        result_code="stop_first",
        return_bps=-100,
    )
    _entry(
        conn,
        "v3-pons",
        venue="pons_v1_4663",
        rule="pons-adaptive-v3",
        lifecycle="radar-adaptive-v3",
        result_code="adaptive_exit",
        return_bps=50,
    )
    conn.close()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_opportunity_blueprint({
        "db_path": str(path),
        "now_fn": lambda: "2026-08-11T00:02:00Z",
    }))
    return app.test_client().get(
        "/api/memecoin/opportunity-radar",
    ).get_json()["paper_results"]


def test_the_payload_holds_one_block_per_lifecycle(tmp_path):
    payload = _payload(tmp_path)
    assert set(payload) == {
        "radar-realistic-v2",
        "radar-realistic-v3",
        "radar-adaptive-v3",
    }


def test_the_unactivated_realistic_v3_cohort_is_present_and_separate(tmp_path):
    """An absent cohort key is not the same as an empty one.

    Before `radar-realistic-v3` was keyed here, activating it would have left v2's
    numbers rendering where the new cohort belonged. It must report itself as
    unactivated and must not inherit any of v2's counts.
    """
    payload = _payload(tmp_path)
    v3 = payload["radar-realistic-v3"]
    assert v3["lifecycle_version"] == "radar-realistic-v3"
    assert v3["activated_at"] is None
    assert v3["valid_results"] == 0
    assert payload["radar-realistic-v2"]["valid_results"] == 2


def test_no_block_pools_two_lifecycles(tmp_path):
    payload = _payload(tmp_path)
    assert payload["radar-realistic-v2"]["valid_results"] == 2
    assert payload["radar-adaptive-v3"]["valid_results"] == 1
    assert "average_net_return_bps" not in payload["radar-realistic-v2"]
    assert "average_net_return_bps" not in payload["radar-adaptive-v3"]


def test_no_block_pools_two_venues(tmp_path):
    payload = _payload(tmp_path)
    for block in payload.values():
        # bags_robinhood_4663 retired 2026-08-19: no result block for a retired venue
        assert set(block["by_venue"]) == {
            "pumpfun_solana",
            "pons_v1_4663",
        }


def test_the_copy_source_cohort_is_absent(tmp_path):
    assert "global_feed" not in str(_payload(tmp_path))
