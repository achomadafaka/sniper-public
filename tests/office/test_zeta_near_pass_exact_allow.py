import hashlib
import json

import pytest

from office.test_sheet_near_pass import env, near, nsheet
from office.test_desk_near_pass import desk, checks, NOW, AT
from sniper.office import desk_near_pass as N
from sniper.office.tools import sheet as T


VERSION='near-pass-allow-v1'
FROZEN_HASH='57e5abc3c23668825f1bc1088343a1af935f3cd77915f7ee5855fd5ae24bf020'
ALLOWED={
 'dump_group':['dump_group_unknown','partial_history'],
 'cluster':['cluster_unknown_dependency','cluster_unknown_dependency:owner_program','cluster_unknown_dependency:quota'],
 'holders':['creator_unknown','supply_unknown'],
 'chart':['age_unknown','age_unknown_decimals_post_cut_immutable','chart_60s_prints_lt_2','chart_60s_prints_lt_2_decimals_post_cut_immutable'],
}


@pytest.mark.parametrize('check,reason',[(k,v) for k,values in ALLOWED.items() for v in values])
def test_every_frozen_literal_is_admitted(check,reason):
 assert T.near_pass_blockers(near({check:{'status':'unknown','reason':reason}}))==[
  {'check':check,'status':'unknown','reason':reason}]


@pytest.mark.parametrize('check,reason',[
 ('chart','age_unknown_anything'),('chart','chart_60s_prints_lt_2_x'),
 ('cluster','cluster_unknown_dependency:trace_error'),('holders','rpc_error_timeout'),
 ('dump_group','partial_history_more'),('cluster','cluster_unknown_dependency:quota:more'),
 ('holders','creator_unknown_extra'),
])
def test_unlisted_variants_are_not_silently_admitted(check,reason):
 assert T.near_pass_blockers(near({check:{'status':'unknown','reason':reason}})) is None


def test_even_a_passing_unknown_check_name_refuses():
 row=near();row['evidence']['checks']['unlisted']={'status':'pass','reason':'ok'}
 assert T.near_pass_blockers(row) is None


def test_in_code_literal_contract_matches_gamma_freeze():
 contract={'allow_unknown':{k:sorted(v) for k,v in T.NEAR_PASS_ALLOWED.items()},
  'checks':sorted(set(T.NEAR_PASS_ALLOWED)|set(T.NEAR_PASS_REQUIRED)),
  'must_pass':sorted(T.NEAR_PASS_REQUIRED),'version':getattr(T,'NEAR_PASS_ALLOW_VERSION',None)}
 assert hashlib.sha256(json.dumps(contract,sort_keys=True,separators=(',',':')).encode()).hexdigest()==FROZEN_HASH


def test_allow_version_is_bound_to_cache_and_sheet_body(env):
 conn,_=env;r=nsheet(conn,near())
 assert r['state']=='known'
 assert r['request'].get('allow_version')==r['sheet'].get('allow_version')==VERSION
 without=dict(r['request']);without.pop('allow_version')
 assert T.cache_key(without)!=r['cache_key']
 assert T._sha(r['sheet'])==r['sheet_hash']
 without_body=dict(r['sheet']);without_body.pop('allow_version')
 assert T._sha(without_body)!=r['sheet_hash']


def test_scout_ticket_metadata_carries_frozen_allow_version(tmp_path):
 conn=desk(tmp_path,[('t','hard_filter','SKIP','unknown:dump_group:dump_group_unknown',
  checks(dump_group={'status':'unknown','reason':'dump_group_unknown'}),AT)])
 out=N.list_near_pass(conn,now=NOW)
 assert out['items'][0].get('allow_version')==VERSION


@pytest.mark.parametrize('missing',sorted(set(ALLOWED)|set(T.NEAR_PASS_REQUIRED)))
def test_all_seven_checks_are_mandatory(missing):
 row=near()
 row['evidence']['checks']={k:{'status':'pass','reason':'ok'} for k in set(ALLOWED)|set(T.NEAR_PASS_REQUIRED)}
 row['evidence']['checks']['dump_group']={'status':'unknown','reason':'dump_group_unknown'}
 del row['evidence']['checks'][missing]
 assert T.near_pass_blockers(row) is None
