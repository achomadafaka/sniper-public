"""Bounded diagnostic copies never change PumpSwap loss/admission authority."""
import base64
import json

import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_pumpswap as P
from sniper.services import memecoin_desk_universe as U
from test_memecoin_desk_pumpswap import buy
from test_memecoin_desk_pumpswap_worker import W, worker


DISC = bytes.fromhex('2b644913e9f66f94')


def frame(*, accounts=26, pump=False, signature=1, data=None, program=0, padding=''):
    u = pb.SubscribeUpdate()
    u.filters.append('pumpswap')
    u.transaction.slot = 10
    info = u.transaction.transaction
    info.signature = bytes([signature]) * 64
    info.index = 3
    msg = info.transaction.message
    msg.account_keys.extend([U.b58decode(P.AMM_PROGRAM), U.b58decode(U.PUMP_PROGRAM), bytes([9]) * 32])
    ix = msg.instructions.add(program_id_index=program)
    ix.accounts = bytes([2]) * accounts
    ix.data = DISC + bytes(16) if data is None else data
    logs = [f'Program {P.AMM_PROGRAM} invoke [1]', buy(ix_name='multi_hop_swap')]
    if pump:
        child = info.meta.inner_instructions.add(index=0).instructions.add(program_id_index=1)
        child.accounts = b'\x02'
        child.data = bytes(8)
        logs += [f'Program {U.PUMP_PROGRAM} invoke [2]', f'Program {U.PUMP_PROGRAM} success']
    if padding:
        logs.append('Program log: ' + padding)
    info.meta.log_messages.extend([*logs, f'Program {P.AMM_PROGRAM} success'])
    return u.SerializeToString()


def witnesses(path):
    directory = path / 'pumpswap' / 'decode_witnesses'
    return sorted(directory.iterdir()) if directory.is_dir() else []


def losses(w):
    return w.files.conn('ps_decode_losses', 100.).execute(
        'SELECT scope,pools_json FROM ps_loss_attribution ORDER BY signature').fetchall()


def test_worker_retains_original_frame_and_clock_once_without_weakening_loss(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    raw = frame()  # Pump is a key, but no Pump instruction executes.
    w.process(raw, received_at=100.25)
    paths = witnesses(tmp_path)
    assert len(paths) == 1 and paths[0].name == 'multi_hop_26_amm.json'
    saved = paths[0].read_bytes()
    out = json.loads(saved)
    assert len(saved) <= 256 * 1024
    assert base64.b64decode(out['raw_b64'], validate=True) == raw
    assert out['received_at'] == 100.25
    assert out['signature'] == U.b58encode(bytes([1]) * 64)
    assert out['decoder_root'] == str(W.ROOT)
    assert out['idl_sha256'] == P.PUMP_AMM_IDL_SHA256
    assert 'unsupported_instruction' in out['reason']
    assert losses(w) == [('unknown', '[]')]
    assert w.stats['ps_decode_witness_stored'] == 1

    w.process(frame(signature=2), received_at=101.)
    assert paths[0].read_bytes() == saved and len(losses(w)) == 2
    w.files.commit()
    w2 = worker(tmp_path, monkeypatch)
    w2.process(frame(signature=3), received_at=102.)
    assert paths[0].read_bytes() == saved
    assert w2.stats['ps_decode_witness_existing'] == 1


def test_only_eight_fixed_instruction_shapes_can_create_diagnostic_files(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    for accounts in (26, 31, 36, 41):
        for pump in (False, True):
            w.process(frame(accounts=accounts, pump=pump, signature=accounts + pump), received_at=100.)
    paths = witnesses(tmp_path)
    assert {p.name for p in paths} == {
        f'multi_hop_{accounts}_{kind}.json' for accounts in (26, 31, 36, 41) for kind in ('amm', 'pump')}
    for args in ({'accounts': 21}, {'accounts': 46}, {'accounts': 32}, {'data': bytes(24)},
                 {'data': DISC + bytes(17)}, {'program': 1}):
        w.process(frame(signature=99, **args), received_at=101.)
    assert witnesses(tmp_path) == paths
    assert w.stats['ps_decode_witness_stored'] == 8
    assert all(p.stat().st_size <= 256 * 1024 for p in paths)


def test_diagnostic_io_failure_does_not_retry_or_disrupt_loss_capture(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    target = tmp_path / 'pumpswap' / 'decode_witnesses'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('blocked directory')
    for signature in (1, 2):
        w.process(frame(signature=signature), received_at=100. + signature)
    assert w.stats['ps_decode_witness_error'] == 1
    assert losses(w) == [('unknown', '[]'), ('unknown', '[]')]
    assert len(w.ps_loss_pending['decode_loss']) == 2 and w.ps_pending_gaps
    assert w.stats['ps_process_errors'] == 0 and w.stats['ps_write_errors'] == 0


@pytest.mark.parametrize('padding_bytes', [200 * 1024, 256 * 1024])
def test_oversized_diagnostic_does_not_retry_or_clear_unknown_scope(tmp_path, monkeypatch, padding_bytes):
    w = worker(tmp_path, monkeypatch)
    w.process(frame(padding='x' * padding_bytes), received_at=100.)
    w.process(frame(signature=2), received_at=101.)
    assert not witnesses(tmp_path)
    assert w.stats['ps_decode_witness_oversize'] == 1
    assert losses(w) == [('unknown', '[]'), ('unknown', '[]')]
