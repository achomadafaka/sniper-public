"""Apply-time TIGHTEN decisions require retained whole-bag evidence and cannot nominate an exit."""
import json
import math
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_desk_risk_apply as A
from sniper.settings import memecoin as cfg
from test_memecoin_desk_risk_apply import desk, _quote  # noqa: F401


def _at(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _apply(c, pos, monkeypatch, source, *, now, rails=None, caller_price=None, **kwargs):
    if source == "risk_live":
        A.apply_orders(c, pos, now_epoch=now, now_iso=_at(now), orders=[{
            "decision_id": 21, "position_id": pos["id"], "mint": pos["mint"],
            "action": "TIGHTEN", "rails_json": json.dumps(rails or {"sl_pct": 12}),
            "decided_at": now - 1, "expires_at": now + 9}], **kwargs)
    else:
        monkeypatch.setattr(cfg, "MEMECOIN_DESK_ATTENTION_S_PCT", 5.0)
        monkeypatch.setattr(cfg, "MEMECOIN_DESK_ATTENTION_THRESHOLDS_VERSION", "guard-v1")
        monkeypatch.setattr(A, "_fired_rows", lambda *_a, **_k: [
            {"id": "c", "signal": "creator_sell", "observed_at": now - 2, "thresholds_version": "guard-v1"},
            {"id": "b", "signal": "sell_burst", "observed_at": now - 1, "thresholds_version": "guard-v1"}])
        A.apply_auto(c, pos, now_epoch=now, now_iso=_at(now),
                     price=caller_price if caller_price is not None else float(pos["entry_price"]),
                     tape_path="unused", runner_path="unused", **kwargs)
    row = c.execute("SELECT * FROM memecoin_desk_risk_applies WHERE source=?", (source,)).fetchone()
    return dict(row) if row else None


@pytest.mark.parametrize("source", ["risk_live", "attention_auto"])
@pytest.mark.parametrize("bad", ["absent", "stale_observed", "stale_quote", "future_observed", "future_quote",
    "invalid_observed", "invalid_quote", "naive_quote", "zero", "nonfinite", "bad_unit", "wrong_mint",
    "wrong_chain", "wrong_position", "wrong_seam", "size", "zero_qty", "unknown_qty"])
def test_missing_or_invalid_exact_quote_refuses_both_sources(desk, monkeypatch, source, bad):
    c, pos, _db, _r = desk; now = time.time()
    before = m.effective_exit_override(c, pos["id"])
    c.execute("DELETE FROM memecoin_route_series")
    changes = {}
    if bad.endswith("observed"): changes["observed_at"] = {"stale_observed": _at(now - 31),
        "future_observed": _at(now + 1), "invalid_observed": "bad"}[bad]
    if bad.endswith("quote"): changes["quoted_at"] = {"stale_quote": _at(now - 31),
        "future_quote": _at(now + 1), "invalid_quote": "bad", "naive_quote": "2026-10-23T00:00:00"}[bad]
    if bad == "zero": changes["expected_usdc"] = 0
    if bad == "nonfinite": changes["expected_usdc"] = math.inf
    if bad == "bad_unit": changes["unit_price"] = math.inf
    if bad == "wrong_mint": changes["mint"] = "other"
    if bad == "wrong_chain": changes["network_id"] = "8453"
    if bad == "wrong_position": changes["position_id"] = pos["id"] + 1
    if bad == "wrong_seam": changes["seam"] = "peak_preview"
    if bad == "size": changes["open_qty"] = m.open_qty(c, pos["id"]) * 0.99
    if bad == "zero_qty": c.execute("DELETE FROM memecoin_fills"); changes["open_qty"] = 0
    if bad == "unknown_qty":
        m.insert_fill(c, position_id=pos["id"], kind="partial_exit", price=None, qty=None,
                      notional=None, fill_source="chain", fraction=0.1, created_at=_at(now))
    if bad != "absent": _quote(c, pos, now=now - 1, **changes)
    ack = _apply(c, pos, monkeypatch, source, now=now)
    assert ack["status"] == "refused"
    assert m.effective_exit_override(c, pos["id"]) == before


@pytest.mark.parametrize("source", ["risk_live", "attention_auto"])
def test_fresh_safe_quote_applies_once_and_auto_uses_exact_price(desk, monkeypatch, source):
    c, pos, _db, _r = desk; now = time.time(); entry = float(pos["entry_price"])
    _quote(c, pos, price=entry * 0.95, now=now - 1)
    first = _apply(c, pos, monkeypatch, source, now=now, caller_price=entry * 0.1)
    assert first["status"] == "applied"
    ov = m.effective_exit_override(c, pos["id"])
    assert ov["sl_pct"] == pytest.approx(9.75 if source == "attention_auto" else 12.0)
    _apply(c, pos, monkeypatch, source, now=now + 1)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_exit_overrides WHERE source='desk_risk'").fetchone()[0] == 1


@pytest.mark.parametrize("rails,ratio", [({"sl_pct": 12}, 0.879), ({"sl_pct": 12.5}, 0.875),
    ({"tp_pct": 25}, 1.25), ({"tp_pct": 20}, 1.21), ({"trail_pct": 10}, 0.9)])
def test_a_changed_rail_already_triggered_is_refused_not_converted_to_exit(desk, monkeypatch, rails, ratio):
    c, pos, _db, _r = desk; now = time.time()
    before = m.effective_exit_override(c, pos["id"])
    _quote(c, pos, now=now - 1, price=float(pos["entry_price"]) * ratio)
    ack = _apply(c, pos, monkeypatch, "risk_live", now=now, rails=rails)
    assert ack["status"] == "refused" and ack["reason"].startswith("would_trigger:")
    assert m.effective_exit_override(c, pos["id"]) == before
    assert ack["action"] == "TIGHTEN"


@pytest.mark.parametrize("current,peak,armed,want", [(0.95, 1.0, True, "applied"),
    (1.08, 1.2, True, "refused"), (1.08, 1.2, False, "applied")])
def test_trail_reads_latest_persisted_peak_and_real_arming(desk, monkeypatch, current, peak, armed, want):
    c, pos, _db, _r = desk; now = time.time(); entry = float(pos["entry_price"])
    # Caller snapshot stays at entry; a confirmed partial's de-risk flag gates a paired trail.
    c.execute("UPDATE memecoin_positions SET executable_peak_price=?, derisked_at=? WHERE id=?",
              (entry * peak, _at(now - 2) if armed else None, pos["id"]))
    m.set_position_exit_override(c, pos["id"], "desk_risk", tp_pct=60, sl_pct=25,
                                 trail_pct=15, derisk_pct=30, actor="prior")
    _quote(c, pos, now=now - 1, price=entry * current)
    ack = _apply(c, pos, monkeypatch, "risk_live", now=now, rails={"trail_pct": 10})
    assert ack["status"] == want


@pytest.mark.parametrize("rail,ratio,want", [("sl_pct", 0.89, "applied"), ("sl_pct", 0.85, "refused"),
    ("tp_pct", 1.1, "applied"), ("tp_pct", 1.3, "refused")])
def test_partials_use_residual_unit_price_and_require_residual_quote_size(desk, monkeypatch, rail, ratio, want):
    c, pos, _db, _r = desk; now = time.time(); qty = m.open_qty(c, pos["id"])
    m.insert_fill(c, position_id=pos["id"], kind="derisk", price=pos["entry_price"] * 2,
                  qty=qty / 2, notional=pos["entry_notional"], fill_source="chain", fraction=0.5,
                  created_at=_at(now - 2))
    _quote(c, pos, now=now - 1, price=pos["entry_price"] * ratio)
    ack = _apply(c, pos, monkeypatch, "risk_live", now=now, rails={rail: 12 if rail == "sl_pct" else 20})
    assert ack["status"] == want


@pytest.mark.parametrize("change", ["closed", "busy", "mint", "chain", "source"])
def test_changed_persisted_position_is_refused(desk, monkeypatch, change):
    c, pos, _db, _r = desk; now = time.time()
    before = m.effective_exit_override(c, pos["id"])
    field, value = {"closed": ("is_open", 0), "busy": ("close_claim_id", "claim"),
                    "mint": ("mint", "other"), "chain": ("network_id", "8453"),
                    "source": ("signal_source", "follow_activity")}[change]
    c.execute("UPDATE memecoin_positions SET %s=? WHERE id=?" % field, (value, pos["id"]))
    ack = _apply(c, pos, monkeypatch, "risk_live", now=now)
    assert ack["status"] == "refused" and m.effective_exit_override(c, pos["id"]) == before


def test_model_ack_failure_rolls_back_the_override(desk, monkeypatch):
    c, pos, _db, _r = desk; now = time.time()
    before = m.effective_exit_override(c, pos["id"])
    monkeypatch.setattr(m, "record_desk_risk_apply", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("offline")))
    A.apply_orders(c, pos, now_epoch=now, now_iso=_at(now), orders=[{
        "decision_id": 21, "position_id": pos["id"], "mint": pos["mint"], "action": "TIGHTEN",
        "rails_json": '{"sl_pct":12}', "decided_at": now - 1, "expires_at": now + 9}])
    c.commit()
    assert m.effective_exit_override(c, pos["id"]) == before


@pytest.mark.parametrize("source", ["risk_live", "attention_auto"])
def test_read_failure_is_refused_with_an_ack_when_main_ledger_is_writable(desk, monkeypatch, source):
    c, pos, _db, _r = desk; now = time.time(); before = m.effective_exit_override(c, pos["id"])
    monkeypatch.setattr(m, "latest_exit_decision_quote", lambda *_a: (_ for _ in ()).throw(sqlite3.OperationalError("offline")))
    ack = _apply(c, pos, monkeypatch, source, now=now)
    assert ack["status"] == "refused" and ack["reason"] == "exact_quote_unreadable"
    assert m.effective_exit_override(c, pos["id"]) == before


@pytest.mark.parametrize("source", ["risk_live", "attention_auto"])
@pytest.mark.parametrize("race", ["operator", "partial", "peak"])
def test_concurrent_ledger_change_cannot_race_the_validated_snapshot(desk, monkeypatch, tmp_path, source, race):
    c, pos, _db, _r = desk; now = time.time()
    path = tmp_path / "snapshot.db"
    a = sqlite3.connect(path, timeout=0.01); c.backup(a); a.row_factory = sqlite3.Row
    a.execute("PRAGMA journal_mode=WAL"); a.commit()
    b = sqlite3.connect(path, timeout=0.01); b.row_factory = sqlite3.Row
    original = A._tighten_refused_fields; changed = []
    def concurrent(conn, fresh, quote, effective, merged, override):
        result = original(conn, fresh, quote, effective, merged, override)
        if not changed:
            if race == "operator":
                m.set_position_exit_override(b, pos["id"], "operator", sl_pct=30, actor="concurrent")
            elif race == "partial":
                qty = m.open_qty(b, pos["id"]) / 2
                m.insert_fill(b, position_id=pos["id"], kind="partial_exit", price=pos["entry_price"], qty=qty,
                              notional=qty * pos["entry_price"], fill_source="chain", fraction=0.5, created_at=_at(now))
            else:
                b.execute("UPDATE memecoin_positions SET executable_peak_price=? WHERE id=?", (pos["entry_price"] * 2, pos["id"]))
            b.commit(); changed.append(race)
        return result
    monkeypatch.setattr(A, "_tighten_refused_fields", concurrent)
    assert _apply(a, pos, monkeypatch, source, now=now, rails={"trail_pct": 10}) is None
    assert changed == [race]
    a.commit()
    assert a.execute("SELECT COUNT(*) FROM memecoin_position_exit_overrides WHERE source='desk_risk'").fetchone()[0] == 0
    a.close(); b.close()


@pytest.mark.parametrize("peak", [None, 0, math.inf])
def test_nominal_peak_cannot_substitute_for_missing_or_invalid_executable_peak(desk, monkeypatch, peak):
    c, pos, _db, _r = desk; now = time.time(); before = m.effective_exit_override(c, pos["id"])
    c.execute("UPDATE memecoin_positions SET executable_peak_price=?,peak_price=100 WHERE id=?", (peak, pos["id"]))
    ack = _apply(c, pos, monkeypatch, "risk_live", now=now, rails={"trail_pct": 10})
    assert ack["status"] == "refused" and ack["reason"] == "quote_peak_invalid"
    assert m.effective_exit_override(c, pos["id"]) == before


@pytest.mark.parametrize("change,want", [({"position_id": 999}, "position_mismatch"),
    ({"decided_at": 1.0, "expires_at": 2.0}, "decision_expired"), ({"decided_at": math.inf}, "decision_time_invalid")])
def test_stale_or_misbound_injected_order_never_bypasses_identity_or_ttl(desk, change, want):
    c, pos, _db, _r = desk; now = time.time(); before = m.effective_exit_override(c, pos["id"])
    order = {"decision_id": 21, "position_id": pos["id"], "mint": pos["mint"], "action": "TIGHTEN",
             "rails_json": '{"sl_pct":12}', "decided_at": now - 1, "expires_at": now + 9, **change}
    A.apply_orders(c, pos, now_epoch=now, now_iso=_at(now), orders=[order])
    ack = dict(c.execute("SELECT * FROM memecoin_desk_risk_applies WHERE source='risk_live'").fetchone())
    assert ack["status"] == "refused" and ack["reason"] == want
    assert m.effective_exit_override(c, pos["id"]) == before


def test_attention_keeps_safe_stop_when_proposed_trail_already_crossed(desk, monkeypatch):
    c, pos, _db, _r = desk; now = time.time()
    frozen = m.get_position(c, pos["id"])["funded_contract_json"]
    _quote(c, pos, now=now - 1, price=pos["entry_price"] * 0.9)
    ack = _apply(c, pos, monkeypatch, "attention_auto", now=now)
    assert ack["status"] == "applied_partial" and ack["action"] == "TIGHTEN"
    detail = json.loads(ack["detail_json"])
    assert detail["refused_fields"] == {"trail_pct": "would_trigger:trail_pct"}
    assert detail["applied_fields"] == {"sl_pct": pytest.approx(14.5)}
    override = m.effective_exit_override(c, pos["id"])
    assert override["sl_pct"] == pytest.approx(14.5) and override["trail_pct"] == 15.0
    assert m.get_position(c, pos["id"])["funded_contract_json"] == frozen
    _apply(c, pos, monkeypatch, "attention_auto", now=now + 1)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_exit_overrides WHERE source='desk_risk'").fetchone()[0] == 1


def test_attention_with_no_trail_minimum_still_applies_its_safe_stop(desk, monkeypatch):
    c, pos, _db, _r = desk; now = time.time()
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", cfg.parse_desk_rail_bounds(""))
    _quote(c, pos, now=now - 1, price=pos["entry_price"] * 0.9)
    ack = _apply(c, pos, monkeypatch, "attention_auto", now=now)
    assert ack["status"] == "applied"
    override = m.effective_exit_override(c, pos["id"])
    assert override["sl_pct"] == pytest.approx(14.5) and override["trail_pct"] == 15.0


def test_attention_without_any_safe_changed_field_refuses_and_records_the_field(desk, monkeypatch):
    c, pos, _db, _r = desk; now = time.time(); before = m.effective_exit_override(c, pos["id"])
    c.execute("UPDATE memecoin_positions SET executable_peak_price=? WHERE id=?", (pos["entry_price"] * 1.25, pos["id"]))
    _quote(c, pos, now=now - 1, price=pos["entry_price"] * 1.1)
    ack = _apply(c, pos, monkeypatch, "attention_auto", now=now)
    assert ack["status"] == "refused" and ack["reason"] == "no_safe_tighten"
    assert json.loads(ack["detail_json"])["refused_fields"] == {"trail_pct": "would_trigger:trail_pct"}
    assert m.effective_exit_override(c, pos["id"]) == before


def test_model_keeps_its_proposal_atomic_when_only_trail_crosses(desk, monkeypatch):
    c, pos, _db, _r = desk; now = time.time(); before = m.effective_exit_override(c, pos["id"])
    _quote(c, pos, now=now - 1, price=pos["entry_price"] * 0.9)
    ack = _apply(c, pos, monkeypatch, "risk_live", now=now, rails={"sl_pct": 14.5, "trail_pct": 8})
    assert ack["status"] == "refused" and ack["reason"] == "would_trigger:trail_pct"
    assert m.effective_exit_override(c, pos["id"]) == before


@pytest.mark.parametrize("peak", [None, 0, math.inf])
def test_attention_unknown_peak_only_refuses_trail_keeps_validated_stop(desk, monkeypatch, peak):
    c, pos, _db, _r = desk; now = time.time()
    c.execute("UPDATE memecoin_positions SET executable_peak_price=? WHERE id=?", (peak, pos["id"]))
    _quote(c, pos, now=now - 1, price=pos["entry_price"] * 0.9)
    ack = _apply(c, pos, monkeypatch, "attention_auto", now=now)
    assert ack["status"] == "applied_partial"
    assert json.loads(ack["detail_json"])["refused_fields"] == {"trail_pct": "quote_peak_invalid"}
    override = m.effective_exit_override(c, pos["id"])
    assert override["sl_pct"] == pytest.approx(14.5) and override["trail_pct"] == 15.0


def test_attention_partial_ack_failure_rolls_back_its_safe_override(desk, monkeypatch):
    c, pos, _db, _r = desk; now = time.time(); before = m.effective_exit_override(c, pos["id"])
    _quote(c, pos, now=now - 1, price=pos["entry_price"] * 0.9)
    seen = []
    def failed_ack(*_a, **kwargs):
        seen.append(kwargs["status"])
        raise RuntimeError("offline partial ack failure")
    monkeypatch.setattr(m, "record_desk_risk_apply", failed_ack)
    assert _apply(c, pos, monkeypatch, "attention_auto", now=now) is None
    c.commit()
    assert seen == ["applied_partial"]
    assert m.effective_exit_override(c, pos["id"]) == before


@pytest.mark.parametrize("source", ["risk_live", "attention_auto"])
def test_current_sweep_quote_wins_over_buffered_history(desk, monkeypatch, source):
    c, pos, _db, _r = desk; now = time.time()
    before = m.effective_exit_override(c, pos["id"])
    current = _quote(c, pos, now=now - 0.1, price=pos["entry_price"] * 0.9)
    _quote(c, pos, now=now - 2, price=pos["entry_price"] * 0.95)
    ack = _apply(c, pos, monkeypatch, source, now=now, rails={"sl_pct": 9.75}, current_quote=current)
    if source == "risk_live":
        assert ack["status"] == "refused" and ack["reason"] == "would_trigger:sl_pct"
        assert m.effective_exit_override(c, pos["id"]) == before
    else:
        assert ack["status"] == "applied_partial"
        assert m.effective_exit_override(c, pos["id"])["sl_pct"] == pytest.approx(14.5)


@pytest.mark.parametrize("source", ["risk_live", "attention_auto"])
@pytest.mark.parametrize("bad", ["missing", "stale", "size", "identity", "seam"])
def test_invalid_current_quote_cannot_fall_back_to_valid_history(desk, monkeypatch, source, bad):
    c, pos, _db, _r = desk; now = time.time()
    before = m.effective_exit_override(c, pos["id"])
    current = _quote(c, pos, now=now - 1)
    current = dict(current)
    if bad == "missing": current = {}
    if bad == "stale": current["quoted_at"] = _at(now - 31)
    if bad == "size": current["open_qty"] *= 0.5
    if bad == "identity": current["mint"] = "other"
    if bad == "seam": current["seam"] = "peak_preview"
    ack = _apply(c, pos, monkeypatch, source, now=now, current_quote=current)
    assert ack["status"] == "refused"
    if bad == "missing":
        assert ack["reason"] == "no_current_exact_quote"
    assert m.effective_exit_override(c, pos["id"]) == before


def test_attention_crossed_stop_does_not_discard_an_independently_safe_trail(desk, monkeypatch):
    from sniper.services import memecoin_desk_attention as att
    c, pos, _db, _r = desk; now = time.time()
    _quote(c, pos, now=now - 1, price=pos["entry_price"] * 0.91)
    monkeypatch.setattr(att, "auto_tighten_rails", lambda *_a, **_k: {"sl_pct": 8, "trail_pct": 10})
    ack = _apply(c, pos, monkeypatch, "attention_auto", now=now)
    assert ack["status"] == "applied_partial"
    detail = json.loads(ack["detail_json"])
    assert detail["applied_fields"] == {"trail_pct": 10}
    assert detail["refused_fields"] == {"sl_pct": "would_trigger:sl_pct"}
    override = m.effective_exit_override(c, pos["id"])
    assert override["sl_pct"] == 25 and override["trail_pct"] == 10


@pytest.mark.parametrize("source", ["risk_live", "attention_auto"])
@pytest.mark.parametrize("offset", [-25200.0, 25200.0])
@pytest.mark.parametrize("age,reason", [(1.0, "tightened"), (31.0, "stale_exact_quote"),
                                      (-1.0, "quote_time_invalid")])
def test_quote_age_uses_corrected_utc_while_decisions_keep_host_time(desk, monkeypatch, source, offset, age, reason):
    from sniper.util import clock
    c, pos, _db, _r = desk; now = time.time()
    monkeypatch.setattr(clock, "offset_s", lambda: offset)
    current = _quote(c, pos, now=now - offset - age, price=pos["entry_price"] * 0.95)
    ack = _apply(c, pos, monkeypatch, source, now=now, current_quote=current)
    assert ack["reason"] == reason
    assert ack["status"] == ("applied" if age == 1.0 else "refused")
    assert ack["lag_s"] == pytest.approx(1.0)
