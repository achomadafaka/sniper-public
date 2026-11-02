"""Per-call grader: one closed Office paper order -> one descriptive row (direction, entry path, exit). Typed unknown, never zero."""
import pytest

from sniper.office import ledger
from sniper.office.call_grader import CONTRACT, grade_call, grade_closed

L = ledger.LAMPORTS


def order(**kw):
    base = dict(order_id="o1", book_id="b", epoch_id="e", network_id="solana", mint="M", state="closed",
                filled_at=100.0, closed_at=400.0, spend_lamports=str(L), peak_lamports=str(int(1.5 * L)),
                proceeds_lamports=str(int(1.2 * L)), exit_reason="trail", exit_requested_by="rails", reason=None)
    base.update(kw)
    return base


def flows(entry=-25.5, exit_=29.4):
    return [dict(kind="entry", usd=entry), dict(kind="exit", usd=exit_)]


def marks(*vals):
    return [dict(at=100.0 + 10 * i, lamports=None if v is None else str(int(v * L))) for i, v in enumerate(vals)]


def test_graded_win_has_path_and_exit_fields():
    g = grade_call(order(), flows(), marks(0.9, 1.5, 1.2))
    assert g["state"] == "graded" and g["contract"] == CONTRACT
    assert g["direction"] == "win" and g["net_usd"] == pytest.approx(3.9)
    assert g["mae_pct"] == pytest.approx(-10.0) and g["mfe_pct"] == pytest.approx(50.0)
    assert g["capture"] == pytest.approx(0.8) and g["hold_s"] == 300.0 and g["exit_reason"] == "trail"


def test_loss_and_flat_are_distinct():
    assert grade_call(order(), flows(-25, 20), marks(1.0))["direction"] == "loss"
    assert grade_call(order(), flows(-25, 25), marks(1.0))["direction"] == "flat"


def test_missing_marks_are_unknown_not_zero():
    g = grade_call(order(), flows(), marks(None, None))
    assert g["state"] == "graded" and g["mae_pct"] is None and g["unpriced_marks"] == 2
    assert grade_call(order(), flows(), [])["mae_pct"] is None


def test_open_is_pending_censored_keeps_reason():
    assert grade_call(order(state="open", closed_at=None), flows()[:1], [])["state"] == "pending"
    g = grade_call(order(state="censored", reason="entry_timeout", filled_at=None), [], [])
    assert g == dict(state="censored", reason="entry_timeout", order_id="o1", contract=CONTRACT)


def test_incomplete_cashflows_censor_never_a_loss():
    g = grade_call(order(), flows()[:1], marks(1.0))
    assert g["state"] == "censored" and g["reason"] == "cashflow_incomplete"
    assert grade_call(order(), [dict(kind="entry", usd=-1), dict(kind="exit", usd=float("nan"))], [])["state"] == "censored"


def test_zero_spend_or_peak_does_not_divide():
    g = grade_call(order(spend_lamports="0"), flows(), marks(1.0))
    assert g["mae_pct"] is None and g["mfe_pct"] is None and g["capture"] is not None
    assert grade_call(order(peak_lamports=None), flows(), marks(1.0))["capture"] is None


def test_marks_outside_hold_window_are_ignored():
    m = marks(0.5) + [dict(at=999.0, lamports=str(L // 10))]
    assert grade_call(order(), flows(), m)["mae_pct"] == pytest.approx(-50.0)


def test_grade_closed_reads_ledger_and_is_idempotent(tmp_path):
    conn = ledger.open_ledger(tmp_path / "l.db")
    conn.execute("INSERT INTO ledger_epochs VALUES('e',25,'v',0,'a','n')")
    conn.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,"
                 "fill_model_version,state,requested_at,filled_at,spend_lamports,peak_lamports,closed_at,proceeds_lamports,exit_reason) "
                 "VALUES('o1','b','e','discovery','solana','M','P','h','{}','p','f','closed',90,100,?,?,400,?, 'sl')",
                 (str(L), str(L), str(L // 2)))
    conn.execute("INSERT INTO cashflows VALUES(1,'o1','entry',100,?, '0','c','e',150,100,-25)", (str(L),))
    conn.execute("INSERT INTO cashflows VALUES(2,'o1','exit',400,?, '0','c','e',150,400,10)", (str(L // 2),))
    conn.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,"
                 "fill_model_version,state,requested_at) VALUES('o2','b','e','discovery','solana','N','P','h','{}','p','f','open',90)")
    ro = ledger.open_readonly(tmp_path / "l.db")
    rows = grade_closed(ro, epoch_id="e")
    assert [r["order_id"] for r in rows] == ["o1", "o2"]
    assert rows[0]["direction"] == "loss" and rows[1]["state"] == "pending"
    assert rows == grade_closed(ro, epoch_id="e") and rows[0]["input_hash"]
    with pytest.raises(Exception):
        ro.execute("DELETE FROM orders")
    page = grade_closed(ro, epoch_id="e", limit=1)
    assert [r["order_id"] for r in page] == ["o1"]
    assert [r["order_id"] for r in grade_closed(ro, epoch_id="e", after=tuple(page[0]["cursor"]))] == ["o2"]
    assert grade_closed(ro, epoch_id="e", after=(1e18, "")) == []
