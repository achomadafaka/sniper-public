"""Event-only absence receipts; temporary tapes, no model or provider calls."""
import base64
import json
import sqlite3
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.services import memecoin_desk_ps_losses as L
from sniper.services import memecoin_desk_pumpswap as PS
from test_desk_ps_loss_scope import db, receipt, batch
from test_memecoin_desk_pumpswap import POOL, ACTOR
from test_memecoin_desk_pumpswap_worker import W, worker, frame


def envelope(c, keys=(ACTOR,), *, sig='one', at=100., known=100.5, slot=10):
    proof = (sig, 'decode_loss', 'amm_event_account_absence', 'pinned-amm-event-bloom4096k4-v1',
             L.event_account_filter(keys), len(keys), PS.PUMP_AMM_IDL_SHA256, at, known, slot, 1)
    c.execute('INSERT OR IGNORE INTO ps_loss_event_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
              (*proof, L.event_envelope_sha256(proof)))


def setup(c, *, keys=(ACTOR,), known=100.5):
    receipt(c, scope='unknown'); envelope(c, keys, known=known); batch(c)


def test_fixed_filter_abi_and_no_inserted_account_can_be_excluded():
    bits = L.event_account_filter(['11111111111111111111111111111111'])
    assert isinstance(bits, bytes) and len(bits) == 512
    assert {i for i in range(4096) if bits[i // 8] & (1 << (i % 8))} == {3502, 948, 1236, 3520}
    keys = [str(Pubkey.from_bytes(bytes([i]) * 32)) for i in range(256)]
    bits = L.event_account_filter(keys)
    assert all(not L.event_account_absent(bits, key) for key in keys)
    assert not L.event_account_absent(bytes([255]) * 512, POOL)  # collisions only retain the loss


def test_checksum_abi_normalizes_sqlite_real_roundtrip():
    bits = L.event_account_filter(['11111111111111111111111111111111'])
    record = ('sig', 'decode_loss', 'amm_event_account_absence', 'pinned-amm-event-bloom4096k4-v1',
              bits, 1, PS.PUMP_AMM_IDL_SHA256, 100, 101, 10, 1)
    assert L.event_envelope_sha256(record) == '7fd1501ac65a2f43426275b33d70828adc06c6c85ef90f27f223364747cef007'
    assert L.event_envelope_sha256(record) == L.event_envelope_sha256((*record[:7],100.,101.,*record[9:]))


@pytest.mark.parametrize('keys', [[], [ACTOR] * 257, [None], [b'bad'], ['invalid'], '11111111111111111111111111111111'])
def test_filter_refuses_missing_malformed_or_overflowing_complete_keys(keys):
    with pytest.raises(ValueError): L.event_account_filter(keys)


def test_unknown_receipt_is_excluded_only_for_definitely_absent_pool(tmp_path):
    files, c = db(tmp_path); setup(c)
    assert L.losses(tmp_path, POOL, 99., 102.) == []
    assert L.losses(tmp_path, ACTOR, 99., 102.) == [100.]
    assert L.losses(tmp_path, POOL, 99., 102., scoped=False) == [100.]
    assert c.execute('SELECT scope,pools_json FROM ps_loss_attribution').fetchone() == ('unknown', '[]')
    c.close()


@pytest.mark.parametrize('mutation', [
    "UPDATE ps_loss_event_envelopes SET kind='writable_accounts'",
    "UPDATE ps_loss_event_envelopes SET profile='future'",
    "UPDATE ps_loss_event_envelopes SET bits=X'00'",
    "UPDATE ps_loss_event_envelopes SET bits=zeroblob(512)",
    "UPDATE ps_loss_event_envelopes SET key_count=0",
    "UPDATE ps_loss_event_envelopes SET key_count=257",
    "UPDATE ps_loss_event_envelopes SET idl_sha256='wrong'",
    "UPDATE ps_loss_event_envelopes SET received_at=99",
    "UPDATE ps_loss_event_envelopes SET known_at=99",
    "UPDATE ps_loss_event_envelopes SET known_at=NULL",
    "UPDATE ps_loss_event_envelopes SET slot=11",
    "UPDATE ps_loss_event_envelopes SET protocol=2",
    'DELETE FROM ps_loss_attribution',
])
def test_invalid_or_orphan_envelope_never_grants_an_exemption(tmp_path, mutation):
    files, c = db(tmp_path); setup(c); c.execute(mutation); c.commit()
    with pytest.raises(L.LossUnreadable, match='envelope|attribution'):
        L.losses(tmp_path, POOL, 99., 102.)
    c.close()


def test_late_proof_cannot_change_a_frozen_prior_cut(tmp_path):
    files, c = db(tmp_path); setup(c, known=102.)
    assert L.losses(tmp_path, POOL, 99., 101.) == [100.]
    assert L.losses(tmp_path, POOL, 99., 102.) == []
    c.close()


@pytest.mark.parametrize('damage', ['clear_inserted_bit', 'change_valid_known_at'])
def test_structurally_valid_corruption_cannot_create_absence_or_change_provenance(tmp_path, damage):
    files, c = db(tmp_path); setup(c)
    target = POOL
    if damage == 'clear_inserted_bit':
        bits = bytearray(c.execute('SELECT bits FROM ps_loss_event_envelopes').fetchone()[0])
        byte = next(i for i,b in enumerate(bits) if b)
        bits[byte] &= bits[byte] - 1
        c.execute('UPDATE ps_loss_event_envelopes SET bits=?', (bytes(bits),)); target = ACTOR
    else:
        c.execute('UPDATE ps_loss_event_envelopes SET known_at=100.6')
    c.commit()
    with pytest.raises(L.LossUnreadable, match='envelope'):
        L.losses(tmp_path, target, 99., 102.)
    c.close()


def test_absent_proof_table_and_old_receipts_stay_global(tmp_path):
    files, c = db(tmp_path); receipt(c, scope='unknown'); batch(c)
    assert L.losses(tmp_path, POOL, 99., 102.) == [100.]
    c.execute('DROP TABLE ps_loss_event_envelopes'); c.commit()
    assert L.losses(tmp_path, POOL, 99., 102.) == [100.]
    c.close()


def test_legacy_reader_ignores_new_proof_schema_but_new_reader_requires_its_primary_key(tmp_path):
    files, c = db(tmp_path); setup(c)
    c.execute('ALTER TABLE ps_loss_event_envelopes RENAME TO saved_envelopes')
    c.execute('CREATE TABLE ps_loss_event_envelopes AS SELECT * FROM saved_envelopes'); c.commit()
    assert L.losses(tmp_path, POOL, 99., 102., scoped=False) == [100.]
    with pytest.raises(L.LossUnreadable, match='index'):
        L.losses(tmp_path, POOL, 99., 102.)
    c.close()


def test_generic_gap_pending_and_partial_batch_are_never_exempted(tmp_path):
    files, c = db(tmp_path); setup(c)
    c.execute("INSERT INTO gaps_ps VALUES(100.2,'reconnect',10)"); c.commit()
    assert L.losses(tmp_path, POOL, 99., 102., pending={'decode_loss':100.3}) == [100.2, 100.3]
    c.execute('DELETE FROM ps_loss_batch_members'); c.commit()
    assert L.losses(tmp_path, POOL, 99., 102.) == [100., 100.2]
    c.close()


def test_malformed_later_member_invalidates_a_coalesced_prior_batch(tmp_path):
    files, c = db(tmp_path); receipt(c, scope='unknown'); envelope(c)
    receipt(c, sig='two', at=100.8, scope='unknown'); envelope(c, sig='two', at=100.8, known=101.)
    batch(c, ('one','two'))
    c.execute("UPDATE ps_loss_event_envelopes SET profile='future' WHERE signature='two'"); c.commit()
    with pytest.raises(L.LossUnreadable, match='envelope'):
        L.losses(tmp_path, POOL, 99., 100.5)
    c.close()


def test_worker_writes_original_unknown_and_first_proof_atomically(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(W.time, 'time', lambda: 101.)
    monkeypatch.setattr(PS, 'events', lambda _: (_ for _ in ()).throw(PS.DecodeLoss('pumpswap_event_malformed')))
    detail = {'reason':'unsupported_buy','scope':'unknown','pools':[], 'invocations':1,
              'discriminator':'67f4521f2cf57777','event_length':508,'event_account_keys':[ACTOR]}
    monkeypatch.setattr(PS, 'loss_details', lambda *a, **k: detail)
    raw = frame(); w.process(raw, received_at=100.)
    c = w.files.conn('ps_decode_losses', 102.)
    first = c.execute('SELECT * FROM ps_loss_event_envelopes').fetchone()
    assert first is not None and first[7:10] == (100., 101., 10)
    assert c.execute('SELECT scope,pools_json FROM ps_loss_attribution').fetchone() == ('unknown','[]')
    assert c.in_transaction and w.ps_loss_pending['decode_loss']
    detail['event_account_keys'] = [POOL]
    monkeypatch.setattr(W.time, 'time', lambda: 105.)
    w.process(raw, received_at=104.)
    assert c.execute('SELECT * FROM ps_loss_event_envelopes').fetchone() == first


@pytest.mark.parametrize('clock,keys', [(99.,[ACTOR]), (float('nan'),[ACTOR]), (101.,[None])])
def test_optional_bad_clock_or_keys_keep_unknown_without_process_or_storage_error(tmp_path, monkeypatch, clock, keys):
    w = worker(tmp_path, monkeypatch)
    clocks = iter((101., clock))
    monkeypatch.setattr(W.time, 'time', lambda: next(clocks, 101.))
    monkeypatch.setattr(PS, 'events', lambda _: (_ for _ in ()).throw(PS.DecodeLoss('pumpswap_event_malformed')))
    monkeypatch.setattr(PS, 'loss_details', lambda *a, **k: {
        'reason':'unsupported_buy','scope':'unknown','pools':[], 'invocations':1,
        'discriminator':'67f4521f2cf57777','event_length':508,'event_account_keys':keys})
    w.process(frame(), received_at=100.)
    c = w.files.conn('ps_decode_losses', 102.)
    assert c.execute('SELECT COUNT(*) FROM ps_loss_event_envelopes').fetchone()[0] == 0
    assert c.execute('SELECT scope FROM ps_loss_attribution').fetchone()[0] == 'unknown'
    assert w.ps_loss_pending['decode_loss'] and w.stats['ps_event_envelope_refused'] == 1
    assert w.stats['ps_process_errors'] == 0 and w.stats['ps_write_errors'] == 0


def test_worker_proof_failure_rolls_back_receipt_and_keeps_global_gap(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch); c = w.files.conn('ps_decode_losses', 99.)
    c.execute("CREATE TRIGGER refuse_envelope BEFORE INSERT ON ps_loss_event_envelopes BEGIN SELECT RAISE(ABORT,'full'); END")
    c.commit()
    monkeypatch.setattr(W.time, 'time', lambda: 101.)
    monkeypatch.setattr(PS, 'events', lambda _: (_ for _ in ()).throw(PS.DecodeLoss('pumpswap_event_malformed')))
    monkeypatch.setattr(PS, 'loss_details', lambda *a, **k: {
        'reason':'unsupported_buy','scope':'unknown','pools':[], 'invocations':1,
        'discriminator':'67f4521f2cf57777','event_length':508,'event_account_keys':[ACTOR]})
    w.process(frame(), received_at=100.)
    assert c.execute('SELECT COUNT(*) FROM ps_decode_losses').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM ps_loss_attribution').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM ps_loss_event_envelopes').fetchone()[0] == 0
    assert w.stats['ps_write_errors'] == 1 and 'write_error' in w.ps_pending_gaps


def test_shared_asof_reader_honors_only_absent_pool(tmp_path):
    from test_desk_ps_loss_integration import provision
    from test_desk_ps_asof import build, candidate, NOW, POOL as TARGET
    from sniper.services import memecoin_desk_ps_asof as A
    desk = provision(build(tmp_path))
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        receipt(c, at=NOW-5, scope='unknown'); envelope(c, at=NOW-5, known=NOW-4); batch(c, at=NOW-5)
    out, _ = A.refresh(candidate(), desk_dir=desk, now=NOW, integrity=True)
    assert out['pool'] == TARGET
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        c.execute('DELETE FROM ps_loss_event_envelopes')
        envelope(c, [TARGET], at=NOW-5, known=NOW-4)
    from sniper.services.memecoin_desk_asof import StaleSnapshot
    with pytest.raises(StaleSnapshot, match='tape_gap_since_cut'):
        A.refresh(candidate(), desk_dir=desk, now=NOW, integrity=True)


@pytest.mark.parametrize('adapter', ['evidence', 'buy', 'sell'])
@pytest.mark.parametrize('present,late', [(False,False), (True,False), (False,True)])
def test_frozen_hard_filter_and_paper_quotes_keep_present_or_future_proof(tmp_path, adapter, present, late):
    from test_desk_ps_frozen_loss_boundary import fixture, provision, invoke, CUT, POOL as TARGET
    desk, cand = fixture(tmp_path); provision(desk)
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        receipt(c, at=CUT-.2, scope='unknown')
        envelope(c, [TARGET] if present else [ACTOR], at=CUT-.2, known=CUT+.1 if late else CUT-.1)
        batch(c, at=CUT-.2)
    assert invoke(adapter, desk, cand) == (not present and not late)


@pytest.mark.parametrize('kind', ['26_amm', '26_pump'])
def test_natural_nested_router_receipt_preserves_all_actual_keys_and_excludes_only_outsider(tmp_path, monkeypatch, kind):
    monkeypatch.setenv('MEMECOIN_DESK_PS_ROUTED_SCOPE','0')  # Isolate the original Bloom-only contract.
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    fixture = Path(__file__).parent/'fixtures/memecoin/desk_multihop_20261027'/f'{kind}.json'
    saved = json.loads(fixture.read_text()); raw = base64.b64decode(saved['raw_b64'], validate=True)
    info = pb.SubscribeUpdate.FromString(raw).transaction.transaction
    keys = [str(Pubkey.from_bytes(k)) for k in (*info.transaction.message.account_keys,
                                              *info.meta.loaded_writable_addresses,
                                              *info.meta.loaded_readonly_addresses)]
    at = saved['received_at']; now = at + 1.
    w = worker(tmp_path, monkeypatch); monkeypatch.setattr(W.time, 'time', lambda: now)
    w.process(raw, received_at=at); w.flush_ps_gaps(now)
    c = w.files.conn('ps_decode_losses', now)
    bits, count, known = c.execute('SELECT bits,key_count,known_at FROM ps_loss_event_envelopes').fetchone()
    assert count == len(set(keys)) and known == now
    assert c.execute('SELECT scope,pools_json FROM ps_loss_attribution').fetchone() == ('unknown','[]')
    assert all(L.losses(tmp_path, key, at-1., now+1.) == [at] for key in keys)
    assert ACTOR not in keys and L.event_account_absent(bits, ACTOR)
    assert L.losses(tmp_path, ACTOR, at-1., now+1.) == []
