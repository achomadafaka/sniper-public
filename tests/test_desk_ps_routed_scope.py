"""Attribution only: real routed frames never manufacture priced swap rows."""
import base64
import copy
import json
from pathlib import Path

import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_pumpswap as P, memecoin_desk_universe as U
try:
    from sniper.services import memecoin_desk_pumpswap_routed as R
except ImportError:
    R = None

FIXTURES = Path(__file__).parent/'fixtures/memecoin/desk_routed_scope_20261027'


def receipt(name='multi_hop_26_amm'):
    saved=json.loads((FIXTURES/(name+'.json')).read_text())
    info=pb.SubscribeUpdate.FromString(base64.b64decode(saved['raw_b64'])).transaction.transaction
    msg=info.transaction.message
    keys=list(msg.account_keys)+list(info.meta.loaded_writable_addresses)+list(info.meta.loaded_readonly_addresses)
    instructions=[]
    groups={g.index:g for g in info.meta.inner_instructions}
    for outer,top in enumerate(msg.instructions):
        instructions.append(dict(program_id_index=top.program_id_index,accounts=list(top.accounts),data=bytes(top.data),
                                 outer_ix=outer,stack_height=1))
        if outer in groups:
            for ix in groups[outer].instructions:
                instructions.append(dict(program_id_index=ix.program_id_index,accounts=list(ix.accounts),data=bytes(ix.data),
                                         outer_ix=outer,stack_height=ix.stack_height if ix.HasField('stack_height') else None))
    return list(info.meta.log_messages),keys,instructions


def prove(logs,keys,ix,complete=True):
    assert R is not None, 'routed pool attribution module missing'
    return R.prove_pools(logs,account_keys=keys,instructions=ix,inner_complete=complete)


@pytest.mark.parametrize('name',['multi_hop_'+shape+'_'+kind for shape in ('26','31','36') for kind in ('amm','pump')])
def test_real_routed_witness_has_only_its_bound_ps_event_pools(name):
    logs,keys,ix=receipt(name)
    original=copy.deepcopy((logs,keys,ix))
    expected=sorted({P.parse_buy_event(line)['pool'] for _,line in P.program_data_logs(logs,P.AMM_PROGRAM)
                     if P.anchor_event_bytes(line)[:8]==P.BUY_EVENT_DISCRIMINATOR})
    with pytest.raises(P.DecodeLoss):
        P.events(logs)
    assert prove(logs,keys,ix)==expected
    assert (logs,keys,ix)==original


@pytest.mark.parametrize('damage',['incomplete','truncated','extra_invoke','missing_cpi','cpi_bytes','wrong_pool',
                                    'missing_group','bad_fixed','depth','outer','missing_pump_event','broken_chain','missing_loaded_key'])
def test_incomplete_or_conflicting_route_never_proves_foreign_scope(damage):
    logs,keys,ix=receipt('multi_hop_26_pump' if damage=='missing_pump_event' else 'multi_hop_26_amm')
    route=next(i for i in ix if i['data'].startswith(P._MULTI_HOP))
    complete=True
    if damage=='missing_loaded_key':keys.pop();complete=False
    elif damage=='incomplete':complete=False
    elif damage=='truncated':logs.pop()
    elif damage=='extra_invoke':logs.extend([f'Program {P.AMM_PROGRAM} invoke [1]',f'Program {P.AMM_PROGRAM} success'])
    elif damage=='missing_cpi':ix.remove(next(i for i in ix if i['data'].startswith(P._EVENT_CPI)))
    elif damage=='cpi_bytes':next(i for i in ix if i['data'].startswith(P._EVENT_CPI))['data']+=b'x'
    elif damage=='wrong_pool':route['accounts'][18]=0
    elif damage=='missing_group':route['accounts']=route['accounts'][:-5]
    elif damage=='bad_fixed':route['accounts'][3]=0
    elif damage=='depth':route['stack_height']+=1
    elif damage=='outer':route['outer_ix']=0
    elif damage=='missing_pump_event':
        at,_=next(iter(P.program_data_logs(logs,U.PUMP_PROGRAM)));logs[at]='Program log: missing trade'
    elif damage=='broken_chain':
        # Keep exact CPI/log agreement while changing the second hop's gross input.
        events=list(P.program_data_logs(logs,P.AMM_PROGRAM))
        at,line=events[0];raw=P.anchor_event_bytes(line);payload=P.parse_buy_event(line)
        from sniper.execution.pumpswap_buy import _BUY_EVENT_FIELDS
        offset=8
        sizes={'u64':8,'i64':8,'pubkey':32,'bool':1}
        for name,kind in _BUY_EVENT_FIELDS:
            if name=='base_amount_out':break
            offset+=4+len(payload[name].encode()) if kind=='string' else sizes[kind]
        # Alter the FIRST event output, so it no longer chains into the second event gross.
        at,line=events[0];raw=P.anchor_event_bytes(line);payload=P.parse_buy_event(line)
        changed=raw[:offset]+(payload['base_amount_out']+1).to_bytes(8,'little')+raw[offset+8:]
        logs[at]='Program data: '+base64.b64encode(changed).decode()
        next(i for i in ix if i['data']==P._EVENT_CPI+raw)['data']=P._EVENT_CPI+changed
    with pytest.raises(P.DecodeLoss):
        prove(logs,keys,ix,complete)
