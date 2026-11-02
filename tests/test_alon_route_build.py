"""ALON's actual unsigned PumpSwap instruction retains the whole-receipt price bound."""
import math
from types import SimpleNamespace

import pytest

from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_slot_lane as lane
from test_alon_native_resume import TX, alon_session


def test_alon_minimum_output_uses_whole_payment_and_acquisition(monkeypatch):
    _, pools, ev, handoff, queued, clock = alon_session(monkeypatch)
    assert handoff.offer(ev)
    buy, = queued
    event = ps.parse_buy_event(buy.trade_event_log)
    accounts = TX['meta']['innerInstructions'][0]['instructions'][23]['accounts']
    facts = dict(pools.get(buy.pool), coin_creator=event['coin_creator'],
        base_token_program=accounts[9], quote_token_program=accounts[10],
        pool_base_token_account=accounts[7], pool_quote_token_account=accounts[8])
    builder = ps.SlotLanePumpSwapBuilder(user=buy.owner, rpc=lambda *_: pytest.fail('no RPC'),
        jup_call=lambda *_: pytest.fail('no quote'), clock=clock.monotonic)
    builder.pools = SimpleNamespace(get=lambda key: facts if key == buy.pool else None)
    builder._buyback = '5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD'
    builder._protocol_recipient = '11111111111111111111111111111112'
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35.)
    monkeypatch.setattr(lane.cfg, 'solana_wallet_chase_cap_pct', lambda *_: 99.)
    monkeypatch.setattr(lane.cfg, 'copy_premium_cap', lambda *_: (35., 'global'))
    quote_usd, cost = .0056, 10_000_000
    leg = {'ixs': [], 'tables': [], 'usdc_atomic': cost, 'output_mint': facts['quote_mint'],
           'min_out_lamports': 1785714285, 'out_lamports': 1785714285, 'fetched_at': clock.now}
    snap = SimpleNamespace(blockhash='11111111111111111111111111111111', last_valid_height=1, sol_usd=100.)
    try:
        message, detail = builder.build(buy, 10., snap, leg=leg, quote=(6, quote_usd, 0, 0))
        assert message is not None, detail
        average = (1497150270 / 1e6) / (10908646100860 / 1e6)
        assert builder.last_build['source_average_price_usd'] == pytest.approx(average)
        keys = [str(key) for key in message.account_keys]
        instruction, = [ix for ix in message.instructions if keys[ix.program_id_index] == ps.AMM_PROGRAM]
        assert bytes(instruction.data)[:8] == ps.BUY_EXACT_QUOTE_IN_DISCRIMINATOR
        minimum = int.from_bytes(bytes(instruction.data)[16:24], 'little')
        assert minimum >= math.ceil(cost / 1e6 / (average * 1.35) * 1e6)
        assert minimum == builder.last_build['min_tokens_out']
    finally:
        handoff.finish(buy)
