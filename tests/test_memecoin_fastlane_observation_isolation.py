"""Execute the runner's real pass body with controlled I/O boundaries."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def _pass_body():
    source = Path('scripts/memecoin_runner_loop.py')
    tree = ast.parse(source.read_text())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == '_pass')
    return compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec')


@pytest.fixture
def controlled_pass(monkeypatch):
    from sniper.services import memecoin_evm_post_close_marks as post
    events, prices, saved = [], [], []
    open_key, closed_key = ('4663', 'open'), ('4663', 'closed')
    live = {open_key: {'price': 2, 'source': 'fastlane_rpc'}}
    closed = {closed_key: {'price': 99, 'source': post.SOURCE},
              open_key: {'price': 1000, 'source': post.SOURCE}}

    def observe(*a, **kw):
        events.append('post_close')
        return closed

    def execute(*a, price_fn, **kw):
        events.append('funded_exit')
        prices.append(price_fn([]) if price_fn else None)

    monkeypatch.setattr(post, 'evm_post_close_marks', observe)
    resource = {'conn': None, 'refresh_fn': lambda *a, **kw: dict(live),
                'demand_fn': None, 'mid_observation_state': None,
                'now_fn': lambda: '2026-09-24T12:00:00+00:00',
                'filter_fn': lambda c, pm, at: dict(pm),
                'write_fn': lambda c, pm, at: (events.append('write'), saved.append(dict(pm))),
                'exit_fn': execute}
    namespace = {'contexts': {'4663': {}}, '_SOL_SWAP_MARKS': False, '_hot_tp': None,
                 '_p': lambda *a: None, '_paper_swap_marks': SimpleNamespace(publish=lambda *a, **kw: None),
                 'time': SimpleNamespace(monotonic=lambda: 0), 'MEMECOIN_FASTLANE_DECIDES': True,
                 '_fastlane_state': None, '_funded_exit_lock': None,
                 'run_fastlane_exit_sweep': lambda *, sweep_fn, **kw: sweep_fn(),
                 'finish_db_cycle': lambda c: True}
    exec(_pass_body(), namespace)
    return namespace['_pass'], resource, events, prices, saved, live, open_key, closed_key


def test_post_close_marks_never_enter_funded_decision_map(controlled_pass):
    run, resource, events, prices, saved, live, open_key, closed_key = controlled_pass
    run(resource)
    assert prices == [live]
    assert saved[-1][open_key]['price'] == 2
    assert saved[-1][closed_key]['price'] == 99


def test_observation_reads_and_writes_wait_until_after_funded_exit(controlled_pass):
    run, resource, events, *_ = controlled_pass
    run(resource)
    assert events.index('funded_exit') < events.index('post_close')
    assert events.index('funded_exit') < events.index('write')


def test_missing_live_price_does_not_substitute_a_closed_position(controlled_pass):
    run, resource, events, prices, saved, live, *_ = controlled_pass
    live.clear()
    run(resource)
    assert prices == [None]


def test_runner_paces_from_provider_work_not_global_book(controlled_pass):
    run, resource, *_ = controlled_pass
    def refresh(*args, stats_out, **kwargs):
        stats_out.update(open_positions=2, deadline_positions=1)
        return {}
    resource['refresh_fn'] = refresh
    run(resource)
    assert resource['open_book'] == 1
