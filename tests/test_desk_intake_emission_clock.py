"""Receipt attribution and producer emission are distinct host clocks."""
import copy
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_intake_book as B
from sniper.services import memecoin_desk_capture as CAP
from sniper.services import memecoin_desk_ps_evidence as E
from test_memecoin_desk_intake_assembler import IA
from test_memecoin_desk_intake_book import sight
from test_memecoin_desk_intake_integrated import MintRpc, iso
from test_memecoin_desk_contract import policy
from test_memecoin_desk_ps_evidence import fixture, mint
from test_desk_ps_asof import MINT, POOL, NOW, swap

RECEIVED = NOW - 10
EMITTED = RECEIVED + .008135


def production(tmp_path):
    root, _ = fixture(tmp_path)
    sight(root, RECEIVED - 1, mint=MINT, offset=0)
    book = B.IntakeBook(intake_dir=root/'intake', db_path=tmp_path/'emitter.db')
    book.reload(RECEIVED - .5)
    sw = swap()
    mapping = dict(pool=POOL, mint=MINT, quote_mint=K.WSOL_MINT, canonical=True, known_at=NOW-600, slot=1)
    event = dict(kind='buy', pool=POOL, post_base_reserve_atomic=sw['post_base'],
                 post_quote_reserve_atomic=sw['post_quote'], lp_fee_bps=20, protocol_fee_bps=5, creator_fee_bps=5,
                 cashback_fee_bps=0, virtual_quote_atomic='0', virtual_quote_source=E.VQ_SOURCE)
    [(tid, body)] = book.on_print(event=event, mapping=mapping, metadata=None, slot=18, signature=sw['sig'],
                                  ordinal=0, tx_index=5, now=EMITTED, received_at=RECEIVED,
                                  sol_usd=100, sol_usd_at=RECEIVED-1)
    book.db.close()
    cand = K.validate_candidate(json.loads(body))
    conn = D.connect(tmp_path/'desk.db')
    hard = {**policy(), 'intake': {'version':'intake-v1','mcap_safety':[5000,50_000_000],'liq_min_quote_sol':1}}
    D.register_cohort(conn, cohort_id='intake', producer='U', prompt_version='desk-judge-v26',
                      policy={'clip_usd':25,'venue':'pumpswap','hard_filter':hard,'evidence_wait_s':30},
                      created_at=iso(NOW-3600))
    desk.ingest_candidate(conn, cand, cohort_id='intake', now_iso=iso(NOW))
    return root, cand, conn, tid, hard


def run(data, **changes):
    root, cand, conn, tid, hard = data
    seen = {}
    def evidence(c, **kw):
        seen.update(kw)
        return E.pumpswap_evidence(c, **kw)
    args = dict(desk_conn=conn, desk_dir=root, rpc=MintRpc(), clip_usd=25, hard_policy=hard,
                now_fn=lambda:NOW+.1, evidence_fn=evidence)
    args.update(changes)
    result = IA.evaluate(cand, tid, **args)
    return result, seen


def test_production_receipt_processing_evaluation_clocks_reach_real_evidence(tmp_path):
    data = production(tmp_path)
    before = copy.deepcopy(data[1])
    (verdict, reason, evidence), seen = run(data)
    assert (verdict, reason) == ('PASS','pass'), evidence['checks']
    from sniper.services.memecoin_desk_intake_book import batch_end
    assert seen['as_of_host'] == batch_end(RECEIVED)            # Am36: the cut is the receipt batch end, not processing
    assert seen['sol_at'] == RECEIVED-1
    assert seen['mint_known_at'] == NOW > EMITTED
    assert data[1] == before
    assert data[1]['snapshot']['fields']['pool_state']['value']['reserve_known_at'] == RECEIVED


def test_capture_must_cover_emission_not_just_receipt_before_mint_dispatch(tmp_path, monkeypatch):
    root, cand, *_ = production(tmp_path)
    monkeypatch.setattr(CAP, 'capture_proof', lambda *a, **kw: {'from':RECEIVED-1,'through':RECEIVED})
    assert IA.committed_through_print(root, cand, NOW) is False
    monkeypatch.setattr(CAP, 'capture_proof', lambda *a, **kw: {'from':RECEIVED-1,'through':EMITTED})
    assert IA.committed_through_print(root, cand, NOW) is True
    monkeypatch.setattr(CAP, 'capture_proof', lambda *a, **kw: {'from':EMITTED,'through':NOW})
    assert IA.committed_through_print(root, cand, NOW) is False


def test_fact_known_after_frozen_emission_is_still_future(tmp_path):
    data = production(tmp_path)
    data[1]['snapshot']['fields']['pool_state']['known_at'] = EMITTED+.001
    rpc = MintRpc()
    (verdict, reason, _), _ = run(data, rpc=rpc)
    assert (verdict, reason) == ('REFUSED', 'future_fact')
    assert rpc.calls == 0
    assert not IA.committed_through_print(data[0], data[1], NOW)
    evidence = E.pumpswap_evidence(data[1], desk_dir=data[0], as_of_host=EMITTED, clip_usd=25,
        sol_usd=100, sol_at=RECEIVED-1, mint_account=mint(),
        mint_known_at=NOW, evaluated_at_host=NOW+.1)
    assert evidence['checks']['market_data']['reason'] == 'pool_metadata_future'


def test_superseding_print_before_emission_cannot_reuse_older_reserves(tmp_path):
    data = production(tmp_path)
    tape = sqlite3.connect(data[0]/'pumpswap/tape.db')
    tape.execute("UPDATE swaps_ps SET received_at=?,slot=19", (RECEIVED+.004,))
    tape.commit(); tape.close()
    (_, _, evidence), _ = run(data)
    assert evidence['checks']['market_data']['reason'] == 'pool_tip_unproven'


@pytest.mark.parametrize('cut',[NOW+1, float('nan'), None, 'bad', -1])
def test_future_or_malformed_emission_cannot_pass_capture_preflight(tmp_path, monkeypatch, cut):
    root, cand, *_ = production(tmp_path)
    cand['snapshot']['emission_at'] = cut
    if cut == NOW+1:
        from test_memecoin_desk_contract import rehash
        cand['emitted_at'] = iso(cut)
        cand = K.validate_candidate(rehash(cand))
    monkeypatch.setattr(CAP,'capture_proof',lambda *a,**kw:{'from':RECEIVED-1,'through':NOW+10})
    assert IA.committed_through_print(root, cand, NOW) is False


def test_late_evaluation_keeps_stale_mint_refusal(tmp_path):
    data = production(tmp_path)
    (_, _, evidence), _ = run(data, now_fn=lambda:NOW+E.SOL_MAX_AGE_S+1)
    assert evidence['checks']['token_safety']['reason'] == 'mint_stale'


def test_future_mint_receipt_is_never_backdated_to_emission(tmp_path):
    data = production(tmp_path)
    class FutureRpc(MintRpc):
        def clock(self):
            return NOW+1
    (_, _, evidence), seen = run(data, rpc=FutureRpc())
    assert seen['mint_known_at'] == NOW+1
    assert evidence['checks']['token_safety']['reason'] == 'mint_future'


@pytest.mark.parametrize('age,reason', [(E.SOL_MAX_AGE_S+1,'sol_usd_stale'), (-1,'field_value')])
def test_bad_frozen_fx_age_cannot_be_replaced_with_evaluation_time(tmp_path, age, reason):
    from test_memecoin_desk_contract import rehash
    data = production(tmp_path)
    data[1]['snapshot']['fields']['pool_state']['value']['sol_age_s'] = age
    cand = rehash(data[1])
    data = (data[0],cand,*data[2:])
    (verdict, got, evidence), _ = run(data)
    assert verdict != 'PASS'
    assert reason in {got, *(row.get('reason') for row in evidence.get('checks',{}).values())}
