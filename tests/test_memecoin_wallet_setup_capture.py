from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey
from sniper.services import memecoin_desk_wallet_setup as S


def fixture():
    payer, user = bytes([1]) * 32, bytes([2]) * 32
    program = Pubkey.from_string(S.PUMP)
    acc = bytes(Pubkey.find_program_address([b'user_volume_accumulator', user], program)[0])
    event = bytes(Pubkey.find_program_address([b'__event_authority'], program)[0])
    keys = [payer, user, acc, bytes(32), event, bytes(program)]
    ix = SimpleNamespace(program_id_index=5, accounts=bytes(range(6)), data=S.INIT)
    return keys, ix


def test_known_sponsor_exposes_new_wallet_without_claiming_funding():
    keys, ix = fixture()
    rows = S.decode_setups(keys, 1, 0, [ix], {str(Pubkey(keys[0]))})
    assert rows == [dict(outer_ix=0, program=S.PUMP, payer=str(Pubkey(keys[0])), wallet=str(Pubkey(keys[1])), kind='account_setup_sponsor')]
    assert S.decode_setups(keys, 1, 0, [ix], set()) == []


@pytest.mark.parametrize('broken', ['unsigned', 'readonly', 'wrong_pda', 'wrong_event', 'wrong_program_account', 'wrong_system', 'extra_data', 'wrong_count'])
def test_invalid_setup_proof_is_not_emitted(broken):
    keys, ix = fixture(); signers, readonly = 1, 0
    if broken == 'unsigned': signers = 0
    elif broken == 'readonly': readonly = 1
    elif broken == 'wrong_pda': keys[2] = bytes([8]) * 32
    elif broken == 'wrong_event': keys[4] = bytes([8]) * 32
    elif broken == 'wrong_program_account': ix.accounts = bytes([0,1,2,3,4,0])
    elif broken == 'wrong_system': keys[3] = bytes([8]) * 32
    elif broken == 'extra_data': ix.data += b'X'
    elif broken == 'wrong_count': ix.accounts = bytes([0,1,2])
    assert S.decode_setups(keys, signers, readonly, [ix], {str(Pubkey(keys[0]))}) == []


def test_instruction_bound_is_explicit():
    keys, ix = fixture()
    with pytest.raises(ValueError, match='instruction_cap'):
        S.decode_setups(keys, 1, 0, [ix] * 129, {str(Pubkey(keys[0]))})


def test_real_capture_records_only_successful_setups_and_survives_decode_error(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    spec = importlib.util.spec_from_file_location('setup_capture_worker', Path(__file__).resolve().parents[1] / 'scripts/memecoin_desk_universe_worker.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE', '1')
    monkeypatch.setenv('MEMECOIN_DESK_INTAKE_EMIT', '0')
    w = module.Worker(module.knobs({}), base=tmp_path, sol_price_fn=lambda: 100., alert_fn=lambda *args: None)
    keys, instruction = fixture()
    w.ps_watch = {str(Pubkey(keys[0]))}
    u = pb.SubscribeUpdate(); u.transaction.slot = 123
    t = u.transaction.transaction; t.signature = bytes([7]) * 64; t.index = 1
    msg = t.transaction.message; msg.header.num_required_signatures = 1
    msg.account_keys.extend(keys)
    ix = msg.instructions.add(); ix.program_id_index = 5; ix.accounts = instruction.accounts; ix.data = S.INIT
    w.process(u.SerializeToString(), received_at=100.)
    w.files.commit()
    c = w.files.conn('wallet_setup_events', 100.)
    row = c.execute('SELECT payer,wallet,received_at,block_time FROM wallet_setup_events').fetchone()
    assert row == (str(Pubkey(keys[0])), str(Pubkey(keys[1])), 100., None)
    assert str(Pubkey(keys[1])) in w.ps_watch
    import json
    path = Path(w.ps_ext_path).with_name('watch_wallets.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(wallets=[str(Pubkey(keys[0]))])))
    w.watch_tick(101.)
    assert str(Pubkey(keys[1])) in w.ps_watch
    path.write_text('broken')
    w.watch_tick(__import__('time').time() + 121.)
    assert str(Pubkey(keys[0])) in w.ps_watch
    assert str(Pubkey(keys[1])) not in w.ps_watch
    w.process(u.SerializeToString(), received_at=101.)
    assert c.execute('SELECT count(*) FROM wallet_setup_events').fetchone()[0] == 1
    t.signature = bytes([8]) * 64; t.meta.err.err = b'failed'
    w.process(u.SerializeToString(), received_at=102.)
    assert c.execute('SELECT count(*) FROM wallet_setup_events').fetchone()[0] == 1
    t.meta.ClearField('err'); msg.header.num_required_signatures = 99
    w.process(u.SerializeToString(), received_at=103.)
    assert w.stats['wallet_setup_errors'] == 1
    for connection in w.files._open.values():
        connection.close()
