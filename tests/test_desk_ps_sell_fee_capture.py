"""Capture explicit sell fees without granting virtual-state or price proof."""
import base64,json,struct,time,hashlib,sqlite3
import pytest
from sniper.services import memecoin_desk_pumpswap as P
from test_desk_ps_sell_post_reserves import FIX,logs
from test_memecoin_desk_pumpswap_worker import worker

OFFSETS={'lp_fee_basis_points':72,'lp_fee':80,'protocol_fee_basis_points':88,'protocol_fee':96,
         'coin_creator_fee_basis_points':344,'coin_creator_fee':352,'cashback_fee_basis_points':360,
         'cashback':368,'buyback_fee_basis_points':376,'buyback_fee':384}

def event(kind='sell',raw=None):
    data=FIX[kind][0] if raw is None else base64.b64encode(raw).decode()
    return P.events(logs(data,{'sell':'Sell','sell_v2':'SellV2'}[kind]))[0][1]

@pytest.mark.parametrize('kind',['sell','sell_v2'])
def test_raw_sell_fee_fields_are_explicit_and_never_virtual_proof(kind):
    raw=base64.b64decode(FIX[kind][0]);e=event(kind);p=e['payload']
    for key,off in OFFSETS.items():assert p.get(key)==int.from_bytes(raw[off:off+8],'little'),key
    assert p['sell_fee_capture_version']=='sell-fees-head360-ext392-v1'
    assert p['sell_fee_idl_sha256']==P.PUMP_AMM_IDL_SHA256
    assert p['sell_fee_capture_reason']is None
    assert e['cashback_fee_bps']==p['cashback_fee_basis_points']
    assert e['buyback_fee_bps']==p['buyback_fee_basis_points']
    assert e.get('virtual_quote_atomic')is None and e.get('virtual_quote_source')is None

@pytest.mark.parametrize('size',[360,364,368,376,391])
def test_absent_or_partial_fee_fields_are_null_not_zero(size):
    raw=base64.b64decode(FIX['sell_v2'][0])[:size];e=event('sell_v2',raw);p=e['payload']
    for key,off in OFFSETS.items():
        expected=int.from_bytes(raw[off:off+8],'little')if len(raw)>=off+8 else None
        assert p.get(key)==expected,key
    assert p['sell_fee_capture_reason']==('extension_absent'if size==360 else'extension_partial')
    assert e.get('virtual_quote_atomic')is None


def test_compacted_tape_keeps_fee_values_abi_and_nulls(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);at=time.time();e=event();expected={k:e['payload'].get(k)for k in OFFSETS}
    try:
        w.on_pumpswap(e,42,'public-sig',0,1,None,at,at);w.commit_files()
        c=w.files.conn('swaps_ps',at);payload,raw,cb,bb=c.execute('SELECT payload_json,payload_b64,cashback_fee_bps,buyback_fee_bps FROM swaps_ps').fetchone();p=json.loads(payload)
        assert {k:p.get(k)for k in OFFSETS}==expected
        assert p['sell_fee_capture_version']=='sell-fees-head360-ext392-v1'
        assert raw is None and cb==str(expected['cashback_fee_basis_points']) and bb==str(expected['buyback_fee_basis_points'])
        e=event('sell_v2',base64.b64decode(FIX['sell_v2'][0])[:360])
        w.on_pumpswap(e,43,'public-sig2',0,1,None,at+.01,at+.01);w.commit_files()
        p=json.loads(c.execute('SELECT payload_json FROM swaps_ps WHERE signature=?',('public-sig2',)).fetchone()[0])
        assert p['cashback_fee_basis_points']is None and p['sell_fee_capture_reason']=='extension_absent'
    finally:
        for c in w.files._open.values():c.close()

def witness_module():
    from sniper.services import memecoin_desk_sell_fee_witness as S
    return S


def test_witness_is_bottom100_per_kind_and_keeps_raw_hash_and_published_fields():
    S=witness_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    for ddl in S.INDEXES['ps_sell_fee_witness']:c.execute(ddl)
    for kind in('sell','sell_v2'):
        for n in range(105):S.record(c,event(kind),signature=kind+str(n),ordinal=n,slot=42,tx_index=1,received_at=101.,known_at=102.)
        rows=c.execute('SELECT signature,ordinal,rank,raw,raw_sha256,published_json FROM ps_sell_fee_witness WHERE kind=?',(kind,)).fetchall()
        assert len(rows)==100
        expected=sorted(hashlib.sha256((kind+str(n)).encode()+b'\0'+n.to_bytes(8,'big')).hexdigest()for n in range(105))[:100]
        assert sorted(r[2]for r in rows)==expected
        for sig,ordinal,rank,raw,digest,published in rows:
            assert hashlib.sha256(raw).hexdigest()==digest
            assert json.loads(published)['cashback_fee_basis_points']==int.from_bytes(raw[360:368],'little')
    assert c.execute('SELECT count(*) FROM ps_sell_fee_witness').fetchone()==(200,)


def checker_module():
    import importlib.util
    p=__import__('pathlib').Path(__file__).resolve().parents[1]/'scripts/research/check_ps_sell_fee_witness.py'
    spec=importlib.util.spec_from_file_location('fee_checker',p);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod


def test_independent_checker_compares_raw_fields_and_rejects_corrupt_published_value():
    S=witness_module();C=checker_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    S.record(c,event(),signature='sig',ordinal=1,slot=42,tx_index=1,received_at=101.,known_at=102.)
    row=c.execute('SELECT signature,ordinal,kind,raw,raw_sha256,published_json FROM ps_sell_fee_witness').fetchone()
    assert C.check_rows([row])['mismatches']==[]
    for wrong in (1,0.0,False):
        p=json.loads(row[-1]);p['cashback']=wrong
        bad=(*row[:-1],json.dumps(p))
        assert C.check_rows([bad])['mismatches'][0]['field']=='cashback'
    assert C.check_rows([row])['ready']is False # one fixture is never 200 live receipts


def test_worker_retains_bounded_authenticated_event_witness(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);at=time.time();e=event()
    try:
        w.on_pumpswap(e,42,'public-sig',0,1,None,at,at);w.commit_files();c=w.files.conn('swaps_ps',at)
        sig,ordinal,raw,published=c.execute('SELECT signature,ordinal,raw,published_json FROM ps_sell_fee_witness').fetchone()
        assert (sig,ordinal)==('public-sig',0)
        assert raw==base64.b64decode(e['payload_b64'])
        assert json.loads(published)['lp_fee']==e['payload']['lp_fee']
    finally:
        for c in w.files._open.values():c.close()


def test_oversize_witness_keeps_hash_selected_identity_but_never_counts_as_decode_pass():
    S=witness_module();C=checker_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    e=event();e['payload_b64']=base64.b64encode(base64.b64decode(e['payload_b64'])+bytes(S.MAX_RAW)).decode()
    e['payload']['sell_fee_event_bytes']=len(base64.b64decode(e['payload_b64']))
    assert S.record(c,e,signature='oversize',ordinal=1,slot=42,tx_index=1,received_at=101.,known_at=102.)=='oversize'
    row=c.execute('SELECT signature,ordinal,kind,raw,raw_sha256,published_json FROM ps_sell_fee_witness').fetchone()
    assert row is not None and row[3]is None
    report=C.check_rows([row]);assert report['valid']==0 and report['unverified']==1 and not report['ready']


def test_witness_failure_never_discards_the_captured_swap(tmp_path,monkeypatch):
    S=witness_module();w=worker(tmp_path,monkeypatch);at=time.time()
    def fail(*a,**k):raise OSError('witness unavailable')
    monkeypatch.setattr(S,'record',fail)
    try:
        w.on_pumpswap(event(),42,'sig',0,1,None,at,at);w.commit_files();c=w.files.conn('swaps_ps',at)
        assert c.execute('SELECT count(*) FROM swaps_ps').fetchone()==(1,)
        assert w.stats['ps_sell_fee_witness:error']==1
    finally:
        for c in w.files._open.values():c.close()


def test_fee_witness_survives_offline_conversion(tmp_path):
    from test_ps_tape_shrink import _old_tape,M
    S=witness_module();base=tmp_path/'pumpswap';_old_tape(base,n_pools=6,per_pool=1)
    c=sqlite3.connect(base/'tape.db');S.record(c,event(),signature='sig',ordinal=1,slot=42,tx_index=1,received_at=101.,known_at=102.)
    before=c.execute('SELECT * FROM ps_sell_fee_witness').fetchall();c.commit();c.close()
    M.convert(base,log=lambda *a:None);c=sqlite3.connect(base/'tape.new.db')
    assert c.execute('SELECT * FROM ps_sell_fee_witness').fetchall()==before;c.close()


def test_idle_expiry_only_removes_bounded_witness_rows(tmp_path,monkeypatch):
    S=witness_module();w=worker(tmp_path,monkeypatch);at=time.time();c=w.files.conn('swaps_ps',at)
    for n in range(9):S.record(c,event(),signature=str(n),ordinal=n,slot=42,tx_index=1,received_at=101.,known_at=102.)
    try:
        w.heartbeat(True)
        assert c.execute('SELECT count(*) FROM ps_sell_fee_witness').fetchone()==(1,)
        w.heartbeat(True)
        assert c.execute('SELECT count(*) FROM ps_sell_fee_witness').fetchone()==(0,)
    finally:
        for c in w.files._open.values():c.close()


def test_sampling_covers_decoded_sells_before_the_matter_filter(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.ps_filter=True;monkeypatch.setattr(w,'matter_set',lambda now:set());at=time.time()
    try:
        w.on_pumpswap(event(),42,'sig',0,1,None,at,at);c=w.files.conn('swaps_ps',at)
        assert c.execute('SELECT count(*) FROM swaps_ps').fetchone()==(0,)
        assert c.execute('SELECT count(*) FROM ps_sell_fee_witness').fetchone()==(1,)
    finally:
        for c in w.files._open.values():c.close()
