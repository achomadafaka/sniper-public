from datetime import datetime,timezone
import pytest
from sniper.research.memecoin_strategy.exit_search import data_gate,simulate_exit_path
from sniper.services.memecoin_copy_arm import select_copy_arm,select_from_cells
from sniper.services.memecoin_profile_fit import profile_for
from sniper.research.memecoin_strategy.pattern_policies import rank_policies


def test_count_only_and_empty_path_are_rejected():
    p={'complete':True,'signal_at':'2026-01-01T00:00:00+00:00','entry_price':1.,'network_id':'56','mint':'m','observations':[]}
    assert not data_gate([p]*200,{'expected':200,'observed':200}).passed
    assert not simulate_exit_path(p,{'tp_pct':20,'sl_pct':20,'sl_confirm':1,'timecap_s':3600},{}).valid


def test_funded_fallback_makes_no_query_and_ignores_summary_cells():
    class Forbidden:
        def execute(self,*a,**k): raise AssertionError('entry made a research query')
    assert select_copy_arm(Forbidden(),'alice',chain='bsc') is None
    assert select_from_cells({'control':{'pnl':999,'closed':1000}},chain='bsc') is None


def test_legacy_fit_artifact_cannot_steer_policy(tmp_path):
    p=tmp_path/'old.json'; p.write_text('{"fit_window_end":"2026-01-01","artifacts":[{"source_user_id":"u","chain":"56","tp_pct":30}]}')
    assert profile_for('u','56',opened_at='2026-02-01',path=p) is None


def test_unsupported_policy_rank_never_publishes_winner():
    rows=[{'mint':str(i),'policy_name':name,'net_return':p} for i in range(30) for name,p in [('base',.1),('new',.9)]]
    r=rank_policies(rows,baseline_name='base')
    assert r['winner'] is None and r['status']=='blocked_protocol'
    assert r['policies']['new']['mean']==.9 and not r['policies']['new']['eligible']


def test_cached_ready_receipt_cannot_be_reused_or_approved():
    import json
    from test_memecoin_strategy_statistics_v0 import _conn,_candidate,_closed,ASOF
    from sniper.research.memecoin_strategy.statistics import candidate_read,approve_first_class_paper
    c=_conn(); candidate=_candidate(c)
    for i in range(40): _closed(c,candidate,i)
    c.commit(); read=candidate_read(c,candidate.candidate_id,ASOF)
    row=c.execute('SELECT * FROM memecoin_candidate_reads WHERE read_id=?',(read.read_id,)).fetchone()
    payload=json.loads(row['manifest_json']); payload['status']='ready'
    key=read.read_id
    if row['read_type']!='fixed_candidate':
        key='legacy-ready'
        c.execute("INSERT INTO memecoin_candidate_reads(read_id,candidate_id,read_type,asof,manifest_json,evidence_hash,created_at) VALUES (?,?,'fixed_candidate',?,?,?,?)",(key,candidate.candidate_id,ASOF,json.dumps(payload),row['evidence_hash'],ASOF)); c.commit()
    assert candidate_read(c,candidate.candidate_id,ASOF).status=='blocked_protocol'
    with pytest.raises(ValueError,match='unsupported memecoin performance'):
        approve_first_class_paper(c,candidate.candidate_id,ASOF,key)


def test_paper_grade_never_claims_sufficient_from_legacy_counts():
    from sniper.services.memecoin_paper_report import _section
    assert _section([],['control'])['decision_grade']=='blocked_protocol'


def test_legacy_standalone_requires_explicit_isolated_diagnostics():
    from sniper.research.wallet_execution.publication import legacy_report,diagnostic_mode,UnsupportedResearch
    from concurrent.futures import ThreadPoolExecutor
    wrapped=legacy_report(lambda: {'legacy':123})
    with pytest.raises(UnsupportedResearch): wrapped()
    with diagnostic_mode():
        assert wrapped()=={'legacy':123}
        with ThreadPoolExecutor() as pool:
            with pytest.raises(UnsupportedResearch): pool.submit(wrapped).result()
    with pytest.raises(UnsupportedResearch): wrapped()


def test_memecoin_performance_brain_ingress_requires_typed_proof():
    from bot.brain_graph.gate import validate_draft
    base={'author':'alpha','kind':'finding','statement':'Memecoin paper strategy is profitable.',
          'detail':'Net hypothetical return is positive.','section':'memecoin',
          'entities':[{'type':'doctrine','name':'wallet-execution-research-v1'}],
          'evidence':[{'type':'episode','ref':'00000000-0000-0000-0000-000000000001'}]}
    assert any('performance' in error for error in validate_draft(base,secret_values=set()))
    base['claim_scope']='operational'
    assert not any('performance' in error for error in validate_draft(base,secret_values=set()))
    base['section']='polymarket'; base.pop('claim_scope')
    assert not any('performance' in error for error in validate_draft(base,secret_values=set()))


def test_legacy_brain_writes_refuse_before_network(monkeypatch):
    from bot import supabase_helper as h
    from sniper.research.wallet_execution.publication import UnsupportedResearch
    monkeypatch.setattr(h,'get_memory',lambda *a,**k: (_ for _ in ()).throw(AssertionError('network used')))
    with pytest.raises(UnsupportedResearch):
        h.upsert_memory('knowledge','finding/memecoin-winning-arm','fake','Paper TP wins')
    with pytest.raises(UnsupportedResearch):
        h.save_finding('memecoin-wallet-ranking','fake','Counterfactual is profitable')


def test_explicit_publication_inventory_is_wrapped_as_code():
    import ast
    from pathlib import Path
    from sniper.research.wallet_execution.legacy_publishers import LEGACY_PUBLISHERS
    root=Path(__file__).resolve().parents[1]
    assert len(LEGACY_PUBLISHERS)>=40
    for path,names in LEGACY_PUBLISHERS:
        tree=ast.parse((root/path).read_text())
        calls=[n for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
               and isinstance(n.value.func,ast.Name) and n.value.func.id=='_quarantine_exports']
        assert calls and set(ast.literal_eval(calls[0].value.args[1]))==set(names),path
