import copy
import json

import pytest

from sniper.services.wallet_identity import read_identity

NOW='2026-10-10T12:00:00+00:00'
T=1791630000
A,B,C,M=('0x'+v*40 for v in 'abcd')
H='0x'+'e'*64


def artifact():
    buys=[dict(uid='u',chain='8453',main_address=A,mint=M,source_signature=H,
               source_order=[100,5],source_t0=T,address=B,buy_signature='0x'+'f'*64,
               order=[100,3],t0=T,proof_method='direct-wallet-paid-v1'),
          dict(uid='u',chain='8453',main_address=A,mint=C,source_signature='0x'+'2'*64,
               source_order=[101,5],source_t0=T+1,address=B,buy_signature='0x'+'1'*64,
               order=[102,3],t0=T+2,proof_method='direct-wallet-paid-v1')]
    return dict(version=1,method='armed-wallet-links-v1',generated_at=NOW,
                pairs=[dict(uid='u',chain='8453',main_addresses=[A],coverage=dict(
                    state='partial',source_units=3,sampled_coins=2,complete_coins=0,unknown_coins=3,
                    funding_state='partial',capped=['history']))],buys=buys,funding=[])


def read(tmp_path,raw=None,claims=None,chain='8453'):
    (tmp_path/'wallet_identity_links.json').write_text(json.dumps(raw or artifact()))
    claims=claims if claims is not None else [dict(uid='u',chain='8453',address=A,evidence_kind='test')]
    return read_identity(tmp_path/'wallet_identity.json',claims,chain=chain,now_iso=NOW)


def test_exact_same_block_before_and_later_buy_reach_existing_reader(tmp_path):
    out=read(tmp_path)['wallets']['u']
    assert out['state']=='partial'
    r,=out['related_wallets']
    assert (r['before_coins'],r['after_coins'],r['n_coins'])==(1,1,2)
    assert r['lead_p50_s']==0
    assert r['missed_coins'] is None
    assert r['ownership_proven'] is False
    assert r['evidence'][0]['direction']=='before'
    assert r['evidence'][0]['source_order']==[100,5]


@pytest.mark.parametrize('hub',[True,False])
def test_funding_native_atomic_scale_and_link_direction_are_preserved(tmp_path,hub):
    raw=artifact();raw['buys']=[]
    raw['funding']=[dict(chain='8453',kind='verified_funding',verified=True,funder=B,recipient=A,
        funding_signature=H,funded_at=T,amount_atomic='1000000000000000000',amount='1',asset='native',
        symbol='ETH',decimals=18,method='confirmed_native_transfer',ownership_proven=False,hub=hub)]
    r,=read(tmp_path,raw)['wallets']['u']['related_wallets']
    assert r['status']=='funding_verified_link'
    assert r['n_coins']==0 and r['lead_p50_s'] is None
    assert r['funding_clues'][0]['amount']=='1'
    assert r['funding_clues'][0]['recipient']==A
    assert r['funding_clues'][0]['hub'] is hub


def test_current_main_binding_change_censors_old_activity(tmp_path):
    raw=artifact()
    out=read(tmp_path,raw,claims=[dict(uid='u',chain='8453',address=C,evidence_kind='new')])['wallets']['u']
    assert out['related_wallets'] is None
    assert out['coverage']['reason']=='account_mapping_changed'


def test_conflicting_main_has_no_related_authority_or_activity(tmp_path):
    claims=[dict(uid=u,chain='8453',address=A,evidence_kind='test') for u in ('u','other')]
    out=read(tmp_path,claims=claims)['wallets']['u']
    assert out['related_wallets'] is None
    assert out['coverage']['reason']=='address_unproven'


def test_pair_and_address_ownership_do_not_bleed_across_chains(tmp_path):
    raw=artifact();claims=[dict(uid='u',chain='56',address=A,evidence_kind='test')]
    out=read(tmp_path,raw,claims=claims,chain='56')['wallets']['u']
    assert out['related_wallets'] is None and out['state']=='not_measured'


@pytest.mark.parametrize('mutate',[
    lambda r:r['buys'][0].update(order=[100,None]),
    lambda r:r['buys'][0].update(t0=T-61),
    lambda r:r['pairs'][0]['coverage'].update(sampled_coins=True),
    lambda r:r.update(generated_at='2026-10-11T00:00:00+00:00'),
    lambda r:r['buys'][0].update(proof_method='swap_sender_topic'),
])
def test_malformed_saved_evidence_never_claims_a_measured_zero(tmp_path,mutate):
    raw=artifact();mutate(raw)
    out=read(tmp_path,raw)
    assert out['links_state']=='unavailable'
    assert out['wallets']['u']['related_wallets'] is None


def test_cropped_evidence_preserves_full_distinct_counts(tmp_path):
    raw=artifact();raw['pairs'][0]['coverage'].update(source_units=8,sampled_coins=8)
    raw['pairs'][0]['statistics']=[dict(address=B,n_coins=8,before_coins=7,after_coins=1,
        days=3,lead_p50_s=5,lead_p90_s=10)]
    r,=read(tmp_path,raw)['wallets']['u']['related_wallets']
    assert (r['n_coins'],r['before_coins'],r['after_coins'])==(8,7,1)
    assert len(r['evidence'])==2


def test_unsigned_or_misscaled_funding_rejected(tmp_path):
    raw=artifact();raw['funding']=[dict(chain='8453',kind='verified_funding',verified=True,
        funder=B,recipient=A,funding_signature=H,funded_at=T,amount_atomic='100',amount='100',
        asset='native',symbol='ETH',decimals=18,method='confirmed_native_transfer',ownership_proven=False)]
    assert read(tmp_path,raw)['links_state']=='unavailable'


def test_single_coin_buy_before_a_verified_funded_peer_is_retained(tmp_path):
    raw=artifact();raw['buys']=raw['buys'][:1]
    raw['funding']=[dict(chain='8453',kind='verified_funding',verified=True,funder=B,recipient=A,
        funding_signature=H,funded_at=T,amount_atomic='1000000000000000000',amount='1',asset='native',
        symbol='ETH',decimals=18,method='confirmed_native_transfer',ownership_proven=False)]
    r,=read(tmp_path,raw)['wallets']['u']['related_wallets']
    assert r['n_coins']==1 and r['before_coins']==1 and r['after_coins']==0


@pytest.mark.parametrize('bad',[None,'3',-1,True])
def test_invalid_relation_omissions_are_unavailable(tmp_path,bad):
    raw=artifact();raw['pairs'][0]['coverage']['relation_omitted']=bad
    assert read(tmp_path,raw)['links_state']=='unavailable'


def test_statistics_cannot_inject_verified_funding_or_authority(tmp_path):
    raw=artifact();raw['pairs'][0]['statistics']=[dict(address=B,n_coins=2,before_coins=1,after_coins=1,
        days=1,lead_p50_s=0,lead_p90_s=0,funding_verified=True,armed=True,
        funding_clues=[dict(kind='verified_funding',verified=True,amount='100',symbol='ETH')])]
    r,=read(tmp_path,raw)['wallets']['u']['related_wallets']
    assert not r.get('funding_verified') and not r.get('armed')
    assert r['funding_clues']==[]


def test_old_activity_is_stale_while_funding_remains_available(tmp_path):
    raw=artifact();raw['pairs'][0]['coverage']['state']='ok'
    (tmp_path/'wallet_identity_links.json').write_text(json.dumps(raw))
    out=read_identity(tmp_path/'wallet_identity.json',[dict(uid='u',chain='8453',address=A)],
                      chain='8453',now_iso='2026-10-19T12:00:00+00:00')
    assert out['links_state']=='stale'
    assert out['wallets']['u']['state']=='stale'


def test_one_signature_cannot_have_two_mined_identities(tmp_path):
    raw=artifact();raw['buys'][1]['source_signature']=H
    assert read(tmp_path,raw)['links_state']=='unavailable'


def test_optional_unknown_leads_do_not_crash_the_reader(tmp_path):
    raw=artifact();raw['pairs'][0]['statistics']=[dict(address=B,n_coins=2,before_coins=1,after_coins=1,days=1)]
    r,=read(tmp_path,raw)['wallets']['u']['related_wallets']
    assert r['lead_p50_s'] is None and r['lead_p90_s'] is None
