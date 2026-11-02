"""Prereg Am5/7: lower bounds for fresh unpriced quotes and stale-leg demotion."""
import json

import pytest

from sniper.office import ledger as L
from office.test_ledger import book,policy,opened,NOW,FX,MINT
from office.test_eval_entry_safety import CONFIG

@pytest.fixture(autouse=True)
def _synthetic_epoch_has_a_kill_rule(monkeypatch):
    # These stores use the synthetic epoch E1; the ledger enforces its kill rule only for listed epochs.
    from sniper.office import ledger as _L
    monkeypatch.setattr(_L,'EVAL_EPOCHS',_L.EVAL_EPOCHS+('E1',))



def mark(conn,at,lamports=None,reason=None):
    quote=None if lamports is None else {'binding_kind':'intake_canonical_pda','lamports':str(lamports),'pool':'P',
        'clock':{'host_at':at,'offset_s':0.},'proof':{'committed_through':at}}
    conn.execute('INSERT INTO marks(order_id,at,lamports,source,reason,binding_kind,quote_proof) VALUES(?,?,?,?,?,?,?)',
        ('o1',at,None if lamports is None else str(lamports),'ps_paper.sell',reason,
         'intake_canonical_pda' if quote else None,None if quote is None else json.dumps(quote)))


@pytest.fixture
def valued(book):
    conn,q,_=book
    conn.execute('UPDATE ledger_epochs SET note=?',(json.dumps({'paper_eval':CONFIG}),))
    opened(conn,q,policy())
    return conn,q


def observe(valued,at):
    return L.evaluation_safety(valued[0],epoch_id='E1',now=at,sol_usd=FX,sol_at=at)


def test_fresh_typed_quote_failure_is_entry_lower_bound_not_a_whole_book_pause(valued):
    conn,_=valued
    mark(conn,NOW+10,reason='real_quote_insufficient')
    out=observe(valued,NOW+10)
    assert out['state']=='partial_lower_bound' and out['unpriced_count']==1
    assert out['unpriced_order_ids']==['o1'] and out['peak_equity_usd']==1000.
    assert out['equity_usd']<970. and out['drawdown_equity_usd']==out['equity_usd']
    result=L.submit(conn,policy(),book_id='A',order_id='next',network_id='solana',mint=MINT+'x',pool='P',
        decision_bundle_hash='c'*64,now=NOW+10,sol_usd=FX,sol_at=NOW+10)
    assert result.state=='pending'


def test_partial_lower_bound_does_not_run_the_no_read_timer_or_raise_peak(valued):
    conn,_=valued
    for age in (10,43000,86000,100000):
        mark(conn,NOW+age,reason='real_quote_insufficient')
        out=observe(valued,NOW+age)
        assert out['state']=='partial_lower_bound' and out['halted_at'] is None
        assert out['peak_equity_usd']==1000.


def test_stale_demotion_resumes_entry_but_uses_last_mark_for_irreversible_halt(valued):
    conn,_=valued
    mark(conn,NOW+10,1_600_000_000)  # USD240; peak ~1210 with the remaining cash.
    peak=observe(valued,NOW+10)['peak_equity_usd']
    assert observe(valued,NOW+71)['reason']=='epoch_mark_stale'
    out=observe(valued,NOW+1871)  # >1800 after last mark expired at +70.
    assert out['state']=='partial_lower_bound' and out['demoted_order_ids']==['o1']
    assert out['equity_usd']<=peak-200.
    assert out['drawdown_equity_usd']==pytest.approx(peak) and out['halted_at'] is None
    mark(conn,NOW+1872,1_600_000_000)
    out=observe(valued,NOW+1872)
    assert out['state']=='known' and out['demoted_order_ids']==[] and out['repromotions']==1


def test_typed_no_route_zeros_for_the_halt_unlike_stale_demotion(valued):
    conn,_=valued
    mark(conn,NOW+10,1_600_000_000);observe(valued,NOW+10)
    mark(conn,NOW+11,reason='real_quote_insufficient')
    out=observe(valued,NOW+11)
    assert out['state']=='halted' and out['halt_kind']=='drawdown'


@pytest.mark.parametrize('reason',['clock_unknown','fx_unknown','quote_binding_mismatch','mark_unproven'])
def test_book_read_failures_never_zero_or_demote_a_leg(valued,reason):
    conn,_=valued
    mark(conn,NOW+10,1_600_000_000);observe(valued,NOW+10)
    mark(conn,NOW+11,reason=reason)
    assert observe(valued,NOW+11)['state']=='unknown'
    out=observe(valued,NOW+1900)
    assert out['state']=='unknown' and out['demoted_order_ids']==[] and out['halted_at'] is None


def test_demoted_never_priced_leg_uses_labeled_entry_cost_for_the_halt_only(valued):
    conn,_=valued
    assert observe(valued,NOW+10)['state']=='unknown'  # filled, no mark was ever observed
    conn.execute("UPDATE epoch_entry_safety SET peak_equity_usd=1190 WHERE epoch_id='E1'")
    out=observe(valued,NOW+1900)
    order=conn.execute("SELECT spend_usd FROM orders WHERE order_id='o1'").fetchone()
    cash=L.equity(conn,epoch_id='E1',now=NOW+1900,mark_max_age_s=60.,sol_usd=FX,sol_at=NOW+1900).cash
    assert out['state']=='partial_lower_bound' and out['halted_at'] is None
    assert out['equity_usd']<=1190.-200.
    assert out['drawdown_equity_usd']==pytest.approx(cash+order['spend_usd'])
    assert out['demoted_never_priced_order_ids']==['o1']


@pytest.mark.parametrize('reason',['quote_unknown','no_pool','provider_timeout','new_quote_error','ps_tape_missing'])
def test_unknown_leg_reason_pauses_then_demotes_without_inventing_a_route_failure(valued,reason):
    conn,_=valued
    mark(conn,NOW+10,1_600_000_000);observe(valued,NOW+10)
    mark(conn,NOW+11,reason=reason)
    first=observe(valued,NOW+11)
    assert first['state']=='unknown' and first['reason']=='epoch_mark_stale' and first['halted_at'] is None
    out=observe(valued,NOW+1812)
    assert out['state']=='partial_lower_bound' and out['demoted_order_ids']==['o1'] and out['halted_at'] is None
    for age in (40000,80000,100000):
        mark(conn,NOW+age,reason=reason)
        out=observe(valued,NOW+age)
        assert out['state']=='partial_lower_bound' and out['halted_at'] is None


def test_never_priced_neutral_basis_does_not_hide_realized_losses(valued):
    conn,_=valued
    observe(valued,NOW+10)
    # Fixture history: seven closed $30 loss clips. This checks the cashflow
    # aggregation/kill boundary, not a simulation of their execution/admission.
    for i in range(7):
        conn.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,"
            "policy_json,policy_hash,fill_model_version,state,requested_at,filled_at,closed_at) "
            "SELECT ?,book_id,epoch_id,book_kind,network_id,?,pool,decision_bundle_hash,policy_json,policy_hash,"
            "fill_model_version,'closed',requested_at,?,? FROM orders WHERE order_id='o1'",
            ('loss-'+str(i),'mint-loss-'+str(i),NOW+5,NOW+11))
        conn.execute("INSERT INTO cashflows(order_id,kind,at,lamports,cost_lamports,cost_basis,cost_evidence,"
            "fx_usd,fx_at,usd) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ('loss-'+str(i),'entry',NOW+11,'200000000','0','fixture','fixture',FX,NOW+11,-30.))
    out=observe(valued,NOW+1900)
    assert out['state']=='halted' and out['halt_kind']=='drawdown'
    assert out['drawdown_equity_usd'] is not None and out['demoted_never_priced_order_ids']==['o1']
