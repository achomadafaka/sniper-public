from test_brain_graph_cli import _ranked_store,_fact
from bot.brain_graph.search import ask


def test_entity_expansion_never_resurrects_cut_vector_ids(monkeypatch):
    store=_ranked_store(monkeypatch)
    for i in range(7):_fact(store,'rule','Extra hub rule %d'%i,'2026-10-01T00:00:00+00:00')
    store.hubs=list(store.rows['brain_facts'])
    _fact(store,'finding','Octopus keyword hit','2026-10-01T00:00:00+00:00')
    entity=store.rows['brain_facts'][-1]['entity_ids']
    for fact in store.hubs:fact['entity_ids']=entity
    _fact(store,'finding','Related only by entity','2026-10-01T00:00:00+00:00')
    store.rows['brain_facts'][-1]['entity_ids']=entity
    stats={};result=ask(store,'octopus',limit=50,stats=stats)
    assert sum('hub rule' in f['statement'].lower() for f in result)==5
    assert any(f['statement']=='Related only by entity' for f in result)
    assert stats==dict(vector_only_seen=12,vector_only_cut=7,vector_only_cap=5)
