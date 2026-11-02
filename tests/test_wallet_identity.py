"""Account attribution and alternate-wallet research must not manufacture ownership."""
import copy
import json
import sqlite3
from datetime import datetime

import pytest

A, B, C, D = (c * 32 for c in 'ABCD')
NOW = '2026-10-10T12:00:00+00:00'


def window(sig, mint, at, **extra):
    return dict(signature=sig, mint=mint, source_user_id='user-a',
                t0=int(datetime.fromisoformat(at).timestamp()), status='ok', control_named=1,
                origin='detection', method_version='upstream-v1:test', **extra)


def inputs():
    windows = [window('source-1', 'coin-1', '2026-10-08T12:00:00+00:00', win=0),
               window('source-2', 'coin-2', '2026-10-09T12:00:00+00:00', win=1),
               window('source-3', 'coin-3', '2026-10-09T13:00:00+00:00', win=None),
               window('source-4', 'coin-4', '2026-10-09T14:00:00+00:00', win=0),
               window('source-5', 'coin-5', '2026-10-09T15:00:00+00:00')]
    windows[-1]['status'] = 'unread'
    return dict(claims=[dict(chain='solana', address=A, uid='user-a', evidence_kind='receipt_match'),
                        dict(chain='solana', address=D, uid='user-b', evidence_kind='receipt_match')],
                windows=windows, early_buys=[dict(signature=f'source-{i}', trader=B, lead_s=lead,
                    slot=100+i, tx_index=2, tokens=1.0, order_proven=1,buy_signature=f'candidate-{i}') for i, lead in ((1, 10), (2, 20), (3, 30))],
                funders=[dict(wallet=B, status='resolved', funder=A, funding_sig='funding-1', funded_at=1)],
                coverage=dict(state='ok', window_from='2026-09-26T12:00:00+00:00'))


def project(raw=None):
    from sniper.services.wallet_identity import build_identity_report
    return build_identity_report(raw or inputs(), now_iso=NOW)


def identity(report, uid='user-a', chain='solana'):
    return report['users'][uid]['chains'][chain]


def test_losing_unpriced_coins_and_failed_windows_have_honest_denominators():
    row = identity(project())
    related, = row['related_wallets']
    assert (related['n_coins'], related['eligible_coins'], related['missed_coins'], related['unknown_coins']) == (3, 4, 1, 1)
    assert related['lead_p50_s'] == 20 and related['lead_p90_s'] == 30
    assert related['status'] == 'suspected_alternate'
    assert related['days'] == 2
    assert related['evidence'][0]['source_signature'] == 'source-1'
    assert related['evidence'][0]['buy_signature'] == 'candidate-1'
    assert not any(k in related for k in ('armed', 'effective', 'funded', 'capital', 'real_approved'))


def test_same_coin_repeated_source_buys_do_not_inflate_support():
    raw = inputs()
    raw['windows'].append(window('source-later', 'coin-1', '2026-10-09T12:30:00+00:00'))
    raw['early_buys'].append(dict(signature='source-later', trader=B, lead_s=50, slot=999, tx_index=2))
    related, = identity(project(raw))['related_wallets']
    assert (related['n_coins'], related['eligible_coins'], related['lead_p50_s']) == (3, 4, 20)


def test_unknown_clock_duplicate_cannot_be_ordered_as_a_later_buy():
    raw=inputs(); raw['windows'].append(dict(raw['windows'][0],signature='unknown-clock',t0=None))
    related,=identity(project(raw))['related_wallets']
    assert (related['n_coins'],related['eligible_coins'],related['unknown_coins'])==(2,3,2)


def test_truncated_early_buyer_intake_cannot_report_measured_absences():
    raw=inputs();raw['coverage'].update(state='partial',capped=['early_buys'])
    related,=identity(project(raw))['related_wallets']
    assert related['missed_coins'] is None


def test_empty_relations_under_an_early_buyer_cap_remain_unknown():
    raw=inputs();raw['coverage'].update(state='partial',capped=['early_buys']);raw['early_buys']=[]
    row=identity(project(raw))
    assert row['related_wallets'] is None and row['coverage']['state']=='partial'


def test_funding_clue_without_a_receipt_cannot_support_suspected_ownership():
    raw=inputs();raw['funders'][0]['funding_sig']=None
    related,=identity(project(raw))['related_wallets']
    assert related['status']=='related_trader'


@pytest.mark.parametrize('status',['no_label','our_fill'])
def test_post_buy_price_labels_do_not_censor_complete_pre_buy_identity(status):
    raw=inputs();raw['windows'][2]['status']=status
    related,=identity(project(raw))['related_wallets']
    assert (related['n_coins'],related['eligible_coins'],related['unknown_coins'])==(3,4,1)


def test_common_funder_and_timing_alone_never_verify_ownership():
    raw = inputs()
    raw['funders'] = [dict(wallet=A, status='resolved', funder=C, funding_sig='fa'),
                      dict(wallet=B, status='resolved', funder=C, funding_sig='fb')]
    related, = identity(project(raw))['related_wallets']
    assert related['status'] == 'related_trader'
    assert related['funding_clues'][0]['kind'] == 'shared_funder'
    assert related['ownership_proven'] is False


def test_initial_funding_clue_from_a_hub_does_not_make_an_alternate():
    raw = inputs()
    raw['funders'] += [dict(wallet=c*32, status='resolved', funder=A, funding_sig='f'+c) for c in 'EFG']
    related, = identity(project(raw))['related_wallets']
    assert related['status'] == 'related_trader'
    assert related['funding_clues'][0]['hub'] is True


def test_another_verified_users_address_remains_that_users_related_trader():
    raw = inputs()
    raw['claims'].append(dict(chain='solana', address=B, uid='user-b', evidence_kind='receipt_match'))
    related, = identity(project(raw))['related_wallets']
    assert related['status'] == 'related_trader' and related['known_owner_uids'] == ['user-b']
    assert B not in [a['address'] for a in identity(project(raw))['verified_addresses']]


def test_competing_account_claims_are_a_conflict_without_last_writer_wins():
    raw = inputs()
    raw['claims'].extend(dict(chain='solana', address=B, uid=u, evidence_kind='receipt_match') for u in ('user-b', 'user-c'))
    related, = identity(project(raw))['related_wallets']
    assert related['status'] == 'ownership_conflict'
    assert related['known_owner_uids'] == ['user-b', 'user-c']
    assert identity(project(raw), 'user-b')['address_conflicts'][0]['address'] == B


def test_zero_second_earlier_index_is_preserved_and_legacy_signature_is_unknown():
    raw = inputs(); raw['early_buys'][0].update(lead_s=0, buy_signature=None)
    related, = identity(project(raw))['related_wallets']
    assert related['evidence'][0]['lead_s'] == 0
    assert related['evidence'][0]['buy_signature'] is None


def test_legacy_zero_lead_without_order_proof_is_not_early_buy_evidence():
    raw=inputs();raw['early_buys'][0].update(lead_s=0,order_proven=None)
    related,=identity(project(raw))['related_wallets']
    assert related['n_coins']==2
    assert related['missed_coins'] is None and identity(project(raw))['coverage']['ordering_unknown_coins']==1


def test_unread_windows_invalid_leads_and_our_custody_are_not_negative_evidence():
    from sniper.services.wallet_identity import build_identity_report
    raw = inputs(); raw['windows'][0]['control_named'] = 0
    raw['early_buys'][1]['lead_s'] = -1; raw['early_buys'][2]['lead_s'] = 61
    assert identity(project(raw))['related_wallets'] == []
    assert identity(project(raw))['coverage']['unknown_coins'] == 2
    assert identity(build_identity_report(inputs(), now_iso=NOW, own_addresses={B}))['related_wallets'] == []


def test_chain_partition_and_solana_case_are_preserved():
    raw = inputs(); raw['claims'] += [dict(chain='base', address='0x'+'Ab'*20, uid='user-a'),
                                    dict(chain='solana', address='a'*32, uid='user-a')]
    report = project(raw)
    assert identity(report, chain='8453')['verified_addresses'][0]['address'] == '0x'+'ab'*20
    assert {x['address'] for x in identity(report)['verified_addresses']} == {A, 'a'*32}
    assert identity(report, chain='8453')['related_wallets'] == []
    assert identity(report, chain='8453')['coverage']['state'] == 'not_measured'


def test_a_missing_artifact_still_shows_current_account_addresses(tmp_path):
    from sniper.services.wallet_identity import read_identity
    result = read_identity(tmp_path/'absent.json', {('solana', A): ('user-a','Alpha')}, chain='solana', now_iso=NOW)
    row = result['wallets']['user-a']
    assert result['state'] == 'unavailable' and row['related_wallets'] is None
    assert row['verified_addresses'][0]['address'] == A


def test_account_absent_from_the_artifact_has_unknown_relations(tmp_path):
    from sniper.services.wallet_identity import read_identity
    report=project();path=tmp_path/'identity.json';path.write_text(json.dumps(report))
    claims=inputs()['claims']+[dict(chain='solana',address=C,uid='new-user')]
    row=read_identity(path,claims,chain='solana',now_iso=NOW)['wallets']['new-user']
    assert row['related_wallets'] is None and row['state']=='unavailable'


def test_claimed_account_without_retained_windows_has_unknown_sample_and_cap():
    raw=inputs();raw['coverage'].update(state='partial',capped=['source_windows'])
    row=identity(project(raw),'user-b')
    assert row['related_wallets'] is None and row['coverage']['state']=='partial'
    assert row['coverage']['capped']==['source_windows']


def test_stale_artifact_cannot_restore_revoked_verified_address(tmp_path):
    from sniper.services.wallet_identity import read_identity
    path=tmp_path/'identity.json'; report=project()
    path.write_text(json.dumps(report))
    result=read_identity(path, {}, chain='solana', now_iso='2026-10-11T12:00:00+00:00')
    assert result['state']=='stale'
    assert result['wallets']['user-a']['verified_addresses']==[]
    assert result['wallets']['user-a']['related_wallets'][0]['status']=='related_trader'
    assert result['wallets']['user-a']['related_wallets'][0]['funding_clues']==[]


@pytest.mark.parametrize('content',['{',json.dumps({'version':999}),json.dumps({'version':1,'generated_at':'2026-10-11T12:00:00+00:00','users':{}})])
def test_malformed_wrong_version_and_future_artifacts_are_unavailable(tmp_path,content):
    from sniper.services.wallet_identity import read_identity
    p=tmp_path/'bad.json';p.write_text(content)
    assert read_identity(p,{},chain='solana',now_iso=NOW)['state']=='unavailable'


@pytest.mark.parametrize('broken',[None,[],{'chains':None},{'chains':{'solana':{'related_wallets':[None]}}}])
def test_malformed_nested_artifact_is_unavailable_instead_of_crashing(tmp_path,broken):
    from sniper.services.wallet_identity import read_identity
    report=project();report['users']['user-a']=broken
    path=tmp_path/'bad.json';path.write_text(json.dumps(report))
    out=read_identity(path,inputs()['claims'],chain='solana',now_iso=NOW)
    assert out['state']=='unavailable' and out['wallets']['user-a']['verified_addresses'][0]['address']==A


def test_invalid_evidence_clock_is_rejected_before_browser_render(tmp_path):
    from sniper.services.wallet_identity import read_identity
    report=project();identity(report)['related_wallets'][0]['evidence'][0]['t0']=None
    path=tmp_path/'bad.json';path.write_text(json.dumps(report))
    assert read_identity(path,{},chain='solana',now_iso=NOW)['state']=='unavailable'


def test_malformed_nested_cap_is_unavailable_before_browser_render(tmp_path):
    from sniper.services.wallet_identity import read_identity
    report=project();identity(report)['coverage']['capped']='source_windows'
    path=tmp_path/'bad.json';path.write_text(json.dumps(report))
    assert read_identity(path,inputs()['claims'],chain='solana',now_iso=NOW)['state']=='unavailable'


def test_malformed_funding_address_is_unavailable_instead_of_crashing(tmp_path):
    from sniper.services.wallet_identity import read_identity
    report=project();identity(report)['related_wallets'][0]['funding_clues'][0]['funder']=[]
    path=tmp_path/'bad.json';path.write_text(json.dumps(report))
    assert read_identity(path,inputs()['claims'],chain='solana',now_iso=NOW)['state']=='unavailable'


def test_atomic_publish_failure_preserves_previous_artifact(tmp_path,monkeypatch):
    from sniper.services.wallet_identity import publish_identity
    p=tmp_path/'identity.json';p.write_text('{"previous":true}')
    def fail(*args):raise OSError('replace failed')
    monkeypatch.setattr('os.replace',fail)
    with pytest.raises(OSError):publish_identity(p,project())
    assert json.loads(p.read_text())=={'previous':True} and list(tmp_path.iterdir())==[p]


def test_read_only_loader_preserves_failed_windows_without_profit_filtering():
    from sniper.persistence.db_memecoin import ensure_memecoin_tables,add_wallet
    from sniper.persistence.db_wallet_identity import load_identity_inputs
    from scripts.research.wallet_upstream import ensure_schema
    ledger=sqlite3.connect(':memory:');ledger.row_factory=sqlite3.Row
    ensure_memecoin_tables(ledger);add_wallet(ledger,'user-a','Alpha','')
    graph=sqlite3.connect(':memory:');graph.row_factory=sqlite3.Row;ensure_schema(graph)
    for w in inputs()['windows']:
        graph.execute('INSERT INTO memecoin_wallet_upstream_reads(signature,mint,source_user_id,origin,t0,status,control_named,method_version,read_at,win) VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (w['signature'],w['mint'],w['source_user_id'],w['origin'],w['t0'],w['status'],w['control_named'],w['method_version'],NOW,w.get('win')))
    ledger.execute('PRAGMA query_only=ON');graph.execute('PRAGMA query_only=ON')
    out=load_identity_inputs(ledger,graph,since_iso='2026-09-26T12:00:00+00:00',now_iso=NOW)
    assert len(out['windows'])==5 and {w['status'] for w in out['windows']}=={'ok','unread'}
    ledger.close();graph.close()


def test_explicit_registry_refutation_overrides_legacy_confirmation():
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_wallet_identity import read_account_claims
    from sniper.services.axiom_address_verify import LEGACY_CONFIRMED
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;m.ensure_memecoin_tables(c)
    m.add_wallet(c,'user-a','Alpha','')
    c.execute('CREATE TABLE memecoin_wallet_addresses(network_id TEXT,address TEXT,source_user_id TEXT,handle TEXT,verify_status TEXT)')
    c.execute('INSERT INTO memecoin_wallet_addresses VALUES (?,?,?,?,?)',('solana',A,'user-a','Alpha',LEGACY_CONFIRMED))
    c.execute("INSERT INTO memecoin_address_registry(source_user_id,network_id,address,status,evidence_kind,first_seen_at,status_at) VALUES ('user-a','solana',?,'refuted','receipt_mismatch',?,?)",(A,NOW,NOW))
    assert not any(r['uid']=='user-a' and r['address']==A for r in read_account_claims(c))
    c.close()


def test_pump_uid_claim_is_retained_when_another_uid_claims_the_same_address():
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_wallet_identity import read_account_claims
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;m.ensure_memecoin_tables(c)
    addr='9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin'
    m.add_wallet(c,'user-a','Alpha','');m.add_wallet(c,'pump:'+addr,'Pump','')
    c.execute("INSERT INTO memecoin_address_registry(source_user_id,network_id,address,status,evidence_kind,first_seen_at,status_at) VALUES ('user-a','solana',?,'verified','receipt_match',?,?)",(addr,NOW,NOW))
    raw=dict(inputs(),claims=read_account_claims(c))
    assert identity(project(raw))['verified_addresses']==[]
    assert identity(project(raw))['address_conflicts'][0]['owner_uids']==['pump:'+addr,'user-a']
    c.close()


def test_new_early_buy_signature_survives_storage_and_legacy_row_remains_unknown():
    from scripts.research import wallet_upstream as up
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    w=inputs()['windows'][0];buy=inputs()['early_buys'][0]
    up.save_reads(c,[w],[buy],read_at=NOW)
    got=c.execute('SELECT buy_signature FROM memecoin_wallet_early_buys').fetchone()[0]
    assert got=='candidate-1'
    c.execute('UPDATE memecoin_wallet_early_buys SET buy_signature=NULL')
    assert c.execute('SELECT buy_signature FROM memecoin_wallet_early_buys').fetchone()[0] is None


def test_offline_publisher_uses_readonly_handles_and_preserves_roster(tmp_path,monkeypatch):
    from sniper.persistence import db_core,db_memecoin as m
    from scripts.research.wallet_identity import refresh_identity
    path=tmp_path/'ledger.db';c=sqlite3.connect(path);m.ensure_memecoin_tables(c)
    m.add_wallet(c,'user-a','Alpha','');c.commit();c.close()
    opened=[];real=db_core.get_readonly_connection
    def observer(path):
        conn=real(path);opened.append(conn.execute('PRAGMA query_only').fetchone()[0]);return conn
    monkeypatch.setattr(db_core,'get_readonly_connection',observer)
    out=refresh_identity(path,tmp_path/'absent.db',tmp_path/'identity.json',now_iso=NOW)
    assert out['rpc_calls']==0 and out['coverage']['state']=='unavailable' and opened==[1]
    c=sqlite3.connect(path)
    assert c.execute('SELECT source_user_id,real_approved FROM memecoin_wallets').fetchall()==[('user-a',0)]
    assert not (tmp_path/'absent.db').exists()
    c.close()
