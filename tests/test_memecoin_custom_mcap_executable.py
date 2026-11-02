"""Custom market-cap exits price the exact sell, including its signed TP floor."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from test_memecoin_real_rails_v2 import _frozen_pos

NOW = "2026-07-18T12:01:00+00:00"


@pytest.fixture
def ledger(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(R.cfg, "MEMECOIN_SOLANA_NATIVE_RUNG", False)
    monkeypatch.setattr(R.cfg, "MEMECOIN_VOLDEATH_ARMED", 0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_STOP_CONFIRM_SECONDS", 0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_NO_LOSS_SELL_S", 0)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 0)
    yield conn
    conn.close()


def position(conn, *, entry=1.0, qty=50.0, supply=1000.0, entry_mcap=None, fills=True):
    frozen = _frozen_pos(tp=0, stop=0, catastrophe=0, derisk=0, trail=0, debounce=1)
    pid = m.open_position(
        conn, source_user_id="u", source_handle="h", mint="MINT", ticker="T",
        entry_price=entry, entry_notional=entry * qty, entry_mcap=entry_mcap,
        entry_fill_source="chain", entry_lag_s=0, their_size_usd=0,
        opened_at=frozen["opened_at"], strategy=R.cfg.MEMECOIN_REAL_POLICY,
        execution_mode="real", network_id="solana",
        **{key: frozen[key] for key in ("funded_contract_version", "funded_contract_hash",
                                       "funded_contract_json")})
    if fills:
        m.insert_fill(conn, position_id=pid, kind="entry", price=entry, qty=qty,
                      notional=entry * qty, fill_source="chain", fraction=1, created_at=NOW)
    if supply is not None:
        conn.execute("UPDATE memecoin_positions SET fdv_supply=? WHERE id=?", (supply, pid))
        conn.commit()
    return m.get_position(conn, pid)


def sweep(conn, pos, *, expected, market_cap, input_quantity=None):
    calls = []
    preview = {"expected_usdc": expected}
    if input_quantity is not None:
        preview["input_quantity"] = input_quantity
    R.real_exit_sweep(conn, {"solana": {
        "preview_exit_fn": lambda _p: preview if expected is not None else None,
        "exit_fn": lambda _c, _p, reason, _now: calls.append(reason) or {"status": "unknown"},
    }}, price_fn=lambda _m: {"MINT": {"price": market_cap / pos["fdv_supply"]}},
                     now_fn=lambda: NOW)
    return calls


@pytest.mark.parametrize("reason,target,expected,market_cap,want", [
    ("real_tp", 1500, 55, 2000, False),
    ("real_tp", 1500, 80, 1000, True),
    ("b_stop", 800, 45, 700, False),
    ("b_stop", 800, 35, 1200, True),
])
def test_nomination_and_confirmation_follow_sell_cap_in_both_directions(
        ledger, reason, target, expected, market_cap, want):
    pos = position(ledger)
    field = "tp_target_mcap" if reason == "real_tp" else "sl_target_mcap"
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, sl_pct=0,
                                 **{field: target})
    override = m.effective_exit_override(ledger, pos["id"])
    allowed, detail = R.executable_exit_allowed(ledger, pos, reason,
        {"expected_usdc": expected}, override=override, current_mcap=market_cap)
    assert allowed is want
    assert detail["executable_mcap"] == pytest.approx(expected / 50 * 1000)
    assert detail["executable_mcap_basis"] == "fdv"
    assert sweep(ledger, pos, expected=expected, market_cap=market_cap) == ([reason] if want else [])
    if want:
        audit = ledger.execute("SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=? "
                               "AND reason=?", (pos["id"], reason)).fetchone()
        detail = json.loads(audit[0])
        assert detail["current_mcap"] == pytest.approx(expected / 50 * 1000)
        assert detail["market_mcap"] == market_cap
        assert detail["mcap_basis"] == "fdv"
    assert m.get_position(ledger, pos["id"])["funded_contract_json"] == pos["funded_contract_json"]


@pytest.mark.parametrize("incident,entry,qty,supply,target,expected,market_cap,reason", [
    (74040, 50 / 210088.622563, 210088.622563, 1e9, 285000, 41.062862, 292740, "real_tp"),
    (73954, .00006705281792054127, 49.427566 / (70397.64631902956 / 961360841.923376),
     961360841.923376, 60000, 49.427566, 52947.493987236645, "b_stop"),
])
def test_reported_market_crossings_do_not_authorize_uncrossed_sell_quotes(
        ledger, incident, entry, qty, supply, target, expected, market_cap, reason):
    pos = position(ledger, entry=entry, qty=qty, supply=supply)
    field = "tp_target_mcap" if reason == "real_tp" else "sl_target_mcap"
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, sl_pct=0,
                                 **{field: target})
    assert sweep(ledger, pos, expected=expected, market_cap=market_cap) == [], incident


@pytest.mark.parametrize("reason", ["real_tp", "b_stop"])
@pytest.mark.parametrize("unknown", ["basis", "quantity", "preview"])
def test_unknown_sell_inputs_cannot_prove_a_custom_crossing(ledger, reason, unknown):
    pos = position(ledger, supply=None if unknown == "basis" else 1000, fills=unknown != "quantity")
    override = {"tp_pct": 0, "sl_pct": 0,
                "tp_target_mcap" if reason == "real_tp" else "sl_target_mcap": 1500}
    allowed, _ = R.executable_exit_allowed(ledger, pos, reason,
        None if unknown == "preview" else {"expected_usdc": 100 if reason == "real_tp" else 10},
        override=override, current_mcap=2000 if reason == "real_tp" else 1000)
    assert allowed is False


@pytest.mark.parametrize("reason,override,expected", [
    ("real_tp", {"tp_pct": 20, "tp_target_mcap": 5000}, 65),
    ("b_stop", {"sl_pct": -20, "sl_target_mcap": 100}, 35),
])
def test_uncrossed_custom_cap_preserves_independent_percent_rails(ledger, reason, override, expected):
    pos = position(ledger)
    assert R.executable_exit_allowed(ledger, pos, reason, {"expected_usdc": expected},
                                     override=override, current_mcap=None)[0]


def test_entry_ratio_is_the_existing_fallback_and_execution_quantity_is_authoritative(ledger):
    pos = position(ledger, supply=None, entry_mcap=1000)
    allowed, detail = R.executable_exit_allowed(ledger, pos, "real_tp", {"expected_usdc": 80},
        override={"tp_pct": 0, "tp_target_mcap": 1500}, current_mcap=1, quote_qty=40)
    assert allowed and detail["executable_mcap"] == 2000
    assert detail["executable_mcap_basis"] == "entry_ratio"


@pytest.mark.parametrize("input_quantity,want", [(40, True), (0, False), (-1, False),
                                                (float("nan"), False), (float("inf"), False)])
def test_custom_target_uses_the_quotes_own_human_quantity_when_supplied(ledger, input_quantity, want):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, tp_target_mcap=1800)
    allowed, _ = R.executable_exit_allowed(ledger, pos, "real_tp",
        {"expected_usdc": 80, "input_quantity": input_quantity},
        override=m.effective_exit_override(ledger, pos["id"]), current_mcap=2000)
    assert allowed is want
    assert sweep(ledger, pos, expected=80, market_cap=2000,
                 input_quantity=input_quantity) == (["real_tp"] if want else [])


@pytest.mark.parametrize("input_quantity", [0, -1, True, "bad", float("nan"), float("inf")])
def test_explicit_invalid_quote_quantity_never_falls_back_to_a_crossed_ledger_cap(ledger, input_quantity):
    pos = position(ledger)
    override = {"tp_pct": 0, "tp_target_mcap": 1500}
    assert not R.executable_exit_allowed(ledger, pos, "real_tp",
        {"expected_usdc": 80, "input_quantity": input_quantity}, override=override)[0]


def test_quote_quantity_changes_custom_caps_only(ledger):
    pos = position(ledger)
    allowed, detail = R.executable_exit_allowed(ledger, pos, "real_tp",
        {"expected_usdc": 80, "input_quantity": 100}, override={"tp_pct": 50})
    assert allowed
    assert detail["executable_unit_price"] == 1.6


def test_typed_preview_with_unknown_actual_quantity_cannot_use_ledger_as_evidence(ledger):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, tp_target_mcap=1500)
    preview = SimpleNamespace(expected_usdc=80, input_quantity=None)
    assert not R.executable_exit_allowed(ledger, pos, "real_tp", preview,
                                         override=m.effective_exit_override(ledger, pos["id"]))[0]
    calls = []
    R.real_exit_sweep(ledger, {"solana": {
        "preview_exit_fn": lambda _p: preview,
        "exit_fn": lambda _c, _p, reason, _now: calls.append(reason),
    }}, price_fn=lambda _m: {"MINT": {"price": 2}}, now_fn=lambda: NOW)
    assert calls == []


@pytest.mark.parametrize("expected,market_cap,want", [(55, 2000, False), (80, 1000, True), (None, 2000, False)])
def test_active_band_uses_the_same_fresh_sell_quote_guard(ledger, expected, market_cap, want):
    pos = position(ledger)
    m.set_exit_target(ledger, pos["id"], 1500, 1700, now_iso=NOW)
    assert sweep(ledger, pos, expected=expected, market_cap=market_cap) == (["mcap_target"] if want else [])
    assert m.active_exit_target(ledger, pos["id"]) is not None


@pytest.mark.parametrize("minimum,want", [(75, True), (74.999999, False), (None, False), (0, False),
                                        (float("nan"), False), (float("inf"), False)])
@pytest.mark.parametrize("band", [False, True])
def test_current_custom_tp_requires_its_encoded_minimum_to_clear_the_target(ledger, minimum, want, band):
    pos = position(ledger)
    if band:
        m.set_exit_target(ledger, pos["id"], 1500, 1700, now_iso=NOW)
    else:
        m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, tp_target_mcap=1500)
    allowed, detail = R.confirm_execution_target(ledger, pos,
        "mcap_target" if band else "real_tp", 80, 50, minimum_usdc=minimum)
    assert allowed is want
    assert detail["execution_quote"] is True
    audit = ledger.execute("SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE position_id=? "
        "AND reason=?", (pos["id"], "mcap_target_execution" if band else "real_tp_execution")).fetchone()
    assert audit is not None
    assert bool(audit[0]) is want
    assert json.loads(audit[1]) == detail


def test_execution_reloads_current_override_and_keeps_percent_tp_semantics(ledger):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, tp_target_mcap=1500)
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=20)
    assert R.confirm_execution_target(ledger, pos, "real_tp", 65, 50)[0]
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0)
    assert not R.confirm_execution_target(ledger, pos, "real_tp", 65, 50, minimum_usdc=60)[0]


def test_execution_refuses_a_cancelled_band(ledger):
    pos = position(ledger)
    m.set_exit_target(ledger, pos["id"], 1500, 1700, now_iso=NOW)
    m.cancel_exit_target(ledger, pos["id"], NOW)
    assert not R.confirm_execution_target(ledger, pos, "mcap_target", 80, 50, minimum_usdc=75)[0]


def test_execution_refuses_unknown_current_target_instead_of_raising_into_fail_open(ledger, monkeypatch):
    pos = position(ledger)
    def broken(*_a):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(m, "effective_exit_override", broken)
    allowed, detail = R.confirm_execution_target(ledger, pos, "real_tp", 80, 50, minimum_usdc=75)
    assert not allowed and detail["code"] == "execution_target_check_failed"
    audit = ledger.execute("SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE position_id=? "
        "AND reason='real_tp_execution'", (pos["id"],)).fetchone()
    assert audit is not None and not audit[0]
    assert json.loads(audit[1])["code"] == "execution_target_check_failed"


def test_audit_failure_cannot_change_a_target_refusal(ledger, monkeypatch):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, tp_target_mcap=1500)
    def broken(*_a, **_k):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(m, "record_exit_trigger_audit", broken)
    allowed, detail = R.confirm_execution_target(ledger, pos, "real_tp", 80, 50, minimum_usdc=70)
    assert not allowed and detail["code"] == "execution_target_floor_not_met"


@pytest.mark.parametrize("expected,quantity", [(None, 50), (35, float("nan")), (35, None)])
def test_unreadable_execution_quote_cannot_authorize_custom_cap_only_stop(ledger, expected, quantity):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", sl_pct=0, sl_target_mcap=800)
    allowed, detail = R.confirm_execution_stop(ledger, pos, expected, quantity)
    assert not allowed
    assert detail["execution_quote"] is True


def test_unreadable_execution_quote_keeps_independently_active_percent_stop(ledger):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", sl_pct=-20, sl_target_mcap=800)
    allowed, detail = R.confirm_execution_stop(ledger, pos, None, 50)
    assert allowed
    assert detail["code"] == "execution_quote_unreadable_sweep_verdict_stands"


@pytest.mark.parametrize("custom_stop", [False, True])
def test_current_override_read_failure_holds_only_a_confirmed_custom_stop(ledger, monkeypatch, custom_stop):
    pos = position(ledger)
    def broken(*_a):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(m, "effective_exit_override", broken)
    if not custom_stop:
        with pytest.raises(sqlite3.OperationalError):
            R.confirm_execution_stop(ledger, pos, 35, 50)
        return
    allowed, detail = R.confirm_execution_stop(ledger, pos, 35, 50, confirmed_custom_stop=True)
    assert not allowed and detail["code"] == "execution_custom_stop_check_failed"
    audit = ledger.execute("SELECT allowed,detail FROM memecoin_exit_trigger_audit "
        "WHERE position_id=? AND reason='b_stop_execution'", (pos["id"],)).fetchone()
    assert audit is not None and not audit[0]
    assert json.loads(audit[1]) == detail


@pytest.mark.parametrize("stop_pct,custom_stop", [(0, True), (-20, False), (-50, True)])
def test_sweep_carries_custom_stop_witness_only_without_independent_percent_stop(ledger, stop_pct, custom_stop):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", sl_pct=stop_pct, sl_target_mcap=800)
    seen, passed = [], []
    def execute(_c, current, reason, _now):
        seen.append((reason, current.get("_exit_custom_stop_confirmed")))
        passed.append(current)
        return {"status": "unknown"}
    R.real_exit_sweep(ledger, {"solana": {"preview_exit_fn": lambda _p: {"expected_usdc": 35},
        "exit_fn": execute}}, price_fn=lambda _m: {"MINT": {"price": 1.2}}, now_fn=lambda: NOW)
    assert seen == [("b_stop", custom_stop)]
    assert "_exit_custom_stop_confirmed" not in passed[0]


def test_configured_but_uncrossed_percent_stop_cannot_keep_an_unreadable_custom_exit(ledger):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", sl_pct=-50, sl_target_mcap=800)
    allowed, detail = R.executable_exit_allowed(ledger, pos, "b_stop", {"expected_usdc": 35},
        override=m.effective_exit_override(ledger, pos["id"]))
    assert allowed and detail.get("mcap_stop_confirmed") and not detail.get("percent_stop_confirmed")
    allowed, detail = R.confirm_execution_stop(ledger, pos, None, 50, confirmed_custom_stop=True)
    assert not allowed and detail["code"] == "execution_custom_stop_unknown_hold"


@pytest.mark.parametrize("reason", ["real_tp", "mcap_target"])
@pytest.mark.parametrize("shared_chain,want", [("solana", False), ("56", True)])
def test_custom_tp_refuses_unbound_quantity_when_two_positions_share_custody_mint(
        ledger, reason, shared_chain, want):
    pos = position(ledger)
    # Legacy shared-custody rows can predate the one-position-per-coin index.
    ledger.execute("DROP INDEX idx_mc_pos_open_uniq_v3")
    other = position(ledger)
    ledger.execute("UPDATE memecoin_positions SET network_id=? WHERE id=?", (shared_chain, other["id"]))
    ledger.commit()
    if reason == "mcap_target":
        m.set_exit_target(ledger, pos["id"], 1500, 1700, now_iso=NOW)
    else:
        m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, tp_target_mcap=1500)
    allowed, detail = R.confirm_execution_target(ledger, pos, reason, 80, 50, minimum_usdc=75)
    assert allowed is want
    if not want:
        assert detail["code"] == "execution_target_quantity_unbound"


def test_shared_custody_guard_keeps_percent_tp_semantics(ledger):
    pos = position(ledger)
    ledger.execute("DROP INDEX idx_mc_pos_open_uniq_v3")
    position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=20)
    assert R.confirm_execution_target(ledger, pos, "real_tp", 65, 50)[0]
