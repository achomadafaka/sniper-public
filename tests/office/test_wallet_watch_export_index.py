"""Many token accounts sharing an owner/mint must not cross-scan each other's history."""
import ast
import inspect
import json
from collections import Counter

from .test_wallet_watch_alias_latest import aliases,snapshot,address
from sniper.office import wallet_watch as W


def alias_query():
    return next(n.value for n in ast.walk(ast.parse(inspect.getsource(W._sync)))
                if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value.lstrip().startswith('WITH latest'))


def test_shared_owner_mint_uses_account_lookup_with_bounded_work_and_identical_results(aliases):
    c,path=aliases
    for i in range(160):
        account=address(100+i)
        for version in range(4):snapshot(c,i*4+version+1,account=account,slot=100+version,post='invalid' if version==3 and i%3 else '0')
    c.commit();steps=0
    def bounded():
        nonlocal steps
        steps+=1000
        return steps>300000
    c.set_progress_handler(bounded,1000)
    try:W._sync(c,path,100.)
    finally:c.set_progress_handler(None,0)
    actual=json.loads(W.watch_file(path).read_text())
    # The latest eligible same-slot versions still include valid zero balances.
    assert actual['token_accounts']==sorted(address(100+i) for i in range(160) if i%3==0),dict(vm_steps=steps,coverage=actual['coverage'])
    assert steps<=300000
    query=alias_query()
    baseline=query.replace(' INDEXED BY watch_token_account_latest ON a.account=s.account',' ON a.account=s.account')
    assert Counter(c.execute(query,(100.,)*6).fetchall())==Counter(c.execute(baseline,(100.,)*6).fetchall())
    plan=[r[3] for r in c.execute('EXPLAIN QUERY PLAN '+alias_query(),(100.,)*6)]
    assert not any('SEARCH a USING INDEX watch_token_accounts_owner' in p for p in plan),plan
