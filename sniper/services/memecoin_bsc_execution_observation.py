"""Exact-hash EVM measurement, outside signing, submission and canonical accounting.

The context ring is the sender's only operation. A daemon publishes datagrams; the existing tape
owns BSC's separate read client/thread. RH reuses successful existing confirmation reads in the
publisher worker and never starts a receipt/head reader. Loss is a witness gap, never a send retry.
"""
from collections import OrderedDict, deque
from itertools import count
import json
import math
import os
from pathlib import Path
import re
import socket
import struct
import threading
import time
import uuid
from hashlib import sha256

from sniper.services import memecoin_fill_timing as ft
from sniper.services.memecoin_evm_post_trade import receipt_observation
from sniper.util.receipt_clock import clock_domain, elapsed_receipt_ms

_HASH = re.compile(r"0x[0-9a-f]{64}")
_MAX_RECORD = 32 * 1024


def _address(root, chain='56'):
    namespace = 'bsc-execution' if str(chain) == '56' else str(chain) + '-execution'
    return '\0sniper:' + namespace + ':' + sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]


def _identity(root, *, proc_root=Path('/proc')):
    """Read pinned runner PID/UID/start generation only on background/startup owners."""
    try:
        root, proc_root = Path(root).resolve(), Path(proc_root)
        manifest = json.loads((root / 'data/memecoin_deploy.json').read_text())
        release = Path(manifest['release_path']).resolve()
        sha = manifest['sha']
        if (release != Path(__file__).resolve().parents[2]
                or Path(manifest['state_root']).resolve() != root
                or not re.fullmatch(r'[0-9a-f]{40}', sha)):
            return None
        pid = int((root / 'data/memecoin_runner.pid').read_text())
        proc = proc_root / str(pid)
        args = (proc / 'cmdline').read_bytes().split(b'\0')
        expected = {str(release / 'scripts/memecoin_runner_loop.py').encode()}
        uid = proc.stat().st_uid
        start = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        if pid <= 0 or uid != os.getuid() or not expected.intersection(args[1:3]):
            return None
        return pid, uid, sha + ':' + str(pid) + ':' + start
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return None


class Publisher:
    """One context's lock-free bounded ring. Its worker owns serialization/socket I/O."""

    def __init__(self, *, generation, chain='56', clock_ns=time.monotonic_ns, boot_id=None):
        self.generation, self.clock_ns = generation, clock_ns
        self.chain = str(chain)
        if self.chain not in {'56', '4663'}:
            raise ValueError('unsupported execution measurement chain')
        self.producer_id = uuid.uuid4().hex
        self.boot_id = clock_domain() if boot_id is None else boot_id
        self.ring, self._sequence = deque(maxlen=32), count(1)
        self.overwritten, self.ipc_failures = 0, 0
        self.thread, self.worker_error = None, None
        self.initialization_error = None if generation is not None else 'pinned_runner_identity_unavailable'
        self._stop = threading.Event()

    def _append(self, kind, record):
        if len(self.ring) == self.ring.maxlen:
            self.overwritten += 1
        runner_generation = self.generation
        generation = runner_generation + ':' + self.producer_id if runner_generation is not None else None
        self.ring.append(dict(record, kind=kind, generation=generation,
                              runner_generation=runner_generation, producer_id=self.producer_id,
                              sequence=next(self._sequence), emitted_ns=self.clock_ns(),
                              boot_id=self.boot_id, chain_id=self.chain))

    def dispatch(self, attempt_id, tx_hash, receipt_clock=None, *, route_lane=None, venue=None,
                 quote_asset=None, ready_at_original_ingress=None, entry_preparation=None):
        self._append('dispatch', {'attempt_id': attempt_id, 'tx_hash': tx_hash,
                                  'receipt_clock': receipt_clock,
                                  'boundary': 'sender_call_after_durable_binding',
                                  'route_lane': route_lane, 'venue': venue, 'quote_asset': quote_asset,
                                  'ready_at_original_ingress': ready_at_original_ingress,
                                  'entry_preparation': entry_preparation})

    def booked(self, attempt_id, tx_hash, receipt, position_id):
        self._append('booked', {'attempt_id': attempt_id, 'tx_hash': tx_hash,
                                'position_id': position_id, 'receipt': {
                                    key: receipt.get(key) for key in
                                    ('transactionHash', 'blockNumber', 'blockHash', 'status', 'logs',
                                     'chainId', 'removed') if key in receipt}})

    def observed(self, attempt_id, tx_hash, receipt, *, observed_ns, boot_id):
        """Entry swap only: queue the stamp from an EXISTING successful confirmation read."""
        self._append('observed', {'attempt_id': attempt_id, 'tx_hash': tx_hash,
                                 'observed_ns': observed_ns, 'observed_boot_id': boot_id,
                                 'observation_kind': 'confirmation_observation', 'receipt': {
                                     key: receipt.get(key) for key in
                                     ('transactionHash', 'blockNumber', 'blockHash', 'status', 'logs',
                                      'chainId', 'removed') if key in receipt}})

    def record_signal(self, record):
        self._append('signal', record)

    def record_outcome(self, record):
        self._append('outcome', record)

    def record_gap(self, record):
        self._append('gap', record)

    def status(self):
        return dict(ready=self.generation is not None and self.worker_error is None,
                    alive=self.thread is not None and self.thread.is_alive(),
                    generation=self.generation, producer_id=self.producer_id,
                    initialization_error=self.initialization_error,
                    worker_error=self.worker_error, overwritten=self.overwritten,
                    ipc_failures=self.ipc_failures, queued=len(self.ring), updated_at=time.time())

    def start(self, path, *, identity_root=None):
        def run():
            timing = None
            try:
                timing = ft.connect()
                # RH has no IPC/head reader. This existing worker validates queued metadata only.
                local = (ReceiptObserver(timing, chain=self.chain, read=None, budget=lambda: False,
                                         boot_id=self.boot_id) if self.chain == '4663' else None)
                owner = 'publisher:' + self.chain + ':' + str(os.getpid()) + ':' + self.producer_id
                refresh = 0
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                    client.setblocking(False)
                    while not self._stop.is_set():
                        if time.monotonic() >= refresh:
                            if identity_root is not None:
                                identity = _identity(identity_root)
                                self.generation = identity[2] if identity and identity[0] == os.getpid() else None
                                self.initialization_error = (None if self.generation is not None else
                                                             'pinned_runner_identity_unavailable')
                            _health(timing, owner, dict(self.status(), chain_id=self.chain))
                            refresh = time.monotonic() + 1
                        try:
                            event = self.ring.popleft()
                        except IndexError:
                            self._stop.wait(0.02)
                            continue
                        event.update(published_ns=self.clock_ns(), overwritten=self.overwritten,
                                     ipc_failures=self.ipc_failures)
                        try:
                            data = json.dumps(event, separators=(',', ':')).encode()
                            if len(data) > _MAX_RECORD:
                                raise ValueError('observer record too large')
                            # IPC failure remains in the separate measurement file, even without tape.
                            timing.execute('INSERT OR IGNORE INTO memecoin_execution_observer_events '
                                           'VALUES (?,?,?,?)', (event['generation'] or 'unavailable:' + str(os.getpid())
                                                               + ':' + self.producer_id,
                                                               event['sequence'], event['kind'], data.decode()))
                            timing.commit()
                            if local is not None:
                                local.admit(event)
                            else:
                                client.sendto(data, str(path))
                        except (OSError, ValueError, TypeError):
                            self.ipc_failures += 1
            except Exception as exc:
                self.worker_error = type(exc).__name__
            finally:
                if timing is not None:
                    try:
                        _health(timing, 'publisher:' + self.chain + ':' + str(os.getpid()) + ':' + self.producer_id,
                                {**self.status(), 'chain_id': self.chain, 'alive': False})
                    finally:
                        timing.close()
        self.thread = threading.Thread(target=run, name=self.chain + '-observation-publisher', daemon=True)
        try:
            self.thread.start()
        except (RuntimeError, OSError) as exc:
            self.worker_error = type(exc).__name__
        return self

    def close(self):
        self._stop.set()
        self.generation = None


def create_publisher(chain):
    from sniper.settings import memecoin as cfg
    chain = str(chain)
    enabled = (cfg.MEMECOIN_BSC_EXECUTION_OBSERVER_ENABLED if chain == '56' else
               getattr(cfg, 'MEMECOIN_RH_EXECUTION_OBSERVER_ENABLED', False) if chain == '4663' else False)
    if not enabled:
        return None
    from sniper.util.state_paths import state_path
    root = Path(state_path('data')).parent
    identity = _identity(root)
    generation = identity[2] if identity is not None and identity[0] == os.getpid() else None
    return Publisher(generation=generation, chain=chain).start(_address(root, chain), identity_root=root)


def _health(timing, owner, state):
    timing.execute('INSERT OR REPLACE INTO memecoin_execution_observer_health VALUES (?,?)',
                   (owner, json.dumps(state)))
    timing.commit()


def read_rpc(client, url, method, params, pacer):
    """Exact JSON-RPC response envelope, with shared typed backoff; called only off-path."""
    from sniper.services.memecoin_research_pacer import DRPC_LIMIT_PAUSE_S, _drpc_limit
    response = client.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, timeout=0.25)
    if response.status_code in {403, 429}:
        pacer.pause(DRPC_LIMIT_PAUSE_S)
    response.raise_for_status()
    answer = response.json()
    if (not isinstance(answer, dict) or answer.get('jsonrpc') != '2.0'
            or type(answer.get('id')) is not int or answer['id'] != 1
            or 'result' not in answer or answer.get('error') is not None):
        if isinstance(answer, dict) and _drpc_limit(answer.get('error')):
            pacer.pause(DRPC_LIMIT_PAUSE_S)
        raise ValueError('receipt provider rejected or malformed response')
    return answer['result']


class ReceiptObserver:
    """Single background owner, at most two hashes/ten seconds/two reads per unique head."""

    def __init__(self, timing, *, read, budget, clock_ns=time.monotonic_ns, boot_id=None,
                 provider_index=0, chain='56'):
        self.timing, self.read, self.budget, self.clock_ns = timing, read, budget, clock_ns
        self.boot_id = clock_domain() if boot_id is None else boot_id
        self.provider_index = provider_index
        self.chain = str(chain)
        if self.chain not in {'56', '4663'}:
            raise ValueError('unsupported execution measurement chain')
        self.active, self.heads, self.sequences = {}, OrderedDict(), {}

    def count(self, name, amount=1):
        name = name if self.chain == '56' else self.chain + ':' + name
        self.timing.execute('INSERT INTO memecoin_execution_observer_stats VALUES (?,?) '
                            'ON CONFLICT(name) DO UPDATE SET count=count+excluded.count', (name, amount))

    def _state(self, event, state):
        self.timing.execute('UPDATE memecoin_execution_observations SET observation_state=? '
                            'WHERE generation=? AND attempt_id=? AND tx_hash=?',
                            (state, event['generation'], event['attempt_id'], event['tx_hash']))

    def expire(self):
        changed = False
        for key, event in list(self.active.items()):
            if self.clock_ns() - event['emitted_ns'] > 10_000_000_000:
                self._state(event, 'expired')
                del self.active[key]
                self.count('expired')
                changed = True
        if changed:
            self.timing.commit()

    def admit(self, event):
        if str(event.get('chain_id', '56')) != self.chain:
            self.count('wrong_chain')
            self.timing.commit()
            return
        generation, sequence = event.get('generation'), event.get('sequence')
        if not isinstance(generation, str) or type(sequence) is not int or sequence <= 0:
            self.count('invalid_admission')
            return
        previous = self.sequences.get(generation, 0)
        self.count('sequence_gaps', max(0, sequence - previous - 1))
        self.sequences[generation] = max(previous, sequence)
        now = self.clock_ns()
        event = dict(event, ipc_received_ns=now)
        self.timing.execute('INSERT OR IGNORE INTO memecoin_execution_observer_events VALUES (?,?,?,?)',
                            (generation, sequence, str(event.get('kind')), json.dumps(event)))
        kind, tx, aid = event.get('kind'), str(event.get('tx_hash') or '').lower(), event.get('attempt_id')
        if kind not in {'dispatch', 'booked', 'observed'}:
            self.timing.commit()
            return
        if not _HASH.fullmatch(tx) or not isinstance(aid, str) or not aid:
            self.count('invalid_admission')
            self.timing.commit()
            return
        event['tx_hash'] = tx
        key = generation, aid, tx
        self.timing.execute('INSERT OR IGNORE INTO memecoin_execution_observations '
                            '(generation,attempt_id,tx_hash,observation_state) VALUES (?,?,?,?)',
                            (*key, 'pending'))
        if kind == 'observed':
            self._observe_existing(event, key)
        elif kind == 'booked':
            self.timing.execute('UPDATE memecoin_execution_observations SET booking_json=? '
                                'WHERE generation=? AND attempt_id=? AND tx_hash=?', (json.dumps(event), *key))
        else:
            self.timing.execute('UPDATE memecoin_execution_observations SET dispatch_json=COALESCE('
                                'dispatch_json,?) WHERE generation=? AND attempt_id=? AND tx_hash=?',
                                (json.dumps(event), *key))
            emitted = event.get('emitted_ns')
            if self.chain == '4663':
                pass  # Existing confirmation metadata is the sole RH observation source.
            elif (event.get('boot_id') != self.boot_id or type(emitted) is not int or emitted > now):
                self._state(event, 'clock_unknown')
            elif now - emitted > 10_000_000_000:
                self._state(event, 'expired')
            elif key not in self.active and len(self.active) >= 2:
                self._state(event, 'refused_capacity')
            else:
                self.active.setdefault(key, event)
        self.timing.commit()

    def _observe_existing(self, event, key):
        """Off-path strict receipt parsing. Never reads a provider or repairs a missing T0."""
        if self.chain != '4663' or event.get('observation_kind') != 'confirmation_observation':
            self.count('invalid_observation')
            return
        found = receipt_observation(self.chain, event['tx_hash'], event.get('receipt'))
        observed, boot = event.get('observed_ns'), event.get('observed_boot_id')
        if (not found or not found.get('source_block_hash') or type(observed) is not int
                or observed < 0 or (boot == self.boot_id and observed > self.clock_ns())):
            self._state(event, 'invalid_receipt')
            self.count('invalid_observation')
            return
        held = self.timing.execute('SELECT dispatch_json FROM memecoin_execution_observations '
                                  'WHERE generation=? AND attempt_id=? AND tx_hash=?', key).fetchone()
        dispatch = json.loads(held[0] or '{}') if held else {}
        witness = dict(chain_id=self.chain, observation_kind='confirmation_observation',
                       tx_hash=event['tx_hash'], block_number=found['source_block_number'],
                       block_hash=found['source_block_hash'], observed_ns=observed, boot_id=boot,
                       observed_at=time.time(), receipt_elapsed_ms=(elapsed_receipt_ms(
                           dispatch.get('receipt_clock'), observed, boot_id=boot)
                           if isinstance(boot, str) and boot else None))
        self.timing.execute('UPDATE memecoin_execution_observations SET first_success_json=COALESCE('
                            'first_success_json,?),observation_state=? WHERE generation=? '
                            'AND attempt_id=? AND tx_hash=?', (json.dumps(witness), 'observed', *key))

    def observe_head(self, head, *, received_ns=None, read_receipts=True):
        if self.chain != '56':
            return  # RH never starts another receipt/head polling path.
        try:
            number = int(str(head['number']), 0)
            block_hash = str(head['hash']).lower()
            if number <= 0 or not _HASH.fullmatch(block_hash):
                raise ValueError('invalid head')
        except (KeyError, TypeError, ValueError):
            self.count('invalid_head')
            self.timing.commit()
            return
        if self.heads.get(number) == block_hash:
            return
        self.heads[number] = block_hash
        while len(self.heads) > 64:
            self.heads.popitem(last=False)
        if not read_receipts:
            return
        received_ns = self.clock_ns() if received_ns is None else received_ns
        self.expire()
        reads = {}
        for key, event in list(self.active.items())[:2]:
            tx = event['tx_hash']
            if tx not in reads:
                if not self.budget():
                    self.count('budget_skipped')
                    continue
                started = self.clock_ns()
                try:
                    receipt = self.read('eth_getTransactionReceipt', [tx])
                    error = None
                except Exception as exc:
                    receipt, error = None, type(exc).__name__
                reads[tx] = receipt, error, started, self.clock_ns()
            receipt, error, started, completed = reads[tx]
            if error is not None:
                self._state(event, 'read_' + error)
                self.count('read_failed')
                continue
            observation = receipt_observation(self.chain, event['tx_hash'], receipt)
            if observation is None or not observation.get('source_block_hash'):
                self._state(event, 'null' if receipt is None else 'invalid_receipt')
                continue
            height, receipt_hash = observation['source_block_number'], observation['source_block_hash']
            if self.heads.get(height) != receipt_hash:
                self._state(event, 'fork_or_unknown_head')
                continue
            witness = dict(tx_hash=event['tx_hash'], block_number=height, block_hash=receipt_hash,
                           chain_id=self.chain, observation_kind='head_receipt_observation',
                           observed_ns=completed, boot_id=self.boot_id, provider_index=self.provider_index,
                           observed_at=time.time(), head_number=number, head_hash=block_hash,
                           head_received_ns=received_ns, provider_rtt_ms=(completed-started)/1e6,
                           observer_queue_ms=(started-received_ns)/1e6,
                           publisher_delay_ms=((event.get('published_ns', event['emitted_ns'])
                                                - event['emitted_ns'])/1e6),
                           ipc_delay_ms=((event['ipc_received_ns']
                                          - event.get('published_ns', event['emitted_ns']))/1e6),
                           receipt_elapsed_ms=elapsed_receipt_ms(event.get('receipt_clock'), completed,
                                                                 boot_id=self.boot_id))
            self.timing.execute('UPDATE memecoin_execution_observations SET first_success_json=COALESCE('
                                'first_success_json,?),observation_state=? WHERE generation=? '
                                'AND attempt_id=? AND tx_hash=?', (json.dumps(witness), 'observed', *key))
            del self.active[key]
        self.timing.commit()


def validate_witness(timing, attempt_id, booked_tx, position_id, *, chain='56'):
    """Later resolver only: compare canonical-accounting receipt AND actual ledger booking."""
    state = 'pending'
    for generation, tx, raw, booked, dispatched in timing.execute(
            'SELECT generation,tx_hash,first_success_json,booking_json,dispatch_json '
            'FROM memecoin_execution_observations WHERE attempt_id=?', (attempt_id,)).fetchall():
        if not raw or not booked:
            continue
        observation, booking = json.loads(raw), json.loads(booked)
        dispatch = json.loads(dispatched or '{}')
        canonical = receipt_observation(str(chain), tx, booking.get('receipt'))
        state = 'verified' if (canonical and canonical.get('source_block_hash')
                              == observation.get('block_hash')
                              and canonical['source_block_number'] == observation.get('block_number')
                              and str(booked_tx).lower() == tx
                              and booking.get('position_id') == position_id
                              and str(booking.get('chain_id', '56')) == str(chain)
                              and str(observation.get('chain_id', '56')) == str(chain)
                              and (str(chain) == '56' or (
                                  dispatch.get('chain_id') == '4663' and dispatch.get('attempt_id') == attempt_id
                                  and str(dispatch.get('tx_hash', '')).lower() == tx))) else 'mismatch'
        timing.execute('UPDATE memecoin_execution_observations SET validation_state=? '
                       'WHERE generation=? AND attempt_id=? AND tx_hash=?', (state, generation, attempt_id, tx))
    timing.commit()
    return state


def validate_pending_witnesses(ledger, timing, *, limit=500):
    """Read already-booked exact fills; no live read or writes to the funded ledger."""
    from sniper.persistence.db_memecoin import entry_fill_for_attempt
    rows = timing.execute('SELECT DISTINCT attempt_id FROM memecoin_execution_observations '
                          "WHERE validation_state='pending' AND first_success_json IS NOT NULL "
                          'AND booking_json IS NOT NULL LIMIT ?', (limit,)).fetchall()
    for (attempt_id,) in rows:
        attempt = ft.one(ledger, 'SELECT network_id,tx_sig,terminal_status FROM '
                         'memecoin_entry_attempts WHERE attempt_id=?', (attempt_id,))
        if not attempt or attempt['network_id'] not in {'56', '4663'} or attempt['terminal_status'] != 'filled':
            continue
        chain = attempt['network_id']
        fill = (confirmed_real_entry_fill(ledger, attempt_id, attempt['tx_sig'], chain=chain)
                if chain == '4663' else entry_fill_for_attempt(ledger, attempt_id, attempt['tx_sig']))
        if fill is not None:
            validate_witness(timing, attempt_id, attempt['tx_sig'], fill['position_id'], chain=chain)


def confirmed_real_entry_fill(ledger, attempt_id, tx_hash, *, chain):
    """Bounded local proof of an actual positive real entry on this exact chain and transaction."""
    from sniper.persistence.db_memecoin import entry_fill_for_attempt
    fill = entry_fill_for_attempt(ledger, attempt_id, tx_hash)
    if fill is None:
        return None
    try:
        if any(not math.isfinite(float(fill[key])) or float(fill[key]) <= 0
               for key in ('qty', 'notional')):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    position = ft.one(ledger, 'SELECT network_id,execution_mode FROM memecoin_positions WHERE id=?',
                      (fill['position_id'],))
    fill_chain = ft.one(ledger, 'SELECT network_id FROM memecoin_fills WHERE id=?', (fill['id'],))
    return fill if (position and str(position['network_id']) == str(chain)
                    and position['execution_mode'] == 'real' and fill_chain
                    and str(fill_chain['network_id']) == str(chain)) else None


def authenticated_event(data, credentials, identity):
    """Only the cached exact pinned runner can submit measurement metadata."""
    if identity is None or credentials is None or credentials[:2] != identity[:2]:
        return None
    try:
        event = json.loads(data)
        if (len(data) > _MAX_RECORD or not isinstance(event, dict)
                or event.get('runner_generation', event.get('generation')) != identity[2]):
            return None
        return event
    except (TypeError, ValueError, UnicodeError):
        return None


class TapeObserver:
    """Existing tape's own client, IPC receiver and bounded head-driven read worker."""

    def __init__(self, *, root, rpc_url):
        self.root, self.rpc_url = Path(root), rpc_url
        self.head_ring, self.head_sequence = deque(maxlen=32), count(1)
        self.worker_error = None
        self.ready = False
        self._stop = threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.server.bind(_address(root))
        self.server.setblocking(False)
        self.thread = threading.Thread(target=self._run, name='bsc-execution-observer', daemon=True)
        try:
            self.thread.start()
        except (OSError, RuntimeError):
            self.server.close()
            raise

    def publish_head(self, item):
        self.head_ring.append((next(self.head_sequence), dict(item), time.monotonic_ns()))

    def close(self):
        self._stop.set()

    def status(self):
        return dict(alive=self.thread.is_alive(), ready=self.ready and self.worker_error is None,
                    worker_error=self.worker_error)

    def _run(self):
        timing, client = None, None
        try:
            import requests
            from sniper.services.memecoin_evm_exact_capture import budget
            from sniper.services.memecoin_research_pacer import SharedPacer, DRPC_GAP_S
            keyed = budget(self.rpc_url) == 'drpc.org (keyed)'
            pacer = (SharedPacer.named('drpc-keyed', gap_s=DRPC_GAP_S) if keyed else
                     SharedPacer.for_url(self.rpc_url))
            timing, client = ft.connect(), None
            client = requests.Session()
            identity, refresh, ready, head_sequence = None, 0, False, 0
            next_identity_read, health_at, last_head_ns = 0, 0, None
            def read(method, params):
                return read_rpc(client, self.rpc_url, method, params, pacer)
            engine = ReceiptObserver(timing, read=read, budget=pacer.try_acquire)
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= refresh:
                    new_identity = _identity(self.root)
                    if new_identity != identity:
                        for event in engine.active.values():
                            engine._state(event, 'generation_invalidated')
                        engine.active.clear()
                    identity, refresh = new_identity, now + 1
                for _ in range(64):
                    try:
                        data, ancillary, flags, _ = self.server.recvmsg(_MAX_RECORD, socket.CMSG_SPACE(12))
                    except BlockingIOError:
                        break
                    credentials = next((struct.unpack('3i', value[:12]) for level, kind, value in ancillary
                                        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS
                                        and len(value) >= 12), None)
                    event = (None if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) else
                             authenticated_event(data, credentials, identity))
                    if event is None:
                        engine.count('ipc_refused')
                    else:
                        engine.admit(event)
                # Identity check shares the same nonblocking budget; never use the source loop.
                if not ready and now >= next_identity_read:
                    next_identity_read = now + 1
                    if pacer.try_acquire():
                        try:
                            ready = int(str(read('eth_chainId', [])), 0) == 56
                            self.ready = ready
                            if not ready:
                                engine.count('wrong_chain')
                                self.worker_error = 'wrong_chain'
                                break
                        except Exception:
                            engine.count('identity_read_failed')
                            next_identity_read = now + 5
                heads = []
                while self.head_ring:
                    try:
                        heads.append(self.head_ring.popleft())
                    except IndexError:
                        break
                for index, (sequence, item, received_ns) in enumerate(heads):
                    last_head_ns = received_ns
                    engine.count('head_sequence_gaps', max(0, sequence - head_sequence - 1))
                    head_sequence = sequence
                    engine.observe_head(item, received_ns=received_ns,
                                        read_receipts=ready and index == len(heads) - 1)
                engine.expire()
                if now >= health_at:
                    _health(timing, 'tape', dict(alive=True, ready=ready, identity_ready=identity is not None,
                                                generation=identity[2] if identity else None,
                                                head_age_ms=(None if last_head_ns is None else
                                                             (time.monotonic_ns()-last_head_ns)/1e6),
                                                active=len(engine.active), provider_index=0,
                                                budget=budget(self.rpc_url), updated_at=time.time()))
                    health_at = now + 1
                timing.commit()
                self._stop.wait(0.02)
        except Exception as exc:
            self.worker_error = type(exc).__name__
            import logging
            logging.getLogger(__name__).exception('BSC execution witness unavailable')
        finally:
            self.ready = False
            if timing is not None:
                try:
                    _health(timing, 'tape', dict(alive=False, ready=False, worker_error=self.worker_error,
                                                updated_at=time.time()))
                except Exception:
                    pass  # status() still exposes failure when the measurement DB is unavailable.
            self.server.close()
            if client is not None:
                client.close()
            if timing is not None:
                timing.close()


def start_tape_observer(*, rpc_url):
    from sniper.settings import memecoin as cfg
    if not cfg.MEMECOIN_BSC_EXECUTION_OBSERVER_ENABLED:
        return None
    from sniper.util.state_paths import state_path
    try:
        return TapeObserver(root=Path(state_path('data')).parent, rpc_url=rpc_url)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('BSC execution observer startup failed')
        return None
