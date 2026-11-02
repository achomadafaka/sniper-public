import json
from pathlib import Path
import pytest


def test_projection_never_pools_chains():
    from sniper.services.memecoin_wallet_map import universe_projection
    out = universe_projection(chain='robinhood', generated_at='2026-10-02T00:00:00+00:00',
        wallets=[{'uid': 'A', 'chain': '4663'}, {'uid': 'A', 'chain': 'solana'}],
        edges=[{'chain': 'solana', 'src_uid': 'A', 'dst_uid': 'B', 'kind': 'slot_follower'}],
        graph_state='not_collected')
    assert out['chain'] == '4663' and len(out['wallets']) == 1 and out['edges'] == []
    assert out['graph_state'] == 'not_collected'


def test_projection_rejects_unknown_chain_and_preserves_case():
    from sniper.services.memecoin_wallet_map import universe_projection
    with pytest.raises(ValueError):
        universe_projection(chain='typo', wallets=[], edges=[], generated_at='x', graph_state='ok')
    rows=[{'uid': 'Aa', 'chain': 'solana'}, {'uid': 'aa', 'chain': 'solana'}]
    out=universe_projection(chain='solana',wallets=rows,edges=[],generated_at='x',graph_state='partial')
    assert [r['uid'] for r in out['wallets']] == ['Aa','aa']
    assert out['wallets'][0]['paper'] is None


def test_atomic_publish_keeps_previous_on_replace_failure(tmp_path, monkeypatch):
    from scripts.research.wallet_follow_funnel import publish_universe
    import os
    p=tmp_path/'universe.json';p.write_text('{"version":1}')
    def fail(*a): raise OSError('disk failure')
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError): publish_universe(p,{'version':2})
    assert json.loads(p.read_text()) == {'version':1}
    assert list(tmp_path.iterdir()) == [p]


def test_relationship_direction_and_distinct_coin_evidence():
    from sniper.services.memecoin_wallet_map import universe_projection
    edges=[dict(chain='solana',src_uid='A',dst_uid='B',kind=k,coins=['M','M','N'])
           for k in ['shared_funder','co_buy_slot','slot_follower']]
    out=universe_projection(chain='solana',wallets=[],edges=edges,generated_at='x',graph_state='ok')
    assert [e['direction'] for e in out['edges']] == ['association','association','ordered']
    assert out['edges'][0]['coins'] == ['M','N']


def test_offline_build_uses_existing_ledger_without_writes():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_wallet_map import build_universe
    c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, 'A', 'alpha', '')
    c.execute('PRAGMA query_only=ON')
    out=build_universe(c,None,chain='8453',generated_at='2026-10-02T00:00:00+00:00')
    assert out['graph_state']=='not_collected'
    assert all(r['chain']=='8453' for r in out['wallets'])


def test_chain_without_legacy_paper_report_is_explicitly_unmeasured():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_wallet_map import build_universe
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;m.ensure_memecoin_tables(c);m.add_wallet(c,'A','a','')
    c.execute('PRAGMA query_only=ON')
    out=build_universe(c,None,chain='5042',generated_at='2026-10-02T00:00:00+00:00')
    assert out['wallets'][0]['setups']==[] and out['wallets'][0]['strict_policy'] is None


def test_research_coverage_reaches_read_only_projection(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db,record_observation
    from sniper.services.wallet_observation import project_research_evidence
    event=lambda:dict(origin='o1',uid='A',chain='solana',address='Owner',signature='sig',event_index='0',mint='M',action='buy',state='decoded',source_at='2026-10-02T00:00:00+00:00',received_at='2026-10-02T00:00:01+00:00',quantity_atomic='1',quote_atomic='2',setup_json='{}',payload_json='{}')
    c=open_observation_db(tmp_path/'r.db');record_observation(c,event());c.commit();c.execute('PRAGMA query_only=ON')
    out=project_research_evidence(c,None,chain='solana')
    assert out['A']['buys']==1 and out['A']['distinct_coins']==1
    assert out['A']['policy_state']=='insufficient_quote_grid'
    assert project_research_evidence(c,None,chain='4663')=={}


def test_latest_contract_uses_open_clock_not_insert_order():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_wallet_map import build_universe
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;m.ensure_memecoin_tables(c);m.add_wallet(c,'A','a','')
    for opened,contract in [('2026-10-02','new'),('2026-09-02','old')]:
        c.execute("INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,is_open,opened_at,funded_contract_hash) VALUES ('A','M','8453','real',0,?,?)",(opened,contract))
    c.execute('PRAGMA query_only=ON')
    out=build_universe(c,None,chain='8453',generated_at='2026-10-02T00:00:00+00:00')
    assert out['wallets'][0]['latest_funded_contract']['hash']=='new'
