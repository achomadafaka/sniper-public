"""Queue pressure is independent of delivery and never authorizes queue mutation."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'tools/crew'
sys.path.insert(0, str(ROOT))


def alarm():
    spec = importlib.util.spec_from_file_location('queue_alarm', ROOT / 'queue_alarm.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_threshold_recovery_unknown_and_thread_replacement():
    a = alarm()
    assert a.transition(79, False) == (False, False)
    assert a.transition(80, False) == (True, True)
    assert a.transition(100, True) == (False, True)
    assert a.transition(None, True) == (False, True)
    assert a.transition(0, True) == (False, False)
    assert a.transition(80, False) == (True, True)


@pytest.mark.parametrize('result', [{}, {'data': None}, {'data': [{}]},
                                    {'data': [{'id': 'same'}] * 2}])
def test_invalid_response_is_unknown(result):
    with pytest.raises(ValueError):
        alarm().depth(result)


def test_pagination_below_threshold_is_unknown_not_zero():
    with pytest.raises(ValueError):
        alarm().depth({'data': [], 'nextCursor': 'more'})
    assert alarm().depth({'data': [{'id': str(i)} for i in range(80)],
                          'nextCursor': 'more'}) == 80


def test_readonly_handshake_and_queue_request():
    import asyncio
    a = alarm()

    class WS:
        def __init__(self):
            self.sent = []

        async def send(self, value):
            import json
            self.sent.append(json.loads(value))

        async def recv(self):
            import json
            request = self.sent[-1]
            return json.dumps({'id': request['id'], 'result':
                               {} if request['id'] == 1 else {'data': []}})

    ws = WS()
    assert asyncio.run(a.queue_depth(ws, 'thread')) == 0
    assert [r['method'] for r in ws.sent] == ['initialize', 'initialized', 'thread/queue/list']
    assert ws.sent[-1]['params'] == {'threadId': 'thread', 'limit': 100}


def test_failed_delivery_retries_and_unknown_preserves_latch():
    a = alarm()
    row = {'Codex': {'thread': 'new', 'depth': 80, 'state': 'observed'}}
    failed = a.notify(row, {}, lambda message: False)
    assert failed['Codex']['notified'] is False
    sent = []
    notified = a.notify(row, failed, lambda message: sent.append(message) or True)
    assert len(sent) == 1 and notified['Codex']['notified'] is True
    assert a.notify(row, notified, lambda message: pytest.fail('duplicate')) == notified
    unknown = {'Codex': dict(row['Codex'], depth=None, state='unknown')}
    assert a.notify(unknown, notified, lambda message: pytest.fail('unknown'))['Codex']['notified'] is True
    replacement = {'Codex': dict(row['Codex'], thread='replacement')}
    a.notify(replacement, notified, lambda message: sent.append(message) or True)
    assert len(sent) == 2


@pytest.mark.parametrize('raw', ['null', '[]', '"text"', '7', 'true'])
def test_nonobject_reply_is_unknown(raw):
    import asyncio
    a = alarm()

    class WS:
        async def send(self, value):
            pass

        async def recv(self):
            return raw

    with pytest.raises(ValueError, match='queue_rpc_unknown'):
        asyncio.run(a.queue_depth(WS(), 'thread'))


def test_collect_continues_after_nonobject_reply(monkeypatch):
    import asyncio
    import json
    import websockets
    a = alarm()

    class WS:
        def __init__(self, malformed):
            self.malformed = malformed
            self.request = None

        async def send(self, value):
            self.request = json.loads(value)

        async def recv(self):
            if self.malformed:
                return 'null'
            return json.dumps({'id': self.request['id'], 'result':
                               {} if self.request['id'] == 1 else {'data': []}})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    connections = iter([WS(True), WS(False)])
    monkeypatch.setattr(websockets, 'unix_connect', lambda *args, **kwargs: next(connections))
    routes = {'Codex': {'thread': '01a1883b-771d-7f52-af43-8b960b5a6617'},
              'Zeta': {'thread': '01a18837-401d-78c0-89b5-99a85cdfc702'}}
    rows = asyncio.run(a.collect(routes, '/unused'))
    assert rows['Codex']['state'] == 'unknown' and rows['Codex']['depth'] is None
    assert rows['Zeta']['state'] == 'observed' and rows['Zeta']['depth'] == 0
