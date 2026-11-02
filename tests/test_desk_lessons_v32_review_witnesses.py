"""Offline witnesses for decision-time feature availability."""
import sqlite3

from test_desk_lessons import L, T0, W, _fixture, path, unit


def test_verdict_rank_survives_a_later_unbuyable_fill_state():
    rows = path([3e-8 + (i % 7) * 1e-10 for i in range(400)])
    changed = [dict(r, real_token=0) if r["received_at"] >= T0 else dict(r) for r in rows]
    candidate = unit(kind="counterfactual", t_ref=T0 - 20, t_fill=T0)
    good, _ = L.facts_for(candidate, L.Tape(rows), launch_at=None, fill_latency=1.5)
    unavailable, _ = L.facts_for(candidate, L.Tape(changed), launch_at=None, fill_latency=1.5)
    assert good.get("entry_rank_prior_pct") is not None
    assert unavailable["censored"] == ["no_buy"]
    assert unavailable.get("entry_rank_prior_pct") == good["entry_rank_prior_pct"]


def test_counterfactual_with_missing_response_does_not_invent_a_verdict_rank(tmp_path):
    desk, ledger, deskdb = _fixture(tmp_path)
    with sqlite3.connect(deskdb) as db:
        db.execute("UPDATE desk_tickets SET response_at=NULL WHERE ticket_id='desk:U:b1' AND stage='vet'")
    units, _ = W.discover(ledger, deskdb, T0 + 6000, W.Clock(0), 100, set())
    candidate = next(u for u in units if u["key"] == "tkt:desk:U:b1")
    facts, _ = W.compute(candidate, desk, 1.5, T0 + 6000, W.Clock(0))
    assert facts.get("entry_rank_prior_pct") is None, facts.get("entry_rank_prior_pct")
