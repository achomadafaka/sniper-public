#!/usr/bin/env python3
"""One bounded native queue read per GPT role; --notify alerts Alpha at 80. Never drains queues."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import subprocess

from crew_session_bridge import GPT_ROLES, private, private_dir, read_json, valid_thread, write_json


def transition(count, notified):
    if count is None:
        return False, notified
    return count >= 80 and not notified, count >= 80


def depth(result):
    rows = result.get('data')
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('queue_response_unknown')
    ids = [r.get('id') if isinstance(r, dict) else None for r in rows]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('queue_response_unknown')
    # A full first page suffices to warn; a short incomplete page cannot prove recovery.
    if result.get('nextCursor') is not None and len(rows) < 80:
        raise ValueError('queue_depth_incomplete')
    return len(rows)


async def queue_depth(ws, thread):
    async def rpc(ident, method, params):
        await ws.send(json.dumps(dict(id=ident, method=method, params=params)))
        for _ in range(100):
            reply = json.loads(await ws.recv())
            if not isinstance(reply, dict):
                raise ValueError('queue_rpc_unknown')
            if reply.get('id') == ident:
                if 'error' in reply or not isinstance(reply.get('result'), dict):
                    raise ValueError('queue_rpc_unknown')
                return reply['result']
        raise ValueError('queue_notifications_unbounded')

    await rpc(1, 'initialize', {'clientInfo': {'name': 'sniper_queue_alarm', 'version': '1'},
                               'capabilities': {'experimentalApi': True}})
    await ws.send(json.dumps({'method': 'initialized'}))
    return depth(await rpc(2, 'thread/queue/list', {'threadId': thread, 'limit': 100}))


async def collect(routes, socket_path):
    import websockets
    results = {}
    for role, route in routes.items():
        thread = valid_thread(route['thread'])
        try:
            async with asyncio.timeout(3):
                async with websockets.unix_connect(str(socket_path), open_timeout=1,
                                                   close_timeout=.1, max_size=8 * 1024 * 1024) as ws:
                    count = await queue_depth(ws, thread)
            results[role] = {'thread': thread, 'depth': count, 'state': 'observed'}
        except (OSError, TimeoutError, ValueError, websockets.WebSocketException):
            results[role] = {'thread': thread, 'depth': None, 'state': 'unknown'}
    return results


def notify(results, previous, send):
    state = {}
    for role, row in results.items():
        prior = previous.get(role, {})
        notified = prior.get('thread') == row['thread'] and prior.get('notified') is True
        alert, retained = transition(row['depth'], notified)
        if alert:
            message = (f"Native queue warning: {role} thread {row['thread']} has >= {row['depth']} "
                       "pending messages (warn threshold 80). No queue changes performed; inspect before recovery.")
            retained = send(message)
        state[role] = dict(row, notified=retained)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--notify', action='store_true')
    args = parser.parse_args()
    root = Path.home() / '.cache/sniper-crew'
    try:
        routes = read_json(root / 'routes.json')['routes']
        if not isinstance(routes, dict) or any(r not in GPT_ROLES for r in routes):
            raise ValueError('queue_routes_unknown')
        # Separate process/lock; never block delivery or any trading service.
        private_dir(root)
        with os.fdopen(os.open(root / 'queue-alarm.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w') as lock:
            private(root / 'queue-alarm.lock')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            rows = asyncio.run(collect(routes, Path.home() / '.codex/app-server-control/app-server-control.sock'))
            if args.notify:
                path = root / 'queue-alarm.json'
                prior = read_json(path) if path.exists() else {}

                def send(message):
                    result = subprocess.run(['sniper-crew', 'send', '--to', 'Alpha', '--from', 'Codex'],
                                            input=message, capture_output=True, text=True, timeout=10)
                    return result.returncode == 0

                # Retention: overwrite one four-role snapshot; never store message bodies/history.
                write_json(path, notify(rows, prior, send))
            print(json.dumps(rows))
            return int(any(row['depth'] is None for row in rows.values()))
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'state': 'unknown', 'reason': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
