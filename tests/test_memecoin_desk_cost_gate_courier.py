"""Courier side of the cost gate: a held ENTER carries its facts, an expiry caused by the hold is a censored
execution (stage courier / enter_expired_accounting), and the worker logs once per ticket and pages once."""
from datetime import datetime, timedelta

from sniper.persistence import db_memecoin_desk as store
from test_memecoin_desk_pipeline import RESPONSE, api, databases, decide, ready  # noqa: F401


def _held(databases):
    cohort, ticket = ready(databases)
    decide(databases, ticket, model_cost_usd=None, cost_provenance="test:unpriced")   # metered unknown: fails closed
    return cohort, ticket


def test_a_held_enter_carries_the_gate_facts(databases):
    cohort, _ = _held(databases)
    [row] = api().dispatch_outbox(*databases, now_iso=RESPONSE)
    assert row["state"] == "pending" and row["reason"] == "cohort_accounting_unknown"
    assert row["hold"]["cohort_id"] == cohort and row["hold"]["unknown_by_kind"] == {"model_unbounded": 1}
    assert row["hold"]["known_net_usd"] == 0.0 and row["hold"]["bound_usd"] > 0


def _after_expiry(databases):
    expires = store.list_outbox(databases[0])[0]["expires_at"]
    return (datetime.fromisoformat(expires) + timedelta(seconds=1)).isoformat()


def _head(databases, ticket):
    return databases[0].execute("SELECT state FROM desk_ticket_heads WHERE ticket_id=?", (ticket,)).fetchone()[0]


def _outbox(databases, ticket):
    return databases[0].execute("SELECT state, reason, first_hold_at FROM desk_outbox WHERE ticket_id=?",
                                (ticket,)).fetchone()


def test_an_expiry_after_a_pre_expiry_hold_is_censored_atomically(databases):
    _, ticket = _held(databases)
    api().dispatch_outbox(*databases, now_iso=RESPONSE)                       # the hold, recorded once
    assert _outbox(databases, ticket)[2] is not None
    [row] = api().dispatch_outbox(*databases, now_iso=_after_expiry(databases))
    assert (row["state"], row["reason"]) == ("expired", "enter_expired_accounting")
    assert tuple(_outbox(databases, ticket)[:2]) == ("expired", "enter_expired_accounting")
    assert _head(databases, ticket) == "expired"                              # never flipped to skipped
    stage = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "courier"][0]
    assert stage["inputs"]["unknown_by_kind"] == {"model_unbounded": 1} and stage["inputs"]["first_hold_at"]


def test_a_cost_unknown_only_after_the_ttl_is_an_ordinary_expiry(databases):
    """Timeline A (Codex P2): no hold was observed before the TTL, so the gate state at the late pass is not
    the cause: entry_expired, no courier stage."""
    cohort, ticket = ready(databases)
    decide(databases, ticket)                                                  # priced: no hold
    from sniper.persistence import db_memecoin as ledger
    ledger.record_desk_cohort_cost(databases[1], cohort_id=cohort, event_id="late", usd=None, kind="model",
                                   occurred_at=RESPONSE, provenance="metered:late")
    [row] = api().dispatch_outbox(*databases, now_iso=_after_expiry(databases))
    assert row["reason"] == "entry_expired"
    assert not [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "courier"]


def test_a_hold_that_cleared_before_the_late_pass_keeps_its_censor_label(databases):
    """Timeline B (Codex P2): held during the TTL, priced before the post-expiry pass: still censored."""
    cohort, ticket = _held(databases)
    api().dispatch_outbox(*databases, now_iso=RESPONSE)
    from sniper.persistence import db_memecoin as ledger
    ledger.record_desk_cohort_cost(databases[1], cohort_id=cohort, event_id="desk-judge:" + ticket, usd=0,
                                   kind="model", occurred_at=RESPONSE, provenance="test:priced-later")
    [row] = api().dispatch_outbox(*databases, now_iso=_after_expiry(databases))
    assert row["reason"] == "enter_expired_accounting"


def test_a_failed_stage_write_leaves_neither_stage_nor_expiry(databases, monkeypatch):
    _, ticket = _held(databases)
    api().dispatch_outbox(*databases, now_iso=RESPONSE)
    real = store._stage_row
    def fail_on_courier(row):          # only the courier stage insert, never the ticket reads (Codex P2)
        if row["stage"] == "courier":
            raise RuntimeError("injected")
        return real(row)
    monkeypatch.setattr(store, "_stage_row", fail_on_courier)
    import pytest
    with pytest.raises(RuntimeError, match="injected"):
        api().dispatch_outbox(*databases, now_iso=_after_expiry(databases))
    monkeypatch.setattr(store, "_stage_row", real)
    assert _outbox(databases, ticket)[0] == "pending" and _head(databases, ticket) == "queued"
    assert not [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "courier"]
    [row] = api().dispatch_outbox(*databases, now_iso=_after_expiry(databases))
    assert row["reason"] == "enter_expired_accounting" and _head(databases, ticket) == "expired"


def test_the_worker_logs_once_per_ticket_and_pages_once_per_cohort_and_once_aged(tmp_path, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "jw", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_judge_worker.py")
    jw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jw)
    hold = {"cohort_id": "desk-cohort:U:abc", "unknown_by_kind": {"execution": 1}, "flat_model": 0,
            "unpriced_closes": 0, "known_net_usd": 1.0, "bounded_net_usd": -3.0, "bound_usd": 4.0,
            "oldest_unknown_at": "2026-10-22T18:00:00+00:00", "legacy": False}
    out = [{"ticket_id": "t1", "reason": "cohort_accounting_unknown", "hold": hold},
           {"ticket_id": "t2", "reason": "cohort_accounting_unknown", "hold": hold}]
    pages, memo, state = [], {}, tmp_path / "hold.json"
    at = datetime.fromisoformat("2026-10-22T18:30:00+00:00").timestamp()
    alert = lambda kind, text: pages.append(text)
    assert len(jw.note_holds(out, state, alert, memo)) == 2
    assert len(jw.note_holds(out, state, alert, memo)) == 0                                  # logged once
    assert len(pages) == 1 and pages[0].startswith("ENTER held")
    gate = {**hold}
    assert jw.note_aged([gate], state, alert, {}, now_epoch=at, page_after_s=3600) == []    # 30 min: not yet
    assert len(jw.note_aged([gate], state, alert, {}, now_epoch=at + 3600, page_after_s=3600)) == 1  # no held ENTER
    assert jw.note_aged([gate], state, alert, {}, now_epoch=at + 7200, page_after_s=3600) == []  # restart: no re-page
    assert len(pages) == 2 and "unpriced > 60 min" in pages[1]
