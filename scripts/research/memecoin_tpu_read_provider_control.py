#!/usr/bin/env python3
"""Bounded, private, health-only TPU read-source control. Never submits transactions."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.memecoin_runtime_verify import FULL_SHA, load_manifest, verify_sidecar
from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
from sniper.execution.solana_tpu_sidecar import _default_connect, _send_all, encode_frame, read_message


def _http_read(method, url, *, body, timeout):
    import httpx
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        with client.stream(method, url, content=body, headers={'Content-Type':'application/json', 'Accept-Encoding':'identity'}) as response:
            if response.status_code != 200:
                return response.status_code, None, None
            if response.headers.get('Content-Encoding', 'identity').strip().lower() != 'identity':
                raise RuntimeError('unexpected response encoding')
            chunks = bytearray()
            for chunk in response.iter_raw():
                if time.monotonic() >= deadline:
                    raise RuntimeError('response deadline')
                chunks.extend(chunk)
                if len(chunks) > 4096:
                    raise RuntimeError('response too large')
            return response.status_code, json.loads(chunks), None


def _http(method, url, *, body, timeout):
    # httpcore caches inactivity timeouts; the owned worker bounds DNS/TLS/headers/body together.
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--identity-http'],
            input=json.dumps({'url': url, 'timeout': timeout}), text=True, env={},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=min(2.0, timeout), check=False)
        if result.returncode != 0:
            raise RuntimeError()
    except Exception:
        raise RuntimeError('TPU control identity unavailable') from None
    return 200, {'jsonrpc': '2.0', 'id': 'tpu-control-genesis',
                 'result': SOLANA_MAINNET_GENESIS}, None


def _identity_http_main():
    try:
        request = json.loads(sys.stdin.read(8192))
        timeout = request['timeout']
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 2:
            raise ValueError()
        def http(method, url, *, body, timeout=None):
            return _http_read(method, url, body=body, timeout=request['timeout'])
        verify_control_identity(request['url'], http=http)
    except Exception:
        raise SystemExit(1) from None


def verify_control_identity(rpc_url, *, http):
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(rpc_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        request = {'jsonrpc':'2.0', 'id':'tpu-control-genesis', 'method':'getGenesisHash', 'params':[]}
        status, payload, error = http('POST', rpc_url, body=json.dumps(request), timeout=2.0)
        if (status != 200 or error is not None or not isinstance(payload, dict)
                or payload.get('id') != request['id'] or 'error' in payload
                or payload.get('result') != SOLANA_MAINNET_GENESIS):
            raise ValueError()
    except Exception:
        raise RuntimeError('TPU control identity unavailable') from None


def _release_head(code, timeout):
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=code,
                                text=True, capture_output=True, timeout=timeout, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def run_control(*, code_root, manifest_path, rpc_env_name, duration_s, output_path,
                spawn=None, connect=None, http=None, clock=time, native_head_fn=None):
    started = clock.monotonic()
    if not 0 < duration_s <= 20:
        raise RuntimeError('TPU control duration must be within 20 seconds')
    deadline = started + duration_s - min(1.0, duration_s / 4)
    code = Path(code_root).resolve()
    manifest = load_manifest(manifest_path)
    if (not manifest or not FULL_SHA.fullmatch(str(manifest.get('sha','')))
            or Path(str(manifest.get('release_path',''))).resolve() != code
            or _release_head(code, min(2.0, max(0.01, deadline-clock.monotonic()))) != manifest['sha']
            or verify_sidecar(manifest.get('sidecar'), code, manifest['sha'])):
        raise RuntimeError('TPU control artifact identity unavailable')
    if clock.monotonic() >= deadline:
        raise RuntimeError('TPU control deadline expired')
    rpc_url = os.environ.get(rpc_env_name, '')
    def identity_http(method, url, *, body, timeout):
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise RuntimeError('deadline')
        return (http or _http)(method, url, body=body, timeout=min(timeout, remaining))
    verify_control_identity(rpc_url, http=identity_http)
    proc = None
    samples = []
    private = tempfile.TemporaryDirectory(prefix='tpu-control-')
    try:
        directory = private.name
        socket_path = str(Path(directory) / 'health.sock')
        env = {'SNIPER_TPU_RPC_URL':rpc_url, 'SNIPER_TPU_SOCKET_PATH':socket_path,
               'SNIPER_TPU_PARENT_PID':str(os.getpid())}
        if clock.monotonic() >= deadline:
            raise RuntimeError('deadline')
        proc = (spawn or subprocess.Popen)([manifest['sidecar']['path']], env=env,
            cwd=str(code), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while clock.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('child exited')
            conn = None
            try:
                timeout = min(0.5, deadline-clock.monotonic())
                conn = (connect or _default_connect)(socket_path, timeout)
                conn.settimeout(timeout)
                request_id = uuid.uuid4().hex
                request = {'request_id':request_id, 'health':True}
                native_head, head_age = None, None
                if native_head_fn is not None:
                    try:
                        slot, stamp = native_head_fn()
                        age = clock.monotonic() - stamp
                        if (type(slot) is int and slot > 0 and type(stamp) in (int, float)
                                and math.isfinite(age) and 0 <= age <= 1):
                            native_head, head_age = slot, age
                            request['min_context_slot'] = slot
                    except Exception:
                        pass  # Missing/untrusted head remains unknown; never invent a slot.
                _send_all(conn, encode_frame(request))
                frame_deadline = min(deadline, clock.monotonic() + 0.5)
                def recv(n):
                    remaining = frame_deadline - clock.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    conn.settimeout(remaining)
                    return conn.recv(n)
                reply = read_message(recv, 4096)
                if (not isinstance(reply,dict) or reply.get('request_id') != request_id
                        or type(reply.get('ready')) is not bool
                        or type(reply.get('leader_count')) is not int
                        or type(reply.get('leader_slot')) is not int):
                    raise RuntimeError('invalid health')
                if not reply['ready'] or reply['leader_count'] <= 0:
                    raise RuntimeError('unready')
                if samples and reply['leader_slot'] < samples[-1]['leader_slot']:
                    raise RuntimeError('regressing schedule')
                lag = max(0, native_head - reply['leader_slot']) if native_head is not None else None
                if lag is not None and lag > 2:
                    raise RuntimeError('native head alignment')
                samples.append({'ready':True, 'elapsed_s':round(clock.monotonic()-started,3),
                                'leader_count':reply['leader_count'], 'leader_slot':reply['leader_slot'],
                                'native_head_slot':native_head, 'routing_slot_lag':lag,
                                'native_head_age_ms':round(head_age * 1000, 3) if head_age is not None else None})
            except OSError:
                if samples:
                    raise RuntimeError('socket lost') from None
            finally:
                if conn is not None:
                    conn.close()
            clock.sleep(min(0.25, max(0,deadline-clock.monotonic())))
        if len(samples) < 2 or samples[-1]['leader_slot'] <= samples[0]['leader_slot']:
            raise RuntimeError('frozen schedule')
        aligned = (all(s['native_head_slot'] is not None for s in samples)
                   and samples[-1]['native_head_slot'] > samples[0]['native_head_slot'])
        result = {'ok':True, 'cluster':'endpoint-reported-mainnet', 'submitted_count':0,
                  'sha':manifest['sha'], 'child_pid':proc.pid, 'sidecar_sha256':manifest['sidecar']['sha256'],
                  'cargo_lock_sha256':manifest['sidecar']['cargo_lock_sha256'],
                  'samples':samples, 'independent_schedule_verified':False,
                  'native_head_alignment_verified':aligned,
                  'alignment_basis':'health_native_minimum_within_two_slots' if aligned else 'unknown',
                  'send_guard_verified':False}
    except Exception as exc:
        reason = str(exc)
        if reason not in {'deadline', 'child exited', 'invalid health', 'unready',
                          'socket lost', 'regressing schedule', 'frozen schedule', 'native head alignment'}:
            reason = 'transport unavailable'
        raise RuntimeError('TPU control unavailable: ' + reason) from None
    finally:
        try:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=0.5)
        except Exception:
            raise RuntimeError('TPU control cleanup unavailable') from None
        finally:
            private.cleanup()
    Path(output_path).write_text(json.dumps(result, indent=2)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--rpc-env-name', required=True)
    parser.add_argument('--duration-s', type=float, default=20)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        run_control(code_root=args.code_root, manifest_path=args.manifest,
                    rpc_env_name=args.rpc_env_name, duration_s=args.duration_s, output_path=args.output)
    except RuntimeError as exc:
        parser.exit(1, str(exc) + '\n')
    except Exception:
        parser.exit(1, 'TPU control unavailable\n')

if __name__ == '__main__':
    if sys.argv[1:] == ['--identity-http']:
        _identity_http_main()
    else:
        main()
