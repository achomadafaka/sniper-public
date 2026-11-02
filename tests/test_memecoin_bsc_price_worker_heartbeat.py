"""Heartbeat observes the BSC memo without reading a price or changing funded cadence."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import memecoin_runner_loop as loop


def status(contexts, enabled):
    tree = ast.parse(Path(loop.__file__).read_text())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == '_fastlane_status')
    scope = dict(contexts=contexts, _FASTLANE=enabled,
                 _fastlane_state=SimpleNamespace(snapshot=lambda **kw: {'status': 'healthy'}),
                 _fastlane_stale_s=10, _hot_tp=None, _source_mirror=None,
                 _mid_observation_state=SimpleNamespace(snapshot=lambda **kw: {}))
    exec(compile(ast.Module(body=[node], type_ignores=[]), loop.__file__, 'exec'), scope)
    return scope['_fastlane_status']()


@pytest.mark.parametrize('enabled', [True, False])
def test_worker_status_is_published_without_calling_the_price_reader(enabled):
    class Peek:
        def __call__(self):
            pytest.fail('heartbeat must never read a price')

        def status(self):
            return {'thread_alive': True, 'ready': True, 'last_error': None}

    got = status({'56': {'quote_price_peek_fn': Peek()}}, enabled)
    assert got['bsc_price_preparation'] == {
        'thread_alive': True, 'ready': True, 'last_error': None}
    assert got['status'] == ('healthy' if enabled else 'disabled')


@pytest.mark.parametrize('contexts', [{}, {'56': {}}, {'4663': {'quote_price_peek_fn': object()}}])
def test_missing_bsc_status_stays_unknown(contexts):
    assert status(contexts, True)['bsc_price_preparation'] == {
        'thread_alive': None, 'ready': None, 'last_error': 'unavailable'}


def test_status_failure_cannot_abort_runner_publication_or_leak_error_text():
    class Peek:
        def status(self):
            raise OSError('private provider details')
    assert status({'56': {'quote_price_peek_fn': Peek()}}, True)['bsc_price_preparation'] == {
        'thread_alive': None, 'ready': None, 'last_error': 'OSError'}
