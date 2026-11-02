"""Foreign loss volume must not spend the selected pool's row budget."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_losses as L
from test_desk_ps_model_facts import tape, facts, NOW, ROWS
from test_memecoin_desk_pumpswap import ACTOR


def add_losses(path, pools, count=20001, *, scope='complete'):
    with sqlite3.connect(path) as db:
        for n in range(count):
            sig, at = 'foreign%d' % n, NOW-30+n/count
            db.execute('INSERT INTO ps_decode_losses VALUES(?,?,?,?)',(sig,'decode_loss',at,50))
            db.execute('INSERT INTO ps_loss_attribution VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (sig,'decode_loss','malformed',scope,json.dumps(pools),1,'67f4521f2cf57777',20,
                 L._AMM_IDL,at,50,1))
            gid=db.execute("INSERT INTO gaps_ps VALUES(?,'decode_loss',50)",(at,)).lastrowid
            db.execute('INSERT INTO ps_loss_batches VALUES(?,?,?,?,?,?)',(gid,at,'decode_loss',1,1,at))
            db.execute('INSERT INTO ps_loss_batch_members VALUES(?,?,?)',(gid,sig,'decode_loss'))


@pytest.mark.parametrize('coalesced',[False,True])
def test_foreign_loss_volume_keeps_target_flow_with_live_shaped_batches(tmp_path,coalesced):
    rows=ROWS+[(-4,2.2,1,2,'extra1'),(-3,2.2,1,3,'extra2')]
    c,path=tape(tmp_path,rows)
    baseline=facts(c,tmp_path)
    add_losses(path,[ACTOR])
    if coalesced:
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM ps_loss_batches WHERE gap_rowid!=1')
            db.execute('DELETE FROM gaps_ps WHERE rowid!=1')
            db.execute('UPDATE ps_loss_batch_members SET gap_rowid=1')
            db.execute('UPDATE ps_loss_batches SET receipt_count=20001,through_at=?',(NOW-29,))
    out=facts(c,tmp_path)
    assert out['flow_60s'] is not None, out['coverage']
    assert out['flow_60s']==baseline['flow_60s']
    assert out['coverage']['60s']['observed_swaps']==baseline['coverage']['60s']['observed_swaps']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM swaps_ps WHERE pool=?',(c['pool'],)).fetchone()[0]==8
    with pytest.raises(L.LossUnreadable,match='loss:row_limit'):
        L.losses(tmp_path,ACTOR,NOW-3600,NOW)


@pytest.mark.parametrize('scope',['target','unknown'])
def test_target_or_unknown_volume_keeps_typed_overflow(tmp_path,scope):
    c,path=tape(tmp_path)
    add_losses(path,[c['pool']] if scope=='target' else [ACTOR],scope='complete' if scope=='target' else 'unknown')
    with pytest.raises(L.LossUnreadable,match='loss:row_limit'):
        L.losses(tmp_path,c['pool'],NOW-3600,NOW)
    assert facts(c,tmp_path)['flow_60s'] is None


def test_foreign_volume_does_not_hide_one_target_loss(tmp_path):
    c,path=tape(tmp_path)
    add_losses(path,[ACTOR])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE ps_loss_attribution SET pools_json=? WHERE signature='foreign0'",(json.dumps([c['pool']]),))
    assert L.losses(tmp_path,c['pool'],NOW-3600,NOW)==[NOW-30]
    assert facts(c,tmp_path)['flow_60s']['own_pool_losses']==1


def test_cap_counts_target_receipts_once_not_foreign_or_attested_wrappers(tmp_path):
    c,path=tape(tmp_path)
    add_losses(path,[ACTOR],count=3)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE ps_loss_attribution SET pools_json=? WHERE signature='foreign0'",(json.dumps([c['pool']]),))
    assert L.losses(tmp_path,c['pool'],NOW-3600,NOW,max_rows=1)==[NOW-30]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE ps_loss_attribution SET pools_json=? WHERE signature='foreign1'",(json.dumps([c['pool']]),))
    with pytest.raises(L.LossUnreadable,match='loss:row_limit'):
        L.losses(tmp_path,c['pool'],NOW-3600,NOW,max_rows=1)


def test_proof_cache_is_bounded_and_content_keyed(tmp_path):
    c,path=tape(tmp_path)
    add_losses(path,[ACTOR],count=1)
    assert L.losses(tmp_path,c['pool'],NOW-3600,NOW)==[]
    before=L._attribution_scope.cache_info()
    assert L.losses(tmp_path,c['pool'],NOW-3600,NOW)==[]
    assert L._attribution_scope.cache_info().hits>before.hits
    assert L._attribution_scope.cache_info().maxsize==32768
    with sqlite3.connect(path) as db:
        db.execute('UPDATE ps_loss_attribution SET pools_json=?',(json.dumps([c['pool']]),))
    assert L.losses(tmp_path,c['pool'],NOW-3600,NOW)==[NOW-30]
