"""Independent cb8eba81b review witnesses; temporary archive only."""
import json
import sqlite3
import pytest
from sniper.services import memecoin_desk_paper as paper
from test_memecoin_desk_pipeline import databases
from test_desk_paper import paper_mode, _open_paper, _decisions

@pytest.fixture(autouse=True)
def committed_tape(monkeypatch):
    """These fixtures write no universe heartbeat: the tape counts as committed through `now` (the frontier rule
    itself is pinned by tests/test_desk_paper_codex_frontier_witnesses.py, which does not use this)."""
    monkeypatch.setattr(paper, "_cut", lambda desk_dir, now, venue: now)


@pytest.mark.parametrize("why", ["no_state", "tape_gap", "state_incomplete", "pool_unknown"])
def test_unknown_tape_must_not_become_a_zero_valuation(databases, paper_mode, tmp_path, monkeypatch, why):
    ticket, t0 = _open_paper(databases, tmp_path)
    conn = databases[0]
    before = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    conn.execute("UPDATE desk_paper_positions SET exit_reason='b_stop', exit_requested_at=?", (paper._iso(t0+4),))
    conn.commit()
    monkeypatch.setattr(paper, "sell_proceeds", lambda *a: (None, why))
    [out] = paper.tick(conn, desk_dir=tmp_path/'desk', now=t0+6, sol_usd=180., sol_at=t0+3)
    after = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    assert out['state'] == 'exit_retry' and out['reason'] == why
    assert after['last_mark_usd'] == before['last_mark_usd'], "unknown tape fabricated a zero exit value"
    assert after['last_mark_at'] == before['last_mark_at'], "unknown tape refreshed valuation timestamp"


def test_unknown_fx_preserves_mark_and_recovers_at_later_time(databases, paper_mode, tmp_path, monkeypatch):
    ticket,t0 = _open_paper(databases,tmp_path)
    conn=databases[0]
    before=dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    conn.execute("UPDATE desk_paper_positions SET exit_reason='b_stop', exit_requested_at=?",(paper._iso(t0+4),))
    conn.commit()
    [out]=paper.tick(conn,desk_dir=tmp_path/'desk',now=t0+6,fx_at=lambda t:None)
    after=dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    assert out['reason']=='fx_unknown'
    assert (after['last_mark_usd'],after['last_mark_at'])==(before['last_mark_usd'],before['last_mark_at'])
    [out]=paper.tick(conn,desk_dir=tmp_path/'desk',now=t0+7,sol_usd=180.,sol_at=t0+6.5)
    assert out['state']=='closed'
    ex=json.loads(conn.execute("SELECT exit_json FROM desk_paper_positions").fetchone()[0])
    assert ex['at']==t0+7 and ex['attempts'][0]['reason']=='fx_unknown'


def test_exit_and_ack_rollback_together_then_survive_reopen(databases,paper_mode,tmp_path):
    ticket,t0=_open_paper(databases,tmp_path)
    conn=databases[0]
    pid=conn.execute("SELECT id FROM desk_paper_positions").fetchone()[0]
    _decisions(tmp_path/'desk',[(pid,'EXIT',None,t0+3.5,t0+60)])
    conn.execute(paper.APPLIES_SQL)
    conn.execute("CREATE TRIGGER fail_ack BEFORE INSERT ON desk_paper_risk_applies BEGIN SELECT RAISE(ABORT,'crash boundary'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError,match='crash boundary'):
        paper.risk_orders(conn,desk_dir=tmp_path/'desk',now=t0+4)
    conn.rollback()
    assert conn.execute("SELECT exit_requested_at FROM desk_paper_positions").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM desk_paper_risk_applies").fetchone()[0]==0
    conn.execute("DROP TRIGGER fail_ack")
    conn.commit()
    assert paper.risk_orders(conn,desk_dir=tmp_path/'desk',now=t0+4)=={ticket:paper.RISK_EXIT_REASON}
    db=conn.execute("PRAGMA database_list").fetchone()[2]
    other=sqlite3.connect(db);other.row_factory=sqlite3.Row
    try:
        row=dict(other.execute("SELECT * FROM desk_paper_positions").fetchone())
        assert row['exit_requested_at']==paper._iso(t0+4)
        assert other.execute("SELECT count(*) FROM desk_paper_risk_applies").fetchone()[0]==1
        [out]=paper.tick(other,desk_dir=tmp_path/'desk',now=t0+6,sol_usd=180.,sol_at=t0+3)
        assert out['state']=='closed' and out['reason']==paper.RISK_EXIT_REASON
        assert paper.risk_orders(other,desk_dir=tmp_path/'desk',now=t0+7)=={}
    finally:
        other.close()
