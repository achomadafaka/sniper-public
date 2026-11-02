import importlib.util

spec=importlib.util.spec_from_file_location('office_brain','scripts/office_brain.py')
B=importlib.util.module_from_spec(spec);spec.loader.exec_module(B)


def test_only_allowed_sections_and_kinds_and_never_a_mixed_fact():
    sections={'a':'memecoin/bot-research/mechanisms','b':'research/methods','c':'crew/alpha','d':'memecoinx','e':'memecoin/sizing'}
    rows=[dict(id=1,kind='finding',entity_ids=['a','b']),dict(id=2,kind='finding',entity_ids=['a','c']),dict(id=3,kind='finding',entity_ids=['c']),
          dict(id=4,kind='finding',entity_ids=[]),dict(id=5,kind='finding',entity_ids=['z']),dict(id=6,kind='finding',entity_ids=['d']),
          dict(id=7,kind='finding',entity_ids=['e']),dict(id=8,kind='decision',entity_ids=['a'])]
    assert [f['id'] for f in B.filter_facts(rows,sections)]==[1]


def test_note_is_in_the_office_namespace_and_crew_filter_drops_it(monkeypatch):
    from bot.brain_graph import api
    from bot.brain_graph.crew_filter import CrewFilter
    seen=[];monkeypatch.setattr(api,'add_episode',lambda store,kind,text,**k:seen.append(k))
    out=B.note(None,'head','hello',frozenset())
    assert out['state']=='recorded' and out['source_ref'].startswith('office:head:')
    assert CrewFilter.episodes([dict(source_ref=seen[0]['source_ref'],author=seen[0]['author'])])==[]


def test_misfiled_epoch_result_in_an_allowed_section_stays_out():
    sections={'a':'memecoin'}
    rows=[dict(id=1,kind='finding',entity_ids=['a'],statement='office-v2-paper-1 net was up'),dict(id=2,kind='finding',entity_ids=['a'],statement='x',detail='see the Look Record'),
          dict(id=3,kind='finding',entity_ids=['a'],statement='x',evidence=[dict(ref='office-paper-eval-1')]),dict(id=4,kind='finding',entity_ids=['a'],statement='plain')]
    assert [f['id'] for f in B.filter_facts(rows,sections)]==[4]


def test_main_note_path_uses_the_env_file_and_refuses_a_secret(tmp_path,monkeypatch,capsys):
    """Through main, not note(): a missing argument there once went unseen."""
    from bot.brain_graph import store as St
    env=tmp_path/'e';env.write_text('SOME_API_KEY=abcdefghijklmnopqrstuvwxyz123456\n')
    calls=[]
    class Fake:
        def insert(self,table,row):calls.append(row);return dict(id='1')
    monkeypatch.setattr(St,'RestStore',Fake)
    monkeypatch.setattr(B.sys,'argv',['x','--env-file',str(env),'note','--actor','head','plain note'])
    B.main();assert '"recorded"' in capsys.readouterr().out and calls[0]['source_ref'].startswith('office:head:')
    calls.clear()
    monkeypatch.setattr(B.sys,'argv',['x','--env-file',str(env),'note','--actor','head','leak abcdefghijklmnopqrstuvwxyz123456'])
    B.main();assert '"unknown"' in capsys.readouterr().out and not calls


def test_the_explore_epoch_name_is_hidden_too():
    assert [f for f in B.filter_facts([dict(id=1,kind='finding',entity_ids=['a'],statement='office-v2-explore-1 was up')],{'a':'memecoin'})]==[]


def test_a_fact_from_after_the_cut_and_an_unparsable_stamp_stay_out():
    rows=[dict(id=1,kind='finding',entity_ids=['a'],recorded_at='2099-01-01T00:00:00+00:00'),dict(id=2,kind='finding',entity_ids=['a'],valid_from='garbage'),dict(id=3,kind='finding',entity_ids=['a'],recorded_at='2020-01-01T00:00:00+00:00')]
    assert [f['id'] for f in B.filter_facts(rows,{'a':'memecoin'},now=2000000000.)]==[3]


def test_a_uuid_only_grade_linked_to_an_epoch_entity_stays_out():
    rows=[dict(id=1,kind='finding',entity_ids=['a','e'],statement='graded up')]
    assert B.filter_facts(rows,{'a':'memecoin','e':'memecoin'},epoch_entities=frozenset({'e'}))==[]
