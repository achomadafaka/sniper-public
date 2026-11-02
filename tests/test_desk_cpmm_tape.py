import json
from pathlib import Path

from sniper.services import memecoin_desk_cpmm as C
from sniper.services.memecoin_desk_cpmm_capture import Store
from sniper.services.memecoin_desk_cpmm_tape import read_window

FIX=json.loads((Path(__file__).parent/'fixtures/desk_cpmm_transaction.json').read_text())
B=C.pool_binding(FIX['frame']['account_keys'][3],FIX['binding_accounts'],999,FIX['binding_context_slot'])
M=B['mint'];P=B['pool']


def seed(tmp_path,*,row=True):
    s=Store(tmp_path);s.begin(990);s.bind(B)
    if row:s.record(C.decode_transaction(FIX['frame'],{P:B})['rows'][0])
    s.fence(200,1002,complete=True);return s


def test_absent_is_unknown_but_proven_quiet_window_is_empty(tmp_path):
    assert read_window(tmp_path,M,999,1001,now=1003)['reason']=='tape_missing'
    with seed(tmp_path,row=False):
        result=read_window(tmp_path,M,999,1001,now=1003)
        assert result['state']=='ok' and result['rows']==[] and result['pool']==P


def test_pre_state_is_explicit_and_no_future_rows_or_bindings(tmp_path):
    with seed(tmp_path):
        out=read_window(tmp_path,M,999,1001,now=1003)
        assert out['state']=='ok' and out['rows'][0]['post_price_sol'] is None
        assert out['rows'][0]['reserve_basis']=='event_effective_pre'
        assert read_window(tmp_path,M,990,998,now=1003)['reason']=='pool_unknown'


def test_stale_cold_future_and_uncommitted_windows_are_unknown(tmp_path):
    with seed(tmp_path):
        for lo,hi,now,reason in [(980,1001,1003,'capture_cold'),(999,1001,1010,'capture_stale'),
                                (999,1003,1003,'capture_incomplete'),(999,1004,1003,'clock_invalid')]:
            assert read_window(tmp_path,M,lo,hi,now=now)['reason']==reason


def test_gaps_ambiguity_and_explicit_pool_mismatch_never_become_zero(tmp_path):
    with seed(tmp_path) as s:
        s.gap('rpc_timeout',1000,1001,P);s.conn.commit()
        assert read_window(tmp_path,M,999,1001,now=1003)['reason']=='capture_gap'
        s.bind(dict(B,pool='other',known_at=998))
        assert read_window(tmp_path,M,999,1001,now=1003)['reason']=='pool_ambiguous'
        assert read_window(tmp_path,M,999,1001,pool='missing',now=1003)['reason']=='pool_unknown'


def test_unreadable_and_row_cap_are_typed(tmp_path):
    path=tmp_path/'cpmm/tape.db';path.parent.mkdir();path.write_text('not sqlite')
    assert read_window(tmp_path,M,999,1001,now=1003)['reason']=='tape_unreadable'


def test_row_cap_and_partial_owner_are_unknown_not_clean(tmp_path,monkeypatch):
    import sniper.services.memecoin_desk_cpmm_tape as T
    with Store(tmp_path) as s:
        s.begin(990);s.bind(B)
        row=C.decode_transaction(FIX['frame'],{P:B})['rows'][0]
        s.record(row)
        row=dict(row,event_id='second',actor=None,actor_status='unknown')
        s.record(row);s.fence(200,1002,complete=True)
        assert read_window(tmp_path,M,999,1001,now=1003)['reason']=='partial_facts'
        monkeypatch.setattr(T,'MAX_ROWS',1)
        assert read_window(tmp_path,M,999,1001,now=1003)['reason']=='row_cap'


def test_same_receipt_and_slot_uses_transaction_index_not_signature_sort(tmp_path):
    with Store(tmp_path) as s:
        s.begin(990);s.bind(B)
        original=C.decode_transaction(FIX['frame'],{P:B})['rows'][0]
        s.record(dict(original,event_id='a',signature='4'*88,tx_index=10))
        s.record(dict(original,event_id='z',signature='5'*88,tx_index=2))
        s.fence(200,1002,complete=True)
        out=read_window(tmp_path,M,999,1001,now=1003)
        assert [r['tx_index'] for r in out['rows']]==[2,10]


def test_metadata_only_fence_never_proves_a_quiet_complete_window(tmp_path):
    with Store(tmp_path) as s:
        s.begin(990);s.bind(B);s.fence(200,1002)
        result=read_window(tmp_path,M,999,1001,now=1003)
        assert result['state']=='unknown' and result['reason']=='capture_fence_unproven'
