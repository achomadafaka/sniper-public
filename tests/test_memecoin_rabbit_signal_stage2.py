"""RabbitStream stage 2: a tracked owner's pump-curve buy, seen on the existing RabbitStream prepare
stream, fires a funded copy BEFORE it executes, on a pre-execution owner-signed proof.

MEMECOIN_SLOT_LANE_RABBIT_SIGNAL (stage 1, the decoder) AND MEMECOIN_SLOT_LANE_RABBIT_BUY (this
stage, the send) must both be on; either off is the lane exactly as before."""
import asyncio
import base64
import contextlib
import dataclasses
import json
import sqlite3
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import base58
import pytest

from sniper.execution import pump_curve_buy as curve
from sniper.execution import pump_fees as PF
from sniper.execution.pump_native_exit import PUMP_PROGRAM
from sniper.execution.solana_native_entry_quote import accounts_for
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_rabbit_signal as R
from sniper.services import memecoin_shyft_prepare as prep
from sniper.services import memecoin_slot_lane as lane
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from sniper.settings import memecoin as cfg
from test_memecoin_rabbit_signal_stage1 import CURVE, DAMM, frame, owner

DIRECT = json.loads((Path(__file__).parent / 'fixtures/pump_curve_buy_frames.json').read_text())[
    'direct_buy_exact_sol_in']
DIRECT_EVENT = curve.parse_trade_event(DIRECT['trade_event_log'])
CREATOR = DIRECT_EVENT['creator']
CURVE_MINT = R.curve_mint_from_refs(CURVE['accounts'])
# The fires run on a recorded direct buy_exact_sol_in (a v0 first buy, 2026-10-16): since the read guard
# a DFlow curve frame is refused before any read (holder_unbound_dflow).
FIRES = json.loads((Path(__file__).parent / 'fixtures/memecoin/rabbit_curve_buys_20261016.json').read_text())
FIRE = next(r for r in FIRES['direct'] if r.get('transaction') and r['holder_pre'] == [None])
FIRE_EVENT = curve.parse_trade_event(FIRE['trade_event_log'])
FIRE_MINT, FIRE_OWNER, FIRE_SLOT = FIRE_EVENT['mint'], FIRE['owner'], FIRE['slot']
FIRE_SPEND = int.from_bytes(bytes.fromhex(FIRE['data_hex'])[8:16], 'little')


def recorded_frame(rec, *, slot=None):
    """A RabbitStream update rebuilt from a recorded getTransaction (legacy or v0, no lookups)."""
    update = pb.SubscribeUpdate()
    update.transaction.slot = slot or rec['slot']
    info, tx = update.transaction.transaction, rec['transaction']
    info.signature = base58.b58decode(tx['signatures'][0])
    info.transaction.signatures.extend(base58.b58decode(sig) for sig in tx['signatures'])
    msg, raw = info.transaction.message, tx['message']
    for src, dst in [('numRequiredSignatures', 'num_required_signatures'),
                     ('numReadonlySignedAccounts', 'num_readonly_signed_accounts'),
                     ('numReadonlyUnsignedAccounts', 'num_readonly_unsigned_accounts')]:
        setattr(msg.header, dst, raw['header'][src])
    msg.account_keys.extend(base58.b58decode(k) for k in raw['accountKeys'])
    msg.recent_blockhash = base58.b58decode(raw['recentBlockhash'])
    msg.versioned = tx['version'] != 'legacy'
    for ix in raw['instructions']:
        msg.instructions.add(program_id_index=ix['programIdIndex'], accounts=bytes(ix['accounts']),
                             data=base58.b58decode(ix['data']))
    return update


def fire_frame():
    return recorded_frame(FIRE)


@pytest.fixture
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BUY', True)


def pre_trade(event=DIRECT_EVENT):
    """The curve as it stood before `event`'s buy, from that buy's own post-trade event."""
    return dict(virtual_sol_reserves=event['virtual_sol_reserves'] - event['sol_amount'],
                virtual_token_reserves=event['virtual_token_reserves'] + event['token_amount'],
                real_sol_reserves=event['real_sol_reserves'] - event['sol_amount'],
                real_token_reserves=event['real_token_reserves'] + event['token_amount'],
                fee_basis_points=event['fee_basis_points'],
                creator_fee_basis_points=event['creator_fee_basis_points'])


def curve_account(state, *, complete=0, mayhem=0, creator=CREATOR, lamports=None, length=151):
    raw = bytearray(151)
    struct.pack_into('<5Q', raw, 8, state['virtual_token_reserves'], state['virtual_sol_reserves'],
                     state['real_token_reserves'], state['real_sol_reserves'], 10 ** 15)
    raw[48] = complete
    raw[49:81] = base58.b58decode(creator)
    raw[81] = mayhem
    return {'owner': PUMP_PROGRAM, 'lamports': lamports or state['real_sol_reserves'] + 2_000_000,
            'data': [base64.b64encode(bytes(raw[:length])).decode(), 'base64']}


def fee_config(state, tiers=1):
    raw = bytearray(PF.FEE_CONFIG_DISCRIMINATOR + bytes(33))
    raw += struct.pack('<3Q', 2, state['fee_basis_points'], state['creator_fee_basis_points'])
    raw += struct.pack('<I', tiers)
    for i in range(tiers):
        raw += struct.pack('<QQ3Q', i * 10 ** 9, 0, 2, state['fee_basis_points'],
                           state['creator_fee_basis_points'])
    return {'owner': 'pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ', 'lamports': 1,
            'data': [base64.b64encode(bytes(raw)).decode(), 'base64']}


def holder_account(mint, owner, amount, *, program=curve.TOKEN_PROGRAM):
    raw = base58.b58decode(mint) + base58.b58decode(owner) + struct.pack('<Q', amount) + bytes(93)
    return {'owner': program, 'lamports': 2_039_280, 'data': [base64.b64encode(raw).decode(), 'base64']}


def fake_rpc(mint, state, *, calls=None, slot=457_074_090, held=0, owner=None, **curve_kw):
    """The processed read. With a fourth key (the source's coin account) it answers `held` tokens of
    `mint` owned by `owner` (FIRE_OWNER by default), no account at all for 0."""
    accounts = {'curve': curve_account(state, **curve_kw), 'mint': {'owner': curve.TOKEN_PROGRAM},
                'config': fee_config(state)}
    keys = accounts_for(mint)

    def rpc(method, params):
        if calls is not None:
            calls.append((method, params))
        assert method == 'getMultipleAccounts'
        assert params[0][:3] == [keys['curve'], mint, keys['curve_fee_config']] and len(params[0]) in (3, 4)
        assert params[1]['commitment'] == 'processed'
        value = [accounts['curve'], accounts['mint'], accounts['config']]
        if len(params[0]) == 4:
            value.append(holder_account(mint, owner or FIRE_OWNER, held) if held else None)
        return {'context': {'slot': slot}, 'value': value}
    rpc.accounts = accounts
    rpc.ping = lambda: None
    return rpc


# --- the source's buy, priced from the read ---------------------------------------------------

def test_a_direct_buy_prices_exactly_as_it_executed_on_chain():
    """Slot 450780443, a live buy_exact_sol_in of 3,000,000 lamports: from the curve before it, the
    estimate is the event to the atomic unit."""
    spend = int.from_bytes(bytes.fromhex(DIRECT['data_hex'])[8:16], 'little')
    fill = R.source_fill(pre_trade(), spend=spend)
    for key in ('sol_amount', 'token_amount', 'fee', 'creator_fee', 'virtual_sol_reserves',
                'virtual_token_reserves', 'real_sol_reserves', 'real_token_reserves'):
        assert fill[key] == DIRECT_EVENT[key], key


def test_a_quoted_output_prices_the_net_that_buys_exactly_that_much():
    fill = R.source_fill(pre_trade(), quoted_out=DIRECT_EVENT['token_amount'])
    assert (fill['sol_amount'], fill['fee'], fill['creator_fee']) == (
        DIRECT_EVENT['sol_amount'], DIRECT_EVENT['fee'], DIRECT_EVENT['creator_fee'])


@pytest.mark.parametrize('amount', [{'quoted_out': 0}, {'quoted_out': 10 ** 30}, {'spend': 0}, {}])
def test_an_amount_the_curve_cannot_fill_prices_nothing(amount):
    assert R.source_fill(pre_trade(), **amount) is None


def test_the_synthesized_event_parses_back_field_for_field():
    state = dict(pre_trade(), creator=CREATOR, slot=1, token_program=curve.TOKEN_PROGRAM)
    fill = R.source_fill(state, spend=3_000_000)
    event = R.synthesized_event(DIRECT_EVENT['mint'], DIRECT_EVENT['user'], state, fill, timestamp=1760000000)
    parsed = curve.parse_trade_event(R.encode_trade_event(event))
    assert parsed == event
    assert parsed['ix_name'] == 'rabbit_pre_execution' and parsed['quote_mint'] == curve.SYSTEM_PROGRAM


def test_a_mayhem_curve_is_read_only_on_request_and_carries_its_mode_into_the_event():
    mint = DIRECT_EVENT['mint']
    assert R.read_curve(mint, fake_rpc(mint, pre_trade(), mayhem=1)) == {'reason': 'mayhem_or_fee_unmodelled'}
    state = R.read_curve(mint, fake_rpc(mint, pre_trade(), mayhem=1), allow_mayhem=True)
    assert state['mayhem_mode'] is True
    assert (state['fee_basis_points'], state['creator_fee_basis_points']) == (95, 30)
    fill = R.source_fill(state, spend=3_000_000)
    event = R.synthesized_event(mint, DIRECT_EVENT['user'], state, fill, timestamp=1760000000)
    assert event['mayhem_mode'] is True       # the builder picks the Mayhem fee recipient from this
    regular = R.read_curve(mint, fake_rpc(mint, pre_trade()), allow_mayhem=True)
    assert regular['mayhem_mode'] is False


# --- the one read: everything unread or unmodelled fails closed --------------------------------

def test_the_read_is_one_processed_get_multiple_accounts():
    calls = []
    state = R.read_curve(DIRECT_EVENT['mint'], fake_rpc(DIRECT_EVENT['mint'], pre_trade(), calls=calls))
    assert len(calls) == 1
    assert state['creator'] == CREATOR and state['token_program'] == curve.TOKEN_PROGRAM
    assert (state['fee_basis_points'], state['creator_fee_basis_points']) == (95, 30)
    assert state['virtual_sol_reserves'] == pre_trade()['virtual_sol_reserves'] and state['slot'] == 457_074_090


@pytest.mark.parametrize('damage, reason', [
    (dict(mayhem=1), 'mayhem_or_fee_unmodelled'),
    (dict(complete=1), 'curve_complete'),
    (dict(length=60), 'curve_unreadable'),
    (dict(lamports=1), 'curve_not_sol_quoted')])
def test_a_curve_the_lane_does_not_model_fails_closed(damage, reason):
    mint = DIRECT_EVENT['mint']
    assert R.read_curve(mint, fake_rpc(mint, pre_trade(), **damage)) == {'reason': reason}


def test_a_multi_tier_fee_config_fails_closed():
    mint = DIRECT_EVENT['mint']
    rpc = fake_rpc(mint, pre_trade())
    rpc.accounts['config'] = fee_config(pre_trade(), tiers=2)
    assert R.read_curve(mint, rpc) == {'reason': 'mayhem_or_fee_unmodelled'}


@pytest.mark.parametrize('slot, value, reason', [
    ('curve', None, 'no_curve'), ('curve', {'owner': curve.TOKEN_PROGRAM}, 'no_curve'),
    ('config', None, 'fee_config_unreadable'), ('mint', None, 'mint_unreadable'),
    ('mint', {'owner': PUMP_PROGRAM}, 'mint_unreadable')])
def test_a_missing_or_foreign_account_fails_closed(slot, value, reason):
    mint = DIRECT_EVENT['mint']
    rpc = fake_rpc(mint, pre_trade())
    rpc.accounts[slot] = value
    assert R.read_curve(mint, rpc) == {'reason': reason}


def test_a_short_answer_fails_closed():
    assert R.read_curve(DIRECT_EVENT['mint'], lambda method, params: {'value': [None]}) == {'reason': 'read_short'}


# --- the frame: owner signed, one pump-curve buy ------------------------------------------------

def test_a_version_1_frame_rests_on_the_leaders_sigverify():
    assert R.owner_signature_basis(frame(CURVE), owner(CURVE)) == 'leader_sigverify_v1'


def _geyser(tx, *, versioned):
    update = pb.SubscribeUpdate()
    update.transaction.slot = 5
    info = update.transaction.transaction
    info.signature = bytes(tx.signatures[0])
    info.transaction.signatures.extend(bytes(s) for s in tx.signatures)
    msg, raw = info.transaction.message, tx.message
    msg.header.num_required_signatures = raw.header.num_required_signatures
    msg.header.num_readonly_signed_accounts = raw.header.num_readonly_signed_accounts
    msg.header.num_readonly_unsigned_accounts = raw.header.num_readonly_unsigned_accounts
    msg.account_keys.extend(bytes(k) for k in raw.account_keys)
    msg.recent_blockhash = bytes(raw.recent_blockhash)
    msg.versioned = versioned
    for ix in raw.instructions:
        msg.instructions.add(program_id_index=ix.program_id_index, accounts=bytes(ix.accounts), data=bytes(ix.data))
    return update


@pytest.mark.parametrize('versioned', [False, True])
def test_a_legacy_or_v0_frame_verifies_the_owners_signature_locally(versioned):
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import Message, MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction, VersionedTransaction
    payer, owner_key = Keypair(), Keypair()
    ix = Instruction(Pubkey.from_string(PUMP_PROGRAM), curve.BUY_EXACT_SOL_IN_DISCRIMINATOR + bytes(17),
                     [AccountMeta(owner_key.pubkey(), True, True)])
    blockhash = Hash.new_unique()
    if versioned:
        tx = VersionedTransaction(MessageV0.try_compile(payer.pubkey(), [ix], [], blockhash), [payer, owner_key])
    else:
        tx = Transaction([payer, owner_key], Message([ix], payer.pubkey()), blockhash)
    update = _geyser(tx, versioned=versioned)
    assert R.owner_signature_basis(update, str(owner_key.pubkey())) == 'ed25519_local'
    forged = update.transaction.transaction.transaction.signatures
    forged[1] = bytes(64)
    assert R.owner_signature_basis(update, str(owner_key.pubkey())) is None


def test_an_owner_present_only_as_a_non_signer_produces_no_signal(knobs):
    update = frame(CURVE)
    msg = update.transaction.transaction.transaction.message
    n = msg.header.num_required_signatures
    bystander = base58.b58encode(msg.account_keys[n]).decode()      # first non-signer account
    stamped = []
    assert prep.preparation_candidate(update, {bystander: ('uid', 'h')}, recv_monotonic=0.0,
                                      generation=1, on_owner_frame=lambda *a: stamped.append(a)) is None
    assert stamped == []
    assert R.owner_signature_basis(update, bystander) is None
    buyer, stats, fired = _buyer(fake_rpc(CURVE_MINT, pre_trade()), owners={bystander: ('uid', 'h')})
    good = prep.preparation_candidate(update, {owner(CURVE): ('uid', 'h')}, recv_monotonic=0.0, generation=1)
    asyncio.run(_offer(buyer, dict(good, owner=bystander), update))
    assert fired == [] and stats['unsigned'] == 1


def test_only_a_single_pump_curve_leg_is_a_rabbit_buy(knobs):
    cand = prep.preparation_candidate(frame(DAMM), {owner(DAMM): ('uid', 'h')}, recv_monotonic=0.0, generation=1)
    assert R.source_buy(cand, prep.preparation_target(cand)) is None             # DAMM final leg
    cand = prep.preparation_candidate(frame(CURVE), {owner(CURVE): ('uid', 'h')}, recv_monotonic=0.0, generation=1)
    mint, amount, route = R.source_buy(cand, prep.preparation_target(cand))
    assert (mint, amount, route) == (CURVE_MINT, {'quoted_out': CURVE['quoted_out_amount']}, 'dflow')


# --- the fire, on the signal loop -----------------------------------------------------------------

async def _offer(buyer, cand, update, *, before_read=None):
    buyer.offer(cand, update)
    if before_read is not None:
        before_read()
    while buyer._tasks:
        await asyncio.sleep(0)
        await asyncio.gather(*list(buyer._tasks), return_exceptions=True)


def backstop(read, exits=None):
    return R.RabbitBackstop(read=read, exit_offer=(exits if exits is not None else []).append,
                            alert_fn=lambda *a: None)


def _buyer(read, *, seen=None, owners=None, fired=None, rabbit=None):
    stats = {}
    fired = fired if fired is not None else []
    rabbit = rabbit or backstop(read)
    landing = R.LandingWatch(backstop=rabbit, head_fn=lambda: (None, None), status={})
    buyer = R.RabbitBuyer(seen=seen if seen is not None else {},
                          owners_fn=lambda: owners or {FIRE_OWNER: ('uid', 'h')},
                          on_buy=fired.append, backstop=rabbit, feed='rabbit:rabbitstream.fra.shyft.to',
                          status=stats, landing=landing,
                          head_fn=lambda: (FIRE_SLOT - 1, 1.0), epoch_fn=lambda: 1760000000.0)
    return buyer, stats, fired


def _curve_candidate():
    return prep.preparation_candidate(fire_frame(), {FIRE_OWNER: ('uid', 'h')},
                                      recv_monotonic=time.monotonic(), generation=4)


def fire_rpc(**kw):
    """The read of the recorded fire's curve as it stood before the source's buy (no coin held yet), at
    the frame's own slot unless told otherwise (a read more than READ_MAX_LAG_SLOTS behind is refused)."""
    kw.setdefault('slot', FIRE_SLOT)
    return fake_rpc(FIRE_MINT, pre_trade(FIRE_EVENT), creator=FIRE_EVENT['creator'], **kw)


def test_a_rabbit_frame_fires_one_pre_execution_buy(knobs):
    buyer, stats, fired = _buyer(fire_rpc())
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert stats['fired'] == 1 and len(fired) == 1
    buy = fired[0]
    assert R.is_rabbit(buy) and buy.venue == 'pump_curve' and buy.mint == FIRE_MINT
    assert buy.token_amount == FIRE_EVENT['token_amount'] and buy.token_program == curve.TOKEN_PROGRAM
    assert buy.signal_feed == 'rabbit:rabbitstream.fra.shyft.to' and buy.receipt_evidence is None
    assert buy.rabbit['signature_basis'] == 'ed25519_local' and buy.rabbit['read_ms'] >= 0
    assert buy.rabbit['slot_status'] == 'pre_execution' and buy.rabbit['read_slot'] == FIRE_SLOT
    assert buy.rabbit['read_basis'] == R.PRE_TRADE and buy.rabbit['holder_atomic'] == 0
    proof = buy.source_initiation
    assert proof['proof_version'] == R.PRE_EXECUTION_PROOF_VERSION and proof['payment_basis'] == 'pre_execution_estimate'
    assert proof['tx_hash'] == FIRE['signature'] and proof['payer'] == proof['beneficiary'] == FIRE_OWNER
    event = curve.parse_trade_event(buy.trade_event_log)
    assert event['creator'] == FIRE_EVENT['creator'] and event['user'] == FIRE_OWNER  # creator from OUR read
    assert stats['read_ms_p50'] is not None and stats['read_ms_p90'] is not None


def test_the_processed_feed_first_means_no_rabbit_fire(knobs):
    key = (FIRE['signature'], FIRE_OWNER)
    seen = {key: 1.0}
    buyer, stats, fired = _buyer(fire_rpc(), seen=seen)
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert fired == [] and stats['duplicate'] == 1
    seen.clear()                                   # processed lands while the read is in flight
    buyer, stats, fired = _buyer(fire_rpc(), seen=seen)
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame(), before_read=lambda: seen.update({key: 2.0})))
    assert fired == [] and stats['processed_first'] == 1


def test_a_failed_read_fires_nothing(knobs):
    def broken(method, params):
        raise RuntimeError('getMultipleAccounts: http 429')
    broken.ping = lambda: None
    buyer, stats, fired = _buyer(broken)
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert fired == [] and stats['read_errors'] == 1 and stats['last_error'] == 'read:RuntimeError'


def test_an_unmodelled_curve_fires_nothing_and_is_counted(knobs):
    buyer, stats, fired = _buyer(fire_rpc(mayhem=1))
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert fired == [] and stats['refused'] == {'mayhem_or_fee_unmodelled': 1}


def test_a_changed_idl_fires_nothing(knobs, monkeypatch, tmp_path):
    from sniper.execution import dflow_swap as D
    cand = prep.preparation_candidate(frame(CURVE), {owner(CURVE): ('uid', 'h')},
                                      recv_monotonic=time.monotonic(), generation=4)
    changed = tmp_path / 'idl.json'
    changed.write_bytes(D.IDL_PATH.read_bytes().replace(b'"RecordId"', b'"RecordIx"', 1))
    monkeypatch.setattr(D, 'IDL_PATH', changed)
    D._idl.cache_clear()
    try:
        buyer, stats, fired = _buyer(fake_rpc(CURVE_MINT, pre_trade()))
        asyncio.run(_offer(buyer, cand, frame(CURVE)))
        assert fired == [] and stats['unsupported'] == 1
    finally:
        D._idl.cache_clear()


def test_an_owner_who_left_the_roster_fires_nothing(knobs):
    buyer, stats, fired = _buyer(fire_rpc(), owners={'Other': ('u', 'h')})
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert fired == [] and stats['roster_refused'] == 1


# --- the knobs ------------------------------------------------------------------------------------

def _run(monkeypatch, *, rabbit, on_buy=lambda b: None):
    from sniper.services import memecoin_slot_signal as signal
    from sniper.services import memecoin_shyft_signal as full
    stop, status, seen_kwargs = threading.Event(), {}, []

    async def fake_rabbit(url, token, owners_fn, on_prepare, feed_status, stop, **kwargs):
        seen_kwargs.append(sorted(kwargs))
        hook = kwargs.get('on_candidate')
        if hook is not None:
            hook(_curve_candidate(), fire_frame())
        await asyncio.sleep(.05)
        stop.set()

    async def fake_full(url, token, owners_fn, on_buy, feed_status, stop, **kwargs):
        while not stop.is_set():
            await asyncio.sleep(.01)
    monkeypatch.setattr(prep, 'run_prepare_feed', fake_rabbit)
    monkeypatch.setattr(full, 'run_feed', fake_full)
    asyncio.run(signal._run_signals(on_buy, lambda: {FIRE_OWNER: ('uid', 'h')}, None, status, stop,
        None, None, None, None, ('token', ('https://grpc.ams.shyft.to',)), infra_provider='shyft',
        prepare_config=('token', 'https://rabbitstream.fra.shyft.to'), on_prepare=lambda c: None,
        rabbit=rabbit))
    return seen_kwargs, status


def test_signal_on_and_buy_off_never_sends(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_SIGNAL', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_BUY', False)
    calls, fired = [], []
    kwargs, status = _run(monkeypatch, rabbit=backstop(fire_rpc(calls=calls)), on_buy=fired.append)
    assert kwargs == [['on_candidate', 'on_owner_frame', 'on_owner_miss', 'on_rejected_candidate']]
    assert status['rabbit_buy']['held'] == 1 and status['rabbit_buy']['fired'] == 0
    assert calls == [] and fired == []                              # no read, no fire
    # and a rabbit proof that reaches the gate anyway is refused
    ev = _ev(_rabbit_buy())
    assert source_initiation_refusal(ev, 'solana') == 'wallet_initiation_unproved'


def test_both_knobs_on_fire_from_the_signal_loop(knobs, monkeypatch):
    fired = []
    kwargs, status = _run(monkeypatch, rabbit=backstop(fire_rpc()), on_buy=fired.append)
    assert kwargs == [['on_candidate', 'on_owner_frame', 'on_owner_miss', 'on_rejected_candidate']]
    assert status['rabbit_buy']['fired'] == 1 and len(fired) == 1 and R.is_rabbit(fired[0])


def test_no_backstop_means_no_hook(knobs, monkeypatch):
    kwargs, status = _run(monkeypatch, rabbit=None)
    assert kwargs == [['on_owner_frame', 'on_owner_miss', 'on_rejected_candidate']] and 'rabbit_buy' not in status


# --- the proof gate ---------------------------------------------------------------------------------

def _rabbit_buy(**kw):
    state = dict(pre_trade(), creator=CREATOR, slot=457_074_090, token_program=curve.TOKEN_PROGRAM)
    fill = R.source_fill(state, spend=3_000_000)
    cand = dict(signature=DIRECT['signature'], owner=DIRECT_EVENT['user'], slot=450_780_443,
                recv_monotonic=time.monotonic(), generation=1)
    buy = R.rabbit_slot_buy(cand, DIRECT_EVENT['mint'], state, fill, source_user_id='w1', handle='h',
                            signature_basis='leader_sigverify_v1', route='direct', read_ms=12.5,
                            recv_epoch=1760000000.0, feed='rabbit:rabbitstream.fra.shyft.to',
                            now_mono=time.monotonic())
    return dataclasses.replace(buy, **kw)


def _ev(buy, **over):
    base = dict(fomo_event_id='slotlane:' + buy.signature, signal_source='slot_lane',
                source_timestamp_trust='rabbit_pre_execution', source_wallet=buy.owner, mint=buy.mint,
                source_user_id=buy.source_user_id, source_block_number=buy.slot,
                source_initiation=buy.source_initiation, network_id='solana')
    base.update(over)
    return SimpleNamespace(**base)


def test_the_pre_execution_proof_is_accepted_only_for_the_rabbit_lane(knobs):
    buy = _rabbit_buy()
    assert source_initiation_refusal(_ev(buy), 'solana') is None
    assert source_initiation_refusal(_ev(buy, signal_source='onchain_lane'), 'solana') == 'wallet_initiation_unproved'
    assert source_initiation_refusal(_ev(buy, source_timestamp_trust='observed_detection'), 'solana') == \
        'wallet_initiation_unproved'
    bad = dict(buy.source_initiation, signature_basis='claimed')
    assert source_initiation_refusal(_ev(buy, source_initiation=bad), 'solana') == 'wallet_initiation_unproved'
    for field, value in (('payer', 'Other'), ('mint', 'Other'), ('block_number', 1), ('source_user_id', 'w2')):
        bad = dict(buy.source_initiation, **{field: value})
        assert source_initiation_refusal(_ev(buy, source_initiation=bad), 'solana') == 'receipt_identity_mismatch'


# --- the lane ---------------------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'ledger.db')
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _lane(conn, tmp_path, monkeypatch, *, armed=('w1',)):
    from test_memecoin_slot_lane import _live_lane
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    ln.armed_ids_fn = lambda: set(armed)
    handed, learned = [], []
    ln.handoff = SimpleNamespace(offer=lambda *a, **k: handed.append(a), offer_busy=lambda *a: False)
    return ln, sent, handed


def _processed(buy):
    return dataclasses.replace(buy, source_initiation=None, proof_provenance=None, rabbit=None,
                               signal_feed='shyft:grpc.ams.shyft.to', trade_event_log=DIRECT['trade_event_log'])


def _attempts(conn):
    return [dict(r) for r in conn.execute(
        'SELECT delivery_lane, source_timestamp_trust, trace_json, terminal_status FROM memecoin_entry_attempts '
        'ORDER BY rowid')]


def test_a_live_rabbit_buy_is_marked_pre_execution_and_never_observed(knobs, conn, tmp_path, monkeypatch):
    ln, sent, handed = _lane(conn, tmp_path, monkeypatch)
    result = ln.handle(_rabbit_buy())
    assert result['status'] == 'filled' and sent == [1]
    row, = _attempts(conn)
    assert row['delivery_lane'] == 'slot_lane_rabbit' and row['source_timestamp_trust'] == 'rabbit_pre_execution'
    trace = json.loads(row['trace_json'])
    signal = next(e for e in trace['substage_events'] if e['name'] == 'rabbit_signal')
    assert signal['read_ms'] == 12.5 and signal['signature_basis'] == 'leader_sigverify_v1'
    assert signal['slot_status'] == 'pre_execution'
    assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0
    assert handed == []


def test_the_processed_copy_of_an_owned_rabbit_buy_is_dropped(knobs, conn, tmp_path, monkeypatch):
    ln, sent, handed = _lane(conn, tmp_path, monkeypatch)
    rabbit = _rabbit_buy()
    assert ln.handle(rabbit)['status'] == 'filled'
    again = ln.handle(_processed(rabbit))
    assert again == {'status': 'rejected', 'reason': lane.RABBIT_DUPLICATE_REASON}
    assert sent == [1] and len(_attempts(conn)) == 1


def test_a_rabbit_buy_refused_before_any_reservation_leaves_the_processed_buy_its_path(
        knobs, conn, tmp_path, monkeypatch):
    ln, sent, handed = _lane(conn, tmp_path, monkeypatch, armed=())
    rabbit = _rabbit_buy()
    assert ln.handle(rabbit)['reason'] == 'not_approved'
    ln.armed_ids_fn = lambda: {'w1'}
    assert ln.handle(_processed(rabbit))['status'] == 'filled' and sent == [1]
    row, = _attempts(conn)
    assert row['delivery_lane'] == 'slot_lane_processed'


def test_a_rabbit_buy_is_never_handed_to_the_onchain_lane(knobs, conn, tmp_path, monkeypatch):
    ln, sent, handed = _lane(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(lane, 'admission_refusal', lambda buy, **k: ('slot_lane_price_unreadable', {}))
    assert 'slot_lane_price_unreadable' in lane.HANDOFF_REASONS
    assert ln.handle(_rabbit_buy())['reason'] == 'slot_lane_price_unreadable'
    assert handed == []
    ln.handle(_processed(_rabbit_buy()))
    assert len(handed) == 1                          # the processed copy is handed off as today


def test_the_rabbit_buy_never_taps_the_sell_mirror():
    tapped = []
    mirror = SimpleNamespace(offer_buy=tapped.append)
    on_buy = lambda buy: (None if R.is_rabbit(buy) else mirror.offer_buy(buy))   # the lane's own rule
    import inspect
    source = inspect.getsource(lane.start_slot_lane)
    assert 'if not is_rabbit(buy):' in source and 'mirror.offer_buy(buy)' in source
    on_buy(_rabbit_buy())
    on_buy(_processed(_rabbit_buy()))
    assert len(tapped) == 1 and not R.is_rabbit(tapped[0])


def test_heartbeat_reports_rabbit_fires(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    lane_status = {'signal': {'rabbit_buy': dict(candidates=3, fired=2, read_ms_p50=40.1, read_ms_p90=80.2,
                                                 refused={'curve_complete': 1})}}
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=lane_status)
    row = json.loads(path.read_text())['slot_lane']['rabbit_buy']
    assert row['fired'] == 2 and row['read_ms_p90'] == 80.2 and row['refused'] == {'curve_complete': 1}


def test_heartbeat_carries_the_read_guards_counters(tmp_path):
    """The read guard's prospective matrix (Gamma B2) is the first read after its deploy: every key
    reaches the heartbeat, copied, so a later in-memory update cannot change a written row."""
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat'
    guard = dict(read_basis={R.PRE_TRADE: 2, 'refused:read_after_source_quoted': 1},
                 read_slot_delta={'0': 2, '-1': 1}, read_matrix={'excluded|' + R.PRE_TRADE: 2}, read_miss=0,
                 by_route={'direct': {R.PRE_TRADE: 2, 'read_ms_p50': 41.0, 'read_ms_p90': 63.0}})
    lane_status = {'signal': {'rabbit_buy': dict(candidates=3, fired=2, refused={}, **guard)}}
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=lane_status)
    row = json.loads(path.read_text())['slot_lane']['rabbit_buy']
    for key, value in guard.items():
        assert row[key] == value, key
