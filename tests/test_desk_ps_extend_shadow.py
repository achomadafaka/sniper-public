"""ExtendAccount candidate stays shadow; source losses and own-pool refusal survive."""
import base64,hashlib,json,time
from pathlib import Path
import pytest
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_pumpswap as P
from sniper.services import memecoin_desk_ps_losses as L
from sniper.services import memecoin_desk_universe as U

FIX=Path(__file__).parent/'fixtures/memecoin/desk_extend_account_20261028.json'
DISC=bytes.fromhex('6161d7905d92167c')
IX=bytes.fromhex('ea66c2cb96483ee5')

def receipt():
    saved=json.loads(FIX.read_text());raw=base64.b64decode(saved['raw_b64'],validate=True)
    assert hashlib.sha256(raw).hexdigest()==saved['frame_sha256']
    info=pb.SubscribeUpdate.FromString(raw).transaction.transaction
    msg,meta=info.transaction.message,info.meta
    keys=[U.b58encode(k)for k in (*msg.account_keys,*meta.loaded_writable_addresses,*meta.loaded_readonly_addresses)]
    instructions=[dict(program_id_index=i.program_id_index,accounts=list(i.accounts),data=bytes(i.data))for i in (*msg.instructions,*(i for g in meta.inner_instructions for i in g.instructions))]
    return raw,list(meta.log_messages),keys,instructions

def candidate(logs,keys,instructions,*,complete=True,idl=P.PUMP_AMM_IDL_SHA256):
    fn=getattr(P,'extend_account_shadow_keys',None)
    assert fn is not None,'shadow candidate missing'
    return fn(logs,account_keys=keys,instructions=instructions,inner_complete=complete,idl_sha256=idl)

def test_real_extend_candidate_is_complete_key_absence_only_and_existing_decode_unchanged():
    _,logs,keys,ix=receipt()
    with pytest.raises(P.DecodeLoss):P.events(logs)
    original=P.loss_details(logs,reason='pumpswap_virtual_quote_unproven',account_keys=keys,instructions=ix,inner_complete=True)
    assert 'swap_tx_account_keys'not in original
    got=candidate(logs,keys,ix);assert got==sorted(set(keys))
    bits=L.event_account_filter(got)
    assert all(not L.event_account_absent(bits,k)for k in keys)
    withdraw=next(i for i in ix if i['data'][:8].hex()=='b712469c946da122')
    assert not L.event_account_absent(bits,keys[withdraw['accounts'][0]]) # mixed receipt still touches its own pool
    assert L.event_account_absent(bits,U.b58encode(bytes([199])*32))

@pytest.mark.parametrize('damage',['short','extra','disc','program','cpi','ix_length','accounts','incomplete','idl','truncated','unknown_ix'])
def test_extend_shadow_refuses_malformed_or_incomplete_proof(damage):
    _,logs,keys,ix=receipt();complete=True;idl=P.PUMP_AMM_IDL_SHA256
    event=next(n for n,line in enumerate(logs)if line.startswith('Program data: ')and P.anchor_event_bytes(line)[:8]==DISC)
    raw=P.anchor_event_bytes(logs[event])
    if damage in ('short','extra','disc'):
        raw=raw[:-1]if damage=='short'else raw+b'\0'if damage=='extra'else b'bad_disc'+raw[8:]
        logs[event]='Program data: '+base64.b64encode(raw).decode()
        cpi=next(i for i in ix if i['data'].startswith(P._EVENT_CPI+DISC));cpi['data']=P._EVENT_CPI+raw
    elif damage=='program':logs=[x.replace(P.AMM_PROGRAM,U.PUMP_PROGRAM)for x in logs]
    elif damage=='cpi':next(i for i in ix if i['data'].startswith(P._EVENT_CPI+DISC))['data']+=b'\0'
    elif damage=='ix_length':next(i for i in ix if i['data'][:8]==IX)['data']+=b'\0'
    elif damage=='accounts':next(i for i in ix if i['data'][:8]==IX)['accounts'].pop()
    elif damage=='incomplete':complete=False
    elif damage=='idl':idl='0'*64
    elif damage=='truncated':logs+=['Log truncated']
    else:ix.append(dict(program_id_index=keys.index(P.AMM_PROGRAM),accounts=[],data=b'unknown!'))
    assert candidate(logs,keys,ix,complete=complete,idl=idl)is None

def shadow_module():
    from sniper.services import memecoin_desk_extend_shadow as S
    return S

def test_shadow_record_preserves_original_and_never_enters_effective_envelope(tmp_path):
    import sqlite3
    S=shadow_module();raw,logs,keys,ix=receipt();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    for statements in S.INDEXES.values():
        for sql in statements:c.execute(sql)
    got=candidate(logs,keys,ix);assert got
    assert S.record(c,raw,signature='public-sig',cause='virtual_quote_unproven',received_at=100.,known_at=101.,slot=42,keys=got,reason='original',original_class='unknown_loss_receipts')=='captured'
    body,blob,status=c.execute('SELECT record_json,raw,raw_status FROM ps_extend_shadow').fetchone();body=json.loads(body)
    assert body['shadow']is True and body['original_class']=='unknown_loss_receipts'
    assert body['shadow_class']=='proven_unrelated_extendaccount'
    assert body['applies_only_when']=='target_absent_from_complete_keys'
    assert body['keys']==got and body['profile']==L.SWAP_TX_ENVELOPE_PROFILE
    assert blob==raw and status=='captured' and len(body['envelope_id'])==64
    assert S.record(c,b'changed',signature='public-sig',cause='virtual_quote_unproven',received_at=100.,known_at=102.,slot=42,keys=got,reason='new',original_class='unknown_loss_receipts')=='duplicate'
    assert c.execute('SELECT raw FROM ps_extend_shadow').fetchone()==(raw,)


def test_bottom200_replaces_only_raw_and_keeps_proof_rows_across_restart(tmp_path):
    import sqlite3
    S=shadow_module();path=tmp_path/'shadow.db';c=sqlite3.connect(path)
    for sql in S.SCHEMAS.values():c.execute(sql)
    for statements in S.INDEXES.values():
        for sql in statements:c.execute(sql)
    signatures=sorted(('s'+str(i)for i in range(205)),key=lambda s:hashlib.sha256(s.encode()).hexdigest())
    def write(s,raw=b'frame'):
        return S.record(c,raw,signature=s,cause='decode_loss',received_at=100.,known_at=101.,slot=42,keys=[P.AMM_PROGRAM],reason='original',original_class='unknown_loss_receipts')
    for s in signatures[3:203]:assert write(s)=='captured'
    c.commit();c.close();c=sqlite3.connect(path) # no process-local cap cache
    victim=signatures[202];proof=c.execute('SELECT record_json FROM ps_extend_shadow WHERE signature=?',(victim,)).fetchone()[0]
    assert write(signatures[0])=='captured'
    assert c.execute('SELECT record_json,raw,raw_status FROM ps_extend_shadow WHERE signature=?',(victim,)).fetchone()==(proof,None,'sampled_out')
    assert write(signatures[204])=='sampled_out'
    assert c.execute('SELECT count(*) FROM ps_extend_shadow WHERE raw IS NOT NULL').fetchone()[0]==200
    assert c.execute('SELECT count(*) FROM ps_extend_shadow').fetchone()[0]==202
    assert write('oversized',b'x'*(S.MAX_RAW+1))=='oversize'
    assert S.status(c,'1970-01-01')['cap_hits']==3
    all_ranks=c.execute('SELECT rank FROM ps_extend_shadow ORDER BY rank LIMIT 200').fetchall()
    selected=c.execute('SELECT rank FROM ps_extend_shadow WHERE sample_selected=1 ORDER BY rank').fetchall()
    assert selected==all_ranks # oversize candidates also occupy their hash-selected slot
    c.close()

def test_worker_shadow_never_changes_effective_losses_and_unresolved_alt_refuses(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker
    raw,logs,keys,ix=receipt();w=worker(tmp_path,monkeypatch);at=time.time()-.01
    try:
        w.process(raw,received_at=at);w.commit_files();db=w.files.conn('ps_decode_losses',at)
        assert db.execute('SELECT count(*) FROM ps_decode_losses').fetchone()==(1,)
        assert db.execute('SELECT count(*) FROM ps_extend_shadow').fetchone()==(1,)
        assert db.execute('SELECT count(*) FROM ps_loss_event_envelopes').fetchone()==(0,)
        foreign=U.b58encode(bytes([199])*32)
        assert L.losses(tmp_path,foreign,at-1,time.time()+1)==[at] # shadow never clears even unrelated pool
        assert w.stats['ps_extend_shadow:captured']==1
        u=pb.SubscribeUpdate.FromString(raw);info=u.transaction.transaction
        info.signature=bytes([4])*64
        lookup=info.transaction.message.address_table_lookups.add();lookup.account_key=bytes([9])*32;lookup.readonly_indexes=b'\0'
        w.process(u.SerializeToString(),received_at=at+.001);w.commit_files()
        assert db.execute('SELECT count(*) FROM ps_decode_losses').fetchone()==(2,)
        assert db.execute('SELECT count(*) FROM ps_extend_shadow').fetchone()==(1,)
    finally:
        for db in w.files._open.values():db.close()

def test_shadow_failure_preserves_original_loss_and_reports_unknown_audit(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker
    S=shadow_module();w=worker(tmp_path,monkeypatch);raw,_,_,_=receipt();at=time.time()-.01
    def fail(*a,**k):raise OSError('shadow disk unavailable')
    monkeypatch.setattr(S,'record',fail)
    try:
        w.process(raw,received_at=at);w.commit_files();db=w.files.conn('ps_decode_losses',at)
        assert db.execute('SELECT count(*) FROM ps_decode_losses').fetchone()==(1,)
        assert db.execute('SELECT count(*) FROM ps_extend_shadow').fetchone()==(0,)
        assert w.stats['ps_extend_shadow:error']==1
        assert L.losses(tmp_path,U.b58encode(bytes([199])*32),at-1,time.time()+1)==[at]
    finally:
        for db in w.files._open.values():db.close()


def test_raw_retention_changes_no_proof_metadata_and_new_day_has_own_budget():
    import sqlite3
    S=shadow_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    def put(sig,at):return S.record(c,b'frame',signature=sig,cause='decode_loss',received_at=at,known_at=at,slot=42,keys=[P.AMM_PROGRAM],reason='original',original_class='unknown_loss_receipts')
    put('old',100.)
    old=c.execute('SELECT record_json FROM ps_extend_shadow').fetchone()[0]
    put('new',3*86400+100.)
    assert c.execute('SELECT record_json,raw,raw_status FROM ps_extend_shadow WHERE signature=?',('old',)).fetchone()==(old,None,'sampled_out')
    assert S.status(c,'1970-01-04')['captured']==1

def test_offline_tape_conversion_preserves_shadow_proof_and_raw(tmp_path):
    import sqlite3
    from test_ps_tape_shrink import _old_tape,M
    S=shadow_module();base=tmp_path/'pumpswap';_old_tape(base,n_pools=6,per_pool=1)
    c=sqlite3.connect(base/'tape.db')
    S.record(c,b'full raw',signature='shadow',cause='decode_loss',received_at=100.,known_at=101.,slot=42,keys=[P.AMM_PROGRAM],reason='original',original_class='unknown_loss_receipts')
    S.note_error(c,101.,'record')
    before=c.execute('SELECT * FROM ps_extend_shadow').fetchall();errors=c.execute('SELECT * FROM ps_extend_shadow_errors').fetchall();c.commit();c.close()
    M.convert(base,log=lambda *a:None)
    c=sqlite3.connect(base/'tape.new.db')
    assert c.execute('SELECT * FROM ps_extend_shadow').fetchall()==before
    assert c.execute('SELECT * FROM ps_extend_shadow_errors').fetchall()==errors
    c.close()

@pytest.mark.parametrize('failure',['uninitialized','status_error','restart'])
def test_shadow_denominator_never_claims_day_completeness_without_durable_coverage(tmp_path,monkeypatch,failure):
    from test_memecoin_desk_pumpswap_worker import worker,W
    S=shadow_module();w=worker(tmp_path,monkeypatch)
    if failure!='uninitialized':w.files.conn('ps_decode_losses',time.time())
    if failure=='status_error':
        def fail(*a,**k):raise OSError('status unavailable')
        monkeypatch.setattr(W.ES,'status',fail)
    try:
        w.heartbeat(True)
        beat=json.loads((tmp_path/'universe.heartbeat').read_text())['ps_extend_shadow']
        assert beat['denominator_complete']is False
        assert beat['denominator_basis']=='retained_rows_only'
    finally:
        for db in w.files._open.values():db.close()


def test_duplicate_replay_runs_bounded_raw_expiry_without_changing_proof():
    import sqlite3
    S=shadow_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    kw=dict(signature='old',cause='decode_loss',received_at=100.,slot=42,keys=[P.AMM_PROGRAM],reason='original',original_class='unknown_loss_receipts')
    S.record(c,b'raw',known_at=101.,**kw)
    proof=c.execute('SELECT record_json FROM ps_extend_shadow').fetchone()[0]
    assert S.record(c,b'raw',known_at=10*86400.,**kw)=='duplicate'
    assert c.execute('SELECT record_json,raw,raw_status FROM ps_extend_shadow').fetchone()==(proof,None,'sampled_out')


def test_idle_heartbeat_expires_raw_without_new_candidate(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker
    S=shadow_module();w=worker(tmp_path,monkeypatch);c=w.files.conn('ps_decode_losses',time.time())
    S.record(c,b'raw',signature='old',cause='decode_loss',received_at=100.,known_at=101.,slot=42,keys=[P.AMM_PROGRAM],reason='original',original_class='unknown_loss_receipts')
    proof=c.execute('SELECT record_json FROM ps_extend_shadow').fetchone()[0]
    try:
        w.heartbeat(True)
        assert c.execute('SELECT record_json,raw,raw_status FROM ps_extend_shadow').fetchone()==(proof,None,'sampled_out')
    finally:
        for db in w.files._open.values():db.close()


def test_shadow_daily_error_survives_worker_restart(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker
    S=shadow_module();w=worker(tmp_path,monkeypatch);raw,_,_,_=receipt();at=time.time()-.01
    def fail(*a,**k):raise OSError('record unavailable')
    monkeypatch.setattr(S,'record',fail)
    w.process(raw,received_at=at);w.commit_files()
    for db in w.files._open.values():db.close()
    restarted=worker(tmp_path,monkeypatch);c=restarted.files.conn('ps_decode_losses',time.time())
    try:
        day=time.strftime('%Y-%m-%d',time.gmtime())
        assert S.status(c,day)['audit_errors']==1
        restarted.heartbeat(True)
        beat=json.loads((tmp_path/'universe.heartbeat').read_text())['ps_extend_shadow']
        assert beat['audit_errors']==1 and beat['denominator_complete']is False
    finally:
        for db in restarted.files._open.values():db.close()


def test_periodic_expiry_batch_is_bounded_and_preserves_all_proof_rows():
    import sqlite3
    S=shadow_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    for i in range(9):
        S.record(c,b'raw',signature=str(i),cause='decode_loss',received_at=100.,known_at=101.,slot=42,keys=[P.AMM_PROGRAM],reason='original',original_class='unknown_loss_receipts')
    assert S.expire(c,'1970-01-11')==8
    assert c.execute('SELECT count(*),sum(raw IS NOT NULL) FROM ps_extend_shadow').fetchone()==(9,1)
    assert S.expire(c,'1970-01-11')==1
    assert c.execute('SELECT count(*),sum(raw IS NOT NULL) FROM ps_extend_shadow').fetchone()==(9,0)
