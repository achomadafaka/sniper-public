"""Task 1 (operator surface) — honest V6 lifecycle reports reconstructed from immutable rows.

Fixtures drive real ledger lifecycles (open, closed winner/loser, unfilled action, censored) on both
chains via the real engine, then assert list/detail/summary honesty: modeled_live_quote everywhere,
no tx signatures, net PnL unavailable with a reason, USD gross only for USDC, censored excluded from
every performance denominator.
"""
import json

import pytest

from sniper.persistence import db_memecoin_v6_paper as ledger
from sniper.services import memecoin_v6_paper_report as report
from sniper.services.memecoin_v6_engine import (
    EvidenceIdentity, V6Action, fingerprint, make_observation, policy_document, policy_hash, step,
)
from sniper.services.memecoin_v6_events import V6MarketEvent

FRACTION = 1_000_000


def _entry(entry_id, network, mint, token_qty=1_000_000):
    doc = policy_document()
    qp = {"in_atomic": 25_000_000, "out_atomic": token_qty}
    quote_in = "USDC" if network == "solana" else "WETH"
    return {
        "entry_id": entry_id, "origin_event_id": f"evt-{entry_id}", "network_id": network,
        "mint": mint, "ticker": "PEP", "source_wallet_id": "w1", "source_wallet_handle": "@w1",
        "signal_source": "fomo", "notional_usd_micros": 25_000_000, "entry_price": "1.0",
        "quote_in_asset": quote_in, "quote_in_atomic": 25_000_000, "quote_in_decimals": 6,
        "quote_out_asset": mint, "quote_out_atomic": token_qty, "quote_out_decimals": 6,
        "token_qty_atomic": token_qty, "route_target": "raydium", "pool_or_vault": "poolX",
        "quote_id": "q", "quote_provider": "jupiter", "quote_age_s": 1.0,
        "entry_mcap": None, "entry_liquidity": None,
        "signal_ts": "2026-07-26T12:00:00+00:00", "quote_ts": "2026-07-26T12:00:00+00:00",
        "fill_ts": "2026-07-26T12:00:00+00:00",
        "policy_hash": policy_hash(), "policy_document": doc,
        "quote_payload": qp, "quote_fingerprint": fingerprint(qp),
        "route_binding": {"venue": "jupiter"}, "costs": {"slippage_bps": 300},
        "net_pnl_computable": True,
    }


def test_runtime_health_separates_active_and_draining_pools(tmp_path):
    from sniper.services.memecoin_v6_events import V6FeedCursor, V6FeedHealth

    conn = ledger.connect(str(tmp_path / "v6.sqlite"))
    entry = _entry("active-sol", "solana", "MintActive")
    entry["route_binding"] = {
        "venue": "jupiter", "pool_or_vault": "VaultActive",
        "amm_keys": ["VaultActive"],
    }
    ledger.record_entry_fill(conn, entry)
    for pool in ("VaultActive", "VaultOld"):
        ledger.upsert_feed_health(
            conn, "solana", pool, V6FeedHealth("subscription", True, False, None, 0),
            cursor=V6FeedCursor("solana", pool, 1, "head"),
            updated_at="2026-07-26T12:00:00+00:00")

    health = report.runtime_health(conn)

    assert set(health["feed_health_active"]["solana"]) == {"VaultActive"}
    assert set(health["feed_health_draining"]["solana"]) == {"VaultOld"}
    assert set(health["feed_health"]["solana"]) == {"VaultActive", "VaultOld"}


def _radar_entry(entry_id, mint, **overrides):
    entry = {
        **_entry(entry_id, "solana", mint),
        "signal_source": "radar_v5",
        "source_receipt_id": f"receipt-{entry_id}",
        "adapter_version": "radar-paper-adapter-v1",
        "fill_model_version": "route-floor-v1",
        "rule_version": "pump-confirmation-truth-v5",
        "evaluator_version": "alert-outcome-v4",
        "launch_mode": "standard",
    }
    entry.update(overrides)
    return entry


def _full(**over):
    base = {"executable_price": 0.8, "price": 0.8, "route_degraded": False,
            "source_wallet_exited": False, "wallet_tier_adjustment": 0, "prior_low": 0.9,
            "net": -1.0, "arrival_rate": 0.0, "buy_share": 0.0, "realized_vol": 0.01,
            "chain_vol_p90": 0.99, "held_s": 10.0, "is_break": True,
            "quote_status": "healthy", "quote_freshness_s": 0.2,
            "executable_high_water": 1.0, "drawdown": 0.2,
            "depth_pull_flag": False, "tape_complete": True,
            "depth_provenance": "trusted", "capture_version": "v6-direct-v1"}
    base.update(over)
    return base


HOLD_PAYLOAD = {"executable_price": 0.8, "price": 0.8, "held_s": 10.0}   # missing evidence


def _exit_fill(network, qty, proceeds_atomic, now):
    out_asset = "USDC" if network == "solana" else "WETH"
    payload = {"in": qty, "out": proceeds_atomic}
    return {
        "token_qty_atomic": qty, "quote_in_asset": "MINT", "quote_in_atomic": qty,
        "quote_in_decimals": 6, "quote_out_asset": out_asset, "quote_out_atomic": proceeds_atomic,
        "quote_out_decimals": 6, "quote_fingerprint": fingerprint(payload),
        "quote_provider": "jupiter", "unit_price": "0.03", "quote_age_s": 0.0,
        "quote_payload": payload, "route_binding": {"venue": "jupiter"},
        "costs": {"slippage_bps": 300}, "net_pnl_computable": True, "observed_at": now,
    }


def _advance(conn, entry_id, network, payload, now, *, proceeds=None):
    oid = "o-" + fingerprint({"e": entry_id, "n": now})[:20]
    obs = make_observation(observation_id=oid, observed_at=now, network_id=network, payload=payload)
    ledger.append_observation(conn, entry_id, obs)
    pos = next(p for p in ledger.open_positions(conn) if p.entry_id == entry_id)
    decision = step(pos.engine_state, obs)
    ledger.append_decision(conn, entry_id, decision)
    if decision.action is V6Action.HOLD:
        ledger.apply_hold_decision(conn, decision, at=now)
    else:
        requested = min(pos.remaining_qty_atomic,
                        max(1, (pos.remaining_qty_atomic * decision.fraction_ppm) // FRACTION))
        if proceeds is None:
            ledger.record_action_unfilled(conn, decision, "quote_down_test", now)
        else:
            ledger.apply_action_fill(conn, decision, _exit_fill(network, requested, proceeds, now))
    return decision


def _book(tmp_path):
    conn = ledger.connect(str(tmp_path / "v6.sqlite"))
    # open + partial derisk (stays open)
    ledger.record_entry_fill(conn, _entry("open1", "solana", "MintOpen"))
    _advance(conn, "open1", "solana", _full(held_s=10.0), "2026-07-26T12:01:00+00:00",
             proceeds=15_000_000)
    # closed solana winner (full exit, 30 USDC proceeds vs 25 cost)
    ledger.record_entry_fill(conn, _entry("winsol", "solana", "MintWin"))
    _advance(conn, "winsol", "solana", _full(held_s=10.0, arrival_rate=0.2, buy_share=0.5),
             "2026-07-26T12:02:00+00:00", proceeds=30_000_000)
    # closed 4663 loser (full exit, proceeds in WETH < cost) -> gross_usd None
    ledger.record_entry_fill(conn, _entry("lose46", "4663", "0xLose"))
    _advance(conn, "lose46", "4663", _full(held_s=10.0, arrival_rate=0.2, buy_share=0.5),
             "2026-07-26T12:03:00+00:00", proceeds=10_000_000)
    # unfilled action (stays open, inventory unchanged)
    ledger.record_entry_fill(conn, _entry("unf1", "solana", "MintUnf"))
    _advance(conn, "unf1", "solana", _full(held_s=10.0), "2026-07-26T12:04:00+00:00", proceeds=None)
    # healthy no-trigger HOLD
    ledger.record_entry_fill(conn, _entry("hold1", "solana", "MintHold"))
    _advance(conn, "hold1", "solana", _full(is_break=False, quote_out_asset="USDC"),
             "2026-07-26T12:04:30+00:00")
    # censored (data-insufficient HOLD, then censor) -> excluded from denominators
    ledger.record_entry_fill(conn, _entry("cen1", "solana", "MintCen"))
    _advance(conn, "cen1", "solana", HOLD_PAYLOAD, "2026-07-26T12:05:00+00:00")
    ledger.censor_position(conn, "cen1", reason="broken experiment run",
                           at="2026-07-26T12:06:00+00:00")
    return conn


# ---- detail / list --------------------------------------------------------------------------

def test_detail_returns_immutable_evidence_and_ordered_timeline(tmp_path):
    conn = _book(tmp_path)
    d = report.position_detail(conn, "winsol")
    assert d["funded"] is False and d["fill_label"] == "modeled_live_quote"
    assert d["entry_evidence"]["mint"] == "MintWin" and d["entry_evidence"]["notional_usd"] == 25.0
    assert d["inventory"]["remaining_qty_atomic"] == 0        # full exit
    assert d["inventory"]["exited_qty_atomic"] == 1_000_000
    assert d["latest_executable_value"]["executable_price"] == 0.8
    assert d["latest_thought"]
    assert [o["observed_at"] for o in d["timeline"]["observations"]] == \
        sorted(o["observed_at"] for o in d["timeline"]["observations"])
    assert d["timeline"]["fills"] and all(f["fill_kind"] == "modeled_live_quote"
                                          for f in d["timeline"]["fills"])
    # no transaction signature anywhere in the serialized detail
    blob = json.dumps(d)
    for forbidden in ("tx_sig", "signature", "signed_payload", "private_key"):
        assert forbidden not in blob


def test_position_card_exposes_pool_identity_for_active_feed_health(tmp_path):
    conn = _book(tmp_path)
    observation = conn.execute(
        "SELECT payload_json FROM v6_paper_observations WHERE position_id='hold1'"
    ).fetchone()
    payload = json.loads(observation[0])
    payload["pool"] = "VaultActive"
    conn.execute(
        "UPDATE v6_paper_observations SET payload_json=? WHERE position_id='hold1'",
        (json.dumps(payload),),
    )

    card = next(row for row in report.list_positions(conn) if row["position_id"] == "hold1")

    assert card["market_facts"]["pool"] == "VaultActive"


def test_detail_reports_post_exit_executable_followthrough(tmp_path):
    conn = _book(tmp_path)
    schedule = conn.execute(
        "SELECT s.* FROM v6_paper_exit_followup_schedules s "
        "WHERE s.position_id='winsol' AND s.horizon_s=30"
    ).fetchone()
    follow = _exit_fill(
        "solana", int(schedule["token_qty_atomic"]), 35_000_000,
        "2026-07-26T12:02:30+00:00")
    ledger.record_exit_followup_result(
        conn, schedule["followup_id"], follow, "2026-07-26T12:02:30+00:00")

    row = report.position_detail(conn, "winsol")["exit_followups"][0]
    assert row["target_horizon_s"] == 30
    assert row["actual_horizon_s"] == 30.0
    assert row["status"] == "healthy"
    assert row["actual_exit_proceeds_atomic"] == 30_000_000
    assert row["followup_proceeds_atomic"] == 35_000_000
    assert row["proceeds_delta_atomic"] == 5_000_000
    assert row["proceeds_delta_quote"] == "5"


def test_detail_reports_typed_4663_followup_route_failure(tmp_path):
    conn = _book(tmp_path)
    schedule = conn.execute(
        "SELECT s.* FROM v6_paper_exit_followup_schedules s "
        "WHERE s.position_id='lose46' AND s.horizon_s=30"
    ).fetchone()
    failure = {
        "status": "route_absent", "provider": "uniswap_best_of",
        "detail_code": "no_executable_route",
    }
    ledger.record_exit_followup_result(
        conn, schedule["followup_id"], failure, "2026-07-26T12:03:30+00:00")

    row = report.position_detail(conn, "lose46")["exit_followups"][0]
    assert row["status"] == "route_absent"
    assert row["actual_horizon_s"] == 30.0
    assert row["actual_exit_proceeds_atomic"] == 10_000_000
    assert row["followup_proceeds_atomic"] is None
    assert row["proceeds_delta_atomic"] is None
    assert row["quote_provider"] == "uniswap_best_of"
    assert row["detail_code"] == "no_executable_route"


def test_detail_exposes_rule_evidence_and_active_intent(tmp_path):
    conn = _book(tmp_path)
    detail = report.position_detail(conn, "unf1")
    rules = detail["latest_thought"]["rule_results"]
    assert rules["lp_loss"]["status"] in {
        "triggered", "not_triggered", "insufficient"}
    assert detail["active_intent"]["token"]
    assert detail["active_intent"]["next_attempt_at"] is None
    assert "break_episode" in detail
    assert detail["quote_status"] is None or isinstance(detail["quote_status"], str)


def test_list_positions_covers_all_states_and_labels_fills(tmp_path):
    conn = _book(tmp_path)
    cards = report.list_positions(conn)
    by_id = {c["entry_id"]: c for c in cards}
    assert set(by_id) == {"open1", "winsol", "lose46", "unf1", "hold1", "cen1"}
    assert all(c["fill_kind"] == "modeled_live_quote" and c["funded"] is False for c in cards)
    assert by_id["winsol"]["status"] == "closed"
    assert by_id["cen1"]["status"] == "censored_operator"
    assert by_id["unf1"]["remaining_qty_atomic"] == 1_000_000     # unfilled kept inventory
    assert by_id["winsol"]["modeled_pnl"]["realized_gross_pnl_usd"] == 5.0
    assert by_id["lose46"]["modeled_pnl"]["realized_gross_pnl_usd"] == -15.0
    assert by_id["lose46"]["modeled_pnl"]["usd_conversion"] == "entry_fx_mark"


def test_list_positions_batches_facts_without_per_position_queries(tmp_path):
    conn = _book(tmp_path)
    statements = []
    conn.set_trace_callback(statements.append)

    report.list_positions(conn)

    conn.set_trace_callback(None)
    reads = [statement.lower() for statement in statements
             if statement.lstrip().lower().startswith("select")]
    assert len(reads) <= 7
    assert not any("where position_id=" in statement for statement in reads)


def test_position_detail_is_bounded_and_cursor_pageable(tmp_path):
    conn = _book(tmp_path)
    for index in range(20):
        position = next(p for p in ledger.open_positions(conn) if p.entry_id == "hold1")
        at = f"2026-07-26T12:05:{index:02d}+00:00"
        evidence = EvidenceIdentity(market=f"market-page-{index}")
        observation = make_observation(
            observation_id=f"obs-page-{index}", observed_at=at, network_id="solana",
            payload=_full(is_break=False, quote_out_asset="USDC"), evidence=evidence)
        decision = step(position.engine_state, observation)
        assert ledger.append_transition(conn, "hold1", observation, decision) is True

    first = report.position_detail(
        conn, "hold1", observation_limit=3, decision_limit=4)
    page = first["timeline"]["pagination"]
    assert len(first["timeline"]["observations"]) == 3
    assert len(first["timeline"]["decisions"]) == 4
    assert page["observations"]["has_more"] is True
    assert page["decisions"]["has_more"] is True

    second = report.position_detail(
        conn, "hold1", observation_limit=3, decision_limit=4,
        before_observation=page["observations"]["next_before"],
        before_decision=page["decisions"]["next_before"])
    first_obs = {row["observation_id"] for row in first["timeline"]["observations"]}
    second_obs = {row["observation_id"] for row in second["timeline"]["observations"]}
    assert first_obs.isdisjoint(second_obs)


def test_runtime_health_reads_only_latest_five_thousand_samples(tmp_path):
    conn = _book(tmp_path)
    conn.executemany(
        "INSERT INTO v6_paper_latency_samples "
        "(stage,duration_us,network_id,provider,feed_mode,recorded_at) "
        "VALUES ('quote',?,'4663','p','async',?)",
        ((index, "2026-07-26T12:00:00+00:00") for index in range(5_100)),
    )

    health = report.runtime_health(conn)

    assert health["quote_ms"]["samples"] == 5_000


def test_summary_does_not_load_large_state_blobs(tmp_path):
    conn = _book(tmp_path)
    statements = []
    conn.set_trace_callback(statements.append)

    report.summary(conn)

    conn.set_trace_callback(None)
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert not any("select * from v6_paper_observations" in statement
                   for statement in normalized)
    assert not any("from v6_paper_decisions" in statement
                   and "state_if_filled_json" in statement for statement in normalized)


def test_summary_aggregates_large_json_in_sql_instead_of_loading_every_blob(tmp_path):
    conn = _book(tmp_path)
    statements = []
    conn.set_trace_callback(statements.append)

    report.summary(conn)

    conn.set_trace_callback(None)
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    observation_reads = [
        statement for statement in normalized
        if "from v6_paper_observations" in statement
    ]
    decision_reads = [
        statement for statement in normalized
        if "from v6_paper_decisions" in statement
    ]
    assert observation_reads
    assert all("select position_id,observation_id,observed_at,payload_json" not in statement
               for statement in observation_reads)
    assert all("select rowid as decision_rowid" not in statement
               for statement in decision_reads)


def test_list_positions_uses_grouped_latest_rows_not_full_window_ranks(tmp_path):
    conn = _book(tmp_path)
    statements = []
    conn.set_trace_callback(statements.append)

    report.list_positions(conn)

    conn.set_trace_callback(None)
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert not any("row_number() over" in statement for statement in normalized)
    assert any("max(rowid)" in statement and "v6_paper_observations" in statement
               for statement in normalized)
    assert any("max(rowid)" in statement and "v6_paper_decisions" in statement
               for statement in normalized)


def test_cockpit_shows_only_open_copy_system_positions(tmp_path):
    conn = ledger.connect(tmp_path / "v6.sqlite")
    ledger.record_entry_fill(conn, _entry("legacy", "solana", "MintLegacy"))
    ledger.record_entry_fill(conn, _radar_entry("current", "MintCurrent"))
    ledger.record_entry_fill(conn, _radar_entry(
        "old-model", "MintOld", fill_model_version="route-floor-v0"))
    ledger.record_entry_fill(conn, _radar_entry("closed", "MintClosed"))
    _advance(
        conn, "closed", "solana",
        _full(held_s=10.0, arrival_rate=0.2, buy_share=0.5),
        "2026-07-26T12:02:00+00:00", proceeds=30_000_000,
    )

    snapshot = report.cockpit_snapshot(conn)

    assert [row["entry_id"] for row in snapshot["positions"]] == ["legacy"]
    assert snapshot["summary"]["counts_by_chain"]["opened"]["total"] == 1
    assert snapshot["summary"]["counts_by_chain"]["open"]["total"] == 1
    assert snapshot["summary"]["counts_by_chain"]["closed"]["total"] == 0
    assert snapshot["summary"]["arm_identity"] == {
        "source": "funded_copy_system",
        "policy_hash": policy_hash(),
    }


def test_cockpit_outcome_summary_excludes_radar_history(tmp_path):
    conn = ledger.connect(tmp_path / "v6.sqlite")
    for entry_id, launch_mode, proceeds in (
        ("radar-win", "standard", 30_000_000),
        ("radar-loss", "cashback", 10_000_000),
    ):
        ledger.record_entry_fill(conn, _radar_entry(
            entry_id, f"Mint-{entry_id}", launch_mode=launch_mode,
        ))
        _advance(
            conn, entry_id, "solana",
            _full(held_s=10.0, arrival_rate=0.2, buy_share=0.5),
            "2026-07-26T12:02:00+00:00", proceeds=proceeds,
        )
    ledger.record_entry_fill(conn, _entry("legacy-win", "solana", "MintLegacy"))
    _advance(
        conn, "legacy-win", "solana",
        _full(held_s=10.0, arrival_rate=0.2, buy_share=0.5),
        "2026-07-26T12:03:00+00:00", proceeds=100_000_000,
    )

    summary = report.cockpit_summary(conn)

    assert summary["outcomes_by_launch_mode"] == {
        "unknown": {
            "graded_lifecycles": 1,
            "winner_rate": 1.0,
            "gross_modeled_pnl_usd": pytest.approx(75.0),
            "net_modeled_pnl_usd": None,
        },
    }
    assert "winner_rate" not in summary
    assert "gross_modeled_pnl_usd" not in summary


def test_list_card_exposes_human_performance_and_bounded_market_facts(tmp_path):
    """Catches price ratios being omitted, zero-filled, or computed from atomic inventory."""
    conn = _book(tmp_path)
    card = {c["entry_id"]: c for c in report.list_positions(conn)}["hold1"]

    assert card["latest_observed_at"] == "2026-07-26T12:04:30+00:00"
    assert card["modeled_price_return_frac"] == pytest.approx(-0.2)
    assert card["modeled_high_water_return_frac"] == pytest.approx(0.0)
    assert card["inventory"]["remaining_fraction"] == pytest.approx(1.0)
    assert card["market_facts"]["quote_status"] == "healthy"
    assert card["market_facts"]["buy_share"] == 0.0
    assert "whole_bag_proceeds_atomic" not in card["market_facts"]


@pytest.mark.parametrize("current,entry", [
    (None, "1"), (0, "1"), (-1, "1"), ("nan", "1"), ("inf", "1"),
    (1, None), (1, "0"), (1, "-1"), (1, "nan"),
])
def test_price_return_rejects_missing_nonpositive_and_nonfinite_values(current, entry):
    """Catches unavailable executable evidence being presented as a zero return."""
    assert report._price_return(current, entry) is None


def test_decision_context_explains_healthy_hold_and_every_rule(tmp_path):
    """Catches regression to the opaque persisted text 'HOLD: no_trigger'."""
    conn = _book(tmp_path)
    card = {c["entry_id"]: c for c in report.list_positions(conn)}["hold1"]
    ctx = card["decision_context"]

    assert ctx["action"] == "HOLD"
    assert ctx["severity"] == "normal"
    assert ctx["why"] == "Holding: no V6 exit rule is triggered; executable quote is healthy."
    assert {item["rule_id"] for item in ctx["watching"]} == {
        "lp_loss", "catastrophe", "route_loss", "time_expiry", "staged_break",
    }
    assert all(item["state"] in {"triggered", "not_triggered", "insufficient"}
               for item in ctx["watching"])
    assert any(item["rule_id"] == "time_expiry" and "remaining" in item["label"]
               for item in ctx["watching"])


def test_decision_context_surfaces_missing_unknown_and_active_intent(tmp_path):
    """Catches silent omission of missing facts, new rules, or an unfilled exit intent."""
    conn = _book(tmp_path)
    unf = {c["entry_id"]: c for c in report.list_positions(conn)}["unf1"]
    assert unf["decision_context"]["severity"] == "urgent"
    assert "awaiting a modeled fill" in unf["decision_context"]["why"]

    cen = {c["entry_id"]: c for c in report.list_positions(conn)}["cen1"]
    missing = cen["decision_context"]["missing_facts"]
    assert missing == sorted(set(missing)) and "is_break" in missing

    row = conn.execute(
        "SELECT rowid,rule_results_json FROM v6_paper_decisions "
        "WHERE position_id='hold1' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    rules = json.loads(row["rule_results_json"])
    rules.append({"rule_id": "future_guard", "status": "not_triggered", "action": "HOLD",
                  "fraction_ppm": 0, "priority": 0, "reason_codes": [], "missing_fields": []})
    conn.execute("UPDATE v6_paper_decisions SET rule_results_json=? WHERE rowid=?",
                 (json.dumps(rules), row["rowid"]))
    updated = {c["entry_id"]: c for c in report.list_positions(conn)}["hold1"]
    assert any(item == {"rule_id": "future_guard", "state": "not_triggered",
                        "label": "future guard: not triggered"}
               for item in updated["decision_context"]["watching"])


def test_route_watch_reports_check_and_elapsed_progress():
    """Catches route-loss insight hiding the 4-check and 60-second confirmation progress."""
    context = report._decision_context(
        action="HOLD",
        rules={"route_loss": {"status": "not_triggered", "missing_fields": []}},
        payload={"quote_status": "route_absent"},
        engine_state={"route_absence": {
            "checks": 2, "first_absent_at": "2026-07-26T12:00:00+00:00",
        }},
        active_intent=None,
        price_return=-0.1,
        has_decision=True,
        observed_at="2026-07-26T12:00:30+00:00",
    )
    route = context["watching"][0]
    assert route["label"] == "Route: absent 2/4 checks; 30s/60s"


def test_decision_context_includes_observation_level_missing_facts():
    context = report._decision_context(
        action="HOLD",
        rules={"staged_break": {"status": "not_triggered", "missing_fields": []}},
        payload={"quote_status": "healthy", "missing_fields": ["tape_complete", "prior_low"]},
        engine_state={}, active_intent=None, price_return=0.1, has_decision=True,
    )

    assert context["severity"] == "degraded"
    assert context["missing_facts"] == ["prior_low", "tape_complete"]


# ---- bounded cockpit ------------------------------------------------------------------------

def test_cockpit_revision_ignores_telemetry_and_changes_on_admission(tmp_path):
    conn = _book(tmp_path)
    before = report.cockpit_revision(conn)

    ledger.append_latency_sample(
        conn, "quote", 1_000, network_id="solana", provider="jupiter",
        feed_mode="poll", recorded_at="2026-07-26T12:07:00+00:00",
    )
    ledger.append_market_events(conn, (V6MarketEvent(
        network_id="solana", pool="poolX", block_height=1, block_hash=None,
        tx_id="sig-1", event_index=0, source_event_at="2026-07-26T12:07:00+00:00",
        received_at="2026-07-26T12:07:00+00:00", kind="swap", payload={"price": 1.0},
    ),))
    assert report.cockpit_revision(conn) == before

    ledger.enqueue_admission(conn, {
        "origin_event_id": "evt-cockpit", "source_position_id": 900,
        "network_id": "solana", "mint": "MintCockpit",
        "received_at": "2026-07-26T12:07:00+00:00",
        "signal": {"origin_event_id": "evt-cockpit", "source_event_id": 900,
                   "network_id": "solana", "mint": "MintCockpit"},
    }, "2026-07-26T12:07:00+00:00")
    assert report.cockpit_revision(conn) != before


def test_cockpit_snapshot_hides_radar_history_and_bounds_latency(tmp_path, monkeypatch):
    conn = _book(tmp_path)
    ledger.record_entry_fill(conn, _radar_entry("radar-open", "MintRadarOpen"))
    monkeypatch.setattr(report, "COCKPIT_LATENCY_LIMIT", 2)
    for index in range(3):
        ledger.append_latency_sample(
            conn, "quote", 1_000 + index, network_id="solana", provider="jupiter",
            feed_mode="poll", recorded_at=f"2026-07-26T12:07:0{index}+00:00",
        )

    snapshot = report.cockpit_snapshot(conn)

    assert snapshot["mode"] == "v6_paper"
    assert snapshot["funded"] is False
    assert snapshot["fill_label"] == "modeled_live_quote"
    assert [row["entry_id"] for row in snapshot["positions"]] == ["hold1", "unf1", "open1"]
    assert snapshot["runtime"]["quote_ms"]["samples"] == 2
    assert {"summary", "runtime", "positions"} <= set(snapshot)


def test_cockpit_snapshot_does_not_call_full_summary(tmp_path, monkeypatch):
    conn = _book(tmp_path)
    monkeypatch.setattr(
        report, "summary",
        lambda _conn: (_ for _ in ()).throw(AssertionError("full summary called")),
    )

    assert report.cockpit_snapshot(conn)["summary"]["current_policy"]["policy_hash"] \
        == policy_hash()


# ---- summary honesty ------------------------------------------------------------------------

def test_summary_counts_and_net_unavailable(tmp_path):
    conn = _book(tmp_path)
    s = report.summary(conn)
    assert s["counts_by_chain"]["opened"]["total"] == 6
    assert s["counts_by_chain"]["open"]["total"] == 3        # open1 + unf1 + hold1
    assert s["counts_by_chain"]["closed"]["total"] == 2
    assert s["counts_by_chain"]["censored"]["total"] == 1
    assert s["counts_by_chain"]["action_unfilled"]["total"] == 1
    assert s["net_modeled_pnl_usd"] is None
    assert s["net_unavailable_reason"] == "unknown_cost_components"


def test_summary_batches_position_facts_without_position_shaped_queries(tmp_path):
    conn = _book(tmp_path)
    statements = []
    conn.set_trace_callback(statements.append)

    report.summary(conn)

    conn.set_trace_callback(None)
    lowered = [statement.lower() for statement in statements]
    fact_tables = (
        "v6_paper_entries", "v6_paper_observations", "v6_paper_decisions",
        "v6_paper_action_intents", "v6_paper_fills",
    )
    assert not any(
        table in statement and "where position_id=" in statement
        for statement in lowered for table in fact_tables
    )


def test_summary_exposes_durable_admission_queue_and_failure_reasons(tmp_path):
    conn = _book(tmp_path)
    base = {
        "network_id": "solana", "mint": "MintQueue", "received_at":
        "2026-07-26T12:00:00+00:00",
    }
    for index, origin in enumerate(("queue-retry", "queue-gap"), start=100):
        ledger.enqueue_admission(conn, {
            **base, "origin_event_id": origin, "source_position_id": index,
            "signal": {"origin_event_id": origin, "source_event_id": index,
                       "network_id": "solana", "mint": "MintQueue"},
        }, "2026-07-26T12:00:00+00:00")
    claim = ledger.claim_admissions(
        conn, "2026-07-26T12:00:00+00:00", max_claims=1, max_open=10,
        open_count=0, lease_s=15)[0]
    ledger.record_entry_quote_result(
        conn, claim, outcome="quote_unavailable",
        completed_at="2026-07-26T12:00:00+00:00", detail="rpc_transport")
    ledger.mark_admission_terminal(
        conn, "queue-gap", outcome="historical_intake_gap", detail="historical_intake_gap",
        updated_at="2026-07-26T12:00:00+00:00")

    admission = report.summary(conn)["admission"]
    assert admission["states"]["retry_wait"] == 1
    assert admission["states"]["terminal_unfilled"] == 1
    assert admission["terminal_reasons"] == {"historical_intake_gap": 1}
    assert admission["attempts"] == 1
    assert admission["historical_gaps"] == 1
    assert "oldest_pending_age_s" in admission


def test_summary_exposes_source_rejections_separately_from_quote_outcomes(tmp_path):
    conn = _book(tmp_path)
    ledger.record_admission_rejection(conn, {
        "source_position_id": 500, "origin_event_id": "evt-rejected",
        "network_id": "ethereum", "mint": "MintRejected",
        "reason_code": "unsupported_network", "detail": {"execution_mode": "paper"},
    }, "2026-07-26T12:00:00+00:00")

    admission = report.summary(conn)["admission"]

    assert admission["source_rejections"] == {
        "total": 1, "reasons": {"unsupported_network": 1},
    }
    assert admission["states"]["terminal_unfilled"] == 0


def test_summary_exposes_cursor_domain_migration_gap(tmp_path):
    conn = _book(tmp_path)
    ledger.set_cursor(
        conn,
        "source_position_historical_gap",
        json.dumps({
            "kind": "cursor_domain_migration",
            "count": "unknown",
            "old_event_cursor": "37710895",
            "new_position_tail": 20921,
            "recorded_at": "2026-07-27T13:31:15+00:00",
        }, sort_keys=True, separators=(",", ":")),
        "2026-07-27T13:31:15+00:00",
    )

    admission = report.summary(conn)["admission"]
    assert admission["historical_gaps"] == 1
    assert admission["historical_gap_marker"]["count"] == "unknown"


def test_summary_attributes_early_point_without_changing_policy(tmp_path):
    conn = _book(tmp_path)
    diagnostics = report.summary(conn)["staged_break_diagnostics"]
    assert diagnostics["triggered"] >= 3
    assert diagnostics["early_point_present"] >= 3
    assert diagnostics["exit_full"] == 2
    assert diagnostics["full_exit_would_derisk_without_early"] == 0
    assert diagnostics["derisk"] >= 1


def test_summary_gross_and_winner_metrics_are_reconstructed(tmp_path):
    conn = _book(tmp_path)
    s = report.summary(conn)
    assert s["graded_lifecycles"] == 2                       # both closed, non-censored
    assert s["gross_modeled_pnl_usd"] == -10.0               # includes WETH at entry FX
    assert s["gross_modeled_pnl_by_quote_asset"]["USDC"] == 5.0
    assert "WETH" in s["gross_modeled_pnl_by_quote_asset"]
    assert s["winner_rate"] == 0.5                           # 1 winner of 2 graded
    assert s["worst_outcome"] < 0                            # the 4663 loser
    assert s["gross_usd_top_winner_removed"] == -15.0


def test_summary_separates_performance_by_engine_policy_and_hash(tmp_path):
    conn = _book(tmp_path)
    old_document = {"engine_policy_id": "v6-position-engine-v1", "revision": "sealed"}
    old_hash = fingerprint(old_document)
    conn.execute(
        "UPDATE v6_paper_entries SET policy_document=?,policy_hash=? WHERE entry_id='lose46'",
        (json.dumps(old_document, sort_keys=True, separators=(",", ":")), old_hash),
    )

    summary = report.summary(conn)

    assert summary["current_policy"] == {
        "engine_policy_id": policy_document()["engine_policy_id"],
        "policy_hash": policy_hash(),
    }
    assert summary["graded_lifecycles"] == 1
    assert summary["gross_modeled_pnl_usd"] == 5.0
    assert summary["winner_rate"] == 1.0
    assert summary["counts_by_chain"]["closed"]["total"] == 1
    assert summary["all_policy_counts_by_chain"]["closed"]["total"] == 2
    cohorts = {
        (row["engine_policy_id"], row["policy_hash"]): row
        for row in summary["policy_cohorts"]
    }
    assert cohorts[("v6-position-engine-v1", old_hash)]["graded_lifecycles"] == 1
    assert cohorts[(policy_document()["engine_policy_id"], policy_hash())][
        "graded_lifecycles"] == 1


def test_summary_includes_valid_catastrophe_low_and_excludes_only_explicit_quarantine(tmp_path):
    conn = _book(tmp_path)

    def _mark(position_id, reason_codes, drawdown):
        decision = conn.execute(
            "SELECT rowid,observation_id,rule_results_json FROM v6_paper_decisions "
            "WHERE position_id=? ORDER BY rowid DESC LIMIT 1", (position_id,),
        ).fetchone()
        rules = json.loads(decision["rule_results_json"])
        catastrophe = next(row for row in rules if row["rule_id"] == "catastrophe")
        catastrophe["reason_codes"] = list(reason_codes)
        conn.execute(
            "UPDATE v6_paper_decisions SET rule_results_json=? WHERE rowid=?",
            (json.dumps(rules), decision["rowid"]),
        )
        observation = conn.execute(
            "SELECT payload_json FROM v6_paper_observations "
            "WHERE position_id=? AND observation_id=?",
            (position_id, decision["observation_id"]),
        ).fetchone()
        payload = json.loads(observation["payload_json"])
        payload["drawdown"] = drawdown
        conn.execute(
            "UPDATE v6_paper_observations SET payload_json=? "
            "WHERE position_id=? AND observation_id=?",
            (json.dumps(payload), position_id, decision["observation_id"]),
        )

    _mark("winsol", ("catastrophe:confirmation_pending",), 0.80)
    _mark(
        "lose46",
        ("catastrophe:confirmation_pending", "catastrophe:quote_quarantined"),
        0.95,
    )

    assert report.summary(conn)["max_drawdown"] == pytest.approx(0.80)


def test_censored_excluded_from_all_denominators(tmp_path):
    conn = _book(tmp_path)
    s = report.summary(conn)
    # the only data-insufficient decision belongs to the censored position -> excluded
    assert s["data_insufficient_decisions"] == 0
    # censored position never enters graded lifecycles or reason performance
    assert s["graded_lifecycles"] == 2
    codes = s["reason_code_outcomes"]
    assert not any(k.startswith("data_insufficient") for k in codes)


def test_measurement_censor_is_counted_and_excluded_like_operator_censor(tmp_path):
    conn = ledger.connect(tmp_path / "v6.sqlite")
    ledger.record_entry_fill(conn, _entry("stale1", "solana", "MintStale"))
    ledger.censor_measurement_position(
        conn, "stale1", status="censored_handoff_stale",
        reason="radar receipt handoff exceeded 30s",
        at="2026-07-26T12:01:00+00:00")

    result = report.summary(conn)

    assert result["counts_by_chain"]["censored"]["total"] == 1
    assert result["graded_lifecycles"] == 0


# --- structural cohort separation ---------------------------------------------

from sniper.services.memecoin_v6_paper_report import (  # noqa: E402
    COHORT_DISCRIMINATORS,
    cohort_summary,
    cohort_filter_sql,
)


def _cohort(**overrides):
    values = {
        "signal_source": "radar_v5",
        "adapter_version": "radar-paper-adapter-v1",
        "fill_model_version": "route-floor-v1",
        "rule_version": "pump-confirmation-truth-v5",
        "evaluator_version": "alert-outcome-v4",
        "launch_mode": "standard",
        "policy_hash": "h1",
    }
    values.update(overrides)
    return values


def test_cohort_filter_binds_every_discriminator():
    sql, params = cohort_filter_sql(**_cohort())
    for column in COHORT_DISCRIMINATORS:
        assert f"e.{column} IS ?" in sql
    assert len(params) == len(COHORT_DISCRIMINATORS) == 7


def test_cohort_filter_refuses_to_widen_silently():
    partial = _cohort()
    partial.pop("launch_mode")
    with pytest.raises(ValueError, match="missing discriminators"):
        cohort_filter_sql(**partial)


def test_cohort_filter_rejects_unknown_discriminators():
    with pytest.raises(ValueError, match="unknown cohort discriminators"):
        cohort_filter_sql(**_cohort(), venue_id="pumpfun_solana")


def test_launch_mode_is_part_of_the_cohort_key():
    # Capturing standard/cashback/mayhem together is fine; pooling them is not.
    standard, _ = cohort_filter_sql(**_cohort(launch_mode="standard"))
    cashback_sql, cashback_params = cohort_filter_sql(**_cohort(launch_mode="cashback"))
    assert standard == cashback_sql               # same predicate shape
    assert "cashback" in cashback_params          # different bound cohort
    assert "standard" not in cashback_params


def test_radar_and_fomo_cohorts_bind_different_values():
    _, radar = cohort_filter_sql(**_cohort())
    _, fomo = cohort_filter_sql(**_cohort(
        signal_source="global_feed", adapter_version=None,
        fill_model_version="modeled_live_quote", rule_version=None,
        evaluator_version=None,
    ))
    assert radar != fomo
    assert "radar_v5" in radar and "global_feed" in fomo


def test_nullable_fomo_cohort_is_queryable_without_widening(tmp_path):
    conn = ledger.connect(tmp_path / "v6.sqlite")
    ledger.record_entry_fill(conn, _entry("fomo1", "solana", "MintFomo"))
    metrics = cohort_summary(conn, **_cohort(
        signal_source="fomo", adapter_version=None, fill_model_version=None,
        rule_version=None, evaluator_version=None, launch_mode=None,
        policy_hash=policy_hash()))
    assert metrics["counts_by_chain"]["opened"]["total"] == 1


def test_summary_never_pools_radar_positions_into_legacy_headline(tmp_path):
    conn = ledger.connect(tmp_path / "v6.sqlite")
    ledger.record_entry_fill(conn, _entry("fomo1", "solana", "MintFomo"))
    radar = {
        **_entry("radar1", "solana", "MintRadar"),
        "signal_source": "radar_v5",
        "source_receipt_id": "r1",
        "adapter_version": "radar-paper-adapter-v1",
        "fill_model_version": "route-floor-v1",
        "rule_version": "pump-confirmation-truth-v5",
        "evaluator_version": "alert-outcome-v4",
        "launch_mode": "standard",
    }
    ledger.record_entry_fill(conn, radar)

    result = report.summary(conn)

    assert result["counts_by_chain"]["opened"]["total"] == 1
    assert len(result["measurement_cohorts"]) == 2
    assert sorted(
        cohort["counts_by_chain"]["opened"]["total"]
        for cohort in result["measurement_cohorts"]) == [1, 1]
