"""Desk POST-MORTEM seat: grades a day's closed desk tickets against the ledger and writes text
proposals; one model call at most, never config (fake model, temporary DBs only)."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from test_memecoin_desk_pipeline import databases  # noqa: F401  (fixture)
from test_memecoin_desk_funnel import funnel, run_workers  # noqa: F401  (fixture)
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk as desk, memecoin_manual_intents as intents
from sniper.services import memecoin_desk_postmortem as PM

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("postmortem", ROOT / "scripts" / "memecoin_desk_postmortem.py")
PW = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PW)


def _closed_ticket(w, realized_pnl):
    from sniper.persistence import db_memecoin as ledger
    run_workers(w)
    request = (store.list_outbox(w["conn"]) or store.list_intents(w["conn"]))[0]
    now = datetime.fromtimestamp(w["clock"][0], timezone.utc).isoformat()  # same day as the frozen JUDGE
    desk.dispatch_outbox(w["conn"], w["ledger"], now_iso=now)
    pid = ledger.open_position(w["ledger"], source_user_id=None, source_handle="offline-fixture",
        mint=w["cand"]["mint"], ticker=None, entry_price=0.0001, entry_notional=25, entry_fill_source="chain",
        entry_lag_s=0, their_size_usd=0, opened_at=now, execution_mode="real", network_id="solana",
        signal_source="desk", funded_contract_json=json.dumps({"source": {"desk": request["payload"]["desk_metadata"]}}))
    intents.resolve(w["ledger"], request["intent_id"], state="filled", now_iso=now, position_id=pid,
                    result_status="filled", resolved_size_usd=25)
    desk.reconcile_intents(w["conn"], w["ledger"], now_iso=now)
    closed = now
    ledger.close_position(w["ledger"], pid, closed, realized_pnl, "trail", recorded_at=closed)
    w["ledger"].commit()
    desk.reconcile_intents(w["conn"], w["ledger"], now_iso=closed)
    return datetime.fromisoformat(closed).date().isoformat()


def _model(calls, **kw):
    def call(system, user):
        calls.append(json.loads(user))
        return {"text": "1. Sample too small; measure reason_code 3 over 10 closes.", "is_error": False,
                "call_error": None, "session_id": "s", "result_id": "r", "model_echo": PM.MODEL,
                "billing_basis": "subscription_flat", "notional_usd": 0.01, **kw}
    return call


def test_a_closed_ticket_is_graded_against_the_ledger_and_reported_once(funnel, tmp_path):
    w = funnel
    day = _closed_ticket(w, 5.0)                                       # +5 USD on a 25 USD clip
    calls, out = [], tmp_path / "pm"
    got = PW.run(w["tmp"] / "desk.db", w["tmp"] / "ledger.db", out, day, call_model=_model(calls))
    assert got["status"] == "reported" and len(calls) == 1
    (fact,) = calls[0]["closes"]
    assert fact["ticket_id"] == w["cand"]["ticket_id"] and fact["action"] == "ENTER" and fact["graded"]
    assert fact["entry_notional"] == 25 and fact["win"] is True and fact["exit_reason"] == "trail"
    assert fact["ret_pct"] == 20.0
    assert set(fact) <= {"ticket_id", "cohort_id", "producer", "state", "action", "reason", "reason_code", "rails",
                         "enter_ttl_s", "next_check_s", "fill_reason", "graded", "exit_reason", "realized_pnl",
                         "entry_notional", "opened_at", "closed_at", "ret_pct", "win"}   # facts only
    report = (out / ("%s.md" % day)).read_text()
    assert report.splitlines()[2] == ("Proposals are reports only. An adopted change ships as a new cohort or a "
                                      "new prompt_version; the running cohort is never tuned on its own outcomes "
                                      "(H1 10-22..28, H2 10-29..11-04).")
    assert "Graded closes: 1 of 1" in report and "measure reason_code 3" in report and "nothing here is applied" in report
    assert json.loads((out / ("%s.json" % day)).read_text())["call"]["result_id"] == "r"
    assert PW.run(w["tmp"] / "desk.db", w["tmp"] / "ledger.db", out, day,
                  call_model=_model(calls))["status"] == "already_reported" and len(calls) == 1


def test_a_dead_login_writes_facts_without_proposals(funnel, tmp_path):
    w = funnel
    day = _closed_ticket(w, -4.0)
    out = tmp_path / "pm"
    PW.run(w["tmp"] / "desk.db", w["tmp"] / "ledger.db", out, day,
           call_model=_model([], is_error=True, text="OAuth session expired"))
    report = (out / ("%s.md" % day)).read_text()
    assert "No proposals: model_result_error." in report and "OAuth" not in report
    assert json.loads((out / ("%s.json" % day)).read_text())["facts"][0]["win"] is False


def test_a_day_without_a_closed_ticket_makes_no_model_call(funnel, tmp_path):
    w = funnel
    calls = []
    got = PW.run(w["tmp"] / "desk.db", w["tmp"] / "ledger.db", tmp_path / "pm", "2020-01-01", call_model=_model(calls))
    assert got["tickets"] == 0 and calls == []
    empty = (tmp_path / "pm" / "2020-01-01.md").read_text()
    assert "No proposals: no desk ticket closed this day." in empty and PM.STANDING_RULE in empty


def test_a_close_without_pnl_is_never_graded_as_zero():
    t = {"ticket_id": "t", "cohort_id": "c", "producer": "U", "state": "closed", "stages": []}
    fact = PM.ticket_facts(t, {"is_open": 0, "realized_pnl": None, "entry_notional": 25})
    assert fact["graded"] is False and "win" not in fact
    assert PM.ticket_facts(t, {"is_open": 0, "realized_pnl": 1.0, "entry_notional": 0})["graded"] is False


def test_main_never_passes_an_api_key_and_honours_the_kill_knob(tmp_path, monkeypatch, capsys):
    import os
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fixture")
    monkeypatch.setenv("MEMECOIN_DESK_POSTMORTEM_ENABLED", "0")
    assert PW.main([]) == 0 and "ANTHROPIC_API_KEY" not in os.environ        # the subscription seat only
    monkeypatch.setenv("MEMECOIN_DESK_POSTMORTEM_ENABLED", "1")
    assert PW.main(["--desk-db", str(tmp_path / "none.db"), "--ledger-db", str(tmp_path / "none.db")]) == 2
    assert "refused" in capsys.readouterr().out


def test_a_dated_snapshot_of_the_model_is_its_answer_and_another_model_is_not():
    ok = {"text": "x", "call_error": None}
    assert PM.call_usable(dict(ok, model_echo=PM.MODEL)) is None                       # exact
    assert PM.call_usable(dict(ok, model_echo=PM.MODEL + "-20261015")) is None         # dated snapshot
    assert PM.call_usable(dict(ok, model_echo=None)) is None                           # no echo recorded
    for other in ("claude-haiku-5-5", PM.MODEL + "x", PM.MODEL[:-2], "", PM.MODEL + "-anything", "nonstr:dict"):
        assert PM.call_usable(dict(ok, model_echo=other)) == "model_mismatch", other
