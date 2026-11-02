"""The rabbit read guard (Gamma B2, Alpha 2026-10-16): the processed read behind a RabbitStream fire can
already hold the source's buy. Applying that buy to it again prices the source high and loosens the chase
reference and the hard copy cap: fail-open on the money path.

The same getMultipleAccounts reads the source's own coin account, bound to the frame:
- 0 held means pre-trade (the buy is applied once);
- held tokens on a direct spend mean post-trade only when un-applying the frame's exact net and those
  tokens reproduces the read to the lamport;
- anything else refuses.
A DFlow curve leg names no destination account, so its read is refused before it is made.

Evidence: 20 landed direct buy_exact_sol_in and 10 DFlow curve buys
(fixtures/memecoin/rabbit_curve_buys_20261016.json)."""
import asyncio
import dataclasses
import json
import time
from pathlib import Path

import pytest

from sniper.execution import pump_curve_buy as curve
from sniper.services import memecoin_rabbit_signal as R
from sniper.services import memecoin_shyft_prepare as prep
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from test_memecoin_rabbit_signal_stage1 import CURVE, frame, owner
from test_memecoin_rabbit_signal_stage2 import (FIRE, FIRE_EVENT, FIRE_MINT, FIRE_OWNER, FIRE_SLOT, FIRE_SPEND, _buyer,
                                                _curve_candidate, _ev, _offer, fake_rpc, fire_frame,
                                                knobs, pre_trade, recorded_frame)  # noqa: F401

FX = json.loads((Path(__file__).parent / 'fixtures/memecoin/rabbit_curve_buys_20261016.json').read_text())
KEYS = ('sol_amount', 'token_amount', 'fee', 'creator_fee', 'virtual_sol_reserves', 'virtual_token_reserves',
        'real_sol_reserves', 'real_token_reserves')
RESERVES = KEYS[4:]


def _event(rec):
    return curve.parse_trade_event(rec['trade_event_log'])


def _spend(rec):
    return int.from_bytes(bytes.fromhex(rec['data_hex'])[8:16], 'little')


def _state(event, *, held, post):
    """A read of the curve: as the landed event left it (post) or as it stood before it."""
    base = pre_trade(event)
    if post:
        base = {**base, **{k: event[k] for k in RESERVES}}
    return dict(base, holder_atomic=held)


def _first_buys():
    return [r for r in FX['direct'] if r['holder_pre'] == [None]]


def _capped(event, spend):
    return event['sol_amount'] + event['fee'] + event['creator_fee'] < spend   # the curve ran out


# --- pump's own rule, to the lamport ------------------------------------------------------------------

def test_the_direct_fill_is_pumps_rule_on_every_landed_buy_but_a_capped_one():
    exact = capped = 0
    for rec in FX['direct']:
        event = _event(rec)
        fill = R.source_fill(pre_trade(event), spend=_spend(rec))
        if _capped(event, _spend(rec)):
            capped += 1
            assert fill is None or fill['token_amount'] != event['token_amount']
            continue
        assert fill is not None and {k: fill[k] for k in KEYS} == {k: event[k] for k in KEYS}, rec['signature']
        exact += 1
    assert (exact, capped) == (19, 1)


# --- the verdict, on every landed first buy ------------------------------------------------------------

def test_a_read_before_the_buy_is_pre_trade_and_prices_the_landed_event():
    for rec in _first_buys():
        event = _event(rec)
        if _capped(event, _spend(rec)):
            continue
        verdict, fill = R.read_verdict(_state(event, held=0, post=False), {'spend': _spend(rec)})
        assert verdict == R.PRE_TRADE and {k: fill[k] for k in KEYS} == {k: event[k] for k in KEYS}


def test_a_read_that_already_holds_the_buy_is_post_trade_and_never_applies_it_again():
    """The fail-open case. The fill is derived from the frame's own amounts, and must equal the landed
    TradeEvent to the lamport; its post-trade reserves are the read itself."""
    checked = 0
    for rec in _first_buys():
        event = _event(rec)
        if _capped(event, _spend(rec)):
            continue
        state = _state(event, held=event['token_amount'], post=True)
        verdict, fill = R.read_verdict(state, {'spend': _spend(rec)})
        assert verdict == R.POST_TRADE, rec['signature']
        assert {k: fill[k] for k in KEYS} == {k: event[k] for k in KEYS}
        assert all(fill[k] == state[k] for k in RESERVES)
        checked += 1
    assert checked >= 15


def test_the_confusion_matrix_on_landed_frames_has_no_miss():
    """Truth from the landed event: a pre-read called pre, a post-read called post or refused. The miss,
    a post-read called pre-trade, is 0. A re-buy (coins held before) refuses either way."""
    matrix = {}
    for rec in FX['direct']:
        event, spend = _event(rec), _spend(rec)
        before = (rec['holder_pre'][0] or [0])[0]
        after = (rec['holder_post'][0] or [0])[0]
        for truth, state in (('excluded', _state(event, held=before, post=False)),
                             ('included', _state(event, held=after, post=True))):
            verdict, _ = R.read_verdict(state, {'spend': spend})
            key = truth + '|' + (verdict or 'refused')
            matrix[key] = matrix.get(key, 0) + 1
    assert matrix.get('included|' + R.PRE_TRADE, 0) == 0
    assert matrix.get('excluded|' + R.POST_TRADE, 0) == 0
    assert matrix['included|' + R.POST_TRADE] >= 15 and matrix['excluded|' + R.PRE_TRADE] >= 15


def test_a_rebuy_refuses_whether_or_not_the_read_holds_the_buy():
    (rec,) = [r for r in FX['direct'] if r['holder_pre'][0] and r['holder_pre'][0][0] > 0]
    event, spend = _event(rec), _spend(rec)
    for state in (_state(event, held=rec['holder_pre'][0][0], post=False),
                  _state(event, held=rec['holder_post'][0][0], post=True)):
        assert R.read_verdict(state, {'spend': spend}) == (None, 'read_after_source_unproven')


def test_a_post_read_that_also_holds_a_later_buy_refuses():
    rec = _first_buys()[0]
    event, spend = _event(rec), _spend(rec)
    state = _state(event, held=event['token_amount'], post=True)
    later = R.source_fill(state, spend=10 ** 9)                    # someone else bought after the source
    moved = dict(state, **{k: later[k] for k in RESERVES})
    assert R.read_verdict(moved, {'spend': spend}) == (None, 'read_after_source_unproven')


def test_a_quoted_route_holding_coins_refuses():
    event = _event(_first_buys()[0])
    state = _state(event, held=event['token_amount'], post=True)
    assert R.read_verdict(state, {'quoted_out': event['token_amount']}) == (None, 'read_after_source_quoted')
    assert R.read_verdict(dict(state, holder_atomic=None), {'spend': 1}) == (None, 'holder_unread')


# --- binding the coin account to the frame ------------------------------------------------------------

def _fire_parts():
    update = fire_frame()
    cand = prep.preparation_candidate(update, {FIRE_OWNER: ('uid', 'h')}, recv_monotonic=0.0, generation=1)
    return update, cand, R.source_buy(cand, prep.preparation_target(cand))


def test_the_direct_coin_account_is_the_buys_own_associated_user_and_writable():
    update, cand, found = _fire_parts()
    assert R.bind_holder(cand, found, R.writable_keys(update)) == (FIRE['accounts'][5], None)
    assert R.bind_holder(cand, found, frozenset()) == (None, 'holder_unbound')


def test_another_instruction_naming_the_coin_account_refuses_the_frame():
    """A bundler can buy and then move the coin out in the same transaction: a zero balance would then
    prove nothing. An ATA create of that account is the one exception."""
    update, cand, found = _fire_parts()
    holder = FIRE['accounts'][5]
    moved = dict(cand, instructions=cand['instructions'] + (
        (curve.TOKEN_2022_PROGRAM, (holder, 'Dest1111111111111111111111111111111111111111', FIRE_OWNER), b'\x03'),))
    assert R.bind_holder(moved, found, R.writable_keys(update)) == (None, 'holder_moved_in_frame')
    created = dict(cand, instructions=(
        (R.ATA_PROGRAM, (FIRE_OWNER, holder, FIRE_OWNER, FIRE_MINT), b'\x01'),) + cand['instructions'])
    assert R.bind_holder(created, found, R.writable_keys(update))[0] == holder


def test_a_dflow_curve_leg_is_refused_before_any_read(knobs):
    """7 of 10 landed DFlow curve buys credited no coin account the owner holds: a zero at the owner's
    ATA would read as pre-trade after the buy ran. The DFlow IDL names no destination, so no read."""
    assert sum(1 for r in FX['dflow'] if not r['owner_coin_accounts_post']) >= 7
    calls = []
    buyer, stats, fired = _buyer(fake_rpc(R.curve_mint_from_refs(CURVE['accounts']), pre_trade(), calls=calls),
                                 owners={owner(CURVE): ('uid', 'h')})
    cand = prep.preparation_candidate(frame(CURVE), {owner(CURVE): ('uid', 'h')},
                                      recv_monotonic=time.monotonic(), generation=1)
    asyncio.run(_offer(buyer, cand, frame(CURVE)))
    assert calls == [] and fired == [] and stats['refused'] == {'holder_unbound_dflow': 1}
    assert stats['by_route']['dflow'] == {'holder_unbound_dflow': 1}


def test_a_coin_account_that_is_not_the_owners_refuses(knobs):
    for kw in ({'owner': FIRE_MINT}, {}):                               # another owner; then the owner's
        buyer, stats, fired = _buyer(fake_rpc(FIRE_MINT, pre_trade(FIRE_EVENT), creator=FIRE_EVENT['creator'],
                                              held=5, slot=FIRE_SLOT, **kw))
        asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
        assert fired == []
        assert stats['refused'] == ({'holder_unreadable': 1} if kw else {'read_after_source_unproven': 1})


# --- the seam: recorded frame -> RabbitBuyer._fire -> rabbit_slot_buy -> funded admission ----------------

def _fire(held, state):
    buyer, stats, fired = _buyer(fake_rpc(FIRE_MINT, state, creator=FIRE_EVENT['creator'], held=held, slot=FIRE_SLOT))
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    return buyer, stats, fired


def test_a_fire_on_a_read_that_holds_the_buy_carries_the_landed_event_and_the_processed_paths_floors(knobs):
    """Alpha's check: the rabbit buy built from a POST read must give the same source average and the
    same signed floors as the processed path's own event. Nothing is applied twice."""
    post = {**pre_trade(FIRE_EVENT), **{k: FIRE_EVENT[k] for k in RESERVES}}
    buyer, stats, (buy,) = _fire(FIRE_EVENT['token_amount'], post)
    assert buy.rabbit['read_basis'] == R.POST_TRADE and buy.rabbit['holder_atomic'] == FIRE_EVENT['token_amount']
    synthesized = curve.parse_trade_event(buy.trade_event_log)
    assert {k: synthesized[k] for k in KEYS} == {k: FIRE_EVENT[k] for k in KEYS}
    assert source_initiation_refusal(_ev(buy), 'solana') is None                 # funded admission
    post_price_usd = (FIRE_EVENT['virtual_sol_reserves'] / 1e9) / (FIRE_EVENT['virtual_token_reserves'] / 1e6) * 150.0
    common = dict(user=FIRE_OWNER, token_program=curve.TOKEN_PROGRAM, buyback_fee_recipient=FIRE_OWNER,
                  source_price_usd=post_price_usd, source_cost_usdc_atomic=75_000_000, source_price_cap_pct=30.0)
    # The lane resolves a Global fee recipient for the synthesized event (system program); give both the
    # landed one, which the amounts never read.
    ours_event = dict(synthesized, fee_recipient=FIRE_EVENT['fee_recipient'])
    _ix, ours = curve.curve_buy_instructions(ours_event, 500_000_000, 30.0, **common)
    _ix, processed = curve.curve_buy_instructions(FIRE_EVENT, 500_000_000, 30.0, **common)
    assert ours == processed
    assert stats['read_basis'] == {R.POST_TRADE: 1} and stats['by_route']['direct'][R.POST_TRADE] == 1


def test_the_processed_copy_joins_each_read_into_the_matrix(knobs):
    """The prospective matrix: every read is joined to the processed copy of its signature, whichever
    arrives first. Truly included but called pre-trade is the miss, counted."""
    buyer, stats, fired = _fire(0, pre_trade(FIRE_EVENT))
    processed = dataclasses.replace(fired[0], virtual_sol_reserves=FIRE_EVENT['virtual_sol_reserves'],
                                    virtual_token_reserves=FIRE_EVENT['virtual_token_reserves'],
                                    sol_amount=FIRE_EVENT['sol_amount'], token_amount=FIRE_EVENT['token_amount'])
    buyer.observe_processed(processed)
    assert stats['read_matrix'] == {'excluded|' + R.PRE_TRADE: 1} and stats['read_miss'] == 0
    # The miss, simulated: a read holding the buy whose coin account still reads 0 (a torn read).
    post = {**pre_trade(FIRE_EVENT), **{k: FIRE_EVENT[k] for k in RESERVES}}
    buyer, stats, fired = _buyer(fake_rpc(FIRE_MINT, post, creator=FIRE_EVENT['creator'], held=0, slot=FIRE_SLOT))
    buyer.observe_processed(processed)                                     # processed first, this time
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert stats['read_matrix'] == {'included|' + R.PRE_TRADE: 1} and stats['read_miss'] == 1
    assert stats['read_slot_delta'] and stats['read_basis'] == {R.PRE_TRADE: 1}


# --- Gamma LOW pins (2026-10-16): guard lines no test covered ------------------------------------------

@pytest.mark.parametrize('ata_ix', [
    ((R.ATA_PROGRAM, (FIRE_OWNER, 'Else111111111111111111111111111111111111111', FIRE_OWNER, FIRE_MINT), b'\x01'), 'another'),
    ((R.ATA_PROGRAM, (FIRE_OWNER, None, FIRE_OWNER, FIRE_MINT), b'\x02'), 'recover_nested'),
])
def test_only_an_ata_create_of_the_coin_account_itself_is_exempt(ata_ix):
    """An ATA instruction that names the coin account in another slot, or is not Create/CreateIdempotent
    (data 0x02 RecoverNested can move tokens), refuses like any other instruction naming it."""
    update, cand, found = _fire_parts()
    holder = FIRE['accounts'][5]
    (program, accounts, data), _label = ata_ix
    accounts = tuple(holder if a is None else a for a in accounts)
    if _label == 'another':
        accounts = (holder,) + accounts[1:]                 # the holder named, but not as the ATA created
    moved = dict(cand, instructions=((program, accounts, data),) + cand['instructions'])
    assert R.bind_holder(moved, found, R.writable_keys(update)) == (None, 'holder_moved_in_frame')


def test_a_coin_account_owned_by_another_program_is_unreadable():
    from test_memecoin_rabbit_signal_stage2 import holder_account
    account = holder_account(FIRE_MINT, FIRE_OWNER, 5, program=curve.TOKEN_2022_PROGRAM)
    assert R._holder_balance(account, mint=FIRE_MINT, owner=FIRE_OWNER, program=curve.TOKEN_PROGRAM) is None
    assert R._holder_balance(account, mint=FIRE_MINT, owner=FIRE_OWNER, program=curve.TOKEN_2022_PROGRAM) == 5


def test_only_a_processed_curve_buy_joins_the_matrix(knobs):
    buyer, stats, fired = _fire(0, pre_trade(FIRE_EVENT))
    amm = dataclasses.replace(fired[0], venue='pump_amm', virtual_sol_reserves=FIRE_EVENT['virtual_sol_reserves'],
                              virtual_token_reserves=FIRE_EVENT['virtual_token_reserves'],
                              sol_amount=FIRE_EVENT['sol_amount'], token_amount=FIRE_EVENT['token_amount'])
    buyer.observe_processed(amm)
    assert stats['read_matrix'] == {}
    buyer.observe_processed(dataclasses.replace(amm, venue='pump_curve'))
    assert stats['read_matrix'] == {'excluded|' + R.PRE_TRADE: 1}


@pytest.mark.parametrize("lag, fires", [(0, True), (2, True), (3, False), (None, False)])
def test_a_curve_read_more_than_the_max_lag_before_the_frame_is_refused(knobs, monkeypatch, lag, fires):
    """Gamma LOW-1 / Alpha 2026-10-17, the AMM handler's rule on the curve: a lagging node's read at S-k
    misses the net sells of S-k..S-1, so the modelled post price sits high and the cap loosens."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS", 2)
    slot = None if lag is None else FIRE_SLOT - lag
    buyer, stats, fired = _buyer(fake_rpc(FIRE_MINT, pre_trade(FIRE_EVENT), creator=FIRE_EVENT['creator'], slot=slot))
    asyncio.run(_offer(buyer, _curve_candidate(), fire_frame()))
    assert bool(fired) is fires
    if not fires:
        assert stats["refused"] == {"read_stale": 1} and stats["read_matrix"] == {}   # before the read guard
