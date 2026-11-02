"""Supervisor-only client. Auth preparation and server identity come from trusted host bindings."""
import json
import math
import os
from pathlib import Path
import socket
import stat
import time
from uuid import UUID, uuid4

MAX_BYTES = 1_048_576
AUTH_ERRORS = ('claude_auth_near_expiry', 'claude_auth_unknown', 'auth_alert_required', 'auth_snapshot_path',
               'codex_busy', 'no_codex_login', 'codex_cleanup_unproven', 'codex_auth_writeback_refused',
               'codex_auth_snapshot_missing', 'trusted_auth_bindings_required', 'auth_private_state',
               'provider_paused', 'provider_busy')


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('not_regular')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('too_large')
        return raw
    finally:
        os.close(fd)


class Client:
    def __init__(self, config, *, clock, auth, verify_server):
        if not all(callable(fn) for fn in (clock, auth, verify_server)):
            raise ValueError('trusted_bindings_required')
        self.config, self.clock, self.auth, self.verify_server = config, clock, auth, verify_server

    def read_result(self, rid, receipt):
        if type(receipt) is not dict or receipt.get('request_id') != rid:
            raise ValueError('launcher_result_identity')
        if receipt.get('outcome') == 'existing_unknown':
            return {'call_error': 'existing_unknown', 'request_id': rid}
        if receipt.get('outcome') == 'refused':
            reason = receipt.get('reason')
            return {'call_error': reason if reason in AUTH_ERRORS else 'launcher_refused', 'request_id': rid}
        if (receipt.get('outcome') != 'success' or receipt.get('exit_code') != 0
                or receipt.get('output_file') != rid + '/result.json'):
            raise ValueError('launcher_result_identity')
        value = receipt.get('model_result')
        if type(value) is not dict:
            raise ValueError('result_not_object')
        return value

    def call(self, adapter, doc, *, deadline_at, request_id=None):
        now = self.clock()
        if (type(now) not in (int, float) or not math.isfinite(now)
                or type(deadline_at) not in (int, float) or not math.isfinite(deadline_at)):
            return {'call_error': 'clock_unknown'}
        if not 0 < deadline_at - now <= 120:
            return {'call_error': 'timeout'}
        if adapter not in ('claude_sdk', 'codex_exec') or type(doc) is not dict:
            return {'call_error': 'invalid_request'}
        started, budget = time.monotonic(), deadline_at - now
        rid = request_id or str(uuid4())
        try:
            if type(rid) is not str or str(UUID(rid)) != rid:
                raise ValueError()
            payload = json.dumps(doc, allow_nan=False).encode()
        except (TypeError, ValueError, AttributeError):
            return {'call_error': 'invalid_request'}
        if len(payload) > MAX_BYTES:
            return {'call_error': 'request_too_large'}
        root = Path(self.config['request_root'])
        if root.is_symlink():
            return {'call_error': 'request_root_invalid'}
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        parent = root / rid
        # One invocation per request ID. Ambiguous transport never silently dispatches a fresh retry.
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            return {'call_error': 'existing_unknown', 'request_id': rid}
        fd = os.open(parent / 'request.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(payload)
        message = dict(adapter_id=adapter, request_id=rid, request_file=rid + '/request.json', deadline_at=deadline_at)
        try:
            with self.auth(adapter, parent), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                left = budget - (time.monotonic() - started)
                if left <= 0:
                    return {'call_error': 'timeout', 'request_id': rid}
                conn.settimeout(left)
                conn.connect(self.config['socket_path'])
                if self.verify_server(conn) is not True:
                    return {'call_error': 'launcher_identity', 'request_id': rid}
                conn.sendall(json.dumps(message, allow_nan=False).encode() + b'\n')
                raw = bytearray()
                while not raw.endswith(b'\n'):
                    left = budget - (time.monotonic() - started)
                    if left <= 0:
                        raise TimeoutError()
                    conn.settimeout(left)
                    chunk = conn.recv(min(65536, MAX_BYTES + 1 - len(raw)))
                    if not chunk or len(raw) + len(chunk) > MAX_BYTES:
                        raise ValueError('launcher_response')
                    raw.extend(chunk)
                return self.read_result(rid, json.loads(raw))
        except (TimeoutError, socket.timeout):
            return {'call_error': 'timeout', 'request_id': rid}
        except (OSError, ValueError, TypeError) as exc:
            reason = getattr(exc, 'reason', None)
            if reason not in AUTH_ERRORS:
                reason = 'launcher_refused'
            return {'call_error': reason, 'request_id': rid}


class Transport:
    """Per-provider injected adapter; no global patches or provider fallback."""
    def __init__(self, provider, client, *, clock):
        if provider not in ('claude', 'codex') or not callable(clock):
            raise ValueError('transport_configuration')
        self.adapter = {'claude':'claude_sdk', 'codex':'codex_exec'}[provider]
        self.client, self.clock = client, clock

    def __call__(self, system, user, *, model, deadline_s, schema_obj):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            return {'call_error':'clock_unknown'}
        return self.client.call(self.adapter, dict(system=system, user=user, model=model,
                                deadline_s=deadline_s, schema=schema_obj), deadline_at=now + deadline_s)
