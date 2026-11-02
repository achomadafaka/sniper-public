"""Expected-correct regressions; only in-memory ledgers and fake exits."""
import base64
import copy
import struct
import pytest
from test_memecoin_source_sell_mirror import (world, _open, _run, _watched, _sells,
    _curve_sale, _audit, FX, ssm, sig, m, OPENED)


def test_interleaved_pool_legs_keep_original_owner_denominator(world):
    _open(world, '65906')
    e = FX['65906']
    owner_raw = sig._owner_bytes(e['owner'])
    a, b = bytes([1])*32, bytes([2])*32
    logs = [f'Program {sig.PUMP_AMM_PROGRAM} invoke [1]']
    # One owner ATA: A10 (100->90), B10 (90->80), A10 (80->70).
    for pool, before in ((a,100),(b,90),(a,80)):
        raw=bytearray(184)
        raw[:8]=sig._AMM_SELL
        struct.pack_into('<QQQ',raw,16,10,0,before)
        raw[120:152]=pool
        raw[152:184]=owner_raw
        logs.append('Program data: '+base64.b64encode(raw).decode())
    logs.append(f'Program {sig.PUMP_AMM_PROGRAM} success')
    frame=copy.deepcopy(e['frame'])
    frame['params']['result']['value']['logs']=logs
    legs=sig.decode_owner_sells(frame,e['owner'],owner_raw,e['source_user_id'],e['handle'],
                               recv_monotonic=100.,recv_utc='x')
    assert [(s.sold_atomic,s.pre_atomic) for s in legs]==[(20,100),(10,90)]
    world.mirror.pools={s.pool:{'base_mint':e['mint']} for s in legs}
    world.mirror.offer_many(legs)
    [rec]=world.mirror.run(world.conn,world.contexts,exit_lock=world.lock)
    assert rec['cumulative']==pytest.approx(.30),rec
    assert rec['outcome']=='mirrored',rec


def test_later_distinct_account_sale_same_slot_is_not_the_first_signed_sale(world):
    _open(world,'65826')
    [base]=_sells('65826')
    acct=_watched(world,'65826',100_000,base.slot-3)
    first=base.__class__(**{**base.__dict__,'signature':'first-sell', 'sold_atomic':10_000})
    [rec]=_run(world,first)
    assert rec['cumulative']==.1
    world.mirror.offer_account(acct,base.slot,90_000,100.)
    first_account=world.mirror.run(world.conn,world.contexts,exit_lock=world.lock)
    assert first_account[-1]['outcome']=='duplicate'
    # Another routed transaction in this slot sells 30k. Its logs do not decode a Pump sale.
    world.mirror.offer_account(acct,base.slot,60_000,100.1)
    [last]=world.mirror.run(world.conn,world.contexts,exit_lock=world.lock)
    assert last['outcome']=='mirrored',last
    assert last['cumulative']==pytest.approx(.4)


def test_queued_retry_honors_tracking_pause(world):
    pid=_open(world,'65826')
    world.mirror._retries=[dict(due=99,attempt=1,position_id=pid,sig='s',path=ssm.PATH_SLOT,
                                handle='h',uid='u')]
    world.conn.execute('UPDATE memecoin_positions SET tracking_paused_at=? WHERE id=?',(OPENED,pid))
    world.conn.commit()
    result=world.mirror.run(world.conn,world.contexts,exit_lock=world.lock)
    assert not world.calls,(result,world.calls)


@pytest.mark.parametrize("first_path", ["signed", "account"])
def test_partial_cross_path_overlap_counts_only_new_balance_drop(world, first_path):
    _open(world, "65826")
    [base] = _sells("65826")
    acct = _watched(world, "65826", 100_000, base.slot - 3)
    first = base.__class__(**{**base.__dict__, "signature": "first", "sold_atomic": 20_000})
    if first_path == "signed":
        _run(world, first)
        world.mirror.offer_account(acct, base.slot, 60_000, 100.1)
        [last] = world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    else:
        world.mirror.offer_account(acct, base.slot, 80_000, 100.)
        world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
        whole = base.__class__(**{**base.__dict__, "signature": "whole", "sold_atomic": 40_000,
                                 "pre_atomic": 100_000})
        [last] = _run(world, whole)
    assert last["cumulative"] == pytest.approx(.4), last
    assert last["outcome"] == "mirrored"


def test_operator_pause_while_exit_waits_for_lock_is_respected(world):
    pid = _open(world, "65826")
    class PauseAtLock:
        def __enter__(self):
            world.conn.execute("UPDATE memecoin_positions SET tracking_paused_at=? WHERE id=?", (OPENED, pid))
            world.conn.commit()
        def __exit__(self, *args):
            pass
    world.mirror.offer(_curve_sale(100, "dump"))
    [rec] = world.mirror.run(world.conn, world.contexts, exit_lock=PauseAtLock())
    assert world.calls == []
    assert rec["outcome"] == "tracking_paused"
    allowed, detail, _ = _audit(world, pid)
    assert allowed == 0 and detail["code"] == "tracking_paused"


def test_reordered_signed_sales_reconcile_with_account_fraction(world):
    _open(world, '65826')
    [base] = _sells('65826')
    acct = _watched(world, '65826', 100_000, base.slot - 3)
    world.mirror.min_fraction = .34
    world.mirror.offer_account(acct, base.slot, 80_000, 100.)
    world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    b = base.__class__(**{**base.__dict__, 'signature':'B','sold_atomic':15_000,'pre_atomic':80_000})
    a = base.__class__(**{**base.__dict__, 'signature':'A','sold_atomic':20_000,'pre_atomic':100_000})
    _run(world, b)
    [last] = _run(world, a)
    assert last['cumulative'] == pytest.approx(.35), last
    assert last['outcome'] == 'mirrored'


def test_same_slot_buy_then_repeat_post_balance_is_a_distinct_sale(world):
    _open(world, '65826')
    [base] = _sells('65826')
    acct = _watched(world, '65826', 100_000, base.slot - 3)
    results = []
    for amount in (80_000, 100_000, 80_000):
        world.mirror.offer_account(acct, base.slot, amount, 100.)
        results += world.mirror.run(world.conn, world.contexts, exit_lock=world.lock)
    assert results[-1]['cumulative'] == pytest.approx(.36), results
    assert results[-1]['outcome'] == 'mirrored'
