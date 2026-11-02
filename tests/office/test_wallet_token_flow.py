"""Account-scoped evidence; no RPC, wallet control or synthetic missing-side zeros."""
import copy
import json
import sqlite3

import pytest
from solders.pubkey import Pubkey

TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN22 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
A, B, X, Y, M = (str(Pubkey.from_bytes(bytes([i])*32)) for i in range(1, 6))


def tx(program=TOKEN, kind='transferChecked'):
    def balance(i, owner, amount):
        return dict(accountIndex=i, owner=owner, mint=M, programId=program,
                    uiTokenAmount=dict(amount=str(amount), decimals=6))
    info = dict(source=X, destination=Y, authority=A, mint=M,
                tokenAmount=dict(amount=str(2**63+5), decimals=6))
    return dict(transaction=dict(signatures=['sig'], message=dict(accountKeys=[dict(pubkey=k) for k in [A,B,X,Y,M]],
        instructions=[dict(programId=program, parsed=dict(type=kind, info=info), accounts=[X,M,Y,A], data='wire')])),
        meta=dict(err=None, preTokenBalanceObservations=[balance(2,A,2**63+10),balance(3,B,1)],
                  postTokenBalanceObservations=[balance(2,A,5),balance(3,B,2**63+6)], innerInstructions=[]))


def test_exact_transfer_and_inventory_are_separate():
    from sniper.office.wallet_token_flow import token_flow
    out=token_flow(tx()); transfer,=out['transfers']
    assert (transfer['source_owner'],transfer['destination_owner'],transfer['mint'])==(A,B,M)
    assert transfer['amount_atomic']==str(2**63+5) and transfer['credit_atomic']==str(2**63+5)
    assert transfer['state']=='known' and transfer['outer_ix']==0 and transfer['inner_ix']==-1
    assert json.loads(transfer['instruction_json'])['data']=='wire'
    assert out['accounts'][0]['pre_atomic']==str(2**63+10)


def test_new_closed_accounts_do_not_become_zero_inventory():
    from sniper.office.wallet_token_flow import token_flow
    value=tx();value['meta']['preTokenBalanceObservations'].pop()
    value['meta']['postTokenBalanceObservations'].pop(0)
    out=token_flow(value)
    assert out['accounts'][0]['post_atomic'] is None
    assert out['accounts'][1]['pre_atomic'] is None
    assert all(v['state']=='unknown' and v['delta_atomic'] is None for v in out['accounts'])


@pytest.mark.parametrize('bad',['decimals','mint','owner','duplicate','amount'])
def test_conflicting_or_malformed_balance_identity_is_not_known(bad):
    from sniper.office.wallet_token_flow import token_flow
    value=tx();row=value['meta']['postTokenBalanceObservations'][0]
    if bad=='decimals':row['uiTokenAmount']['decimals']=9
    elif bad=='mint':row['mint']=B
    elif bad=='owner':row['owner']=B
    elif bad=='duplicate':value['meta']['postTokenBalanceObservations'].append(copy.deepcopy(row))
    else:row['uiTokenAmount']['amount']='-1'
    out=token_flow(value)
    assert out['state']!='known'
    assert not out['transfers'] or out['transfers'][0]['state']!='known'


def test_unchecked_transfer_needs_account_mint_and_inner_path_is_preserved():
    from sniper.office.wallet_token_flow import token_flow
    value=tx();ix=value['transaction']['message']['instructions'].pop()
    ix['parsed']['type']='transfer';ix['parsed']['info']['amount']='7'
    del ix['parsed']['info']['tokenAmount'];del ix['parsed']['info']['mint']
    value['meta']['innerInstructions']=[dict(index=0,instructions=[ix])]
    value['transaction']['message']['instructions']=[dict(programId=A)]
    out=token_flow(value);transfer,=out['transfers']
    assert (transfer['outer_ix'],transfer['inner_ix'],transfer['mint'],transfer['decimals'])==(0,0,M,6)
    value['meta']['preTokenBalanceObservations']=[];value['meta']['postTokenBalanceObservations']=[]
    transfer,=token_flow(value)['transfers']
    assert transfer['mint'] is None and transfer['state']=='unknown'


def test_token2022_ordinary_transfer_net_credit_stays_unknown_even_with_balance_delta():
    from sniper.office.wallet_token_flow import token_flow
    transfer,=token_flow(tx(TOKEN22))['transfers']
    assert transfer['amount_atomic']==str(2**63+5)
    assert transfer['fee_atomic'] is None and transfer['credit_atomic'] is None
    assert transfer['state']=='unknown' and transfer['reason']=='token2022_fee_unknown'


def test_explicit_token2022_fee_is_instruction_evidence_not_inventory_reconciliation():
    from sniper.office.wallet_token_flow import token_flow
    value=tx(TOKEN22,'transferCheckedWithFee')
    value['transaction']['message']['instructions'][0]['parsed']['info']['feeAmount']=dict(amount='5',decimals=6)
    transfer,=token_flow(value)['transfers']
    assert transfer['fee_atomic']=='5' and transfer['credit_atomic']==str(2**63)


def test_persist_scopes_to_actual_observed_proof_refs_and_stores_text(tmp_path):
    from sniper.office.wallet_token_flow import SCHEMA,persist,scoped_rows,token_flow
    c=sqlite3.connect(tmp_path/'token.db');c.executescript(SCHEMA)
    accounts,transfers=scoped_rows(token_flow(tx()),[A])
    with c:persist(c,signature='sig',accounts=accounts,transfers=transfers,proof_refs={A:'existing-proof'},source='fixture',flow_id=1,known_at=1)
    assert c.execute('select typeof(amount_atomic),fetch_id from watch_token_transfers').fetchone()==('text','existing-proof')
    assert c.execute('select count(*) from watch_token_accounts').fetchone()[0]==1
    with c:persist(c,signature='sig',accounts=accounts,transfers=transfers,proof_refs={A:'existing-proof'},source='fixture',flow_id=1,known_at=1)
    assert c.execute('select count(*) from watch_token_transfers').fetchone()[0]==1
    with pytest.raises(ValueError):persist(c,signature='other',accounts=accounts,transfers=transfers,proof_refs={B:'invented'},source='fixture',flow_id=1,known_at=1)
    assert scoped_rows(token_flow(tx()),[])==([],[])


def test_owner_change_keeps_scoped_unknown_observation_with_original_actor_proof(tmp_path):
    from sniper.office.wallet_token_flow import token_flow,scoped_rows,persist,SCHEMA
    value=tx();value['meta']['postTokenBalanceObservations'][0]['owner']=B
    accounts,transfers=scoped_rows(token_flow(value),[A])
    assert len(accounts)==1 and accounts[0]['owner'] is None and accounts[0]['delta_atomic'] is None
    c=sqlite3.connect(tmp_path/'owners.db');c.executescript(SCHEMA)
    with c:persist(c,signature='sig',accounts=accounts,transfers=transfers,proof_refs={A:'observed-A'},source='fixture',flow_id=1,known_at=1)
    assert c.execute('select owner,state,fetch_id from watch_token_accounts').fetchone()==(None,'unknown','observed-A')


def test_empty_raw_token_sides_are_unknown_inventory_not_known_absence():
    from sniper.office.wallet_token_flow import token_flow
    value=tx();value['meta']['preTokenBalanceObservations']=[];value['meta']['postTokenBalanceObservations']=[]
    value['transaction']['message']['instructions']=[dict(programId=A)]
    out=token_flow(value)
    assert out['accounts']==[] and out['transfers']==[]
    assert out['state']=='unknown' and out['reason']=='no_token_observations'


def test_old_storage_migration_does_not_invent_causal_context(tmp_path):
    from sniper.office.wallet_token_flow import SCHEMA,init_storage
    old=SCHEMA.replace(',source TEXT,flow_id INTEGER,known_at REAL','')
    c=sqlite3.connect(tmp_path/'old.db');c.executescript(old)
    c.execute("INSERT INTO watch_token_accounts(signature,account,owner,state,fetch_id) VALUES('sig','account','owner','unknown','proof')");c.commit()
    init_storage(c)
    assert c.execute('select source,flow_id,known_at from watch_token_accounts').fetchone()==(None,None,None)
