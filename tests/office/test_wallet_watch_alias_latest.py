"""Alias history work stays bounded without weakening identity/availability exclusions."""
import json
import sqlite3

import pytest
from solders.pubkey import Pubkey

from sniper.office import wallet_network as N, wallet_watch as W, wallet_token_flow as TF
from sniper.office.wallet_watch_collect import SCHEMA as COLLECT_SCHEMA


def address(n):return str(Pubkey.from_bytes(n.to_bytes(32,'big')))


@pytest.fixture
def aliases(tmp_path):
    (tmp_path/'pumpswap').mkdir()
    net=N.WalletNetwork(tmp_path/'graph.db');c=net._conn()
    c.executescript(W.SCHEMA);TF.init_storage(c);c.executescript(COLLECT_SCHEMA)
    c.execute('INSERT INTO watch_list VALUES(?,NULL,?,?,?,1,NULL)',(address(1),'fixture','host','test'))
    c.execute('CREATE INDEX IF NOT EXISTS watch_token_account_latest ON watch_token_accounts(account)')
    yield c,tmp_path
    c.close()


def snapshot(c,flow,*,account=None,owner=None,slot=100,after=99.,known=94.,post='20'):
    sig=f'sig{flow:08d}'
    c.execute('INSERT INTO watch_flow_timing(source,flow_id,signature,graph_commit_after,flow_state,slot,selector_json,gaps_json) VALUES(?,?,?,?,?,?,?,?)',('tape',flow,sig,after,'known',slot,'{}','{}'))
    c.execute('INSERT INTO watch_token_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(sig,account or address(3),address(2),owner or address(1),TF.TOKEN22,6,'10',post,'10','known',None,'proof','tape',flow,known))


def exported(aliases):
    c,path=aliases;c.commit();W._sync(c,path,100.)
    return json.loads(W.watch_file(path).read_text())['token_accounts']


def test_alias_export_many_versions_fits_bounded_vm_work(aliases):
    c,path=aliases
    expected=[address(100+i) for i in range(12)]
    for i,account in enumerate(expected):
        for version in range(224):snapshot(c,i*224+version+1,account=account,slot=100+version)
    c.commit();steps=0
    def bounded():
        nonlocal steps
        steps+=1000
        return steps>1000000
    c.set_progress_handler(bounded,1000)
    try:
        W._sync(c,path,100.)
        actual=json.loads(W.watch_file(path).read_text())
    finally:c.set_progress_handler(None,0)
    assert actual['token_accounts']==sorted(expected),dict(vm_steps=steps,coverage=actual['coverage'])
    assert steps<=1000000


@pytest.mark.parametrize('newer,expected',[
    (dict(slot=200,after=None),False),
    (dict(slot=200,after=None,owner=address(4)),False),
    (dict(slot=None,after=None,owner=address(4)),False),
    (dict(slot=None,after=None,post=None),False),
    (dict(slot=100,owner=address(4)),False),
    (dict(slot=0),True),
    (dict(slot=None,after=None),True),
    (dict(slot=200,known=101.,owner=address(4)),True),
    (dict(slot=200,known=-1.,owner=address(4)),False),
    (dict(slot=200,post='0'),True),
    (dict(slot=200,post=None),False),
])
def test_latest_slot_prefilter_preserves_unknown_conflict_and_zero_rules(aliases,newer,expected):
    c,_=aliases;snapshot(c,1);snapshot(c,2,**newer)
    assert exported(aliases)==([address(3)] if expected else [])


def test_alias_removal_still_overrides_latest_known_account(aliases):
    c,_=aliases;snapshot(c,1)
    c.execute('INSERT INTO watch_list VALUES(?,NULL,?,?,?,1,99)',(address(3),'fixture','host','test'))
    assert exported(aliases)==[]


def test_late_older_alias_does_not_resurrect_an_unwatched_latest_owner(aliases):
    c,_=aliases
    snapshot(c,1,owner=address(4),slot=200)
    snapshot(c,2,slot=100,known=96.)
    assert exported(aliases)==[]


def test_newer_identity_without_any_timing_row_suppresses_old_alias(aliases):
    c,_=aliases;snapshot(c,1);snapshot(c,2,owner=address(4))
    c.execute('DELETE FROM watch_flow_timing WHERE flow_id=2')
    assert exported(aliases)==[]


def test_same_slot_identity_history_keeps_valid_zero_without_quadratic_conflict_work(aliases):
    c,path=aliases
    for flow in range(1,2689):snapshot(c,flow,post='invalid' if flow<2688 else '0')
    c.commit();steps=0
    def bounded():
        nonlocal steps
        steps+=1000
        return steps>1000000
    c.set_progress_handler(bounded,1000)
    try:
        W._sync(c,path,100.)
        actual=json.loads(W.watch_file(path).read_text())
    finally:c.set_progress_handler(None,0)
    assert actual['token_accounts']==[address(3)],dict(vm_steps=steps,coverage=actual['coverage'])
    assert steps<=1000000


# Frozen pre-optimization query is a semantic oracle, not the performance contract.
OLD_ALIAS_QUERY="SELECT a.account,a.owner,a.mint,a.program,a.decimals,a.post_atomic,a.state,a.reason,a.known_at,t.graph_commit_after\n                    FROM watch_token_accounts a JOIN watch_list w ON w.address=a.owner AND w.removed_at IS NULL\n                    JOIN watch_flow_timing t ON t.source=a.source AND t.flow_id=a.flow_id AND t.signature=a.signature\n                    WHERE a.known_at>0 AND a.known_at<=? AND t.flow_state='known' AND t.graph_commit_after>=a.known_at AND t.graph_commit_after<=? AND typeof(t.slot)='integer' AND t.slot>0\n                    AND NOT EXISTS(SELECT 1 FROM watch_token_accounts newer LEFT JOIN watch_flow_timing n ON n.source=newer.source AND n.flow_id=newer.flow_id AND n.signature=newer.signature\n                        WHERE newer.account=a.account AND newer.known_at<=?\n                        AND (n.slot>t.slot OR (n.slot=t.slot OR n.slot IS NULL OR n.graph_commit_after IS NULL OR n.flow_state!='known') AND (newer.owner IS NOT a.owner OR newer.mint IS NOT a.mint OR newer.program IS NOT a.program OR newer.decimals IS NOT a.decimals OR newer.post_atomic IS NULL)))\n                    AND NOT EXISTS(SELECT 1 FROM watch_list removed WHERE removed.address=a.account AND removed.removed_at IS NOT NULL)\n                    ORDER BY a.account"


@pytest.mark.parametrize('seed',range(12))
def test_alias_export_matches_old_query_for_mixed_small_histories(aliases,seed):
    import random
    rng=random.Random(seed);c,path=aliases
    for i in range(4):snapshot(c,i+1,account=address(100+i))
    for flow in range(5,45):
        snapshot(c,flow,account=address(100+rng.randrange(4)),owner=address(rng.choice([1,4])),slot=rng.choice([None,0,99,100,101]),after=rng.choice([None,98.,101.]),known=rng.choice([-1.,94.,101.]),post=rng.choice([None,'0','20','bad']))
        c.execute('UPDATE watch_token_accounts SET state=?,reason=?,program=?,decimals=? WHERE signature=?',(rng.choice(['known','unknown']),rng.choice([None,'token_balance_side_missing','other']),rng.choice([TF.TOKEN22,'wrong']),rng.choice([6,None,256]),f'sig{flow:08d}'))
        c.execute('UPDATE watch_flow_timing SET flow_state=? WHERE flow_id=?',(rng.choice(['known',None,'unknown']),flow))
    c.commit();expected=set()
    for account,owner,mint,program,decimals,post,state,reason,known,after in c.execute(OLD_ALIAS_QUERY,(100.,)*3):
        if program not in (TF.TOKEN,TF.TOKEN22) or type(decimals) is not int or not 0<=decimals<=255:continue
        if not (state=='known' and reason is None or state=='unknown' and reason=='token_balance_side_missing') or post is None:continue
        try:
            TF._amount(post);TF._address(owner);TF._address(mint);TF._address(account)
            expected.add(account)
        except (TypeError,ValueError):pass
    W._sync(c,path,100.)
    assert json.loads(W.watch_file(path).read_text())['token_accounts']==sorted(expected)
