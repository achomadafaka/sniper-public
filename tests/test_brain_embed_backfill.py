"""Bounded backfill behavior with no model, database, or network dependency."""
import importlib.util
from pathlib import Path

import pytest

from bot.brain_graph.store import BrainUnavailable

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location('brain_embed_backfill', ROOT / 'scripts/brain_embed_backfill.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Store:
    def __init__(self):
        self.rows = {t: [] for t in ('brain_facts', 'brain_entities', 'brain_episodes')}
        self.calls = []

    def rows_page(self, table, *, select, filters, after_id, limit):
        assert ('embedding', 'is', 'null') in filters
        return [r for r in self.rows[table] if r.get('embedding') is None and
                (after_id is None or r['id'] > after_id)][:limit]

    def _call(self, method, path, args):
        assert (method, path) == ('POST', 'rpc/brain_set_embeddings')
        self.calls.append(args['p_rows'])
        for item in args['p_rows']:
            next(r for r in self.rows[item['table']] if r['id'] == item['id'])['embedding'] = item['embedding']
        return len(args['p_rows'])


def test_batches_missing_rows_and_resumes_global_limit():
    mod, store, sleeps = module(), Store(), []
    store.rows['brain_facts'] = [dict(id=str(i), statement=f'Fact {i}', detail='context') for i in range(5)]
    store.rows['brain_facts'][1]['embedding'] = '[existing]'
    store.rows['brain_entities'] = [dict(id='e', name='entity', summary=None)]
    store.rows['brain_episodes'] = [dict(id='p', body='source body')]
    texts = []
    def embed(batch):
        texts.extend(batch)
        return [[0.25] * 384 for _ in batch]
    result = mod.run(store, embed_fn=embed, limit=3, batch_size=2, sleep_fn=sleeps.append)
    assert result['written'] == 3 and result['selected'] == 3
    assert [len(call) for call in store.calls] == [2, 1]
    assert len(sleeps) >= 2 and all(s >= .2 for s in sleeps)
    result = mod.run(store, embed_fn=embed, limit=10, batch_size=2, sleep_fn=sleeps.append)
    assert result['written'] == 3
    assert len(texts) == 6 and 'source body' in texts and 'entity' in texts
    assert store.rows['brain_facts'][1]['embedding'] == '[existing]'
    assert mod.run(store, embed_fn=lambda _: pytest.fail('loaded model on empty corpus'), sleep_fn=sleeps.append)['written'] == 0


def test_only_live_facts_are_embedded():
    # 2026-10-02: 8,008 of 8,572 facts without a vector were superseded or retracted (the hourly
    # wallet_link_graph run supersedes ~200 measurements a run). brain_match_facts never returns
    # them, so embedding them only spends the run's limit.
    from brain_graph_fake_store import FakeStore
    mod, store = module(), Store()
    store.rows_page = lambda table, **kw: FakeStore.rows_page(store, table, **kw)
    store.rows['brain_facts'] = [
        dict(id='a', statement='live', valid_to=None, status='unverified'),
        dict(id='b', statement='old', valid_to='2026-10-01T00:00:00Z', status='superseded'),
        dict(id='c', statement='withdrawn', valid_to='2026-10-01T00:00:00Z', status='retracted')]
    texts = []
    mod.run(store, embed_fn=lambda batch: texts.extend(batch) or [[1.] * 384 for _ in batch],
            sleep_fn=lambda _: None)
    assert texts == ['live']


def test_transient_rpc_failure_retries_with_backoff_then_stops():
    mod, store, sleeps = module(), Store(), []
    store.rows['brain_facts'] = [dict(id='a', statement='A real claim')]
    calls = []
    def fail(*args):
        calls.append(args)
        raise BrainUnavailable('private upstream detail')
    store._call = fail
    with pytest.raises(BrainUnavailable):
        mod.run(store, embed_fn=lambda texts: [[1.] * 384 for _ in texts], sleep_fn=sleeps.append)
    assert len(calls) == 3
    assert .5 in sleeps and 1. in sleeps
    assert 'embedding' not in store.rows['brain_facts'][0]


@pytest.mark.parametrize('vectors', [[], [[1.]], [[float('nan')] * 384]])
def test_bad_model_output_cannot_persist_partial_batch(vectors):
    mod, store = module(), Store()
    store.rows['brain_facts'] = [dict(id='a', statement='A real claim')]
    with pytest.raises(ValueError):
        mod.run(store, embed_fn=lambda _: vectors, sleep_fn=lambda _: None)
    assert store.calls == []


def test_process_limits_are_set_before_loading_model(monkeypatch):
    mod, events = module(), []
    monkeypatch.setattr(mod.os, 'getpriority', lambda *_: 7)
    monkeypatch.setattr(mod.os, 'setpriority', lambda *args: events.append(args))
    monkeypatch.setenv('BRAIN_EMBED_THREADS', '64')
    mod.configure_process()
    assert events == [(mod.os.PRIO_PROCESS, 0, 19)]
    assert mod.os.environ['BRAIN_EMBED_THREADS'] == '4'
    assert mod.os.environ['HF_HUB_OFFLINE'] == '1'


def test_real_rest_store_preserves_zero_batch_count_on_replay():
    from bot.brain_graph.store import RestStore
    mod = module()
    answers = iter([[dict(id='a', statement='A claim with concurrent embedding')], 0])
    def request(method, path, body, headers, timeout):
        return next(answers, [])
    result = mod.run(RestStore(request=request), limit=1,
                     embed_fn=lambda _: [[1.] * 384], sleep_fn=lambda _: None)
    assert result['selected'] == 1 and result['written'] == 0
