"""Disposable producer schemas only; no production reads or model calls."""
import importlib
import json
import sqlite3

import pytest

from sniper.office import ledger as L, quota_measurement as Q, seat_events as E, v2_explore as X

START=1793318400.
END=START+3600
EPOCH='office-v2-explore-1'


def report(o,l,q,**kw):
    return importlib.import_module('sniper.office.explore_report').explore_report(o,l,q,now=kw.pop("now",END),**kw)


@pytest.fixture
def dbs(tmp_path):
    o=sqlite3.connect(':memory:');o.row_factory=sqlite3.Row
    o.execute('CREATE TABLE epochs(id TEXT PRIMARY KEY,payload_json TEXT)')
    X.ensure_schema(o);E.ensure_schema(o)
    epoch=dict(epoch_id=EPOCH,opened_at=START,closed_at=None,kind='v2_explore',exploratory=True,evaluation_allowed=False,
               release_sha='a'*40,record_sha256=X.RECORD_SHA256,book_id=EPOCH,book={'clip_usd':25.,'max_concurrent':3,'daily_loss_usd':100.})
    o.execute('INSERT INTO epochs VALUES(?,?)',(EPOCH,json.dumps(epoch)))
    o.execute('INSERT INTO office_v2_changes VALUES(?,?,?,?,?,?,?)',(EPOCH,1,START,'a'*40,'b'*64,'epoch_open','opened'))
    l=L.open_ledger(tmp_path/'ledger.db');X.provision_ledger(l,now=START)
    q=sqlite3.connect(':memory:');q.row_factory=sqlite3.Row;Q.ensure_schema(q)
    yield o,l,q
    for c in (o,l,q):c.close()


def order(l, oid='one',at=START+10):
    l.execute('INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,requested_at,filled_at,spend_usd,tokens_atomic) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (oid,EPOCH,EPOCH,'evaluated','solana',oid,'pool','h','{}','h','office-ps-paper-v1','open',at,at,25.,'100'))


def label(o,l,oid='one',source='self',hash='c'*64):
    l.execute('INSERT INTO decision_receipts(action_key,decision_id,book_id,bundle_hash,outcome,order_id,actor_id,created_at,epoch_id,judgements_hash,decision_label) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (oid,oid,EPOCH,'h','submitted',oid,'judge',START+10,EPOCH,hash,'ENTER'))
    o.execute('INSERT INTO office_v2_order_labels VALUES(?,?,?,?,?,?,?,?,?)',
        (oid,hash,EPOCH,'judge',1,source,2.,START+10,json.dumps(dict(segment_id=1,tainted_by_web=True,operator_influenced=False,virtual_quote_source='exact'))))


def wake(q,n,at,over=False):
    Q.record_wake(q,epoch_id=EPOCH,actor_id='head',generation=1,incarnation='one',session_id='session',wake_id=str(n),
        account_alias='claude-main',outcome='ok',counters=dict(input_tokens=100,output_tokens=10,cache_creation_input_tokens=0,cache_read_input_tokens=50),
        result_id=str(n),started_at=at-1,ended_at=at,per_wake_max=50 if over else 500,usage_scope='per_wake',sdk_version='pinned')


def test_segment_hole_refuses_section(dbs):
    o,l,q=dbs;o.execute('INSERT INTO office_v2_changes VALUES(?,?,?,?,?,?,?)',(EPOCH,3,START+20,'a'*40,'c'*64,'release','next'))
    assert report(*dbs)['segments']['state']=='unknown'


def test_missing_or_wrong_hash_label_never_defaults_feed(dbs):
    o,l,q=dbs;order(l);label(o,l,hash='c'*64)
    o.execute('UPDATE office_v2_order_labels SET decision_hash=?',('d'*64,))
    row=report(*dbs)['orders']['items'][0]
    assert row['label_state']=='label_missing' and row['source']=='unknown'


def test_outside_wakes_cannot_dilute_window_rate(dbs):
    o,l,q=dbs;wake(q,0,START+10,over=True)
    before=report(*dbs,window_start=START,window_end=END)['seats']['items'][0]['proof']['breach_rate']
    for n in range(1,101):wake(q,n,END+n)
    after=report(*dbs,window_start=START,window_end=END)['seats']['items'][0]['proof']['breach_rate']
    assert before==after==1.


def test_unpriced_equity_withholds_net_and_keeps_count(dbs):
    order(dbs[1]);out=report(*dbs)['book']
    assert out['now']['unpriced_count']==1 and out['office_net'] is None
    assert out['statistics']['withheld_reason']


def test_cap_does_not_read_as_complete_prefix(dbs):
    o,l,q=dbs
    o.executemany('INSERT INTO office_v2_candidates VALUES(?,?,?,?,?,?)',[(EPOCH,'solana',str(i),None,'scout',START+10) for i in range(2001)])
    out=report(*dbs)['candidates']
    assert out['state']=='unknown' and out['complete'] is False


def test_self_has_no_null_counterpart(dbs):
    o,l,q=dbs;order(l);label(o,l)
    out=report(*dbs)
    assert out['orders']['by_source']['self']==1
    assert out['null_arm']['self']['count']==1 and out['null_arm']['self']['null_counterpart'] is None


@pytest.mark.parametrize('kw',[{'epoch_id':'office-paper-eval-1'},{'window_start':START},
    {'window_start':END,'window_end':START},{'window_start':START,'window_end':float('nan')},
    {'window_start':START,'window_end':START+32*86400}])
def test_bad_epoch_or_window_refuses(dbs,kw):
    out=report(*dbs,**kw)
    assert out['state']=='unknown' and out['reason']=='window_bounds_unknown'


def test_output_budget_degrades_counts_without_invalid_json(dbs,monkeypatch):
    import sniper.office.explore_report as R
    monkeypatch.setattr(R,'OUTPUT_BYTES',65536)
    o,l,q=dbs
    for i in range(300):
        oid=str(i);order(l,oid);label(o,l,oid)
    out=report(*dbs);raw=R._json(out)
    assert len(raw)<=65536 and out['orders']['truncated'] is True
    assert out['orders']['count']==300 and out['orders']['items'] and out['orders']['items_omitted']==300-len(out['orders']['items'])


def test_no_writes_and_touched_day_cost(dbs):
    before=[c.total_changes for c in dbs]
    out=report(*dbs)
    assert [c.total_changes for c in dbs]==before
    assert out['book']['cost']['cost_basis']=='touched_days_full'
    assert out['book']['cost']['touched_days']==['2026-10-30']
    assert out['book']['cost']['amount_usd']==pytest.approx(800/31)


def test_override_cannot_silently_keep_original_cost(dbs):
    o,l,q=dbs;o.execute('INSERT INTO office_v2_changes VALUES(?,?,?,?,?,?,?)',(EPOCH,2,START+20,'a'*40,'c'*64,'override','Alpha changed dollar basis'))
    out=report(*dbs)
    assert out['book']['cost']['state']=='unknown' and out['book']['office_net'] is None


def test_absent_sources_and_empty_unprovisioned_epoch_are_unknown(dbs):
    out=report(*dbs)
    assert out['integrity']['writer_audit']['reason']=='brain_audit_source_unavailable'
    assert out['null_arm']['state']=='unknown' and out['coverage']['state']=='unknown'
    o,l,q=dbs;o.execute('DELETE FROM epochs')
    assert report(*dbs)['state']=='unknown'


def test_sqlite_authorizer_confirms_select_only(dbs):
    def auth(action,*args):
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY
    for c in dbs:c.set_authorizer(auth)
    try:
        out=report(*dbs)
        assert out['epoch_id']==EPOCH and out['book']['cost']['state']=='known'
    finally:
        for c in dbs:c.set_authorizer(None)


def test_missing_schema_and_invalid_json_are_unknown_without_backend_text(dbs):
    o,l,q=dbs;o.execute("UPDATE epochs SET payload_json='{broken secret payload'")
    out=report(*dbs)
    assert out['state']=='unknown' and 'secret' not in json.dumps(out)


def test_window_end_is_exclusive_for_orders_and_wakes(dbs):
    o,l,q=dbs;order(l,at=END);wake(q,0,END)
    out=report(*dbs,window_start=START,window_end=END)
    assert out['orders']['count']==0
    assert out['seats']['items'][0]['proof']['cohort_wakes']==0


def test_overlapping_outage_seconds_are_not_counted_twice(dbs):
    o,l,q=dbs;order(l)
    l.executemany('INSERT INTO outages(order_id,kind,from_at,to_at) VALUES(?,?,?,?)',
                  [('one','a',START+10,START+30),('one','b',START+20,START+40)])
    assert report(*dbs)['outages']['sampled_seconds']==30


@pytest.mark.parametrize('raw,mapped',[('pumpswap_rpc:pool_state','exact'),('tape_tip','tape'),('ps_tape','tape'),('pumpswap_grpc:buy_event_virtual_quote','tape')])
def test_producer_quote_sources_keep_raw_and_map_stratum(dbs,raw,mapped):
    o,l,q=dbs;order(l);label(o,l)
    o.execute('UPDATE office_v2_order_labels SET labels_json=?',(json.dumps(dict(segment_id=1,virtual_quote_source=raw)),))
    row=report(*dbs)['orders']['items'][0]
    assert row['virtual_quote_source_raw']==raw and row['virtual_quote_source']==mapped


def test_actual_partial_equity_keeps_priced_and_unpriced_legs(dbs):
    o,l,q=dbs;order(l,'priced');order(l,'unpriced')
    l.execute('INSERT INTO cashflows(order_id,kind,at,lamports,cost_lamports,cost_basis,cost_evidence,fx_usd,fx_at,usd) VALUES(?,?,?,?,?,?,?,?,?,?)',
              ('priced','entry',END-1,'-1','0','fixture','fixture',100.,END-1,-25.))
    quote=dict(binding_kind='intake_canonical_pda',lamports='1000000000',pool='pool',clock=dict(host_at=END-1,offset_s=0.),proof=dict(committed_through=END-1))
    l.execute('INSERT INTO marks(order_id,at,lamports,source,binding_kind,quote_proof) VALUES(?,?,?,?,?,?)',
              ('priced',END-1,'1000000000','fixture','intake_canonical_pda',json.dumps(quote)))
    out=report(*dbs)['book']
    assert out['now']['state']=='partial' and out['now']['unpriced_count']==1
    assert out['office_net'] is None and out['statistics']['withheld_reason']


def test_plain_sqlite_connections_need_no_row_factory_mutation(dbs):
    for c in dbs:c.row_factory=None
    out=report(*dbs)
    assert out['epoch_id']==EPOCH and out['book']['cost']['state']=='known'
    assert all(c.row_factory is None for c in dbs)


def test_matching_label_with_missing_recorded_fields_is_partial(dbs):
    o,l,q=dbs;order(l);label(o,l)
    o.execute("UPDATE office_v2_order_labels SET labels_json='{}'")
    out=report(*dbs)['orders']
    assert out['state']=='partial' and out['label_missing']==0
    assert out['items'][0]['recorded_fields_state']=='unknown'


def test_second_initial_flow_is_an_unexpected_capital_change(dbs):
    l=dbs[1]
    l.execute('INSERT INTO capital_flows(book_id,at,kind,usd,actor,note) VALUES(?,?,?,?,?,?)',(EPOCH,START+1,'initial',25.,'crew','duplicate'))
    assert len(report(*dbs)['book']['unexpected_capital_flows'])==1


def recorded_label(o,**values):
    data=dict(segment_id=1,tainted_by_web=False,operator_influenced=False,equity_snapshot_id='snapshot',
              post_swap_first_wake=False,workspace_manifest_hash='a'*64,brain_fact_ids=[],db_read_hashes=[],
              notebook_manifest_hash='absent_by_manifest',granted_request_ids='absent_by_manifest')
    data.update(values)
    o.execute('UPDATE office_v2_order_labels SET labels_json=?',(json.dumps(data),))


@pytest.mark.parametrize('field,value,state',[('workspace_manifest_hash','unknown','unknown'),
    ('equity_snapshot_id','absent','absent'),('notebook_manifest_hash','absent_by_manifest','absent_by_manifest')])
def test_recorded_field_markers_are_distinct_states(dbs,field,value,state):
    o,l,q=dbs;order(l);label(o,l);recorded_label(o,**{field:value})
    row=report(*dbs)['orders']['items'][0]
    assert row['recorded_field_states'][field]==state
    assert row['recorded_fields_state']!='known'


@pytest.mark.parametrize('mode',['stored','override','unrecorded','future_override'])
def test_order_policy_identity_matches_stored_or_prior_override_only(dbs,mode):
    o,l,q=dbs;order(l);label(o,l)
    epoch=json.loads(o.execute('SELECT payload_json FROM epochs').fetchone()[0]);epoch['book_policy']={'sha256':'a'*64}
    o.execute('UPDATE epochs SET payload_json=?',(json.dumps(epoch),))
    actual='a'*64 if mode=='stored' else 'b'*64
    recorded_label(o,book_policy_sha256=actual)
    if mode in ('override','future_override'):
        at=START+5 if mode=='override' else START+20
        o.execute('INSERT INTO office_v2_changes VALUES(?,?,?,?,?,?,?)',(EPOCH,2,at,'a'*40,actual,'override','Alpha policy reference'))
    out=report(*dbs)['orders'];row=out['items'][0]
    assert row['book_policy_sha256']==actual
    assert row['policy_identity_state']==('unrecorded' if mode=='future_override' else mode)
    assert out['by_policy_identity'][actual]==1


def test_missing_policy_hash_is_unknown_not_stored(dbs):
    o,l,q=dbs;order(l);label(o,l)
    out=report(*dbs)['orders']
    assert out['items'][0]['policy_identity_state']=='unknown'
    assert out['by_policy_identity']=={'unknown':1}


def test_open_wake_window_does_not_claim_completed_hour_coverage(dbs):
    wake(dbs[2],0,START+10)
    assert report(*dbs)['seats']['items'][0]['proof']['complete'] is False
    closed=report(*dbs,now=END+1,window_start=START,window_end=END)
    assert closed['seats']['items'][0]['proof']['complete'] is True


def test_persisted_paper_tool_results_supply_refusal_counts(dbs):
    o,l,q=dbs
    for tool,state,reason in [('paper_order','refused','self_sheet_unavailable'),('paper_order','unknown','fx_unknown'),
                              ('candidate_register','refused','candidate_invalid'),('book_halt','refused','halt_not_allowed')]:
        E.record_event(o,epoch_id=EPOCH,actor_id='judge',generation=1,incarnation=1,at=START+10,kind='tool_result',
                       text=json.dumps(dict(state=state,reason=reason)),refs=dict(tool=tool,is_error=True))
    out=report(*dbs)['refusals']
    assert out['state']=='known' and out['complete'] is True
    assert out['by_reason']==dict(self_sheet_unavailable=1,fx_unknown=1,candidate_invalid=1,halt_not_allowed=1)
    assert out['self_attempt_refused']==1 and out['by_segment']=={'1':4}


def test_truncated_refusal_result_is_unknown_not_zero(dbs):
    o,l,q=dbs
    E.record_event(o,epoch_id=EPOCH,actor_id='judge',generation=1,incarnation=1,at=START+10,kind='tool_result',
                   text='x'*5000,refs=dict(tool='paper_order',is_error=True))
    assert report(*dbs)['refusals']['state']=='unknown'


def poll(q,id,at,failed=False):
    raw={} if failed else dict(five_hour=dict(utilization=5.,resets_at=END+3600),seven_day=dict(utilization=5.,resets_at=END+3600))
    Q.record_account_snapshot(q,snapshot_id=id,provider='claude',account_alias='claude-main',observed_at=at,raw=raw,error='rate_limited' if failed else None)


def test_failed_polls_do_not_refresh_reading_age(dbs):
    q=dbs[2];poll(q,'success',START+100);poll(q,'failure1',START+200,True);poll(q,'failure2',START+300,True)
    out=report(*dbs)['seats']['account_polls']
    assert out['state']=='known' and out['total']==3 and out['with_windows']==1 and out['failed']==2
    assert out['by_reason']=={'rate_limited':2} and out['last_failed_at']==START+300
    assert out['last_success_at']==START+100 and out['newest_reading_age_s']==END-START-100


def test_poll_window_end_is_exclusive(dbs):
    poll(dbs[2],'at-end',END,True)
    out=report(*dbs)['seats']['account_polls']
    assert out['total']==0 and out['last_failed_at'] is None and out['newest_reading_age_s'] is None


def test_poll_history_past_retention_is_partial_not_complete_zero(dbs):
    now=START+31*86400
    out=report(*dbs,now=now)['seats']['account_polls']
    assert out['state']=='partial' and out['complete'] is False and out['lower_bound'] is True
    assert '30 days' in out['retention_caveat']


@pytest.mark.parametrize('mode',['missing','capped'])
def test_poll_history_unknown_on_missing_table_or_cap(dbs,mode):
    q=dbs[2]
    if mode=='missing':q.execute('DROP TABLE quota_account_snapshots')
    else:q.executemany('INSERT INTO quota_account_snapshots VALUES(?,?,?,?)',
                      [(str(i),'claude-main',START+1,json.dumps(dict(state='unknown',windows={},reason='rate_limited'))) for i in range(10001)])
    out=report(*dbs)['seats']['account_polls']
    assert out['state']=='unknown' and out['complete'] is False


def test_release_rollup_groups_all_prompt_segments_without_relogging(dbs):
    o,l,q=dbs
    for segment,at,kind in [(2,START+5,'release'),(3,START+6,'prompt'),(4,START+7,'prompt')]:
        o.execute('INSERT INTO office_v2_changes VALUES(?,?,?,?,?,?,?)',(EPOCH,segment,at,'b'*40,'c'*64,kind,'prompt role'))
    order(l,'one',START+1);label(o,l,'one')
    order(l,'two',START+10);label(o,l,'two')
    o.execute('UPDATE office_v2_order_labels SET labels_json=? WHERE order_id=?',(json.dumps(dict(segment_id=4)),'two'))
    wake(q,0,START+12)
    E.record_event(o,epoch_id=EPOCH,actor_id='head',generation=1,incarnation=1,at=START+15,kind='lifecycle',text='parked',refs=dict(reason='account_guard'))
    before=o.total_changes;out=report(*dbs)['by_release']
    assert o.total_changes==before and out['state']=='known'
    rows={r['release_sha']:r for r in out['items']}
    assert rows['a'*40]['orders']==1
    assert rows['b'*40]['segments']==[2,3,4]
    assert (rows['b'*40]['orders'],rows['b'*40]['fills'],rows['b'*40]['wakes'],rows['b'*40]['parks'])==(1,1,1,1)


def test_unknown_order_segment_never_turns_into_known_zero_rollup(dbs):
    o,l,q=dbs;order(l);label(o,l)
    o.execute("UPDATE office_v2_order_labels SET labels_json='{}'")
    out=report(*dbs)['by_release']
    assert out['state']=='partial' and out['items'][0]['orders'] is None


def test_historical_poll_reading_age_preserves_window_close_cut(dbs):
    poll(dbs[2],'reading',START+100)
    old=report(*dbs,now=END+100,window_start=START,window_end=END)['seats']['account_polls']
    later=report(*dbs,now=END+200,window_start=START,window_end=END)['seats']['account_polls']
    assert old['age_at_window_end_s']==later['age_at_window_end_s']==END-START-100
    assert later['newest_reading_age_s']-old['newest_reading_age_s']==100


def test_open_poll_window_has_no_close_age_and_missing_reading_is_unknown(dbs):
    poll(dbs[2],'reading',START+100)
    assert report(*dbs)['seats']['account_polls']['age_at_window_end_s'] is None
    dbs[2].execute('DELETE FROM quota_account_snapshots')
    out=report(*dbs,now=END+1,window_start=START,window_end=END)['seats']['account_polls']
    assert out['age_at_window_end_s'] is None and out['newest_reading_age_s'] is None


@pytest.fixture
def market_connection(tmp_path):
    path=tmp_path/'market.db';writer=sqlite3.connect(path)
    writer.execute('CREATE TABLE calls(at REAL,outcome TEXT)');writer.execute('CREATE INDEX calls_at ON calls(at)');writer.commit()
    reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    yield writer,reader
    reader.close();writer.close()


def seed_calls(market_connection,rows):
    market_connection[0].executemany('INSERT INTO calls VALUES(?,?)',rows);market_connection[0].commit()


def test_market_default_is_unavailable_without_implicit_file_creation(dbs):
    out=report(*dbs)['seats']['market_data']
    assert out['state']=='unknown' and out['reason']=='market_source_unavailable'
    assert out['funded_breaker']['state']=='unknown'


def test_market_rows_keep_unknown_outcomes_and_inferred_dial_split(dbs,market_connection,monkeypatch):
    from sniper.office.market_data import MarketData
    def forbidden(*a,**kw):raise AssertionError('report cannot call provider/store methods')
    monkeypatch.setattr(MarketData,'counts',forbidden);monkeypatch.setattr(MarketData,'_conn',forbidden)
    seed_calls(market_connection,[(START+10,x) for x in ('fetched','cache_hit',None,'provider_rate_limited','no_matching_pair','provider_backoff')])
    before=market_connection[0].total_changes
    out=report(*dbs,market_conn=market_connection[1])['seats']['market_data']
    assert out['total']==6 and out['by_outcome']['outcome_unknown']==1
    assert out['dials']==4 and out['dials_by_outcome']['no_matching_pair']==1
    assert out['other_by_outcome']==dict(cache_hit=1,provider_backoff=1)
    assert out['dial_basis']=='inferred_from_legacy_outcome' and out['lower_bound'] is True
    assert out['funded_breaker']['state']=='unknown' and market_connection[0].total_changes==before


def test_market_zero_is_no_calls_recorded_not_no_activity(dbs,market_connection):
    out=report(*dbs,market_conn=market_connection[1])['seats']['market_data']
    assert out['total']==0 and out['activity']=='no calls recorded' and out['lower_bound'] is True


def test_market_stop_cap_is_explicit_with_known_omission(dbs,market_connection):
    seed_calls(market_connection,[(START+i,'provider_rate_limited') for i in range(101)])
    out=report(*dbs,market_conn=market_connection[1])['seats']['market_data']
    assert len(out['stop_windows'])==100 and out['stops_truncated'] is True and out['stops_omitted']==1
    assert out['stop_windows'][0]['start']==START
    assert out['stop_windows'][0]['start_basis']=='reservation_time_lower_bound'


@pytest.mark.parametrize('mode',['missing_table','capped'])
def test_market_unreadable_or_capped_never_returns_zero_prefix(dbs,market_connection,mode):
    if mode=='missing_table':market_connection[0].execute('DROP TABLE calls');market_connection[0].commit()
    else:seed_calls(market_connection,[(START+1,'fetched')]*10001)
    out=report(*dbs,market_conn=market_connection[1])['seats']['market_data']
    assert out['state']=='unknown' and out['complete'] is False
    assert out['reason']==('market_db_unreadable' if mode=='missing_table' else 'market_rows_capped')


def test_market_window_half_open_and_retention_label(dbs,market_connection):
    seed_calls(market_connection,[(START-1,'fetched'),(START,'cache_hit'),(END,'fetched')])
    out=report(*dbs,market_conn=market_connection[1])['seats']['market_data']
    assert out['total']==1 and out['by_outcome']=={'cache_hit':1}
    old=report(*dbs,now=START+31*86400,market_conn=market_connection[1])['seats']['market_data']
    assert old['retention_covered'] is False and old['state']=='partial'


def test_market_prefers_dial_and_actual_stop_columns(dbs,market_connection):
    writer,reader=market_connection
    writer.execute('ALTER TABLE calls ADD COLUMN dial INTEGER');writer.execute('ALTER TABLE calls ADD COLUMN stop_until REAL')
    writer.executemany('INSERT INTO calls VALUES(?,?,?,?)',[(START+10,'bad_arguments',0,None),(START+10,None,1,None),
        (START+10,'no_matching_pair',1,None),(START+10,'provider_rate_limited',1,START+1000)])
    writer.commit()
    out=report(*dbs,market_conn=reader)['seats']['market_data']
    assert out['dial_basis']=='dial_column' and out['dials']==3 and out['other_by_outcome']=={'bad_arguments':1}
    assert out['stop_windows']==[dict(start=START+100,end=START+1000,start_basis='response_time')]


def test_market_upgraded_store_labels_null_legacy_columns_as_proxy(dbs,market_connection):
    writer,reader=market_connection
    writer.execute('ALTER TABLE calls ADD COLUMN dial INTEGER');writer.execute('ALTER TABLE calls ADD COLUMN stop_until REAL')
    writer.executemany('INSERT INTO calls VALUES(?,?,?,?)',[(START+10,'provider_rate_limited',None,None),(START+20,'fetched',1,None)]);writer.commit()
    out=report(*dbs,market_conn=reader)['seats']['market_data']
    assert out['dial_basis']=='mixed_column_and_legacy_inference' and out['inferred_classification_rows']==1
    assert out['stop_windows'][0]['start_basis']=='reservation_time_lower_bound'


def test_book_latest_mark_does_not_materialize_mark_history(dbs):
    o,l,q=dbs;order(l)
    l.executemany('INSERT INTO marks(order_id,at,lamports,source) VALUES(?,?,?,?)',
                  [('one',START+20+i*.1,'100','test') for i in range(2100)])
    l.execute('INSERT INTO marks(order_id,at,lamports,source) VALUES(?,?,?,?)',('one',END+1,'100','test'))
    out=report(*dbs)['book']
    assert out['open_positions']==[dict(order_id='one',mark_age_s=END-(START+20+2099*.1))]
    assert out['daily_realized_usd']==0 and out['daily_loss_limit_usd']==100


def test_orders_include_recorded_exit_reason_and_equity_binding_counts(dbs):
    o,l,q=dbs;order(l);l.execute('UPDATE orders SET exit_reason=? WHERE order_id=?',('trail','one'))
    out=report(*dbs)
    assert out['orders']['items'][0]['exit_reason']=='trail'
    assert out['book']['now']['binding_kind_counts']=={}


@pytest.mark.parametrize('age,known',[(1.,True),(120.,True),(120.1,False),(-1.,False)])
def test_display_fx_is_fresh_causal_and_never_revalues_history(dbs,age,known):
    o,l,q=dbs;order(l)
    l.execute('INSERT INTO cashflows(order_id,kind,at,lamports,cost_lamports,cost_basis,cost_evidence,fx_usd,fx_at,usd) VALUES(?,?,?,?,?,?,?,?,?,?)',
              ('one','entry',START+10,'-1','0','fixture','fixture',100.,START+10,-25.))
    quote=dict(binding_kind='intake_canonical_pda',lamports='1000000000',pool='pool',clock=dict(host_at=END-1,offset_s=0.),proof=dict(committed_through=END-1))
    l.execute('INSERT INTO marks(order_id,at,lamports,source,binding_kind,quote_proof) VALUES(?,?,?,?,?,?)',
              ('one',END-1,'1000000000','fixture','intake_canonical_pda',json.dumps(quote)))
    before=l.total_changes
    out=report(*dbs,sol_usd=150.,sol_at=END-age)['book']
    assert (out['now']['state']=='known') is known
    assert out['now']['fx_source']==('universe_heartbeat' if known else 'latest_causal_ledger_cashflow')
    if known:assert out['now']['fx_age_s']==age and out['now']['binding_kind_counts']=={'intake_canonical_pda':1}
    historic=report(*dbs,window_start=START,window_end=END-300,sol_usd=150.,sol_at=END-age)['book']
    assert historic['window_bounds'][1]['state']=='unknown'
    assert historic['window_bounds'][1]['fx_source']=='latest_causal_ledger_cashflow'
    assert l.total_changes==before


def test_estimated_and_strict_closes_are_never_one_mean():
    from sniper.office import explore_report as X
    closed=[dict(order_id='a',exit_fill_kind=None),dict(order_id='b',exit_fill_kind='strict'),dict(order_id='c',exit_fill_kind='estimated_terminal')]
    stats=dict(mean_trading_net=1.,median=1.)
    X._stratify_exit_fills(stats,closed,dict(a=2.,b=4.,c=-9.))
    assert stats['mean_trading_net'] is None and stats['median'] is None and stats['blended_mean_withheld'] is True
    assert stats['by_exit_fill_kind']['strict']==dict(n=2,mean_trading_net=3.,median=3.,win_count=2,worst=2.) and stats['by_exit_fill_kind']['estimated_terminal']['n']==1


def test_terminal_exit_fields_reach_the_full_report(dbs):
    o,l,q=dbs
    for oid,kind,trigger,usd in (('strict1','strict','not_rails',5.),('est1','estimated_terminal','estimated',20.)):
        order(l,oid);label(o,l,oid)
        l.execute("UPDATE orders SET state='closed',closed_at=?,exit_fill_kind=?,exit_trigger_kind=? WHERE order_id=?",(START+40,kind,trigger,oid))
        for leg,amount,at in (('entry',-25.,START+10),('exit',25.+usd,START+40)):
            l.execute("INSERT INTO cashflows(order_id,kind,at,lamports,cost_lamports,cost_basis,cost_evidence,fx_usd,fx_at,usd) VALUES(?,?,?,'1','0','x','x',100.,?,?)",(oid,leg,at,at,amount))
    order(l,'stuck')
    l.execute("UPDATE orders SET exit_requested_at=?,exit_reason='trail',due_at=? WHERE order_id='stuck'",(START+11,START+12))
    out=report(*dbs)
    kinds={r['order_id']:(r['exit_fill_kind'],r['exit_trigger_kind']) for r in out['orders']['items']}
    assert kinds['est1']==('estimated_terminal','estimated') and kinds['strict1']==('strict','not_rails') and kinds['stuck']==(None,None)
    assert out['orders']['by_exit_fill_kind']=={'strict':1,'estimated_terminal':1}
    stats=out['book']['statistics']
    assert stats['terminal_exits']['closed_estimated_terminal']==1 and stats['terminal_exits']['exit_pending_past_hold']==1


def readable_orders(l):
    for oid,at in [('profit',START+10),('zero',START+20),('unknown',START+30),('censored_long_mint_123456789',START+40)]:
        order(l,oid,at)
    l.execute("UPDATE orders SET state='closed',ticker='GOOD',closed_at=?,exit_usd=30.,costs_usd=.5,exit_reason='trail',exit_requested_at=?,exit_fill_kind='estimated_terminal',exit_trigger_kind='estimated',fill_proof=?,exit_proof=?,entry_unit_price_usd=.000000002 WHERE order_id='profit'",
              (START+70,START+20,json.dumps({'binding_kind':'carried'}),json.dumps({'binding_kind':'curve_derived_unchecked'})))
    l.execute("UPDATE orders SET state='closed',closed_at=?,exit_usd=0,costs_usd=.5 WHERE order_id='zero'",(START+80,))
    l.execute("UPDATE orders SET state='closed',closed_at=?,exit_usd=NULL,fill_proof='broken',exit_proof='[]' WHERE order_id='unknown'",(START+90,))
    l.execute("UPDATE orders SET state='censored',filled_at=NULL,reason='provider_timeout' WHERE order_id='censored_long_mint_123456789'")


def test_readable_order_projection_uses_real_ledger_amounts_and_proof_bindings(dbs):
    readable_orders(dbs[1]);before=[c.total_changes for c in dbs]
    items=report(*dbs)['orders']['items'];by_id={r['order_id']:r for r in items}
    assert [r['order_id'] for r in items]==['censored_long_mint_123456789','unknown','zero','profit']
    p=by_id['profit']
    assert (p['ticker'],p['spend_usd'],p['exit_usd'],p['costs_usd'])==('GOOD',25.,30.,.5)
    assert p['modelled_net_usd']==4.5 and p['modelled_net_pct']==18.
    assert p['exit_pending_s']==50 and p['exit_requested_at']==START+20
    assert by_id['zero']['exit_pending_s'] is None
    assert p['hold_s']==60 and p['entry_binding_kind']=='carried' and p['exit_binding_kind']=='curve_derived_unchecked'
    assert 'fill_proof' not in p and 'exit_proof' not in p
    assert by_id['zero']['modelled_net_usd']==-25.5 and by_id['zero']['modelled_net_pct']==-102.
    assert by_id['unknown']['modelled_net_usd'] is None
    assert by_id['unknown']['entry_binding_kind']=='invalid_proof' and by_id['unknown']['exit_binding_kind']=='invalid_proof'
    assert by_id['zero']['entry_binding_kind']=='unknown'
    assert by_id['censored_long_mint_123456789']['reason']=='provider_timeout'
    assert [c.total_changes for c in dbs]==before
    dbs[1].execute("UPDATE orders SET filled_at=NULL WHERE order_id='profit'")
    legacy={r['order_id']:r for r in report(*dbs)['orders']['items']}['profit']
    assert legacy['modelled_net_usd']==4.5 and legacy['hold_s'] is None
    dbs[1].execute("UPDATE orders SET state='open',closed_at=NULL,exit_requested_at=? WHERE order_id='zero'",(END-30,))
    assert {r['order_id']:r for r in report(*dbs)['orders']['items']}['zero']['exit_pending_s']==30
    dbs[1].execute("UPDATE orders SET exit_requested_at=closed_at WHERE order_id='profit'")
    assert {r['order_id']:r for r in report(*dbs)['orders']['items']}['profit']['exit_pending_s']==0
    dbs[1].execute("UPDATE orders SET exit_requested_at=closed_at+1 WHERE order_id='profit'")
    assert {r['order_id']:r for r in report(*dbs)['orders']['items']}['profit']['exit_pending_s'] is None


def test_real_ledger_order_rows_render_plainly_in_browser(dbs):
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    readable_orders(dbs[1]);data=report(*dbs)
    root=Path(__file__).resolve().parents[2]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--renderer-process-limit=2'])
        try:
            page=browser.new_page(viewport={'width':1200,'height':1000});page.set_default_timeout(3000)
            page.set_content('<article></article>');page.evaluate('window.module={exports:{}}');page.add_script_tag(path=str(root/'static/office-v2.js'))
            page.evaluate('r=>{window.ui=module.exports.mount(document,document.querySelector("article"),{});ui.renderReport(r)}',data)
            page.get_by_text('Recorded paper orders · partial',exact=True).click()
            table=page.get_by_role('table',name='Recorded paper orders');rows=table.locator('tbody > tr')
            assert rows.count()==4 and 'censor' in rows.nth(0).inner_text()
            censored=rows.nth(0).inner_text()
            assert 'Not filled: provider timeout' in censored and 'Requested' in censored
            assert 'Bought' not in censored and 'Sold' not in censored
            assert 'Sold' not in censored and 'Bought' not in censored and 'Exit fill:' not in censored
            profit=rows.nth(3)
            assert 'GOOD' in profit.inner_text() and 'Bought $25.00' in profit.inner_text() and 'Sold $30.00' in profit.inner_text()
            assert '+$4.50' in profit.inner_text() and '+18.0%' in profit.inner_text() and 'modelled' in profit.inner_text()
            assert '1m 0s' in profit.inner_text() and 'trail' in profit.inner_text()
            assert 'exit pending 50s' in profit.inner_text()
            assert 'Net +$4.50 (upper-biased)' in profit.inner_text()
            assert 'Binding carried' in profit.inner_text() and 'Binding curve_derived_unchecked' in profit.inner_text()
            assert 'estimated · upper-biased' in profit.inner_text() and 'Entry $0.00' not in profit.inner_text()
            assert 'Segment' not in profit.inner_text()
            profit.get_by_text('Details',exact=True).click();assert 'Segment' in profit.inner_text() and 'Full mint: profit' in profit.inner_text()
            page.evaluate('r=>ui.renderReport(r)',data)
            assert 'Full mint: profit' in rows.nth(3).inner_text()
            assert 'Sold $0.00' in rows.nth(2).inner_text() and '−$25.50' in rows.nth(2).inner_text()
            assert 'Net Unknown' in rows.nth(1).inner_text()
            page.set_viewport_size({'width':390,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            del data['orders']['items'];data['orders']['items_omitted']=4
            page.evaluate('r=>ui.renderReport(r)',data)
            assert 'No row details included in this report; older rows were omitted.' in table.inner_text()
            assert 'No orders in this report window.' not in table.inner_text()
        finally:browser.close()


def test_partial_and_known_closes_never_share_one_mean_and_unstamped_stays_unstamped():
    closed=[dict(order_id='a',sheet_state='known',sells_basis='fresh_flow'),dict(order_id='b',sheet_state='partial',sells_basis='sell_probe'),dict(order_id='c',sheet_state=None,sells_basis=None)]
    stats=dict(mean_trading_net=1.,median=1.)
    from sniper.office import explore_report as R
    R._stratify_sheets(stats,closed,dict(a=2.,b=-4.,c=1.))
    assert stats['mean_trading_net'] is None and stats['blended_mean_withheld'] is True
    assert stats['by_sheet_and_sells']['partial/sell_probe']['n']==1 and stats['by_sheet_and_sells']['known/fresh_flow']['mean_trading_net']==2. and 'unstamped/unstamped' in stats['by_sheet_and_sells']
    known=dict(mean_trading_net=2.,median=2.)
    R._stratify_sheets(known,closed[:1],dict(a=2.))
    assert known['mean_trading_net']==2.


def test_the_partial_canary_names_the_first_ten_in_submission_order():
    from sniper.office import explore_report as R
    items=[dict(order_id=str(i),sheet_state='partial',requested_at=100.-i,filled_at=float(i),mint='m',state='open') for i in range(12)]+[dict(order_id='k',sheet_state='known',requested_at=1.)]
    got=R._partial_canary(items)
    assert len(got)==10 and got[0]['order_id']=='11' and all(r['order_id']!='k' for r in got)


def test_the_canary_query_reads_the_real_ledger_outside_the_window():
    from sniper.office import explore_report as R
    import inspect
    assert "AND requested_at<? ORDER BY requested_at,order_id LIMIT 10" in inspect.getsource(R._orders)      # bounded by the report's end only, never its start


def _orders(n):
    return dict(state='known',epoch_id='E',segments=dict(state='known',items=[dict(note='x'*200)]*204),
                orders=dict(state='known',count=n,items=[dict(order_id=str(i),mint='M'*44,pad='p'*1900,recorded_fields=dict(pad='r'*877)) for i in range(n)]))


def test_two_hundred_orders_all_show(monkeypatch):
    import sniper.office.explore_report as R
    out=R._fit(_orders(200))
    assert len(out['orders']['items'])==200 and 'recorded_fields' in out['orders']['items'][0] and 'truncated' not in out['orders']


def test_a_tight_budget_slims_then_drops_oldest_and_counts_them(monkeypatch):
    import sniper.office.explore_report as R
    monkeypatch.setattr(R,'OUTPUT_BYTES',450000)
    out=R._fit(_orders(200))
    assert len(out['orders']['items'])==200 and 'recorded_fields' not in out['orders']['items'][0] and out['orders']['recorded_fields_omitted'] and out['segments']['items_omitted']==204
    monkeypatch.setattr(R,'OUTPUT_BYTES',65536)
    out=R._fit(_orders(200));kept=out['orders']['items']
    assert kept and kept[0]['order_id']=='0' and out['orders']['items_omitted']==200-len(kept) and len(R._json(out))<=65536
