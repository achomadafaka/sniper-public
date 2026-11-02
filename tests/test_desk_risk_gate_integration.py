"""Evidence-gated watcher behavior, fake model and temporary observer DB only."""
import copy
import json

import pytest

from test_memecoin_desk_risk_watcher import Env, L, T0, W, pos
from sniper.services import memecoin_desk_risk_apply as A


@pytest.fixture
def gated(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_RISK_EVIDENCE_GATES", "1")
    e = Env(tmp_path, monkeypatch, [pos(1), pos(2)])
    e.evidence = {i: {"fresh": True, "last_price": 1.0, "entry_price": 0.9, "n5": .01,
                       "buy_sol_30s": 1.0, "sell_sol_30s": 2.0, "distinct_sellers_30s": 3,
                       "known_through": e.t - 2, "coverage_from": e.t - 500,
                       "high_since_fill": 1.0, "low_since_fill": .9} for i in (1, 2)}

    def read(p, *, now, **_):
        out = copy.deepcopy(e.evidence[p["id"]])
        out["as_of"] = now
        return out

    monkeypatch.setattr(W, "evidence_of", read, raising=False)
    monkeypatch.setattr(W, "rail_proximity", lambda *_a, **_kw: {}, raising=False)
    monkeypatch.setattr(W, "structure_of", lambda *_a, **_kw: {"tape": "unknown", "reason": "fixture"})
    e.answer = lambda blocks: json.dumps([{"k": b["k"], "a": "E", "r": None, "c": 11} for b in blocks])
    return e


def attention_once(pid, now):
    return [("sell_burst", T0 + 79)] if pid == 1 else []


def test_first_observation_is_audited_baseline_without_a_model_call(gated):
    stats = gated.tick(attention_fn=lambda *_: [])
    assert gated.calls == []
    assert stats["gated_positions"] == 2 and stats["avoided_calls"] == 1
    rows = gated.rows()
    assert {r["reason"] for r in rows} == {"evidence_gated:baseline"}
    assert all(r["expires_at"] is None for r in rows)
    audit = json.loads(rows[0]["gate_json"])
    assert audit["selection"] == "baseline" and audit["params"]["min_sellers"] == 3


def test_material_move_selects_only_the_changed_position_and_counts_actual_calls(gated):
    gated.tick(attention_fn=lambda *_: [])
    gated.evidence[1]["last_price"] = 1.02
    stats = gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])
    assert [[b["k"] for b in chunk] for chunk in gated.calls] == [["pos:1"]]
    assert stats["gated_positions"] == 1 and stats["avoided_calls"] == 0
    assert {r["key"]: r["action"] for r in gated.rows()[-2:]} == {"pos:1": "EXIT", "pos:2": "HOLD"}


@pytest.mark.parametrize("unknown", [
    {"fresh": False}, {"fresh": None}, {"n5": None}, {"n5": 0.0}, {"last_price": None},
])
def test_unknown_evidence_calls_model_instead_of_claiming_no_change(gated, unknown):
    gated.tick(attention_fn=lambda *_: [])
    gated.evidence[1].update(unknown)
    stats = gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])
    assert [[b["k"] for b in chunk] for chunk in gated.calls] == [["pos:1"]]
    assert stats["calls"] == 1 and stats["gated_positions"] == 1 and stats["avoided_calls"] == 0
    row = next(r for r in gated.rows()[-2:] if r["key"] == "pos:1")
    assert json.loads(row["gate_json"])["selection"] == "evidence_gated:unknown_called"
    if "fresh" in unknown and unknown["fresh"] is not True:
        assert row["action"] == "HOLD" and row["reason"].startswith("unbacked_reason:11")
        assert row["expires_at"] is None


def test_unknown_evidence_still_respects_the_existing_hourly_call_cap(gated):
    gated.tick(attention_fn=lambda *_: [])
    gated.evidence[1]["fresh"] = False
    stats = gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [], hourly_cap=0)
    assert stats["calls"] == 0 and gated.calls == [] and stats["skipped"] == 1
    row = next(r for r in gated.rows()[-2:] if r["key"] == "pos:1")
    assert row["call_error"] == "budget_exhausted" and row["call_id"] is None
    assert json.loads(row["gate_json"])["selection"] == "evidence_gated:unknown_called"


@pytest.mark.parametrize("unknown", [{"fresh": False}, {"fresh": None}, {"last_price": None}])
def test_cold_start_unknown_keeps_calling_until_first_usable_baseline(gated, unknown):
    gated.evidence[1].update(unknown)
    for dt in (0, L.CYCLE_S + 1):
        stats = gated.tick(dt, attention_fn=lambda *_: [])
        assert stats["calls"] == 1
        assert "pos:1" not in gated.state["gate_references"]
        row = next(r for r in gated.rows()[-2:] if r["key"] == "pos:1")
        assert json.loads(row["gate_json"])["selection"] == "evidence_gated:unknown_called"
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [], hourly_cap=0)["skipped"] == 1
    assert len(gated.calls) == 2
    gated.evidence[1].update(fresh=True, last_price=1.0)
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])["calls"] == 0
    assert "pos:1" in gated.state["gate_references"]
    row = next(r for r in gated.rows()[-2:] if r["key"] == "pos:1")
    assert row["reason"] == "evidence_gated:baseline"
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])["calls"] == 0


def test_attention_wakes_a_baseline_but_backing_is_rechecked_after_the_answer(gated):
    model = gated.model

    def delayed(system, user):
        answer = model(system, user)
        gated.t += 1
        gated.evidence[1]["distinct_sellers_30s"] = 0
        return answer

    gated.model = delayed
    stats = gated.tick(attention_fn=attention_once)
    first = next(r for r in gated.rows() if r["key"] == "pos:1")
    assert stats["calls"] == 1 and stats["unbacked_decisions"] == 1
    assert first["action"] == "HOLD" and first["reason"].startswith("unbacked_reason:11")
    assert first["expires_at"] is None
    audit = json.loads(first["gate_json"])
    assert audit["decision_evidence"]["distinct_sellers_30s"] == 0
    assert audit["decision_evidence"]["as_of"] == gated.t


def test_a_closed_position_cannot_receive_a_delayed_action(gated):
    model = gated.model

    def closed(system, user):
        answer = model(system, user)
        gated.positions = []
        return answer

    gated.model = closed
    gated.tick(attention_fn=attention_once)
    first = next(r for r in gated.rows() if r["key"] == "pos:1")
    assert first["action"] == "HOLD" and first["reason"] == "position_changed"
    assert first["expires_at"] is None


def test_gating_never_renews_or_revokes_an_existing_order_ttl(gated):
    gated.tick(attention_fn=attention_once)
    initial = next(r for r in gated.rows() if r["key"] == "pos:1")
    assert initial["action"] == "EXIT"
    gated.tick(L.CYCLE_S + .1, attention_fn=lambda *_: [])
    orders = A.live_orders(gated.out, 1, gated.t)
    assert len(gated.calls) == 1 and len(orders) == 1
    assert orders[0]["expires_at"] == initial["expires_at"]
    assert A.live_orders(gated.out, 1, initial["expires_at"]) == []


def test_failed_call_retains_the_material_reference_for_retry_after_backoff(gated):
    gated.tick(attention_fn=lambda *_: [])
    gated.evidence[1]["last_price"] = 1.02
    model = gated.model
    gated.model = lambda *_: {"call_error": "rate_limited", "text": None}
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])["failed_calls"] == 1
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])["calls"] == 0
    gated.model = model
    assert gated.tick(61, attention_fn=lambda *_: [])["calls"] == 1


def test_failed_initial_rail_wake_does_not_become_a_consumed_baseline(gated, monkeypatch):
    monkeypatch.setattr(W, "rail_proximity", lambda *_a, **_kw: {"stop": True})
    model = gated.model
    gated.model = lambda *_: {"call_error": "rate_limited", "text": None}
    assert gated.tick(attention_fn=lambda *_: [])["failed_calls"] == 1
    gated.model = model
    assert gated.tick(61, attention_fn=lambda *_: [])["calls"] == 1


def test_restart_establishes_a_new_baseline_and_rollback_preserves_legacy_calls(gated, monkeypatch):
    gated.tick(attention_fn=lambda *_: [])
    gated.evidence[1]["last_price"] = 1.5
    gated.state = W.new_state()
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])["calls"] == 0
    monkeypatch.setenv("MEMECOIN_DESK_RISK_EVIDENCE_GATES", "0")
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])["calls"] == 1


def test_exact_rail_proximity_uses_residual_unit_price_not_whole_bag_pnl():
    block = {"pnl_pct_exact": -50, "ev": {"n5": .01}, "rails": {"sl_pct": 10, "tp_pct": 30, "trail_pct": 10},
             "exact_exit_unit_price": .9, "trail_arm_pct": 0}
    p = {"entry_price": 1.0, "executable_peak_price": 1.0}
    from sniper.services import memecoin_desk_risk_gate as G
    assert W.rail_proximity(block, p, G.DEFAULTS) == {"stop": True, "tp": False, "trail": True}
    block["exact_exit_unit_price"] = None
    assert W.rail_proximity(block, p, G.DEFAULTS) == {"stop": None, "tp": None, "trail": None}


def test_unproven_initial_evidence_cannot_permanently_hide_later_material_changes(gated):
    gated.evidence[1].update(fresh=False, last_price=None)
    gated.tick(attention_fn=lambda *_: [])
    gated.evidence[1].update(fresh=True, last_price=1.0)
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])["calls"] == 0
    gated.evidence[1]["last_price"] = 1.02
    assert gated.tick(L.CYCLE_S + 1, attention_fn=lambda *_: [])["calls"] == 1


def test_enabled_prompt_and_audit_bind_the_provisional_thresholds(gated):
    import zlib
    gated.tick(attention_fn=attention_once)
    call = gated.rows("risk_calls")[0]
    user = json.loads(zlib.decompress(call["user_z"]))
    audit = json.loads(next(r for r in gated.rows() if r["key"] == "pos:1")["gate_json"])
    assert user["gate"]["thresholds"] == audit["params"]
    assert user["gate"]["provisional"] is True
    assert call["risk_version"] == "desk-risk-live-v12"


def test_invalid_gate_switch_fails_before_a_call(gated, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_RISK_EVIDENCE_GATES", "maybe")
    with pytest.raises(ValueError, match="must be 0 or 1"):
        gated.tick(attention_fn=attention_once)
    assert gated.calls == []


@pytest.mark.parametrize("bad", [None, "quantity_changed", "unknown_exit", "stale", "wrong_unit", "zero", "nonfinite"])
def test_exact_rail_quote_requires_current_known_residual_quantity(monkeypatch, bad):
    from datetime import datetime, timezone
    from sniper.services import memecoin_desk_risk_gate as G
    monkeypatch.setattr(W.clock, "offset_s", lambda: 0)
    monkeypatch.setattr(W, "evidence_of", lambda *_a, **_kw: {"fresh": True, "n5": .01})
    now = T0 + 80
    p = {**pos(1), "entry_price": 1.0, "entry_notional": 100, "open_qty": 75.0, "open_qty_known": True}
    mark = {"sweep_ts": datetime.fromtimestamp(now - 1, timezone.utc).isoformat(),
            "executable_exit_value": 67.5, "open_qty": 75.0, "unit_price": .9}
    if bad == "quantity_changed":
        p["open_qty"] = 70
    elif bad == "unknown_exit":
        p["open_qty_known"] = False
    elif bad == "stale":
        mark["sweep_ts"] = p["opened_at"]
    elif bad == "wrong_unit":
        mark["unit_price"] = 67.5
    elif bad == "zero":
        mark["open_qty"] = 0
    elif bad == "nonfinite":
        mark["unit_price"] = float("nan")
    block = {"rails": {"sl_pct": 10, "tp_pct": 30, "trail_pct": 0}}
    W._gate_block(block, p, {}, mark, desk_dir=None, now=now, sol_usd=100, fill_sigs_of=None,
                  sb=False, params=G.DEFAULTS, bars=[])
    assert block["exact_exit_unit_price"] == (.9 if bad is None else None)
    assert block["rail_proximity"]["stop"] is (True if bad is None else None)
    assert block["rail_proximity"]["trail"] is None
