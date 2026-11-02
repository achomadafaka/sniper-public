"""Observed-tape scope is explicit; incomplete capture is never a full-history proof."""
import pytest
from sniper.services import memecoin_large_buyer as B,memecoin_origin as O
from sniper.settings import memecoin as cfg
from test_codex_crowd_admission import T,NOW,db

MINT_FACT=dict(decimals=6,metadata_slot=123,metadata_known_at=NOW-1,
               metadata_source='helius_research:get_account_info:json_parsed',token_program='spl-token')


def request(state='unknown',**changes):
    return B.request_for(dict(T,history_state=state,history_reason='complete_pool_history_unproven',**changes),
                         cohort_id=B.CROWD_COHORT_ID,decimals=6,sol_usd=100.,now=NOW,mint_metadata=MINT_FACT)


@pytest.mark.parametrize('state',[None,'unknown','observed_only',''])
def test_observed_tape_does_not_require_complete_capture(state):
    t=dict(T,history_state=state)
    assert B._skip(t,db(),B.CROWD_COHORT_ID,lambda _:6,lambda _:100.,NOW,30,1) is None
    assert B.POLICY['history_basis']=='observed_tape' and B.POLICY['capture_complete'] is False
    assert 'observed sell' in B.POLICY['rule']
    assert request(state)['desk_metadata']['history_basis']=='observed_tape'


def test_snapshot_identity_retains_unknown_history_instead_of_claiming_complete():
    assert request('unknown')['desk_metadata']['snapshot_hash']!=request('known')['desk_metadata']['snapshot_hash']
    assert B.POLICY['capture_complete'] is False


@pytest.mark.parametrize('change,decimals,sol,why',[
    ({},None,100.,'decimals_unknown'),({},6,None,'sol_unknown'),
    ({'entry_due':NOW-6},6,100.,'late'),({'crowd_state':'unknown'},6,100.,'crowd_proof_unknown'),
    ({'source_tx_index':0},6,100.,'crowd_order_unknown')])
def test_other_admission_gates_still_refuse(change,decimals,sol,why):
    t=dict(T,history_state='unknown');t.update(change)
    assert B._skip(t,db(),B.CROWD_COHORT_ID,lambda _:decimals,lambda _:sol,NOW,30,1)==why


def test_observed_basis_does_not_bypass_effective_token_safety(monkeypatch):
    monkeypatch.setattr(cfg,'MEMECOIN_LARGE_BUYER_ENABLED',True)
    monkeypatch.setattr(cfg,'MEMECOIN_DESK_ENABLED',False)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',False)
    assert not O.desk_lane_open(request()['desk_metadata'])
