import json
import pytest
from sniper.office import store
from sniper.office.report import read_state,daily


@pytest.mark.parametrize('damage',['payload','predecessor'])
def test_corrupted_release_cannot_authorize_numbers_or_head_commentary(tmp_path,damage):
    conn=store.open_store(tmp_path/'office.db')
    row=dict(epoch_id='e',kind='epoch_economics',released_at=100.,aggregates=dict(state='known',opportunity_count=1,entered_count=1,net_pnl_usd=1.,cost_usd=0.,office_net_usd=1.),source_heads={'grades':'a'*64})
    if damage=='predecessor':
        store.append(conn,'sealed_views',dict(row,kind='discovery_status'))
    store.append(conn,'sealed_views',row)
    seq=conn.execute('SELECT MAX(seq) FROM sealed_views').fetchone()[0]
    conn.execute('INSERT INTO messages VALUES(?,?)',('head',json.dumps(dict(kind='head_report',author_role='Head',known_at=150.,text='PRIVATE_COMMENTARY',refs=[seq]))))
    if damage=='payload':
        conn.execute('DROP TRIGGER sealed_views_no_update')
        changed=dict(row,aggregates=dict(row['aggregates'],net_pnl_usd=999.,office_net_usd=999.))
        conn.execute('UPDATE sealed_views SET payload_json=? WHERE seq=?',(store.canonical_json(changed).decode(),seq))
    else:
        conn.execute('DROP TRIGGER sealed_views_no_delete')
        conn.execute('DELETE FROM sealed_views WHERE seq=1')
    conn.commit()
    assert not store.verify_chain(conn,'sealed_views')[0]
    conn.close()
    result=read_state(tmp_path,now=200.)
    assert result['sealed_views']['state']=='read_error'
    assert result['sealed_views']['items']==[] and result['head_commentary'] is None
    assert '$999' not in daily(result) and 'PRIVATE_COMMENTARY' not in daily(result)
    assert 'PRIVATE_COMMENTARY' not in json.dumps(result)
    assert 'Economics unavailable' in daily(result)
