"""Import lifecycle and atomic decisions against disposable real PostgreSQL."""
import json
from test_brain_graph_postgres import pytestmark, pg,sql,row,call


def test_closed_import_and_uuid_replay_preserve_status(pg):
    old=row('This retired rule must never be revived.')
    old.update(author='import',author_session='import:v1',status='superseded',
               valid_from='2026-08-01T00:00:00Z',valid_to='2026-08-02T00:00:00Z')
    saved=json.loads(sql(pg,'SET ROLE service_role; '+call(old)).stdout)
    assert saved['status']=='superseded' and saved['expired_at']
    replay=json.loads(sql(pg,call(dict(old,status='unverified',valid_to=None))).stdout)
    assert replay['id']==saved['id'] and replay['status']=='superseded'
    broken=dict(row('A known broken reference stays explicitly broken.'),author='import',author_session='import:v1',status='evidence_broken')
    assert json.loads(sql(pg,call(broken)).stdout)['status']=='evidence_broken'
    assert sql(pg,'SELECT count(*) FROM brain_facts;').stdout.strip()=='2'


def test_import_options_require_import_session_and_decisions_commit_together(pg):
    bad=dict(row('An ordinary client must not inject imported status.'),status='superseded',valid_to='2026-08-02T00:00:00Z')
    assert sql(pg,call(bad),ok=False).returncode
    old=row('First original claim about shared git state.'); sql(pg,call(old))
    new=row('Refinement of shared git state evidence.')
    action=[dict(id=old['id'],version=1,verdict='refines')]
    sql(pg,"""CREATE FUNCTION fail_judgement() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'test_judgement_failure'; END $$;
      CREATE TRIGGER fail_judgement BEFORE INSERT ON brain_judgements FOR EACH ROW EXECUTE FUNCTION fail_judgement();""")
    assert sql(pg,call(new,action),ok=False).returncode
    assert sql(pg,'SELECT count(*) FROM brain_facts;').stdout.strip()=='1'
    assert sql(pg,'SELECT count(*) FROM brain_links;').stdout.strip()=='0'
    sql(pg,'DROP TRIGGER fail_judgement ON brain_judgements;')
    sql(pg,call(new,action))
    assert sql(pg,"SELECT verdict FROM brain_judgements;").stdout.strip()=='refines'
    assert sql(pg,"SELECT rel FROM brain_links;").stdout.strip()=='derived_from'
