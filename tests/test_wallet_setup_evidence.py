import pytest


def row(origin='o1',arm='fixed',mint='M',**over):
    return dict(dict(origin=origin,chain='solana',uid='A',mint=mint,setup='pump-native',cohort_id='c1',
                     policy_id='p-'+arm,arm=arm,known_at='2026-10-01T00:00:00+00:00',
                     opened_at='2026-10-02T00:00:00+00:00',closed=True,censored=False,
                     notional=25,net_pnl=5),**over)


def summary(rows):
    from sniper.services.wallet_setup_evidence import summarize_setup
    return summarize_setup(rows,chain='solana',uid='A',setup='pump-native',arms=['fixed','trail'],
                           cutoff='2026-10-01T12:00:00+00:00')


def test_missing_arm_is_retained_as_an_evidence_gap():
    out=summary([row()])
    assert out['origins']==1 and out['complete_origins']==0 and out['missing_arm_origins']==1
    assert out['policies']['fixed']['return'] is None


def test_paired_weighting_concentration_and_leave_best_out():
    rows=[row('o1',a,'M',notional=10,net_pnl=10) for a in ['fixed','trail']]
    rows += [row('o2',a,'N',notional=90,net_pnl=-9) for a in ['fixed','trail']]
    out=summary(rows);p=out['policies']['fixed']
    assert out['distinct_coins']==2 and p['return']==pytest.approx(.01)
    assert p['leave_best_coin_out_return']==pytest.approx(-.1)
    assert p['top_coin_share']==pytest.approx(10/19)


@pytest.mark.parametrize('over,field',[({'closed':False},'open_origins'),({'censored':True},'censored_origins'),
    ({'net_pnl':None},'censored_origins'),({'known_at':'2026-10-03T00:00:00+00:00'},'late_setup_origins')])
def test_whole_origin_censors_one_bad_arm(over,field):
    out=summary([row(),row(arm='trail',**over)])
    assert out['complete_origins']==0 and out[field]==1


def test_chain_and_duplicate_origins_do_not_leak():
    out=summary([row(),row(),row(arm='trail'),row('other',chain='4663')])
    assert out['origins']==1 and out['duplicate_origins']==1 and out['complete_origins']==0


def test_bar_refusal_model_is_not_executable():
    from sniper.services.wallet_setup_evidence import blocker_evidence
    out=blocker_evidence([dict(origin='o1',gate='mcap',inputs_state='partial')],
                         [dict(origin='o1',kind='bar_model',state='ok')],origin='o1')
    assert out['outcome_kind']=='bar_model' and out['inputs_state']=='partial'


def test_paper_adjustment_rejects_safety_and_multiple_changes():
    from sniper.services.wallet_setup_evidence import freeze_adjustment
    kwargs=dict(chain='robinhood',uid='A',cutoff='2026-10-02T00:00:00+00:00',setup='V3:WETH',arms=['control'])
    with pytest.raises(ValueError): freeze_adjustment(**kwargs,change={'category':'selection','fields':{'signing':True}})
    with pytest.raises(ValueError): freeze_adjustment(**kwargs,change={'category':'selection','fields':{'sell_share':1,'losing_reentry':2}})
    out=freeze_adjustment(**kwargs,change={'category':'selection','fields':{'sell_share':False}})
    assert out['authority']=='none' and out['state']=='unvalidated' and out['chain']=='4663'


def test_multiple_cohorts_never_pool_pnl_or_concentration():
    out=summary([row('o1',a) for a in ['fixed','trail']] +
                [row('o2',a,cohort_id='c2') for a in ['fixed','trail']])
    assert out['policies']['fixed']['net_pnl'] is None
    assert out['policies']['fixed']['top_coin_share'] is None


def test_full_book_counts_and_frozen_policies_are_visible_when_net_costs_unknown():
    out=summary([row(),row(arm='trail',net_pnl=None),row('o2',closed=False)])
    assert out['full_book']['fixed']=={'rows':2,'priced':1,'open':1,'censored':0}
    assert out['policy_ids']['fixed']==['p-fixed']


def test_stored_exact_refusal_capture_is_joined_by_chain_and_origin():
    import sqlite3
    from sniper.services.wallet_setup_evidence import refusal_facts
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    from sniper.persistence.db_memecoin_captures import SCHEMA
    c.executescript(SCHEMA)
    c.execute("INSERT INTO memecoin_refusal_captures(capture_id,origin_event_id,network_id,mint,refused_at,observed_at,quote_price_basis,quote_side,quote_price) VALUES ('c','o1','solana','M','t','t','exact_route','buy',1)")
    c.execute('PRAGMA query_only=ON')
    rows=refusal_facts(c,chain='solana',origin='o1')
    assert rows[0]['kind']=='exact_quote' and rows[0]['mint']=='M'
    assert refusal_facts(c,chain='4663',origin='o1')==[]


def test_frozen_export_policies_are_scoped_and_ambiguity_is_explicit():
    from sniper.services.wallet_setup_evidence import frozen_setup_policies
    rows=[dict(row(),policy_json='{}'),dict(row('other',setup='v3'),policy_id='other',policy_json='{}'),
          dict(row('old',opened_at='2026-09-01T00:00:00+00:00'),policy_id='old',policy_json='{}')]
    out=frozen_setup_policies(rows,uid='A',setup='pump-native',arms=['fixed'],cutoff='2026-10-01T00:00:00+00:00')
    assert [p['policy_id'] for p in out['frozen_policies']]==['p-fixed'] and out['exportable']
    rows.append(dict(row('second'),policy_id='different',policy_json='{}'))
    assert not frozen_setup_policies(rows,uid='A',setup='pump-native',arms=['fixed'],cutoff='2026-10-01T00:00:00+00:00')['exportable']
