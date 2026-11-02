"""DBC shadow census core: frame decode, pool resolution, coverage check. Measurement only.

Run by scripts/memecoin_dbc_census_worker.py as its own process on its own Shyft gRPC connection.
Never imported by the runner, never touches the ledger. The tape schema is dbc_census_tape.

SOURCE FILTER (stated in run.source_filter): transactions with account_include=[DBC program], vote=False,
`failed` left unset (failed txs delivered), commitment CONFIRMED. A tx that includes the program in its
account keys is delivered whether DBC is called directly or by CPI. The coverage check tests that claim.
Failed txs carry no readable swap event: they are stored as one row, ordinal 0, every swap field NULL,
err set. Whether a failed tx was a swap is therefore unknown.
"""
import base64
import collections
import json
import struct
import time

from sniper.research import dbc_census_tape as T

PROGRAM = 'dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN'
EVENT_CPI = bytes.fromhex('e445a52e51cb9a1d')
EVT_SWAP = bytes([27, 60, 21, 213, 138, 170, 187, 147])
EVT_SWAP2 = bytes([189, 66, 51, 168, 38, 80, 117, 153])
EVT_INIT = bytes([228, 50, 246, 85, 203, 66, 134, 37])
STATIC_DECIMALS = {T.WSOL: 9, 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 6}
SOURCE_FILTER = ('grpc transactions account_include=[%s] vote=False failed=unset commitment=CONFIRMED; '
                 'arrival_ts = host receipt time' % PROGRAM)


def b58(raw):
    import base58
    return base58.b58encode(bytes(raw)).decode()


def decode_evt_swap(body):
    """EvtSwap payload after the 8-byte discriminator (layout as sniper/research/memecoin_opportunity/
    bags_solana.py): pool, config, direction u8 (1 = quote to base = buy), has_referral, requested_in,
    minimum_out, actual_in, output, next_sqrt_price u128, trading/protocol/referral fee u64, amount_in,
    timestamp. Raises ValueError on a short or odd payload (counted by the caller, never guessed)."""
    from sniper.research.memecoin_opportunity.decoders import BorshReader
    r = BorshReader(body)
    pool, config = r.pubkey(), r.pubkey()
    direction, _ref = r.u8(), r.bool()
    _req, _min = r.u64(), r.u64()
    actual_in, output = r.u64(), r.u64()
    _sqrt = r.u128()
    fees = (r.u64(), r.u64(), r.u64())
    if direction not in (0, 1):
        raise ValueError('direction')
    return dict(pool=pool, config=config, direction='buy' if direction == 1 else 'sell', amount_in=str(actual_in),
                amount_out=str(output), fee_json=json.dumps(dict(zip(('trading', 'protocol', 'referral'), fees))))


def cpi_events(keys, inner):
    """[(disc, body)] for the DBC self-CPI events of one tx, in order. `inner` = [(program_index, data)]."""
    out = []
    for pi, data in inner:
        if pi < len(keys) and keys[pi] == PROGRAM and data[:8] == EVENT_CPI and len(data) >= 16:
            out.append((bytes(data[8:16]), bytes(data[16:])))
    return out


class Census:
    """Single-writer core. `process(raw)` takes a serialized SubscribeUpdate. Pool facts come later from
    `resolve_pending(rpc)`; until then mint/quote_mint/decimals are NULL (unknown), never defaulted."""

    QUEUE_MAX = 20000

    def __init__(self, conn, run_id, *, clock=time.time):
        self.c, self.run_id, self.clock = conn, run_id, clock
        self.stats = collections.Counter()
        self.pool_created = {}      # pool -> create slot, only for pools created inside this run
        self.pool_swaps = set()
        self.ring = collections.deque()
        self.dropped_pending = 0
        self.stop_reason = None

    def push(self, raw, received_at):
        if len(self.ring) >= self.QUEUE_MAX:
            self.ring.popleft()
            self.dropped_pending += 1
        self.ring.append((raw, received_at))

    def flush_drops(self):
        if self.dropped_pending:
            T.record_drop(self.c, self.run_id, self.clock(), self.dropped_pending)
            self.dropped_pending = 0
        self.stop_reason = None

    def process(self, raw, received_at=None):
        from sniper.feeds.yellowstone import geyser_pb2 as pb
        now = self.clock() if received_at is None else received_at
        u = pb.SubscribeUpdate.FromString(raw)
        if u.WhichOneof('update_oneof') != 'transaction':
            return
        info, slot = u.transaction.transaction, u.transaction.slot
        msg = info.transaction.message
        keys = [b58(k) for k in list(msg.account_keys) + list(info.meta.loaded_writable_addresses)
                + list(info.meta.loaded_readonly_addresses)]
        sig = b58(info.signature)
        failed = info.meta.HasField('err')
        # proto3 omits 0, so an index of 0 cannot be told from absent: NULL, not 0
        tx_index = int(info.index) if 0 < info.index < 2 ** 63 else None
        T.count_tx(self.c, self.run_id, now, failed)
        self.stats['tx'] += 1
        if failed:
            err = info.meta.err.err.hex() or 'failed'
            T.insert_swap(self.c, self.run_id, sig, 0, now, slot=slot, tx_index=tx_index, program_id=PROGRAM,
                          commitment='confirmed', err=err)
            self.stats['failed_tx'] += 1
            return
        inner = [(ix.program_id_index, bytes(ix.data)) for g in info.meta.inner_instructions for ix in g.instructions]
        events = cpi_events(keys, inner)
        ordinal, saw_swap2 = 0, False
        for disc, body in events:
            if disc == EVT_INIT:
                try:
                    from sniper.research.memecoin_opportunity.decoders import BorshReader
                    self.pool_created[BorshReader(body).pubkey()] = slot
                except ValueError:
                    self.stats['skip:init_decode'] += 1
            elif disc == EVT_SWAP2:
                saw_swap2 = True
            elif disc == EVT_SWAP:
                try:
                    f = decode_evt_swap(body)
                except ValueError:
                    self.stats['skip:swap_decode'] += 1
                    T.record_skip(self.c, self.run_id, now, 'swap_decode')
                    continue
                created = self.pool_created.get(f['pool'])
                first = None if created is None else int(f['pool'] not in self.pool_swaps)
                self.pool_swaps.add(f['pool'])
                T.insert_swap(self.c, self.run_id, sig, ordinal, now, slot=slot, tx_index=tx_index,
                              program_id=PROGRAM, commitment='confirmed', err='', pool_create_slot=created,
                              is_first_swap_for_pool=first, **f)
                ordinal += 1
                self.stats['swap'] += 1
        if saw_swap2 and ordinal == 0:      # EvtSwap2 alone: not decoded in v1, counted not dropped silently
            self.stats['skip:evtswap2_only'] += 1
            T.record_skip(self.c, self.run_id, now, 'evtswap2_only')

    def resolve_pending(self, rpc, limit=10):
        """One batch: pools with NULL mint. rpc(method, params) -> result or raises. Returns pools tried.
        A parse refusal (hooked pool, odd length) leaves NULL and counts; a transport error re-raises so
        the caller stops on 429."""
        import base64 as b64
        from sniper.execution import meteora_dbc_buy as D
        rows = self.c.execute('SELECT DISTINCT pool, config FROM swaps WHERE run_id=? AND mint IS NULL AND '
                              'pool IS NOT NULL AND err=\'\' LIMIT ?', (self.run_id, limit)).fetchall()
        for pool, config in rows:
            vals = rpc('getMultipleAccounts', [[pool, config], {'encoding': 'base64', 'commitment': 'confirmed'}])['value']
            try:
                pdata = b64.b64decode(vals[0]['data'][0])
                cdata = b64.b64decode(vals[1]['data'][0])
                base_mint, quote_mint = D.parse_pool(pdata)['base_mint'], D.parse_config(cdata)['quote_mint']
            except (TypeError, KeyError, IndexError, ValueError):
                self.stats['resolve_refused'] += 1
                self.c.execute("UPDATE swaps SET mint='' WHERE run_id=? AND pool=? AND mint IS NULL",
                               (self.run_id, pool))      # '' = tried, unreadable (hook/odd layout); not NULL-retried
                continue
            dec = {}
            need = [m for m in (base_mint, quote_mint) if m not in STATIC_DECIMALS]
            if need:
                got = rpc('getMultipleAccounts', [need, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}])['value']
                for m, v in zip(need, got):
                    try:
                        dec[m] = int(v['data']['parsed']['info']['decimals'])
                    except (TypeError, KeyError):
                        dec[m] = None
            dec.update({m: d for m, d in STATIC_DECIMALS.items() if m in (base_mint, quote_mint)})
            self.c.execute('UPDATE swaps SET mint=?, quote_mint=?, quote_decimals=?, base_decimals=? '
                           'WHERE run_id=? AND pool=? AND mint IS NULL',
                           (base_mint, quote_mint, dec.get(quote_mint), dec.get(base_mint), self.run_id, pool))
            self.stats['resolved'] += 1
        return len(rows)


def coverage_check(c, run_id, rpc, windows, *, max_calls=150, tolerance=0.05):
    """Compare tape tx counts with an independent getSignaturesForAddress(PROGRAM) count in each window
    (start_ts, end_ts). rpc(method, params) returns the result or raises; any raise stops the check and
    the verdict is 'partial'. Anchor: the first tape tx at/after the window end is the `before` cursor
    (a cursor only; the counted signatures are the RPC's). Returns dict(verdict, windows[], calls)."""
    calls, out = 0, []
    for lo, hi in windows:
        tape = c.execute('SELECT COALESCE(SUM(txs),0) FROM tx_minutes WHERE run_id=? AND minute>=? AND minute<?',
                         (run_id, int(lo // 60), int(hi // 60))).fetchone()[0]
        anchor = c.execute('SELECT sig FROM swaps WHERE run_id=? AND arrival_ts>=? ORDER BY arrival_ts LIMIT 1',
                           (run_id, hi)).fetchone()
        n, before, done, reason = 0, anchor[0] if anchor else None, False, None
        while calls < max_calls:
            params = {'limit': 1000, 'commitment': 'confirmed'}
            if before:
                params['before'] = before
            try:
                calls += 1
                page = rpc('getSignaturesForAddress', [PROGRAM, params])
            except Exception as exc:
                reason = type(exc).__name__
                break
            if not page:
                done = True
                break
            for row in page:
                bt = row.get('blockTime')
                if bt is not None and bt < lo:
                    done = True
                    break
                if bt is not None and bt < hi:
                    n += 1
            if done:
                break
            before = page[-1]['signature']
        else:
            reason = 'call_budget'
        ok = done and n > 0 and abs(tape - n) / n <= tolerance
        out.append(dict(window=(lo, hi), tape=tape, rpc=n, complete=done, reason=reason, within_tolerance=ok))
    verdict = 'pass' if out and all(w['within_tolerance'] for w in out) else 'partial'
    return dict(verdict=verdict, windows=out, calls=calls)


FATAL_CODES = ('RESOURCE_EXHAUSTED', 'UNAUTHENTICATED', 'PERMISSION_DENIED')
FATAL_WORDS = ('resource_exhausted', 'unauthenticated', 'permission_denied', 'limit exceeded', 'too many',
               'max streams', 'refused')
MAX_RECONNECTS = 3
RECONNECT_BACKOFF_S = (30.0, 60.0, 120.0)


def stream_error_is_fatal(exc):
    """A refused / limit-exceeded / unauthenticated subscribe. The census exits at once: it must never
    hold a slot or race a funded reconnect on the shared token."""
    code = getattr(exc, 'code', None)
    name = getattr(code() if callable(code) else code, 'name', None)
    if name in FATAL_CODES:
        return True
    text = (str(exc) + ' ' + str(getattr(exc, 'details', lambda: '')() if callable(getattr(exc, 'details', None)) else '')).lower()
    return any(w in text for w in FATAL_WORDS)
