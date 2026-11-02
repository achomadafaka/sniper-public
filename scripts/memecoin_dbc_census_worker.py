#!/usr/bin/env python3
"""DBC shadow census worker (Gamma-signed design 2026-10-23). Own process, own Shyft gRPC connection.

Never imports the runner, never opens the ledger, never reads a funded RPC lane. Writes one tape,
data/research/dbc-census/tape-<run_id>.sqlite. Research reads (pool facts, coverage check) go through the
shared research pacer on the first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry, stop on a 429, and the
coverage check is capped at 150 calls. Run it only with the operator's yes, niced:

    nice -n 19 python3 scripts/memecoin_dbc_census_worker.py --env-root=/home/bigpapi/Desktop/Sniper --window-s 21600
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

ENV_ROOT = next((a.split('=', 1)[1] for a in sys.argv[1:] if a.startswith('--env-root=')), None) or str(state_root())
load_env_files(ENV_ROOT)    # a worktree has no .env; --env-root=/home/bigpapi/Desktop/Sniper points at the real one

from sniper.research import dbc_census as C                          # noqa: E402
from sniper.research import dbc_census_tape as T                     # noqa: E402

log = logging.getLogger('dbc_census')
HOST = os.environ.get('MEMECOIN_DBC_CENSUS_GRPC_HOST', 'grpc.fra.shyft.to')
COVERAGE_MAX_CALLS = 150


def research_rpc(url, pacer):
    """JSON-RPC behind the shared research pacer. A 429 pauses every reader of the key and raises."""
    def call(method, params):
        pacer.acquire()
        req = urllib.request.Request(url, json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode(),
                                     {'content-type': 'application/json', 'user-agent': 'Mozilla/5.0 Sniper/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
        except Exception as exc:
            if getattr(exc, 'code', None) == 429:
                pacer.pause(60.0)
            raise
        if 'error' in body:
            raise RuntimeError('rpc error %r' % body['error'])
        return body['result']
    return call


def build_request(pb):
    txs = {'dbc': pb.SubscribeRequestFilterTransactions(vote=False, account_include=[C.PROGRAM])}
    return pb.SubscribeRequest(transactions=txs, commitment=pb.CommitmentLevel.CONFIRMED)


async def stream(census, token, stop):
    import grpc
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    req = build_request(pb)

    async def reqs():
        yield req
        n = 0
        while not stop.is_set():
            await asyncio.sleep(10)
            n += 1
            yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=n))
    gaps = 0
    while not stop.is_set():
        try:
            async with grpc.aio.secure_channel(HOST + ':443', grpc.ssl_channel_credentials(), options=[
                    ('grpc.max_receive_message_length', 16 * 1024 * 1024), ('grpc.keepalive_time_ms', 10000)]) as ch:
                call = ch.stream_stream('/geyser.Geyser/Subscribe', request_serializer=pb.SubscribeRequest.SerializeToString,
                                        response_deserializer=lambda b: b)
                async for raw in call(reqs(), metadata=(('x-token', token),)):
                    census.push(raw, time.time())
                    if stop.is_set():
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:       # every failure is a visible gap
            name = type(exc).__name__
            T.record_skip(census.c, census.run_id, time.time(), 'stream_gap:' + name)
            census.c.commit()
            log.warning('dbc census stream error=%r', exc)
            if C.stream_error_is_fatal(exc):
                census.stop_reason = 'subscribe_refused:' + name
                stop.set()
                return
            if gaps >= C.MAX_RECONNECTS:
                census.stop_reason = 'reconnect_cap:' + name
                stop.set()
                return
            await asyncio.sleep(C.RECONNECT_BACKOFF_S[gaps])
            gaps += 1


async def processor(census, stop, end_ts):
    last = 0.0
    while not stop.is_set() and time.time() < end_ts:
        if census.ring:
            raw, at = census.ring.popleft()
            try:
                census.process(raw, at)
            except Exception:
                census.stats['process_errors'] += 1
                T.record_skip(census.c, census.run_id, time.time(), 'process_error')
                log.exception('dbc census frame failed')
        else:
            await asyncio.sleep(0.005)
        if time.time() - last >= 1:
            last = time.time()
            census.flush_drops()
            census.c.commit()


def resolver(path, run_id, rpc, stop, stats):
    """Own thread, own connection. A 429 or any transport error ends resolution (rows stay NULL)."""
    c = T.connect(path)
    k = C.Census(c, run_id)
    while not stop.is_set():
        try:
            n = k.resolve_pending(rpc)
            c.commit()
        except Exception as exc:
            T.record_skip(c, run_id, time.time(), 'resolver_stop:' + type(exc).__name__)
            c.commit()
            return
        stats.update(k.stats)
        if not n:
            stop.wait(5)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--window-s', type=float, default=6 * 3600)
    ap.add_argument('--no-resolve', action='store_true', help='no resolver thread: the desk shares the research key; resolve from the tape after the run')
    ap.add_argument('--no-coverage', action='store_true', help='skip the end-of-run research-key coverage check (run it separately when cleared)')
    ap.add_argument('--env-root', default=None, help='state root holding .env (read before import; use --env-root=PATH)')
    ap.add_argument('--out-dir', default=str(Path(ENV_ROOT) / 'data' / 'research' / 'dbc-census'))
    args = ap.parse_args(argv)
    token = os.environ.get('MEMECOIN_SHYFT_GRPC_TOKEN')
    urls = [u.strip() for u in (os.environ.get('MEMECOIN_RESEARCH_HELIUS_RPC_URLS') or '').split(',') if u.strip()]
    if not token or not urls:
        print('[dbc-census] need MEMECOIN_SHYFT_GRPC_TOKEN and MEMECOIN_RESEARCH_HELIUS_RPC_URLS', file=sys.stderr)
        return 2
    from sniper.services.memecoin_research_pacer import SharedPacer
    rpc = research_rpc(urls[0], SharedPacer.for_url(urls[0]))
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    run_id = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:6]
    path = str(Path(args.out_dir) / ('tape-%s.sqlite' % run_id))
    start = time.time()
    c = T.connect(path)
    T.start_run(c, run_id, start, args.window_s, C.SOURCE_FILTER)
    print(json.dumps(dict(run_id=run_id, pid=os.getpid(), start_ts=start, tape=path)), flush=True)
    census = C.Census(c, run_id)
    stop, rstats = threading.Event(), {}
    if not args.no_resolve:
        threading.Thread(target=resolver, args=(path, run_id, rpc, stop, rstats), daemon=True).start()

    async def both():
        task = asyncio.ensure_future(stream(census, token, stop))
        await processor(census, stop, start + args.window_s)
        stop.set()
        task.cancel()
    reason = 'window_end'
    census.stop_reason = None
    try:
        asyncio.run(both())
    except KeyboardInterrupt:
        reason = 'interrupted'
    stop.set()
    reason = census.stop_reason or reason
    census.flush_drops()
    T.stop_run(c, run_id, reason, min(time.time(), start + args.window_s))
    # coverage: 3 random 5-minute windows inside the observed span; any rpc error ends it as 'partial'
    import random
    span_end = min(time.time(), start + args.window_s)
    if span_end - start > 900 and not args.no_coverage:
        wins = [(lo, lo + 300) for lo in sorted(random.uniform(start + 60, span_end - 360) for _ in range(3))]
        cov = C.coverage_check(c, run_id, rpc, wins, max_calls=COVERAGE_MAX_CALLS)
        c.execute('CREATE TABLE IF NOT EXISTS coverage(run_id TEXT, result TEXT)')
        c.execute('INSERT INTO coverage VALUES(?,?)', (run_id, json.dumps(cov, default=str)))
        c.commit()
        print(json.dumps(dict(coverage=cov['verdict'], calls=cov['calls'])), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
