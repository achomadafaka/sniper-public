"""Bounded diagnostics cannot affect the tape or publish torn evidence."""
import base64
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path

import pytest

try:
    from sniper.services import memecoin_desk_state_witness as D
except ImportError:
    D=None

NOW=1_800_000_000.
DISC='82a42461e48287a5'


def writer(root,**kw):
    assert D is not None, 'state witness writer missing'
    return D.Writer(root,**kw)


def capture(w,*,raw=b'public frame',at=NOW,disc=DISC):
    return w.capture(raw,signature='public-signature',slot=42,tx_index=7,received_at=at-.01 if type(at) in (int,float) else NOW-.01,known_at=at,
                     discriminator=disc,reason='pumpswap_virtual_quote_unproven',idl_sha256='b7d8c57a4d9c4dd0109a9ab893052352333252d4eedced10ac091d4d66cab89b',decoder_root='/pinned/release')


def test_full_frame_and_identity_are_preserved_with_hash(tmp_path):
    w=writer(tmp_path)
    path=capture(w,raw=b'original bytes')
    saved=json.loads(path.read_text())
    assert base64.b64decode(saved['raw_b64'],validate=True)==b'original bytes'
    assert saved['frame_sha256']==hashlib.sha256(b'original bytes').hexdigest()
    assert saved['received_at']==NOW-.01 and saved['known_at']==NOW
    assert saved['slot']==42 and saved['tx_index']==7 and saved['discriminator']==DISC
    assert path.stat().st_size<=256*1024 and not path.with_name(path.name+'.tmp').exists()


def test_daily_kind_cap_survives_restart_and_is_independent(tmp_path):
    w=writer(tmp_path)
    paths=[capture(w) for _ in range(25)]
    assert sum(p is not None for p in paths)==20
    restarted=writer(tmp_path)
    assert capture(restarted) is None
    other='3f451c16305cc2b9'
    assert capture(restarted,disc=other) is not None
    assert capture(restarted,at=NOW+86400) is not None


def test_disabled_or_unknown_discriminator_never_touches_disk(tmp_path):
    w=writer(tmp_path,enabled=False)
    assert capture(w) is None
    assert not (tmp_path/'pumpswap').exists()
    w=writer(tmp_path)
    assert capture(w,disc='ffffffffffffffff') is None
    assert not (tmp_path/'pumpswap').exists()


def test_oversized_or_invalid_clock_never_publishes(tmp_path):
    w=writer(tmp_path)
    assert capture(w,raw=b'x'*(256*1024)) is None
    for at in (None,float('nan'),-1,True):
        assert capture(w,at=at) is None
    assert w.stats['oversize']==1


def test_retention_removes_only_owned_files_and_partial_temps(tmp_path):
    w=writer(tmp_path)
    first=capture(w)
    foreign=first.parent/'operator-note.txt';foreign.write_text('preserve')
    temp=first.with_name(first.name+'.tmp');temp.write_bytes(b'torn')
    latest=capture(w,at=NOW+3*86400)
    assert latest is not None and not first.exists() and not temp.exists()
    assert foreign.read_text()=='preserve'


def test_atomic_publication_failure_leaves_no_final_witness(tmp_path,monkeypatch):
    w=writer(tmp_path)
    def fail(*args,**kw):raise OSError('disk failure')
    monkeypatch.setattr(D.os,'link',fail)
    assert capture(w) is None and w.stats['storage_error']==1
    assert w.stats['stored']==0


def test_existing_final_file_is_never_overwritten(tmp_path):
    first=capture(writer(tmp_path),raw=b'first')
    original=first.read_bytes()
    second=capture(writer(tmp_path),raw=b'second')
    assert second!=first and first.read_bytes()==original


def test_directory_scan_only_at_first_day_read(tmp_path,monkeypatch):
    w=writer(tmp_path);assert capture(w) is not None
    def fail(*args,**kwargs):raise AssertionError('unexpected per-frame scan')
    monkeypatch.setattr(Path,'iterdir',fail)
    assert capture(w) is not None


def test_clock_rollback_and_symlinked_namespace_refuse(tmp_path):
    w=writer(tmp_path);assert capture(w) is not None
    assert capture(w,at=NOW-86400) is None
    root=tmp_path/'second';root.mkdir()
    external=tmp_path/'external';external.mkdir()
    (root/'pumpswap').mkdir();(root/'pumpswap'/'state_witnesses').symlink_to(external,target_is_directory=True)
    assert capture(writer(root)) is None
    assert not list(external.iterdir())


def state_frame(disc=DISC, signature=1):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from test_desk_ps_routed_scope import FIXTURES
    from sniper.services import memecoin_desk_pumpswap as P
    saved=json.loads((FIXTURES/'multi_hop_26_pump.json').read_text())
    update=pb.SubscribeUpdate.FromString(base64.b64decode(saved['raw_b64']))
    info=update.transaction.transaction
    info.signature=bytes([signature])*64
    at,_=next(iter(P.program_data_logs(info.meta.log_messages,P.AMM_PROGRAM)))
    info.meta.log_messages[at]='Program data: '+base64.b64encode(bytes.fromhex(disc)).decode()
    return update.SerializeToString(),saved['received_at']


def test_worker_captures_original_refused_frame_without_any_loss_or_swap_change(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker,W
    from sniper.util import clock
    monkeypatch.setattr(clock,'offset_s',lambda:0.)
    raw,rx=state_frame()
    monkeypatch.setattr(W.time,'time',lambda:rx+.05)
    w=worker(tmp_path,monkeypatch)
    w.process(raw,received_at=rx)
    assert w.ps_state_witness.stats['stored']==1
    c=w.files.conn('ps_decode_losses',rx)
    assert c.execute('SELECT cause FROM ps_decode_losses').fetchall()==[('virtual_quote_unproven',)]
    assert w.ps_loss_pending['virtual_quote_unproven']
    assert c.execute('SELECT count(*) FROM swaps_ps').fetchone()[0]==0
    w.heartbeat(True)
    assert json.loads((tmp_path/'universe.heartbeat').read_text())['ps_state_witness']['stored']==1
    w.commit_files()
    from sniper.services import memecoin_desk_ps_losses as L
    from test_memecoin_desk_pumpswap import ACTOR
    assert L.losses(tmp_path,ACTOR,rx-1,rx+1)==[rx]


def test_worker_witness_failure_preserves_pending_and_other_processing_and_logs_once(tmp_path,monkeypatch,caplog):
    from test_memecoin_desk_pumpswap_worker import worker,W
    from sniper.util import clock
    monkeypatch.setattr(clock,'offset_s',lambda:0.)
    raw,rx=state_frame()
    monkeypatch.setattr(W.time,'time',lambda:rx+.05)
    w=worker(tmp_path,monkeypatch)
    other=[]
    w.curve_capture=False
    monkeypatch.setattr(w,'_retired_curve_event',lambda *a,**k:other.append(a))
    def broken(*a,**k):
        assert w.ps_loss_pending['virtual_quote_unproven']
        assert w.files.conn('ps_decode_losses',rx).execute('SELECT count(*) FROM ps_decode_losses').fetchone()[0]>=1
        raise RuntimeError('injected diagnostic failure')
    monkeypatch.setattr(D.Writer,'capture',broken)
    w.process(raw,received_at=rx)
    w.process(state_frame(signature=2)[0],received_at=rx)
    assert len(w.ps_loss_pending['virtual_quote_unproven'])==2 and other
    assert w.stats['ps_state_witness_error']==2
    assert len([r for r in caplog.records if 'state witness capture failed' in r.message])==1


def test_worker_kill_knob_is_read_once_and_leaves_original_losses(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker,W
    from sniper.util import clock
    monkeypatch.setattr(clock,'offset_s',lambda:0.)
    raw,rx=state_frame()
    monkeypatch.setattr(W.time,'time',lambda:rx+.05)
    monkeypatch.setenv('MEMECOIN_DESK_PS_STATE_WITNESSES','0')
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setenv('MEMECOIN_DESK_PS_STATE_WITNESSES','1')
    w.process(raw,received_at=rx)
    assert w.ps_state_witness_enabled is False and w.ps_state_witness is None and w.ps_loss_pending['virtual_quote_unproven']
    assert not (tmp_path/'pumpswap'/'state_witnesses').exists()


def test_noncanonical_foreign_slot_name_is_not_retention_owned(tmp_path):
    w=writer(tmp_path);first=capture(w)
    foreign=first.parent/(DISC+'-+1.json');foreign.write_text('operator data')
    capture(w,at=NOW+3*86400)
    assert foreign.read_text()=='operator data'


@pytest.mark.parametrize('disc',[
    '82a42461e48287a5','3f451c16305cc2b9','1609851aa02c47c0','ae7c4af90451f611','78f83d531f8e6b90'])
def test_all_five_pinned_kinds_are_diagnostic_only(tmp_path,disc):
    saved=json.loads(capture(writer(tmp_path),disc=disc).read_text())
    assert saved['discriminator']==disc and saved['diagnostic_only'] is True
    assert saved['label_basis']=='first_blocking_discriminator'


def test_failed_temp_is_ignored_and_cleaned_before_next_restart_publish(tmp_path,monkeypatch):
    w=writer(tmp_path)
    with monkeypatch.context() as mp:
        def fail(*a,**k):raise OSError('publish unavailable')
        mp.setattr(D.os,'link',fail)
        assert capture(w,raw=b'old temporary') is None
    day=datetime.fromtimestamp(NOW,timezone.utc).date().isoformat()
    assert not any(p.name.endswith('.json') for p in (w.root/day).iterdir())
    saved=capture(writer(tmp_path),raw=b'new complete')
    assert base64.b64decode(json.loads(saved.read_text())['raw_b64'])==b'new complete'
    assert not saved.with_name(saved.name+'.tmp').exists()


def test_failed_startup_directory_preparation_is_not_retried_per_frame(tmp_path,monkeypatch):
    w=writer(tmp_path);mkdir=Path.mkdir;calls=[]
    def denied(path,*args,**kwargs):
        if path==w.root:
            calls.append(path);raise PermissionError('diagnostics denied')
        return mkdir(path,*args,**kwargs)
    monkeypatch.setattr(Path,'mkdir',denied)
    assert capture(w) is None and capture(w) is None
    assert len(calls)==1 and w.stats['storage_error']==1 and w.stats['storage_blocked']==1


def test_worker_never_matches_discriminator_by_raw_frame_search(tmp_path,monkeypatch):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from test_desk_ps_routed_scope import FIXTURES
    from test_memecoin_desk_pumpswap_worker import worker,W
    from sniper.util import clock
    saved=json.loads((FIXTURES/'multi_hop_26_amm.json').read_text())
    update=pb.SubscribeUpdate.FromString(base64.b64decode(saved['raw_b64']))
    update.transaction.transaction.meta.return_data.data=bytes.fromhex(DISC)
    raw=update.SerializeToString()
    assert bytes.fromhex(DISC) in raw
    monkeypatch.setattr(clock,'offset_s',lambda:0.)
    monkeypatch.setattr(W.time,'time',lambda:saved['received_at']+.05)
    from sniper.services import memecoin_desk_pumpswap as PS
    original_details=PS.loss_details
    def unknown_details(*args,**kw):
        detail=original_details(*args,**kw)
        detail['discriminator']='ffffffffffffffff'
        return detail
    monkeypatch.setattr(PS,'loss_details',unknown_details)
    w=worker(tmp_path,monkeypatch)
    w.process(raw,received_at=saved['received_at'])
    assert w.stats['ps_decode_loss']==1 and w.ps_state_witness.stats['stored']==0
    assert not w.ps_state_witness.root.exists()


def test_optional_writer_initialization_failure_cannot_stop_tape(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker,W
    from sniper.util import clock
    raw,rx=state_frame();monkeypatch.setattr(clock,'offset_s',lambda:0.)
    monkeypatch.setattr(W.time,'time',lambda:rx+.05)
    def broken(*a,**k):raise RuntimeError('writer init failed')
    monkeypatch.setattr(D.Writer,'__init__',broken)
    w=worker(tmp_path,monkeypatch)
    w.process(raw,received_at=rx)
    assert w.ps_loss_pending['virtual_quote_unproven']
    assert w.stats['ps_state_witness_error']==1


def test_optional_status_failure_cannot_stop_heartbeat(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker,W
    from sniper.util import clock
    raw,rx=state_frame();monkeypatch.setattr(clock,'offset_s',lambda:0.)
    monkeypatch.setattr(W.time,'time',lambda:rx+.05)
    w=worker(tmp_path,monkeypatch);w.process(raw,received_at=rx)
    def broken():raise RuntimeError('status failed')
    monkeypatch.setattr(w.ps_state_witness,'status',broken)
    w.heartbeat(True)
    beat=json.loads((tmp_path/'universe.heartbeat').read_text())
    assert beat['connected'] is True and beat['ps_state_witness']['reason']=='status_error'


def test_short_write_is_not_published_as_complete_witness(tmp_path,monkeypatch):
    original=Path.open
    class Short:
        def __init__(self,stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):self.stream.close()
        def write(self,data):return self.stream.write(data[:8])
    def shortened(path,*args,**kwargs):
        stream=original(path,*args,**kwargs)
        return Short(stream) if path.name.endswith('.tmp') else stream
    monkeypatch.setattr(Path,'open',shortened)
    w=writer(tmp_path)
    assert capture(w) is None and w.stats['stored']==0


@pytest.mark.parametrize('disc,reason',[
    ('82a42461e48287a5','pumpswap_virtual_quote_unproven'),
    ('82a42461e48287a5','pumpswap_virtual_quote_unproven:scope_unknown_instruction_a039592ab58b2b42_a8_d8'),
    ('82a42461e48287a5','pumpswap_virtual_quote_unproven:scope_unknown_instruction_a039592ab58b2b42_a8_d8'),
    ('67f4521f2cf57777','pumpswap_event_malformed:unsupported_or_inconsistent_buy:unsupported_instruction:scope_unknown_instruction_5e06ca73ff60e8b7_a6_d8'),
    ('67f4521f2cf57777','pumpswap_event_malformed:unsupported_or_inconsistent_buy:inconsistent_gross_quote_buy_exact_quote_in:scope_layout_data_c62e1552b4d9e870_a26_d24'),
])
def test_first_live_loss_event_list_selects_all_five_rows(tmp_path,disc,reason):
    # Retained call1998/1999 identities: instruction suffixes are not event discriminators.
    w=writer(tmp_path)
    path=w.capture(b'public synthetic full frame',signature='public-signature',slot=42,tx_index=7,
                   received_at=NOW-.01,known_at=NOW,discriminator=disc,reason=reason,
                   idl_sha256='b'*64,decoder_root='/pinned/release')
    assert path is not None
    body=json.loads(path.read_text())
    assert body['discriminator']==disc and body['reason']==reason
    assert body['label_basis'] in ('first_blocking_discriminator','first_blocking_scope_instruction') and body['diagnostic_only']

@pytest.mark.parametrize('disc,suffix',[
    (DISC,'scope_unknown_instruction_a039592ab58b2b42_a8_d8'),
    ('67f4521f2cf57777','scope_unknown_instruction_5e06ca73ff60e8b7_a6_d8'),
    ('67f4521f2cf57777','scope_layout_data_c62e1552b4d9e870_a26_d24'),
])
def test_scope_subtype_not_starved_by_common_event_and_survives_restart(tmp_path,disc,suffix):
    def scoped(w):
        return w.capture(b'frame',signature='sig',slot=42,tx_index=7,received_at=NOW-.01,known_at=NOW,
                         discriminator=disc,reason='original:'+suffix,idl_sha256='b'*64,decoder_root='/release')
    w=writer(tmp_path)
    assert sum(capture(w,disc=disc) is not None for _ in range(20))==20
    path=scoped(w)
    assert path is not None
    body=json.loads(path.read_text())
    assert body['discriminator']==disc
    assert body['scope_instruction']==suffix.split('_')[-3]
    assert body['label_basis']=='first_blocking_scope_instruction'
    assert sum(scoped(w) is not None for _ in range(25))==19
    assert scoped(writer(tmp_path)) is None


def test_hard_daily_total_cap_spans_keys_and_restarts(tmp_path,monkeypatch):
    monkeypatch.setattr(D,'TOTAL_DAY',2,raising=False)
    w=writer(tmp_path)
    assert capture(w) and capture(w,disc='67f4521f2cf57777')
    assert capture(w,disc='1609851aa02c47c0') is None
    assert capture(writer(tmp_path),disc='1609851aa02c47c0') is None
    assert capture(writer(tmp_path),at=NOW+86400) is not None


@pytest.mark.parametrize('suffix',[
    'scope_unknown_instruction_a039_a8_d8',
    'scope_unknown_instruction_a039592ab58b2b42_a8_d8_extra',
    'prefix_scope_unknown_instruction_a039592ab58b2b42_a8_d8',
    'scope_unknown_instruction_a039592ab58b2b42_a9_d8',
])
def test_scope_budget_requires_exact_suffix(tmp_path,suffix):
    w=writer(tmp_path)
    path=w.capture(b'frame',signature='s',slot=42,tx_index=7,received_at=NOW-.01,known_at=NOW,
                   discriminator=DISC,reason='original:'+suffix,idl_sha256='b'*64,decoder_root='/release')
    body=json.loads(path.read_text())
    assert body['capture_key']==DISC and body['scope_instruction'] is None


def test_scope_file_retention_recognizes_only_fixed_pair_names(tmp_path):
    w=writer(tmp_path)
    key=DISC+'_a039592ab58b2b42'
    path=w.capture(b'frame',signature='s',slot=42,tx_index=7,received_at=NOW-.01,known_at=NOW,
                   discriminator=DISC,reason='original:scope_unknown_instruction_a039592ab58b2b42_a8_d8',
                   idl_sha256='b'*64,decoder_root='/release')
    foreign=path.parent/(key+'-+1.json');foreign.write_text('preserve')
    capture(w,at=NOW+3*86400)
    assert not path.exists() and foreign.read_text()=='preserve'

@pytest.mark.parametrize('tx_index,valid',[(0,True),(None,True),(True,False),(-1,False),(1.5,False),(2**63,False)])
def test_transaction_index_zero_is_known_and_invalid_indices_stay_rejected(tmp_path,tx_index,valid):
    path=writer(tmp_path).capture(b'frame',signature='s',slot=42,tx_index=tx_index,
        received_at=NOW-.01,known_at=NOW,discriminator=DISC,reason='loss',
        idl_sha256='b'*64,decoder_root='/release')
    assert (path is not None) is valid
    if valid:
        assert json.loads(path.read_text())['tx_index']==tx_index

@pytest.mark.parametrize('set_default_zero',[False,True])
def test_proto_default_zero_has_no_presence_and_witness_keeps_unknown(tmp_path,monkeypatch,set_default_zero):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from test_desk_ps_routed_scope import FIXTURES
    from test_memecoin_desk_pumpswap_worker import worker,W
    saved=json.loads((FIXTURES/'multi_hop_26_amm.json').read_text())
    update=pb.SubscribeUpdate.FromString(base64.b64decode(saved['raw_b64']))
    info=update.transaction.transaction
    info.ClearField('index')
    if set_default_zero:
        info.index=0
    assert info.DESCRIPTOR.fields_by_name['index'].has_presence is False
    with pytest.raises(ValueError):
        info.HasField('index')
    monkeypatch.setattr(W.time,'time',lambda:saved['received_at']+.05)
    w=worker(tmp_path,monkeypatch)
    w.process(update.SerializeToString(),received_at=saved['received_at'])
    paths=list(w.ps_state_witness.root.rglob('*.json'))
    assert len(paths)==1 and json.loads(paths[0].read_text())['tx_index'] is None
