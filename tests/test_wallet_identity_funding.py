"""Confirmed funding links persist independently of activity and never confer ownership."""
import json

import pytest

from test_wallet_identity import A, B, NOW, inputs, project
from sniper.services.wallet_identity import read_identity

SIG = '4' * 88


def funding_receipts():
    return dict(version=1, chain='solana', observed_at=NOW, commitment='confirmed', transactions=[
        dict(slot=100, blockTime=1_790_000_000, meta=dict(err=None, preBalances=[2_000_005_000, 0],
             postBalances=[1_000_000_000, 1_000_000_000]), transaction=dict(signatures=[SIG], message=dict(
             accountKeys=[dict(pubkey=B, signer=True), dict(pubkey=A, signer=False)], instructions=[
                 dict(program='system', programId='11111111111111111111111111111111', parsed=dict(
                      type='transfer', info=dict(source=B, destination=A, lamports=1_000_000_000)))])))])


def read(tmp_path, *, proofs=None, claims=None, report=True, chain='solana'):
    if report:
        (tmp_path/'wallet_identity.json').write_text(json.dumps(project()))
    if proofs is not None:
        (tmp_path/'wallet_funding_receipts.json').write_text(json.dumps(proofs))
    return read_identity(tmp_path/'wallet_identity.json', inputs()['claims'] if claims is None else claims,
                         chain=chain, now_iso=NOW)


def test_signed_funding_link_is_verified_without_promoting_ownership(tmp_path):
    result = read(tmp_path, proofs=funding_receipts())
    related, = result['wallets']['user-a']['related_wallets']
    assert related['status'] == 'funding_verified_link' and related['funding_verified'] is True
    assert related['ownership_proven'] is False and related['n_coins'] == 3
    clue = next(c for c in related['funding_clues'] if c.get('verified'))
    assert (clue['funder'], clue['recipient'], clue['funding_signature'], clue['amount_atomic'], clue['amount_sol']) == (B, A, SIG, '1000000000', '1')
    assert result['wallets']['user-a']['verified_addresses'][0]['address'] == A
    assert all(k not in related for k in ('armed','effective','funded','capital','real_approved'))


def test_funding_link_survives_missing_activity_artifact(tmp_path):
    result = read(tmp_path, proofs=funding_receipts(), report=False)
    row = result['wallets']['user-a']; related, = row['related_wallets']
    assert row['state'] == 'unavailable' and related['status'] == 'funding_verified_link'
    assert related['n_coins'] == 0 and related['lead_p50_s'] is None and related['missed_coins'] is None
    assert related['ownership_proven'] is False


def test_funding_out_of_the_main_preserves_exact_fractional_sol(tmp_path):
    raw=funding_receipts();tx=raw['transactions'][0]
    tx['transaction']['message']['accountKeys']=[dict(pubkey=A,signer=True),dict(pubkey=B,signer=False)]
    tx['transaction']['message']['instructions'][0]['parsed']['info'].update(source=A,destination=B,lamports=1_000_000_001)
    tx['meta']['postBalances'][1]=1_000_000_001
    related,=read(tmp_path,proofs=raw)['wallets']['user-a']['related_wallets']
    clue=next(c for c in related['funding_clues'] if c.get('verified'))
    assert (clue['funder'],clue['recipient'],clue['amount_sol'])==(A,B,'1.000000001')


def test_one_receipt_retains_funding_to_two_main_addresses(tmp_path):
    d='E'*32;raw=funding_receipts();tx=raw['transactions'][0];msg=tx['transaction']['message']
    msg['accountKeys'].append(dict(pubkey=d,signer=False))
    msg['instructions'].append(dict(program='system',programId='11111111111111111111111111111111',
        parsed=dict(type='transfer',info=dict(source=B,destination=d,lamports=14_000_000_000))))
    tx['meta'].update(preBalances=[16_000_005_000,0,0],postBalances=[1_000_000_000,1_000_000_000,14_000_000_000])
    claims=inputs()['claims']+[dict(chain='solana',address=d,uid='user-a')]
    related,=read(tmp_path,proofs=raw,claims=claims)['wallets']['user-a']['related_wallets']
    assert {(c['funder'],c['recipient'],c['amount_sol']) for c in related['funding_clues'] if c.get('verified')}=={(B,A,'1'),(B,d,'14')}


def test_funding_link_survives_an_expired_activity_sample(tmp_path):
    read(tmp_path,proofs=funding_receipts())
    result=read_identity(tmp_path/'wallet_identity.json',inputs()['claims'],chain='solana',now_iso='2026-10-30T12:00:00+00:00')
    assert result['wallets']['user-a']['state']=='stale'
    assert result['wallets']['user-a']['related_wallets'][0]['funding_verified'] is True


@pytest.mark.parametrize('bad',[None,'many',-1,True])
def test_malformed_activity_omission_preserves_verified_addresses_and_funding(tmp_path,bad):
    raw=project();raw['users']['user-a']['chains']['solana']['omitted']=bad
    (tmp_path/'wallet_identity.json').write_text(json.dumps(raw))
    result=read(tmp_path,proofs=funding_receipts(),report=False)
    row=result['wallets']['user-a'];related,=row['related_wallets']
    assert row['state']=='unavailable' and row['verified_addresses'][0]['address']==A
    assert related['funding_verified'] is True and related['n_coins']==0


def test_activity_artifact_cannot_supply_a_verified_funding_flag(tmp_path):
    raw = project()
    raw['users']['user-a']['chains']['solana']['related_wallets'][0]['funding_verified'] = True
    (tmp_path/'wallet_identity.json').write_text(json.dumps(raw))
    related, = read(tmp_path, report=False)['wallets']['user-a']['related_wallets']
    assert related.get('funding_verified') is not True


@pytest.mark.parametrize('bad', ['failed', 'unsigned', 'wrong_program', 'no_error_field', 'no_credit', 'zero', 'float', 'future', 'wrong_chain', 'unconfirmed'])
def test_invalid_receipt_cannot_verify_a_cached_funding_clue(tmp_path, bad):
    raw = funding_receipts(); tx = raw['transactions'][0]
    if bad == 'failed': tx['meta']['err'] = {'InstructionError': [0, 'InvalidArgument']}
    elif bad == 'unsigned': tx['transaction']['message']['accountKeys'][0]['signer'] = False
    elif bad == 'wrong_program': tx['transaction']['message']['instructions'][0]['programId'] = B
    elif bad == 'no_error_field': tx['meta'].pop('err')
    elif bad == 'no_credit': tx['meta']['postBalances'][1] = 0
    elif bad == 'zero': tx['transaction']['message']['instructions'][0]['parsed']['info']['lamports'] = 0
    elif bad == 'float': tx['transaction']['message']['instructions'][0]['parsed']['info']['lamports'] = 1e9
    elif bad == 'future': raw['observed_at'] = '2026-10-11T12:00:00+00:00'
    elif bad == 'wrong_chain': raw['chain'] = '56'
    elif bad == 'unconfirmed': raw['commitment'] = 'processed'
    related, = read(tmp_path, proofs=raw)['wallets']['user-a']['related_wallets']
    assert related.get('funding_verified') is not True and related['status'] == 'suspected_alternate'


def test_revoked_or_conflicting_main_mapping_cannot_attach_the_funding_link(tmp_path):
    claims = [dict(chain='solana', address=A, uid='user-a', conflict=True)]
    related, = read(tmp_path, proofs=funding_receipts(), claims=claims)['wallets']['user-a']['related_wallets']
    assert related.get('funding_verified') is not True
    assert read(tmp_path, proofs=funding_receipts(), claims={})['wallets']['user-a']['related_wallets'][0].get('funding_verified') is not True


def test_a_conflicting_alternate_retains_the_funding_fact_and_owner_conflict(tmp_path):
    claims = inputs()['claims'] + [dict(chain='solana', address=B, uid=u) for u in ('user-b','user-c')]
    related, = read(tmp_path, proofs=funding_receipts(), claims=claims)['wallets']['user-a']['related_wallets']
    assert related['status'] == 'ownership_conflict' and related['funding_verified'] is True
    assert related['known_owner_uids'] == ['user-b','user-c'] and related['ownership_proven'] is False


def test_receipt_file_is_chain_scoped_and_failed_read_preserves_activity(tmp_path):
    result = read(tmp_path, proofs=funding_receipts(), chain='8453')
    assert result['wallets']['user-a']['related_wallets'] is None
    (tmp_path/'wallet_funding_receipts.json').write_text('{')
    related, = read(tmp_path)['wallets']['user-a']['related_wallets']
    assert related['status'] == 'suspected_alternate'
