"""Desk cohort cost gate (operator yes 2026-10-22, Gamma rules): an unknown cost is valued at a conservative
upper bound, never 0, and a cohort is refused only when known_net - sum(bounds) reaches the kill limit.
Rows stay immutable; the bound lives in the gate."""
import pytest

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from test_memecoin_desk_cohort import NOW, exact_entry_cost, ledger, position, register  # noqa: F401
from test_memecoin_desk_cohort import knobs  # noqa: F401  (autouse desk knobs)


@pytest.fixture(autouse=True)
def bound_knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_COST_GATE_LEGACY", False)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_BOUND_SOL_USD", 300.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_VERDICT_FEE_PCT", 1.25)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", 12_000_000)
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS", 5_000_000)


def unpriced_entry(conn, cohort, pid):
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id=f"entry:{pid}", usd=None, kind="execution",
                              occurred_at=NOW, provenance="receipt:unpriced", position_id=pid)


def test_the_entry_bound_is_the_unconditional_clamp_plus_the_sender_tip_times_one_and_a_half():
    from sniper.execution.solana_entry_bids import MAX_TOTAL_LAMPORTS
    from sniper.execution.solana_sender_ab import TIP_CAP_LAMPORTS
    b = m.desk_execution_cost_bound()
    assert b["lanes"] == {"entry_clamp": MAX_TOTAL_LAMPORTS, "entry_crowded": 12_000_000}
    assert b["priority_lamports"] == 12_000_000 + TIP_CAP_LAMPORTS
    sol = (12_000_000 + TIP_CAP_LAMPORTS + 2_039_280 + 5_000) / 1e9
    assert b["usd"] == pytest.approx((25.0 * 0.0125 + sol * 300.0) * 1.5)


@pytest.mark.parametrize("ceiling", [None, 6_520_000 * 100])
def test_an_entry_leg_without_a_proven_footprint_holds_the_cohort(ceiling):
    """Codex P1 on ffbaa9e43: an entry leg is bounded only through its own signed pending row's recorded footprint.
    None = signed by the old runner / unproven bytes; above the bound's priority+tip lamports = not capped."""
    from test_memecoin_desk_cohort import reserve
    conn = ledger()
    cohort = register(conn)
    if ceiling is None:
        position(conn, cohort)                                       # no signed pending row linked at all
    else:
        position(conn, cohort, ceiling=ceiling)
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    assert gate["refusal"] == "cohort_accounting_unknown" and gate["unknown_by_kind"] == {"entry_unbounded": 1}
    assert reserve(conn, cohort, mint="x") is None


def test_a_recovered_or_resigned_attempt_is_bounded_only_if_every_signed_row_is():
    """A recovered entry (opened_at = receipt time) and a resign (two signed rows of one attempt): the leg is bounded
    only when EVERY signed pending row of its attempt carries a proven footprint; one NULL holds."""
    from test_memecoin_desk_cohort import contract, reserve
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort, "rec", ceiling=6_520_000)
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") is None
    old = m.reserve_pending(conn, mint="rec2", source_user_id=None, strategy="control", notional=25, reserved_at=NOW,
                            network_id="solana", signal_source="desk", attempt_id="attempt-rec",
                            funded_contract_json=contract(cohort))
    conn.execute("UPDATE memecoin_pending_entries SET tx_sig='old-unclamped', status='confirmed' WHERE id=?", (old,))
    conn.commit()                                                     # an older signature of the SAME attempt, NULL
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    assert gate["unknown_by_kind"].get("entry_unbounded") == 1 and gate["refusal"] == "cohort_accounting_unknown"
    assert reserve(conn, cohort, mint="z") is None
    assert pid


@pytest.mark.parametrize("leg", ["exit", "partial", "exit_row"])
def test_an_unknown_exit_leg_always_holds_the_cohort(leg):
    """No exit path enforces a fee ceiling at signing (Codex's exit map), so an unpriced exit or partial is
    unbounded and holds, in the gate and the reservation (Alpha/Gamma 2026-10-22)."""
    from test_memecoin_desk_cohort import reserve
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort)
    exact_entry_cost(conn, cohort, pid)
    if leg == "exit":
        conn.execute("UPDATE memecoin_positions SET close_claim_id='c', close_sig='paid' WHERE id=?", (pid,))
    elif leg == "partial":
        conn.execute("UPDATE memecoin_positions SET derisk_claim_id='c', derisk_sig='paid', derisked_at=? "
                     "WHERE id=?", (NOW, pid))
    else:                                       # a NULL exit fee row written at the close
        m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id=f"exit:{pid}:sig", usd=None, kind="execution",
                                  occurred_at=NOW, provenance="receipt:unpriced", position_id=pid)
    conn.commit()
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    assert gate["refusal"] == "cohort_accounting_unknown" and gate["unknown_by_kind"] == {"exit_unbounded": 1}
    assert reserve(conn, cohort, mint="y") is None


def test_an_overflowing_bound_refuses_in_the_gate_and_the_reservation(monkeypatch):
    from test_memecoin_desk_cohort import reserve
    conn = ledger()
    cohort = register(conn)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_VERDICT_FEE_PCT", 1e308)
    with pytest.raises(ValueError, match="not finite"):
        m.desk_execution_cost_bound()
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") == "cohort_accounting_unknown"
    assert reserve(conn, cohort, mint="ovf") is None


@pytest.mark.parametrize("knob,value", [("MEMECOIN_DESK_BOUND_SOL_USD", 0), ("MEMECOIN_DESK_BOUND_SOL_USD", -1),
                                        ("MEMECOIN_DESK_BOUND_SOL_USD", float("nan")),
                                        ("MEMECOIN_DESK_VERDICT_FEE_PCT", 0),
                                        ("MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", -5)])
def test_a_bad_bound_component_raises_never_masked(monkeypatch, knob, value):
    monkeypatch.setattr(cfg, knob, value)
    with pytest.raises(ValueError, match=knob):
        m.desk_execution_cost_bound()


def test_an_unknown_execution_cost_is_never_valued_at_zero():
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort, ceiling=6_520_000)
    unpriced_entry(conn, cohort, pid)
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    assert gate["refusal"] is None
    assert gate["unknown_by_kind"] == {"execution": 1}
    assert gate["bounded_net_usd"] == pytest.approx(-m.desk_execution_cost_bound()["usd"])
    assert gate["bounded_net_usd"] < gate["known_net_usd"]


def test_a_near_kill_cohort_does_not_trade_through_its_unknowns():
    conn = ledger()
    cohort = register(conn)
    loser = position(conn, cohort, "loss")
    exact_entry_cost(conn, cohort, loser)
    bound = m.desk_execution_cost_bound()["usd"]
    m.close_position(conn, loser, NOW, -(200.0 - bound / 2), "stop")   # known net just above the kill
    assert not m.desk_cohort_killed(conn, cohort)
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") is None
    pid = position(conn, cohort, "next", ceiling=6_520_000)
    unpriced_entry(conn, cohort, pid)                                   # one bound pushes it past
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") == "cohort_accounting_unknown"


def test_model_invalid_fails_closed_and_only_validated_flat_or_no_call_is_zero():
    """Codex/Alpha 2026-10-22: model-invalid also covers malformed BILLED receipts; without a caller-enforced
    per-call ceiling it is an unbounded unknown and refuses, in every cohort."""
    conn = ledger()
    cohort = register(conn)
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="desk-judge:flat", usd=None, kind="model",
                              occurred_at=NOW, provenance="desk:no_model_call")
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    assert gate["refusal"] is None and gate["flat_model"] == 1 and gate["bounded_net_usd"] == 0.0
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="desk-judge:a", usd=None, kind="model",
                              occurred_at=NOW, provenance="model-invalid:" + "f" * 64)
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    assert gate["refusal"] == "cohort_accounting_unknown" and gate["unknown_by_kind"] == {"model_unbounded": 1}


def test_codex_billed_receipt_reproducer_refuses():
    conn = ledger()
    cohort = register(conn)
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="bill", usd=199.0, kind="model", occurred_at=NOW,
                              provenance="model:billed_receipt:" + "b" * 64)
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="desk-judge:x", usd=None, kind="model",
                              occurred_at=NOW, provenance="model-invalid:" + "c" * 64)
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") == "cohort_accounting_unknown"
    from test_memecoin_desk_cohort import reserve
    assert reserve(conn, cohort, mint="z") is None


def test_an_unpriced_close_still_fails_closed():
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort)
    exact_entry_cost(conn, cohort, pid)
    m.close_position(conn, pid, NOW, None, "stop")
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") == "cohort_accounting_unknown"


def test_the_legacy_knob_restores_the_old_refusal(monkeypatch):
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort, ceiling=6_520_000)
    unpriced_entry(conn, cohort, pid)
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") is None
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_COST_GATE_LEGACY", True)
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") == "cohort_accounting_unknown"


def test_the_gate_names_the_oldest_unknown_for_the_age_page():
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort, ceiling=6_520_000)
    unpriced_entry(conn, cohort, pid)
    assert m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")["oldest_unknown_at"] == NOW


def test_a_signed_pending_entry_without_its_fee_is_one_bounded_execution_unknown():
    from test_memecoin_desk_cohort import reserve
    conn = ledger()
    cohort = register(conn)
    rid = reserve(conn, cohort)
    conn.execute("UPDATE memecoin_pending_entries SET tx_sig='sig1' WHERE id=?", (rid,))
    assert m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")["unknown_by_kind"] == {"entry_unbounded": 1}
    conn.execute("UPDATE memecoin_pending_entries SET entry_fee_ceiling_lamports=6520000 WHERE id=?", (rid,))
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    assert gate["unknown_by_kind"] == {"execution": 1} and gate["oldest_unknown_at"] == NOW
    assert m.desk_cohort_accounting(conn, cohort)["unknown_costs"] == 1


# ---- through the atomic reservation (Gamma BLOCKER on 3f19c5853: the INSERT's WHERE had the old rule) ----

def _try_reserve(conn, cohort, mint):
    from test_memecoin_desk_cohort import reserve
    rid = reserve(conn, cohort, mint=mint)
    label = None if rid else m.lost_reservation_reason(
        conn, network_id="solana", mint=mint, signal_source="desk", at_iso=NOW,
        funded_contract_json=__import__("test_memecoin_desk_cohort").contract(cohort))
    if rid:
        conn.execute("DELETE FROM memecoin_pending_entries WHERE id=?", (rid,))   # probe only, keep state
        conn.commit()
    return rid, label


def _state(conn, case):
    cohort = register(conn)
    if case == "clean":
        return cohort
    pid = position(conn, cohort, "p", ceiling=6_520_000)    # opening writes one NULL entry:<pid> execution row
    if case == "one_unknown_far":
        return cohort
    if case in ("near_kill_unknown", "near_kill_priced"):
        exact_entry_cost(conn, cohort, pid)
        bound = m.desk_execution_cost_bound()["usd"]
        m.close_position(conn, pid, NOW, -(200.0 - bound / 2), "stop")
        if case == "near_kill_unknown":
            position(conn, cohort, "q", ceiling=6_520_000)
        return cohort
    if case == "unpriced_close":
        exact_entry_cost(conn, cohort, pid)
        m.close_position(conn, pid, NOW, None, "stop")
        return cohort
    exact_entry_cost(conn, cohort, pid)
    provenance, kind = {"metered_model": ("metered:per-token", "model"),
                        "flat_model": ("desk:no_model_call", "model"),
                        "flat_model_upper": ("MODEL:CLAUDE_SDK:SUBSCRIPTION_FLAT:" + "a" * 64, "model"),
                        "flat_proof_valid": ("model:claude_sdk:subscription_flat:" + "0123456789abcdef" * 4, "model"),
                        "flat_proof_upper_hex": ("model:claude_sdk:subscription_flat:" + "A" * 64, "model"),
                        "flat_proof_short": ("model:claude_sdk:subscription_flat:" + "a" * 63, "model"),
                        "flat_proof_suffixed": ("model:claude_sdk:subscription_flat:" + "a" * 64 + "x", "model"),
                        "flat_proof_not_hash": ("model:claude_sdk:subscription_flat:not-a-hash", "model"),
                        "no_call_suffixed": ("desk:no_model_call:metered", "model"),
                        "flat_proof_empty": ("model:claude_sdk:subscription_flat:", "model"),
                        "invalid_model": ("model-invalid:" + "a" * 64, "model"),
                        "unlisted_kind": ("x:y", "tip")}[case]
    if kind == "tip":      # the writer refuses unlisted kinds; a raw row stands for a future/foreign writer
        conn.execute("INSERT INTO memecoin_desk_cohort_costs(cohort_id,event_id,kind,usd,occurred_at,provenance) "
                     "VALUES(?,?,?,NULL,?,?)", (cohort, "e", kind, NOW, provenance))
        conn.commit()
    else:
        m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="e", usd=None, kind=kind, occurred_at=NOW,
                                  provenance=provenance)
    return cohort


CASES = {"clean": True, "one_unknown_far": True, "near_kill_priced": True, "flat_model": True,
         "near_kill_unknown": False, "unpriced_close": False, "metered_model": False, "unlisted_kind": False,
         "invalid_model": False, "flat_model_upper": False, "flat_proof_valid": True,
         "flat_proof_upper_hex": False, "flat_proof_short": False, "flat_proof_suffixed": False,
         "flat_proof_not_hash": False, "no_call_suffixed": False, "flat_proof_empty": False}


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_python_gate_and_the_reservation_sql_agree(case):
    conn = ledger()
    cohort = _state(conn, case)
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    rid, label = _try_reserve(conn, cohort, "probe-" + case)
    assert (gate["refusal"] is None) is CASES[case]
    assert bool(rid) is CASES[case]
    if not CASES[case]:
        assert label == "cohort accounting unknown: refused inside the reservation"   # never the race default


def test_the_legacy_knob_restores_the_old_reservation_clauses(monkeypatch):
    conn = ledger()
    cohort = _state(conn, "one_unknown_far")
    assert _try_reserve(conn, cohort, "a")[0]
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_COST_GATE_LEGACY", True)
    assert _try_reserve(conn, cohort, "b") == (None, "cohort accounting unknown: refused inside the reservation")


def test_cohort_gates_lists_every_live_cohort_with_its_oldest_unknown():
    conn = ledger()
    cohort = register(conn)
    position(conn, cohort, ceiling=6_520_000)
    [g] = m.desk_cohort_gates(conn)
    assert g["cohort_id"] == cohort and g["oldest_unknown_at"] == NOW and g["unknown_by_kind"] == {"execution": 1}
