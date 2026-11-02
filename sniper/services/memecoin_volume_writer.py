"""Record the Robinhood V4 volume source in a separate database."""
from __future__ import annotations

import collections
import json
import logging
import math
import os
import queue
import threading
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from sniper.persistence import db_ticks
from sniper.services.memecoin_evm_v4_events import (
    V4_SWAP, V4_INITIALIZE, V4_POOL_MANAGERS, decode_v4_log, v4_pool_id)
from sniper.services.memecoin_onchain_volume import onchain_volume_m5
from sniper.services.memecoin_tick_stream import (
    PONS_V2_CURVE_BUY, PONS_V2_CURVE_SELL, PONS_V2_LAUNCHED, PONS_V2_LAUNCHER, SOLIDLY_SWAP, V2_SWAP,
    V3_SWAP, PANCAKE_V3_SWAP, decode_evm_log, decode_pons_v2_launched)
from sniper.util.state_paths import state_root

NATIVE = '0x' + '00' * 20
# ONE REGISTRY, NOT TWO. `decode_v4_log` drops any log whose emitter is not the manager IT has for
# that chain, so a private copy here that drifted from it would subscribe to an address whose every
# log is then discarded — and an empty window reads as "no volume", never as "no feed".
MANAGERS = V4_POOL_MANAGERS


def manager_for(chain):
    """The verified V4 PoolManager for `chain`. Raises rather than guess an address."""
    try:
        return MANAGERS[str(chain)]
    except KeyError:
        raise ValueError(
            'volume writer has no verified V4 PoolManager for chain %s' % chain) from None


# websockets 12's sync client sends no keepalive, so after subscribing the server sees a silent
# client. CAUSE NOT PROVEN (2026-09-16): the observer closed three times at 20:00 spacing (17:16:57,
# 17:36:59, 17:57:00Z) and pinging clients held 1,570 s and 3,601 s, but the silent observer itself
# then held 17:57-18:26Z. The ping costs one frame per 20 s. The resets that remained were publicnode
# dropping 7-12 headers AND their logs without a close, which no client change can prevent.
PING_INTERVAL_S = 20.0
# publicnode delivered headers ...136, 138, 137, 139 twice in one hour (19:10:24, 19:15:51Z) with no
# block missing. A missing header may arrive as late as the third header counting the one that
# opened the hole; a third header that does not fill it resets the window.
# A HOLE IS A REORDER UNTIL IT IS NOT. Sized against the 2026-09-23 publicnode bursts: the widest
# real permutation spanned 8 blocks inside 1 ms, so 16 blocks is twice that and still far below a
# genuine jump (tonight's reconnects were 348 and 1442 blocks). 8 headers at ~13 blocks/s on 4663
# is well under a second, and 3 s is the outer bound before a missing block stops being late.
REORDER_WINDOW_BLOCKS = 16
REORDER_GRACE_HEADERS = 8
REORDER_GRACE_S = 3.0
WINDOW_S = 300.0
# How long the tape keeps a swap and its header. Every gate read is bounded by WINDOW_S (measure)
# and MAX(block_ts) (freshness), so retention beyond the window changes no gate value. 4663 keeps
# 65 minutes (operator yes 2026-10-01): the pre-signal recorder copies each signal's hour at first
# sight, once a minute, and a 60-minute tape had already pruned the first minute or two of it.
KEEP_S = {'4663': 3900.0}
KEEP_DEFAULT_S = 3600.0
DECIMALS_CALL = '0x313ce567'
FEED_STATUS_FIELDS = ('available', 'feed_age_s', 'window_age_s', 'logs_seen', 'resets_24h',
                      'last_reset_at', 'last_reset_reason')


def db_path():
    return state_root() / 'data/memecoin_onchain_volume.db'


def connect(path=None):
    path = Path(path or db_path()).resolve()
    if path.name in {'sniper_tracker.db', 'memecoin_ticks.db', 'memecoin_sol_tape.db'}:
        raise ValueError('volume writer requires its own database')
    path.parent.mkdir(parents=True, exist_ok=True)
    c = db_ticks.connect_ticks(str(path))
    # WAL + NORMAL: a commit no longer fsyncs (only a checkpoint does). The observer commits ~60
    # swaps and ~10 headers a second; at FULL that was one fsync per swap and 70% of a core on
    # 4663 (2026-09-22). Durability against a power loss is the only thing traded, and a lost
    # second of ticks is a lower bound the next backfill refills.
    c.execute('PRAGMA synchronous=NORMAL')
    from sniper.analysis.tick_features import ensure_feature_tables
    ensure_feature_tables(c)
    c.executescript('''
        CREATE TABLE IF NOT EXISTS onchain_pool_protocols (
            chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT,
            PRIMARY KEY(chain_id,pool,mint));
        CREATE INDEX IF NOT EXISTS volume_mint_pools ON onchain_pool_protocols(chain_id,mint);
        CREATE TABLE IF NOT EXISTS volume_feed (
            chain_id TEXT PRIMARY KEY, since REAL, through REAL, received REAL, logs_seen INTEGER);
        CREATE TABLE IF NOT EXISTS volume_quote_prices (
            chain_id TEXT, token TEXT, usd REAL, observed REAL, PRIMARY KEY(chain_id,token));
        CREATE TABLE IF NOT EXISTS volume_token_decimals (
            chain_id TEXT, token TEXT, decimals INTEGER, observed REAL, PRIMARY KEY(chain_id,token));
        CREATE TABLE IF NOT EXISTS volume_token_supply (
            chain_id TEXT, token TEXT, decimals INTEGER, total_supply TEXT, observed REAL,
            PRIMARY KEY(chain_id,token));
        CREATE TABLE IF NOT EXISTS volume_feed_resets (
            chain_id TEXT, at REAL, reason TEXT, window_s REAL);
        CREATE INDEX IF NOT EXISTS volume_feed_resets_at ON volume_feed_resets(chain_id,at);
        -- The hop pricer asks "which pools pair with token X" a few hundred times a pass; without
        -- these every ask scanned the chain's 73k pool_meta rows (2026-09-22: 43% of a core).
        CREATE INDEX IF NOT EXISTS pool_meta_token0 ON pool_meta(chain_id, token0);
        CREATE INDEX IF NOT EXISTS pool_meta_token1 ON pool_meta(chain_id, token1);
        CREATE INDEX IF NOT EXISTS volume_pool_meta_resolution ON pool_meta(chain_id, resolve_state, resolved_at);
        -- tape_states asks for a pool's LATEST tick; the existing (chain,pool,kind,observed_at)
        -- index cannot serve ORDER BY observed_at without scanning the pool's rows (0.75 s on a
        -- 10k-tick pool; 107 s of a 150 s pricer pass on 2026-09-22).
        CREATE INDEX IF NOT EXISTS pool_ticks_pool_time ON pool_ticks(chain_id, pool, observed_at);
    ''')
    # volume_feed keeps its five columns: a rolled-back writer inserts five positional values.
    # ⚠️ PER-PROTOCOL DELIVERY LAG GETS ITS OWN TABLE FOR EXACTLY THAT REASON (2026-09-23).
    # Adding the columns to `volume_feed` breaks `INSERT INTO volume_feed VALUES (?,?,?,?,?)`
    # outright -- SQLite refuses five values for nine columns -- so a rolled back release would
    # fail on every header. `volume_feed_resets` was added the same way and for the same reason.
    c.execute('CREATE TABLE IF NOT EXISTS volume_feed_lag ('
              'chain_id TEXT PRIMARY KEY, v4_lag_s REAL, v3_lag_s REAL, '
              'samples INTEGER, observed REAL)')
    # PONS IS ITS OWN SUBSCRIPTION (ULTRONICS, 4663, 2026-10-07): its frames were noted as V3, so a
    # Pons subscription that was confirmed and delivered nothing read healthy. Appended by ALTER:
    # every writer names its columns, so a rolled-back writer's REPLACE leaves it NULL (silent ->
    # blind at the gate), never a false measurement.
    if 'pons_lag_s' not in {r[1] for r in c.execute('PRAGMA table_info(volume_feed_lag)')}:
        c.execute('ALTER TABLE volume_feed_lag ADD COLUMN pons_lag_s REAL')
    # THE V2 TOPICS COUNT ONLY ONCE THIS SOCKET CONFIRMED BOTH (2026-09-28, Beta T3). A refused or
    # never-confirmed V2 subscription would otherwise read as a measured zero for every V2 pool.
    # Its own table: adding a column would break a rolled-back writer's positional inserts.
    c.execute('CREATE TABLE IF NOT EXISTS volume_feed_optional ('
              'chain_id TEXT PRIMARY KEY, v2_confirmed_at REAL)')
    # WHEN THE EVIDENCE BEHIND A QUOTE PRICE WAS TRUE, which is not when we wrote it (F4,
    # 2026-09-23). A hop price is derived from a pool's last swap, and that swap can be an hour old
    # while the row it produces is stamped `now`. This carries the OLDEST evidence time the price
    # depends on, so the age can be read honestly without changing what `observed` means yet.
    #
    # A SIBLING TABLE AND NOT A FIFTH COLUMN, and that is not a style choice: `volume_quote_prices`
    # is written POSITIONALLY in three live places (`memecoin_equity_hop_price.py:365`,
    # `memecoin_volume_writer.py:554` and `:557`), so a new column breaks all three on the spot.
    # Same reason `volume_feed_lag` exists rather than columns on `volume_feed`.
    c.execute('CREATE TABLE IF NOT EXISTS volume_quote_price_evidence ('
              'chain_id TEXT, token TEXT, evidence_at REAL, source TEXT, '
              'PRIMARY KEY (chain_id, token))')
    c.commit()
    return c


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def _insert_tick(conn, row):
    """One tick, NO commit. `db_ticks.insert_ticks` commits on every call, which was one fsync per
    swap at ~58 swaps a second; the observer commits per header instead."""
    conn.execute(
        "INSERT OR IGNORE INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index,"
        " maker, amount0, amount1, observed_at, tick, tick_lower, tick_upper, liquidity,"
        " sqrt_price_x96, block_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["chain_id"], row["pool"], row["kind"], row.get("block_no"), row.get("tx_sig"),
         row.get("log_index"), row.get("maker"), row.get("amount0"), row.get("amount1"),
         row["observed_at"], row.get("tick"), row.get("tick_lower"), row.get("tick_upper"),
         row.get("liquidity"), row.get("sqrt_price_x96"), row.get("block_hash")))


LAG_SAMPLES = 512                # a few minutes of frames at the observed 25-60 a second
# How long a delivery-lag sample stays evidence (N2, 2026-09-23 recheck). The deque evicts by
# length alone, so a quiet subscription kept its last samples for ever and a single stale one held
# a protocol "lagging" indefinitely. 300 s is the window the m5 gate itself reasons over, so a
# sample outside it cannot describe the window being judged.
LAG_SAMPLE_MAX_AGE_S = float(os.getenv("MEMECOIN_ONCHAIN_M5_LAG_SAMPLE_MAX_AGE_S", "300") or 0)
# The most history one stall-triggered refill may ask for. The 2026-09-23 stall reached
# 652.9 s of lag; a cap keeps a pathological reading from turning into an hour of eth_getLogs
# against publicnode, whose heavy reads from this IP stall the live tape (2026-09-22).
BACKFILL_WINDOW_CAP_S = 900.0


# The columns this writer's own statements name. Checked ONCE per tape, never per block.
BLOCK_TIMES_COLUMNS = ('chain_id', 'block_no', 'block_ts', 'fetched_at', 'block_hash')


def assert_block_times_shape(conn):
    """Fail loudly at start if the table cannot take the statements this writer is about to run.

    The mirror of the deploy guard (`scripts/check_positional_block_times_writes.py`). That one
    refuses a RELEASE whose writers cannot express the table's shape; this one refuses a TABLE that
    cannot accept this release's statements -- a database left behind by an older release, or a
    migration that half-ran. Without it the first symptom is an OperationalError per header on a
    thread whose exceptions are swallowed, which reads as a dark feed rather than a broken one.

    Once per `VolumeTape`, one `PRAGMA table_info`. Nothing on any entry path: this object is the
    tape writer, and the funded entry path never constructs one.
    """
    have = {row[1] for row in conn.execute('PRAGMA table_info(block_times)')}
    missing = [name for name in BLOCK_TIMES_COLUMNS if name not in have]
    if missing:
        raise RuntimeError(
            'block_times is missing %s; this release writes %s and cannot run against that table'
            % (', '.join(missing), ', '.join(BLOCK_TIMES_COLUMNS)))


class VolumeTape:
    """Track contiguous heads and verified pool identities for one connection."""
    def __init__(self, conn, chain, now=0):
        self.manager = manager_for(chain)        # raises on a chain with no verified manager
        assert_block_times_shape(conn)
        self.conn, self.chain = conn, str(chain)
        self.last_block = self.last_hash = self.since = None
        self.logs_seen = 0
        self.reset_count = 0
        # PER-SUBSCRIPTION DELIVERY LAG, in memory (2026-09-23). One rolling window of
        # `arrival - block timestamp` per protocol. Computing it from the tape costs p50 17.8 ms on
        # 4663, which is a synchronous read this project refuses to add to order filling; here it is
        # free, because the writer already holds both numbers when the frame lands.
        self.lag = {'v4': collections.deque(maxlen=LAG_SAMPLES),
                    'v3': collections.deque(maxlen=LAG_SAMPLES),
                    'pons': collections.deque(maxlen=LAG_SAMPLES)}
        self.last_delivery = {}
        self.reset(now, 'init')

    def reset(self, now, reason='reset'):
        """Drop the covered window. `received` keeps the last header time, so a dark feed looks dark.

        A reset that loses a live window is recorded with its time, reason and length, so the share
        of a day that carried a bound can be rebuilt from `volume_feed_resets`.
        """
        row = self.conn.execute('SELECT since FROM volume_feed WHERE chain_id=?', (self.chain,)).fetchone()
        self.reset_count += 1
        self.last_block = self.last_hash = self.since = self.through = None
        self.holes = {}              # block -> (opened_at, headers_seen when it opened)
        self.headers_seen = 0
        self.hashes = {}
        self.block_ts = {}           # block -> header timestamp, for the contiguous frontier
        for samples in getattr(self, 'lag', {}).values():
            samples.clear()          # a new socket has no delivery evidence yet: unknown, not 0
        self.logs_seen = 0
        self.conn.execute('INSERT OR IGNORE INTO volume_feed (chain_id,logs_seen) VALUES (?,0)',
                          (self.chain,))
        self.conn.execute('UPDATE volume_feed SET since=NULL,through=NULL,logs_seen=0 WHERE chain_id=?',
                          (self.chain,))
        self.conn.execute('DELETE FROM volume_feed_optional WHERE chain_id=?', (self.chain,))
        if row is not None and row[0] is not None:
            window = now - row[0]
            self.conn.execute('INSERT INTO volume_feed_resets VALUES (?,?,?,?)',
                              (self.chain, now, reason, window))
            print(f'{iso(now)} volume window reset: {reason} after {window:.0f}s', flush=True)
        self.conn.commit()

    def confirm_optional(self, confirmed_ids, now):
        """Record that THIS socket confirmed both V2 subscriptions (ids 7 and 8); a reset clears it.
        One family alone is not the V2 tape, so it records nothing."""
        if set(OPTIONAL_SUBSCRIPTION_IDS) <= set(confirmed_ids):
            self.conn.execute('INSERT OR REPLACE INTO volume_feed_optional (chain_id, v2_confirmed_at) '
                              'VALUES (?,?)', (self.chain, float(now)))
            self.conn.commit()

    def token_decimals(self, token):
        row = self.conn.execute('SELECT decimals FROM volume_token_decimals WHERE chain_id=? AND token=?',
                                (self.chain, str(token).lower())).fetchone()
        return None if row is None else row[0]

    def set_token_decimals(self, token, value, now):
        """Cache decimals read from the chain. Anything but a plain integer in 0..255 is refused."""
        if type(value) is not int or not 0 <= value <= 255:
            return False
        self.conn.execute('INSERT OR REPLACE INTO volume_token_decimals VALUES (?,?,?,?)',
                          (self.chain, str(token).lower(), value, now))
        self.conn.commit()
        return True

    def stables(self):
        from sniper.settings import memecoin as cfg
        return tuple(t.lower() for t in cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get(self.chain, ()))

    def head(self, head, now):
        """Take one header. A REORDER IS NOT A GAP (2026-09-23).

        `last_block` is the highest CONTIGUOUS block, not the last one received. That distinction
        is the whole fix: publicnode began delivering 4663 headers permuted inside single
        milliseconds, and comparing each arrival against the previous ARRIVAL made every
        permutation look like a jump. Measured on the 03:19-03:31Z burst, 21 window resets in an
        hour, of which 16 were permutations with NOTHING missing -- 03:27:44 received
        687, 690, 688, 689, 692, 693, 691, 694, a complete run of 687..694, and reset five times.
        Each reset dropped the covered window, so the m5 gate read minutes of history instead of
        five (`covered 136s`) and fell back to the provider.

        Holes are tracked instead of feared: a block missing below the highest arrival opens a
        hole, a late header closes it, and only a hole still open after REORDER_GRACE_HEADERS
        later headers or REORDER_GRACE_S seconds is a real gap. Duplicates and reorders never
        reset; a genuine gap and a reconnect still do.

        LIMITATION, DELIBERATE: a hole cannot expire while NO header arrives, because the clock is
        only read when the next one lands. A silent feed with an open hole stays open. That is
        `feed_age`'s job and it already does it; a timer thread here would be a second liveness
        mechanism competing with the one that works.
        """
        try:
            block, at = int(head['number'], 16), int(head['timestamp'], 16)
        except (KeyError, TypeError, ValueError):
            # A FRAME WITHOUT A HEIGHT IS NOT A DISCONNECT (2026-09-28). It raised KeyError into the
            # loop, which closed the socket and dropped the window: 54 times on 8453 and 77 on 4663
            # in the logs. Skipping it loses nothing a real header would carry: a block that is
            # genuinely missing still opens a hole, and the hole still expires into a gap reset.
            if now - getattr(self, '_bad_head_logged', 0.0) >= 60.0:
                self._bad_head_logged = now
                print(f'{iso(now)} volume header skipped: keys {sorted(head)[:12] if isinstance(head, dict) else type(head).__name__}',
                      flush=True)
            return False
        block_hash = head.get('hash')
        known = self.hashes.get(block)
        if known == block_hash:
            return True                                   # a duplicate header changes nothing
        self.headers_seen += 1
        if known is not None:
            # SAME HEIGHT, DIFFERENT HASH IS A REORG, not a reorder, and it must still drop the
            # window: swaps already stored from the replaced block are no longer on the chain and
            # their `removed` notice may never arrive. The strict-successor code caught this only
            # by accident, because every non-successor reset; tolerating reorders would have lost
            # it silently.
            self.reset(now, f'head_gap:reorg:{block}')
        elif self.last_block is not None:
            if block <= self.last_block:
                if block < self.last_block - REORDER_WINDOW_BLOCKS:
                    # Too far back to be a reorder: a different socket, or a deep reorg.
                    self.reset(now, f'head_gap:backward:{self.last_block}->{block}')
                else:
                    # Old news inside the window: a height we already covered, never seen with a
                    # different hash. It cannot extend coverage and it invalidates nothing.
                    self._record(block, at, block_hash, now)
                    return True
            elif block > self.last_block + REORDER_WINDOW_BLOCKS:
                self.reset(now, f'head_gap:jump:{self.last_block}->{block}')
        if self.since is None:
            # The window opens when THIS socket received its first header. A lagging node's older
            # header would otherwise reopen time the previous socket covered, including any swap it
            # stored from a fork whose `removed` notice can no longer arrive.
            self.since = max(at, now)
        self.hashes[block] = block_hash
        self.block_ts[block] = at
        self.last_hash = block_hash
        if self.last_block is None:
            self.last_block = block
        while (self.last_block + 1) in self.hashes:      # advance over the late arrivals
            self.last_block += 1
        # THROUGH FOLLOWS THE CONTIGUOUS FRONTIER, never the highest arrival: coverage must not
        # claim a block whose predecessor is still missing.
        frontier_ts = self.block_ts.get(self.last_block)
        if frontier_ts is not None:
            self.through = frontier_ts if self.through is None else max(self.through, frontier_ts)
        highest = max(self.hashes)
        for b in range(self.last_block + 1, highest + 1):
            if b not in self.hashes and b not in self.holes:
                self.holes[b] = (now, self.headers_seen)
        for b in [b for b in self.holes if b in self.hashes or b <= self.last_block]:
            del self.holes[b]                            # the late header arrived: not a gap
        for b, (opened_at, opened_headers) in sorted(self.holes.items()):
            if (self.headers_seen - opened_headers >= REORDER_GRACE_HEADERS
                    or now - opened_at >= REORDER_GRACE_S):
                self.reset(now, f'head_gap:hole_expired:{b}->{block}')
                self.since = max(at, now)
                self.last_block, self.last_hash = block, block_hash
                self.hashes, self.block_ts = {block: block_hash}, {block: at}
                self.through = at
                break
        self._record(block, at, block_hash, now)
        return True

    def note_delivery(self, protocol, block_no, now):
        """One delivery-lag sample: how late this frame arrived after its block's own timestamp.

        ⚠️ THE HEADS SUBSCRIPTION CANNOT SEE THIS. On 2026-09-23 publicnode let the address-filtered
        V4 subscription drift to 652.9 s of lag while the topic-only V3 subscription on the same
        socket stayed at 0.6-1.4 s and `newHeads` stayed current, so `newest_block_age_s` read
        fresh, `feed_stale` never fired, and the lane summed an empty V4 window as a measurement.

        A frame whose header has not arrived contributes NO sample rather than a guessed one.
        """
        stamp = self.block_ts.get(block_no)
        if stamp is None:
            return
        samples = self.lag.get(protocol)
        if samples is not None:
            # (ARRIVAL, DELAY), not a bare delay (N2, 2026-09-23 recheck). The deque only evicts by
            # LENGTH, so a protocol that goes quiet keeps its last samples for ever: one 600 s
            # sample kept a subscription "lagging" indefinitely and refilled the window every pass,
            # while `_record` stamped that historical median `observed=now`. A sample now expires
            # by its own arrival time.
            samples.append((float(now), float(now) - float(stamp)))
            self.last_delivery[protocol] = float(now)

    def protocol_lag_s(self, protocol, now=None):
        """The MEDIAN lag for this protocol over RECENT samples, or None when there is no recent
        evidence.

        The median, not the max: per-minute MAX lag sits at 9.8 s (4663 v4) and 9.1 s (4663 v3) even
        when healthy and reached 18.7 s on a clean minute, so a max-rule fires on healthy traffic.
        The median separated the stall by two orders of magnitude -- 0.6-1.4 s against 57.9 s,
        339.9 s and 652.9 s.

        ⚠️ AND IT EXPIRES (N2). Samples leave this window by AGE, not only by the deque's length.
        Without that, a subscription that stops delivering keeps whatever it last measured: a single
        600 s sample held a protocol "lagging" for ever, refilled the window on every pass, and was
        written out with `observed=now` as though it had just been measured.

        EXPIRY IS UNKNOWN, NEVER ZERO. With no sample inside the window this returns None, which the
        gate already routes to blind. Returning 0.0 would claim a protocol measured as perfectly
        prompt at the exact moment it stopped speaking.

        `now=None` keeps the unexpired reading for callers that have no clock, which is the historic
        behaviour and is used by nothing that decides.
        """
        raw = list(self.lag.get(protocol) or ())
        if not raw:
            return None
        if now is not None and LAG_SAMPLE_MAX_AGE_S > 0:
            floor = float(now) - LAG_SAMPLE_MAX_AGE_S
            raw = [row for row in raw if row[0] >= floor]
        if not raw:
            return None
        return statistics.median(sorted(delay for _arrival, delay in raw))

    def last_delivery_s(self, protocol, now):
        """Seconds since this subscription last delivered a frame we could time, or None.

        The evidence the median cannot carry once it expires: "no recent sample" and "never any
        sample" are different states, and only this separates them.
        """
        seen = self.last_delivery.get(protocol)
        return None if seen is None else max(0.0, float(now) - float(seen))

    def stalled_window_s(self, ceiling_s, *, now=None, cap_s=BACKFILL_WINDOW_CAP_S):
        """Seconds of history to refill when a protocol is behind, or None when none is.

        Only a MEASURED lag triggers this. A chain that simply had no V4 swaps for a while has
        no samples, so it reads None and nothing is fetched -- the difference between quiet and
        late is exactly what the samples carry, and a block-count rule alone cannot tell them
        apart. Bounded by `cap_s` so one pass can never ask publicnode for an hour of logs.
        """
        try:
            ceiling = float(ceiling_s or 0.0)
        except (TypeError, ValueError):
            return None
        if ceiling <= 0:
            return None
        worst = None
        for protocol in ('v4', 'v3'):
            # WITH THE CLOCK, so a stale sample cannot keep refilling the window for ever (N2).
            lag = self.protocol_lag_s(protocol, now)
            if lag is not None and lag > ceiling and (worst is None or lag > worst):
                worst = lag
        if worst is None:
            return None
        return min(float(worst), float(cap_s))

    def _record(self, block, at, block_hash, now):
        self.hashes[block] = block_hash
        if len(self.hashes) > 64:
            del self.hashes[min(self.hashes)]
        # REPLACE carries the hash onto a row written before the column existed, so the window
        # stops reading `hash_unverified` as soon as a height is re-observed (F1).
        self.conn.execute('INSERT OR REPLACE INTO block_times '
                          '(chain_id, block_no, block_ts, fetched_at, block_hash) VALUES (?,?,?,?,?)',
                          (self.chain, block, iso(at), iso(now),
                           str(block_hash).lower() if block_hash else None))
        self.conn.execute('UPDATE volume_feed SET since=?,through=?,received=?,logs_seen=? WHERE chain_id=?',
                          (self.since, self.through, now, self.logs_seen, self.chain))
        self.conn.execute('INSERT OR REPLACE INTO volume_feed_lag '
                          '(chain_id,v4_lag_s,v3_lag_s,samples,observed,pons_lag_s) '
                          'VALUES (?,?,?,?,?,?)',
                          (self.chain, self.protocol_lag_s('v4', now),
                           self.protocol_lag_s('v3', now),
                           len(self.lag['v4']) + len(self.lag['v3']), now,
                           self.protocol_lag_s('pons', now)))
        self.conn.commit()

    def _known_quotes(self, now):
        """Tokens a pool may be quoted in, with their decimals: native, the chain quote, cached
        stables, and every unit the hop pricer priced in the last 10 minutes."""
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        quote = CHAIN_QUOTE[self.chain]
        known = {NATIVE: 18, quote['token'].lower(): quote['decimals']}
        for stable in self.stables():
            if self.token_decimals(stable) is not None:
                known[stable] = self.token_decimals(stable)
        for token, in self.conn.execute(
                'SELECT token FROM volume_quote_prices WHERE chain_id=? AND usd IS NOT NULL AND observed>=?',
                (self.chain, now - 600)):
            token = str(token).lower()
            if token not in known and self.token_decimals(token) is not None:
                known[token] = self.token_decimals(token)
        return known

    def register_v3_pool(self, pool, token0, token1, now, protocol='v3'):
        """A Uniswap V3 pool (its address is the id) with its two tokens, quote side by the same rule
        as a V4 pool. V3 carries about 27% of 4663's swap count (563 of 2,130 in 240 blocks,
        2026-09-22) and was invisible to the gate until this."""
        pool = str(pool).lower()
        if not (pool.startswith('0x') and len(pool) == 42):
            raise ValueError('a V3 pool id is a 20-byte address')
        currencies = [str(token0).lower(), str(token1).lower()]
        known = self._known_quotes(now)
        side = next((i for i, token in enumerate(currencies) if token in known), None)
        decimals = [known.get(token) for token in currencies]
        self.conn.execute('INSERT OR REPLACE INTO pool_meta '
                          '(chain_id,pool,token0,token1,quote_side,resolved_at,token0_decimals,token1_decimals) '
                          'VALUES (?,?,?,?,?,?,?,?)',
                          (self.chain,pool,*currencies,side,iso(now),*decimals))
        self.conn.executemany('INSERT OR REPLACE INTO onchain_pool_protocols VALUES (?,?,?,?)',
                             [(self.chain,pool,protocol,mint) for mint in currencies])
        self.conn.commit()

    def log_v3(self, log, now):
        """One V3 Swap frame from the topic-only subscription -> a v3_swap tick. The pool is
        registered later by the resolver thread; a tick for a pool not yet in pool_meta is kept
        (the gate prices it once the pool resolves) and never counted as a header-less swap."""
        if log.get('removed'):
            if log.get('transactionHash'):
                index = int(log['logIndex'], 16) if log.get('logIndex') is not None else None
                self.conn.execute('DELETE FROM pool_ticks WHERE chain_id=? AND tx_sig=? AND '
                                  '(? IS NULL OR log_index=?)',
                                  (self.chain, log['transactionHash'], index, index))
            self.reset(now, 'log_removed')
            return
        if (log.get('topics') or [None])[0] not in (V3_SWAP, PANCAKE_V3_SWAP):
            return
        row = decode_evm_log(self.chain, log, iso(now))
        if not row or row.get('kind') != 'v3_swap':
            return
        if row['block_no'] is None or row['log_index'] is None or not row['tx_sig']:
            self.reset(now, 'log_incomplete')
            return
        _insert_tick(self.conn, row)
        self.note_delivery('v3', row['block_no'], now)
        self.logs_seen += 1
        self.conn.execute('UPDATE volume_feed SET logs_seen=? WHERE chain_id=?',
                          (self.logs_seen, self.chain))
        self.conn.commit()

    def log_v2(self, log, now):
        """One Uniswap-V2 or Solidly/Aerodrome Swap frame from its topic-only subscription -> a
        `v2_swap` tick, exactly as `log_v3` (the resolver registers the pair later). No delivery-lag
        sample of its own: the reader judges `v2` pools on the V3 lag (memecoin_onchain_volume)."""
        if log.get('removed'):
            if log.get('transactionHash'):
                index = int(log['logIndex'], 16) if log.get('logIndex') is not None else None
                self.conn.execute('DELETE FROM pool_ticks WHERE chain_id=? AND tx_sig=? AND '
                                  '(? IS NULL OR log_index=?)',
                                  (self.chain, log['transactionHash'], index, index))
            self.reset(now, 'log_removed')
            return
        if (log.get('topics') or [None])[0] not in (V2_SWAP, SOLIDLY_SWAP):
            return
        row = decode_evm_log(self.chain, log, iso(now))
        if not row or row.get('kind') != 'v2_swap':
            return
        if row['block_no'] is None or row['log_index'] is None or not row['tx_sig']:
            self.reset(now, 'log_incomplete')
            return
        _insert_tick(self.conn, row)
        self.logs_seen += 1
        self.conn.execute('UPDATE volume_feed SET logs_seen=? WHERE chain_id=?',
                          (self.logs_seen, self.chain))
        self.conn.commit()

    def register_pons_curve(self, curve, mint, now, *, counter=None):
        """A launch proves curve/token; only the verified factory registry proves its counter.

        The curve is the pool id, not the token address displayed by some indexers. Pons supports
        both native and ERC-20 counters. An unknown counter stays unpriced until background
        verification; a repeated launch must preserve already verified metadata.
        """
        curve, mint = str(curve).lower(), str(mint).lower()
        if not (curve.startswith('0x') and len(curve) == 42 and mint.startswith('0x')
                and len(mint) == 42):
            raise ValueError('a Pons v2 curve and its token are 20-byte addresses')
        if counter is None:
            # The resolver commits on another connection. A SELECT followed by REPLACE can erase
            # its intervening proof; the condition belongs inside the same atomic write.
            with self.conn:
                written = self.conn.execute('INSERT INTO pool_meta '
                    '(chain_id,pool,token0,token1,quote_side,resolved_at,resolve_state) '
                    "VALUES (?,?,NULL,?,NULL,?,'pons_launch_only') ON CONFLICT(chain_id,pool) "
                    "DO UPDATE SET token0=NULL,token1=excluded.token1,quote_side=NULL,"
                    'resolved_at=excluded.resolved_at,resolve_state=excluded.resolve_state '
                    "WHERE COALESCE(pool_meta.resolve_state,'')!='pons_verified'",
                    (self.chain, curve, mint, iso(now)))
                if written.rowcount:
                    self.conn.execute('DELETE FROM onchain_pool_protocols WHERE chain_id=? AND pool=?',
                                      (self.chain,curve))
                    self.conn.execute('INSERT OR REPLACE INTO onchain_pool_protocols VALUES (?,?,?,?)',
                                      (self.chain,curve,'pons_v2',mint))
            return
        counter = str(counter).lower() if counter is not None else None
        if counter is not None:
            import re
            if not re.fullmatch(r'0x[0-9a-f]{40}', counter) or counter in (mint, curve):
                raise ValueError('invalid Pons counter')
        currencies = [counter, mint]
        decimals = [18 if token == NATIVE else self.token_decimals(token) if token else None
                    for token in currencies]
        self.conn.execute('INSERT OR REPLACE INTO pool_meta '
                          '(chain_id,pool,token0,token1,quote_side,resolved_at,token0_decimals,token1_decimals,resolve_state) '
                          'VALUES (?,?,?,?,?,?,?,?,?)',
                          (self.chain,curve,*currencies,0 if counter is not None else None,
                           iso(now),*decimals,
                           'pons_verified' if counter is not None else 'pons_launch_only'))
        self.conn.execute('DELETE FROM onchain_pool_protocols WHERE chain_id=? AND pool=?',
                          (self.chain, curve))
        self.conn.executemany('INSERT OR REPLACE INTO onchain_pool_protocols VALUES (?,?,?,?)',
                             [(self.chain,curve,'pons_v2',token) for token in currencies if token is not None])
        self.conn.commit()

    def log_pons(self, log, now):
        """One Pons v2 CurveBuy/CurveSell or Launched frame.

        A trade on a curve we have not seen launched is still written, exactly like a V3 tick for an
        unresolved pool: the row carries the curve as its pool id and the background resolver
        verifies its token and counter. Dropping it would lose the venue's only recorded swaps.
        """
        if log.get('removed'):
            if log.get('transactionHash'):
                index = int(log['logIndex'], 16) if log.get('logIndex') is not None else None
                self.conn.execute('DELETE FROM pool_ticks WHERE chain_id=? AND tx_sig=? AND '
                                  '(? IS NULL OR log_index=?)',
                                  (self.chain, log['transactionHash'], index, index))
            self.reset(now, 'log_removed')
            return
        head = (log.get('topics') or [None])[0]
        if head == PONS_V2_LAUNCHED:
            pair = decode_pons_v2_launched(log)
            if pair:
                self.register_pons_curve(pair[1], pair[0], now)
            return
        if head not in (PONS_V2_CURVE_BUY, PONS_V2_CURVE_SELL):
            return
        row = decode_evm_log(self.chain, log, iso(now))
        if not row or row.get('kind') != 'pons_v2_swap':
            return
        if row['block_no'] is None or row['log_index'] is None or not row['tx_sig']:
            self.reset(now, 'log_incomplete')
            return
        _insert_tick(self.conn, row)
        # Pons is measured under its OWN key (ULTRONICS, 2026-10-07): noted as V3, a Pons
        # subscription that delivered nothing read healthy beside live V3 frames.
        self.note_delivery('pons', row['block_no'], now)
        self.logs_seen += 1
        self.conn.execute('UPDATE volume_feed SET logs_seen=? WHERE chain_id=?',
                          (self.logs_seen, self.chain))
        self.conn.commit()

    def register_pool(self, pool, key, now):
        if v4_pool_id(key).lower() != str(pool).lower():
            raise ValueError('pool key hash mismatch')
        currencies = [key['currency0'].lower(), key['currency1'].lower()]
        known = self._known_quotes(now)
        side = next((i for i, token in enumerate(currencies) if token in known), None)
        decimals = [known.get(token) for token in currencies]
        # FEE, TICKSPACING AND HOOKS TRAVEL WITH THE CURRENCIES (2026-09-24). They are already
        # in `key`, and already verified by the hash check above, and without them nothing can
        # rebuild the PoolKey -- so the entry ladder had to relearn this pool from a provider or
        # an `eth_getLogs`. Measured on 4663: of the keys the ladder learned inside one retained
        # hour, `verified_at` came a median 346 s AFTER the pool's first tick (p90 417 s), which
        # is exactly the window a freshly graduated coin trades in. NAMED in the statement on
        # purpose: `INSERT OR REPLACE` erases any column it does not name, so a later
        # re-registration would otherwise blank a key `memecoin_v4_key_seed` had already used.
        self.conn.execute('INSERT OR REPLACE INTO pool_meta '
                          '(chain_id,pool,token0,token1,quote_side,resolved_at,token0_decimals,'
                          'token1_decimals,fee,tick_spacing,hooks) '
                          'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                          (self.chain,pool,*currencies,side,iso(now),*decimals,
                           int(key['fee']), int(key['tick_spacing']),
                           str(key.get('hooks') or '0x' + '00' * 20).lower()))
        self.conn.executemany('INSERT OR REPLACE INTO onchain_pool_protocols VALUES (?,?,?,?)',
                             [(self.chain,pool,'v4',mint) for mint in currencies])
        self.conn.commit()

    def log(self, log, now):
        if log.get('removed'):
            # A reorged swap must leave the tape, or a short covered window could count it.
            if log.get('transactionHash'):
                index = int(log['logIndex'], 16) if log.get('logIndex') is not None else None
                self.conn.execute('DELETE FROM pool_ticks WHERE chain_id=? AND tx_sig=? AND '
                                  '(? IS NULL OR log_index=?)',
                                  (self.chain, log['transactionHash'], index, index))
            self.reset(now, 'log_removed')
            return
        if str(log.get('address','')).lower() != self.manager:
            return
        if (log.get('topics') or [None])[0] == V4_INITIALIZE:
            from sniper.execution.evm_v4_quote import decode_initialize_log
            key = decode_initialize_log(log)
            if key:
                self.register_pool(log['topics'][1].lower(),key,now)
            return
        row = decode_v4_log(self.chain,log,iso(now))
        if not row or (log.get('topics') or [None])[0] != V4_SWAP:
            return
        if row['block_no'] is None or row['log_index'] is None or not row['tx_sig']:
            self.reset(now, 'log_incomplete')
            return
        _insert_tick(self.conn,row)
        self.note_delivery('v4', row['block_no'], now)
        self.logs_seen += 1
        self.conn.execute('UPDATE volume_feed SET logs_seen=? WHERE chain_id=?',
                          (self.logs_seen,self.chain))
        # COMMIT PER SWAP, WITHOUT AN FSYNC (2026-09-22). Batching swaps into the next header's
        # commit kept a write transaction open almost continuously, and the pricer thread, the
        # V3 resolver and the OTHER chain's worker (one store file) died with "database is locked"
        # within minutes (15 restarts an hour). synchronous=NORMAL was the saving; the batch was not.
        self.conn.commit()

    def set_quote_price(self, value, now):
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        valid = value is not None and math.isfinite(float(value)) and float(value)>0
        for token in [NATIVE,CHAIN_QUOTE[self.chain]['token'].lower()]:
            self.conn.execute(
                'INSERT OR REPLACE INTO volume_quote_prices (chain_id, token, usd, observed)'
                ' VALUES (?,?,?,?)',
                (self.chain,token,float(value) if valid else None,now))
        for token in self.stables():
            self.conn.execute(
                'INSERT OR REPLACE INTO volume_quote_prices (chain_id, token, usd, observed)'
                ' VALUES (?,?,?,?)',
                (self.chain,token,1.0,now))
        self.conn.commit()


def _protocol_lags(conn, chain):
    """The writer's own per-protocol delivery lag, or empty when it has never written one.

    A store from before this existed, or one a rolled-back writer is filling, yields no
    measurements and the gate behaves exactly as it did. Absent is unknown, never 'fresh'.
    """
    try:
        row = conn.execute('SELECT v4_lag_s, v3_lag_s, pons_lag_s FROM volume_feed_lag '
                           'WHERE chain_id=?', (str(chain),)).fetchone()
    except sqlite3.OperationalError:              # a store from before the Pons column
        try:
            row = conn.execute('SELECT v4_lag_s, v3_lag_s FROM volume_feed_lag WHERE chain_id=?',
                               (str(chain),)).fetchone()
        except sqlite3.OperationalError:
            return {}
        return {} if row is None else {'v4': row[0], 'v3': row[1]}
    if row is None:
        return {}
    return {'v4': row[0], 'v3': row[1], 'pons': row[2]}


def _quote_price_fn(conn, chain, at):
    """The tape's own USD price for a pool's QUOTE token at `at`, or None when stale."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.settings import memecoin as _cfg
    _bases={NATIVE,CHAIN_QUOTE[str(chain)]['token'].lower()}|{t.lower() for t in _cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get(str(chain),())}
    def price(_chain, token):
        # The loop rewrites the base prices every 30 s, so 60 s is stale for them. A hop-priced
        # unit (an equity token) is rewritten by a pass that itself takes tens of seconds, and an
        # equity price does not move enough in three minutes to change a five-minute volume sum.
        row=conn.execute('SELECT usd,observed FROM volume_quote_prices WHERE chain_id=? AND token=?',
                         (_chain,str(token).lower())).fetchone()
        limit=60 if str(token).lower() in _bases else float(_cfg.MEMECOIN_ONCHAIN_M5_HOP_PRICE_MAX_AGE_S)
        return row['usd'] if row and 0<=at-row['observed']<=limit else None
    return price


# E14 (spec 2026-10-08 funded momentum own data): the chains whose tape this store holds. 56 joins
# when the BSC store keeps a spot history (E14 step 2); 1 and 5042 have no pre-decision source.
MOMENTUM_CHAINS = ("4663", "8453")
MOMENTUM_WINDOW_S = 900.0        # MEMECOIN_FUNDED_TAPE_WINDOW_S's default; read below at call time


def momentum_series(conn, chain, mint, at):
    """The own spot series for the funded momentum shadow, under the volume gate's feed rules."""
    from sniper.services.memecoin_onchain_volume import own_spot_series, _subscription_key
    from sniper.services.memecoin_evm_tape_marks import LAG_TTL_S
    from sniper.settings import memecoin as _cfg
    unknown = {'reason': 'window_incomplete', 'points': []}
    feed = conn.execute('SELECT * FROM volume_feed WHERE chain_id=?', (str(chain),)).fetchone()
    if not feed or feed['since'] is None or feed['through'] is None or not feed['logs_seen']:
        return unknown
    if feed['received'] is None or not 0 <= at-feed['received'] <= 15 or not 0 <= at-feed['through'] <= 15:
        return {**unknown, 'reason': 'feed_stale'}
    # Shadow evidence must prove delivery even when the legacy volume gate has no lag samples.
    # Keep _protocol_lags and its admission compatibility unchanged.
    try:
        measurement = conn.execute('SELECT observed FROM volume_feed_lag WHERE chain_id=?',
                                   (str(chain),)).fetchone()
        lag_age = at - float(measurement[0])
    except (sqlite3.OperationalError, TypeError, ValueError):
        return {**unknown, 'reason': 'protocol_lag_unknown'}
    if not math.isfinite(lag_age) or not 0 <= lag_age <= LAG_TTL_S:
        return {**unknown, 'reason': 'protocol_lag_unknown'}
    lags = _protocol_lags(conn, chain)
    for key, value in lags.items():
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = None
        lags[key] = value if value is not None and math.isfinite(value) and value >= 0 else None
    series = own_spot_series(
        conn, chain, mint, at, since=float(feed['since']),
        window_s=float(getattr(_cfg, 'MEMECOIN_FUNDED_TAPE_WINDOW_S', MOMENTUM_WINDOW_S)),
        price_fn=_quote_price_fn(conn, chain, at), max_feed_age_s=15,
        protocol_lag_s=lags,
        max_protocol_lag_s=float(getattr(_cfg, 'MEMECOIN_ONCHAIN_M5_PROTOCOL_LAG_S', 0.0) or 0.0))
    if series['points']:
        key = _subscription_key(series.get('protocol'), lags)
        try:
            selected_lag = float(lags.get(key))
        except (TypeError, ValueError):
            selected_lag = None
        if selected_lag is None or not math.isfinite(selected_lag) or selected_lag < 0:
            return {**series, 'reason': 'protocol_lag_unknown', 'points': []}
    return series


def read_momentum(chain, mint):
    """One read-only momentum read of the separate tape store; never a network call, never raises."""
    from sniper.persistence.db_core import get_readonly_connection
    started = time.monotonic()
    try:
        c = get_readonly_connection(db_path())
        try:
            from sniper.util.clock import real_utc_now
            c.execute('PRAGMA busy_timeout=500')
            c.execute('BEGIN')
            at = real_utc_now()
            out = {**momentum_series(c, chain, mint, at), 'read_at': iso(at)}
        finally:
            c.close()
    except (sqlite3.Error, ValueError, OSError):
        out = {'reason': 'volume_store_unavailable', 'points': []}
    except Exception:
        logging.getLogger('memecoin').exception('own momentum read failed chain=%s mint=%s', chain, mint)
        out = {'reason': 'read_failed', 'points': []}
    out['read_ms'] = (time.monotonic() - started) * 1000.0
    return out


def measure(conn, chain, mint, at, *, exclude_tx=None, source_tokens=None):
    """Return a fresh lower bound over known V4 pools, or an explicit unknown."""
    unknown = {'usd':None,'swaps':0,'pools':[],'coverage':'unknown','reason':'window_incomplete'}
    feed = conn.execute('SELECT * FROM volume_feed WHERE chain_id=?',(str(chain),)).fetchone()
    if not feed or feed['since'] is None or feed['through'] is None or not feed['logs_seen']:
        return unknown
    if feed['received'] is None or not 0 <= at-feed['received'] <= 15 or not 0 <= at-feed['through'] <= 15:
        return {**unknown,'reason':'feed_stale'}
    # BOUND OVER THE COVERED INTERVAL ONLY (2026-09-16). This replaced a 300 s warm-up after every
    # reset. Only blocks stamped after this socket's first header count, so every swap summed arrived
    # on this socket, and a reorg on it deletes the swap. A short window UNDER-counts, and the gate
    # reads the number through max(). It cannot over-admit.
    covered = min(WINDOW_S, at-feed['since'])
    if covered <= 0:
        return unknown
    # A swap without a matching header cannot silently disappear from the measured window.
    missing = conn.execute('SELECT 1 FROM pool_ticks t LEFT JOIN block_times b '
                          'ON b.chain_id=t.chain_id AND b.block_no=t.block_no '
                          'WHERE t.chain_id=? AND t.observed_at>=? AND b.block_no IS NULL LIMIT 1',
                          (str(chain),iso(at-WINDOW_S))).fetchone()
    if missing:
        return {**unknown,'reason':'header_missing'}
    from sniper.settings import memecoin as _cfg
    price = _quote_price_fn(conn, chain, at)
    # The writer measured these; the gate only reads them. See `_worst_protocol_lag`.
    lags = _protocol_lags(conn, chain)
    out=onchain_volume_m5(conn,chain,mint,at,exclude_tx=exclude_tx,source_tokens=source_tokens,
                          price_fn=price,max_feed_age_s=15,
                          window_s=covered, protocol_lag_s=lags,
                          max_protocol_lag_s=float(getattr(
                              _cfg, 'MEMECOIN_ONCHAIN_M5_PROTOCOL_LAG_S', 0.0) or 0.0))
    out['covered_s']=covered
    if out['usd'] is not None:
        # The stream proves V4 coverage, not the absence of other protocols or unknown pools.
        out['coverage']='partial'
        out['coverage_scope']='known_v4_v3_pools'
        if not out['pools']:
            out['pools']=[{'pool':r[0],'protocol':r[1],'swaps':0,'usd':0.0} for r in conn.execute(
                'SELECT pool,protocol FROM onchain_pool_protocols WHERE chain_id=? AND mint=?',
                (str(chain),str(mint).lower()))]
    return out


def pool_meta_key(chain, pool):
    """The V4 PoolKey fields this tape resolved for `pool` (pool_meta), else None. Read-only and
    fail-quiet: the entry ladder's source-route hint (evm_source_route) treats None as unknown."""
    from sniper.persistence.db_core import get_readonly_connection
    conn = None
    try:
        conn = get_readonly_connection(str(db_path()))
        conn.execute("PRAGMA busy_timeout=50;")
        row = conn.execute(
            "SELECT token0, token1, fee, tick_spacing, hooks FROM pool_meta "
            "WHERE chain_id=? AND pool=?", (str(chain), str(pool).lower())).fetchone()
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()
    if not row or any(v is None for v in row):
        return None
    t0, t1, fee, spacing, hooks = row
    return {"currency0": str(t0).lower(), "currency1": str(t1).lower(), "fee": int(fee),
            "tick_spacing": int(spacing), "hooks": str(hooks).lower()}


def read_for_gate(chain, mint, *, exclude_tx=None, source_tokens=None):
    """Use only the separate local database. Do not fetch network data on an entry."""
    from sniper.persistence.db_core import get_readonly_connection
    try:
        c=get_readonly_connection(db_path())
        try:
            from sniper.util.clock import real_utc_now
            c.execute("BEGIN")
            c.execute("SELECT chain_id FROM volume_feed LIMIT 1").fetchone()
            at = real_utc_now()
            return {**measure(c,chain,mint,at,exclude_tx=exclude_tx,source_tokens=source_tokens),
                    "measured_at": at}
        finally:
            c.close()
    except (sqlite3.Error,ValueError,OSError):
        return {'usd':None,'reason':'volume_store_unavailable','pools':[],'coverage':'unknown'}


def feed_status(chain, now, *, path=None):
    """Read-only liveness of the observer: last header age, covered-window age and resets."""
    out = dict.fromkeys(FEED_STATUS_FIELDS)
    out['available'] = False
    path = Path(path or db_path())
    if not path.exists():
        return out
    from sniper.persistence.db_core import get_readonly_connection
    try:
        c = get_readonly_connection(str(path))
        try:
            c.execute('PRAGMA busy_timeout=500')   # the funded loop calls this; never wait long
            row = c.execute('SELECT * FROM volume_feed WHERE chain_id=?', (str(chain),)).fetchone()
            try:   # absent until the first writer of this release has run
                count = c.execute('SELECT COUNT(*) FROM volume_feed_resets WHERE chain_id=? AND at>?',
                                  (str(chain), now - 86400)).fetchone()[0]
                last = c.execute('SELECT at,reason FROM volume_feed_resets WHERE chain_id=? '
                                 'ORDER BY at DESC LIMIT 1', (str(chain),)).fetchone()
            except sqlite3.Error:
                count = last = None
        finally:
            c.close()
    except (sqlite3.Error, OSError):
        return out
    if row is None:
        return out
    out.update(available=True, logs_seen=row['logs_seen'], resets_24h=count,
               last_reset_at=last and last[0], last_reset_reason=last and last[1],
               feed_age_s=None if row['received'] is None else now - row['received'],
               window_age_s=None if row['since'] is None else now - row['since'])
    return out


def watch_feed(state, chain, status, *, threshold_s, send_fn=None):
    """Page once when the feed goes stale and once when it recovers. Returns the text sent."""
    age = (status or {}).get('feed_age_s')
    stale = age is None or age > threshold_s
    was_stale = bool(state.get(chain))
    state[chain] = stale
    if stale == was_stale:
        return None
    if stale:
        text = (f'{chain} on-chain m5 volume feed STALE: ' +
                ('store unavailable' if age is None else f'last header {age:.0f}s ago') +
                f', resets in 24 h {(status or {}).get("resets_24h")}. The volume gate falls back to the provider.')
        icon = '🟠'
    else:
        text = f'{chain} on-chain m5 volume feed recovered: last header {age:.0f}s ago'
        icon = '🟢'
    if send_fn is None:
        from sniper.services.memecoin_alerts import send_alert as send_fn
    send_fn('onchain_volume_feed', text, icon=icon)
    return text


def watch_feeds(state, now, *, send_fn=None):
    """Status of every enabled chain's feed for the runner heartbeat, paging stale transitions."""
    from sniper.settings import memecoin as cfg
    out = {}
    for chain in sorted(cfg.MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS):
        out[chain] = feed_status(chain, now)
        watch_feed(state, chain, out[chain], threshold_s=cfg.MEMECOIN_ONCHAIN_M5_FEED_STALE_ALERT_S,
                   send_fn=send_fn)
    return out or None


def _read_stable_decimals(ws, tape, now):
    """Ask the chain, once per stable, before subscribing. A failed read leaves the pool unpriced.

    Returns True when a new value was cached, so pools registered without it are registered again.
    """
    cached = False
    for rid, token in enumerate(tape.stables(), start=10):
        if tape.token_decimals(token) is not None:
            continue
        ws.send(json.dumps({'jsonrpc':'2.0','id':rid,'method':'eth_call',
                            'params':[{'to':token,'data':DECIMALS_CALL},'latest']}))
        try:
            reply = json.loads(ws.recv(timeout=15))
            value = int(reply['result'], 16) if reply.get('id') == rid else None
        except (TimeoutError, KeyError, TypeError, ValueError):
            value = None
        cached = tape.set_token_decimals(token, value, now) or cached
    return cached


_HOP_THREADS = {}

BACKFILL_BUDGET_S = 20.0
BACKFILL_PAGE_BLOCKS = 600
BACKFILL_SEED_HEADERS = 240
BACKFILL_ANCHOR_BLOCKS = 60
# How many tick-bearing V4 heights one pass will fetch a header for to re-verify their identity
# (F1, 2026-09-23). In a healthy pass this set is EMPTY: every canonical V4 tick-bearing height
# carries a log in the same pass. It fills only after a reorg during an outage, where it is a
# handful. A set larger than this means the log fetch itself is in doubt, so those identities are
# invalidated to NULL instead of asserted — unknown, never a smaller number.
BACKFILL_REPAIR_CAP = 200
BACKFILL_RESET_THROTTLE_S = 15.0


class RpcErrorReply:
    """A JSON-RPC error the caller tolerated, CARRIED rather than flattened to None.

    Collapsing every error into None made a rate limit indistinguishable from `execution reverted`,
    and `resolve_v3_pools` cached both as "not a pool" forever (2026-09-23 review, F5). None keeps
    its own meaning here: no reply arrived at all.
    """

    __slots__ = ('code', 'message')

    def __init__(self, error):
        error = error if isinstance(error, dict) else {}
        self.code = error.get('code')
        self.message = str(error.get('message') or '')[:200]

    @property
    def reverted(self):
        """The node ANSWERED that the contract refuses. A property of the contract, not of the
        connection, so a caller may cache it. Everything else is about the link or the node."""
        text = self.message.lower()
        return (self.code == 3 or 'execution reverted' in text or 'invalid opcode' in text
                or 'out of gas' in text)

    def __repr__(self):
        return 'RpcErrorReply(code=%r, message=%r)' % (self.code, self.message)


def _ws_caller(ws, *, tolerate_errors=False):
    """`call(list of (method, params)) -> list of results` over a socket with NO subscriptions on
    it, so every frame that comes back is a reply. Up to 100 requests are pipelined per call.
    With `tolerate_errors` a JSON-RPC error reply becomes an `RpcErrorReply` for that request
    instead of failing the batch (an `execution reverted` on one contract must not stall the other
    24), and a request whose reply never arrives stays None."""
    def call(calls):
        out = [None] * len(calls)
        for lo in range(0, len(calls), 100):
            chunk = calls[lo:lo + 100]
            for i, (method, params) in enumerate(chunk):
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': lo + i, 'method': method, 'params': params}))
            for _ in chunk:
                reply = json.loads(ws.recv(timeout=30))
                if not isinstance(reply, dict) or reply.get('error') or 'result' not in reply:
                    if tolerate_errors and isinstance(reply, dict) and reply.get('id') is not None:
                        out[int(reply['id'])] = RpcErrorReply(reply.get('error'))
                        continue
                    raise RuntimeError('rpc error %s' % str((reply or {}).get('error'))[:120])
                out[int(reply['id'])] = reply['result']
        return out
    return call


def backfill_window(tape, chain, rpc_call, now, *, window_s=None, budget_s=BACKFILL_BUDGET_S,
                    page_blocks=BACKFILL_PAGE_BLOCKS, stored_pool_key=None,
                    repair_cap=BACKFILL_REPAIR_CAP):
    """Fill the last `window_s` of manager logs and the block times behind them, so a socket that
    just connected (or just reset) is not blind for five minutes.

    WHY (2026-09-22). Every deploy, every watchdog relaunch and every stream reset opened a 300 s
    window in which the gate read `window_incomplete` and fell through to the provider's number;
    JOLLYBOT (d058ca73) was refused on $0.49 that way while the coin ran +98%, and one day's
    deploys alone cost about an hour of blind gate. The chain still has those five minutes.

    TRANSPORT. `rpc_call` is a pipelined caller over a SECOND socket to the same provider as the
    tape (publicnode serves manager-filtered eth_getLogs over WebSocket and refuses it over HTTP;
    the custody RPC serves it but rate-limits and competes with funded reads). Measured: five
    600-block pages hold about 16,900 logs and take about 8 s; 300 exact headers take 2.5 s.

    BLOCK TIMES. Nearly every block carries a swap (about 62 swaps/s over 10 blocks/s), so exact
    headers for all 3,000 blocks cost 23 s. Headers are exact for the newest BACKFILL_SEED_HEADERS
    blocks (their hashes let the headers the socket queued meanwhile arrive as duplicates instead
    of as a backwards jump that resets the window) and at every BACKFILL_ANCHOR_BLOCKS-th block;
    the rest are interpolated between anchors, which measured under 1 s of error (timestamps are
    whole seconds, 269 of 300 consecutive deltas were 0). The window filter is the only reader,
    so a sub-second edge error cannot over-count a five-minute sum.

    HONESTY RULES. Nothing is written until every log and every header it needs is in hand, so a
    failure or a blown budget leaves the store exactly as an unfilled connect would (a tick with no
    header would otherwise make the whole window `header_missing`). The window opens at the first
    backfilled block's own timestamp. Returns a summary dict, or None when nothing was written.
    """
    from sniper.execution.evm_v4_quote import _stored_pool_key as _default_key, decode_initialize_log
    stored_pool_key = stored_pool_key or _default_key
    window_s = float(window_s or WINDOW_S)
    started = time.monotonic()

    def budget():
        if time.monotonic() - started > budget_s:
            raise TimeoutError('backfill budget %.0fs exceeded' % budget_s)

    def block_ts(blk):
        return int(blk['timestamp'], 16)
    try:
        chain = str(chain)
        manager = manager_for(chain)
        head = int(rpc_call([('eth_blockNumber', [])])[0], 16)
        probe = max(0, head - page_blocks)
        head_blk, probe_blk = rpc_call([('eth_getBlockByNumber', [hex(head), False]),
                                        ('eth_getBlockByNumber', [hex(probe), False])])
        head_ts = block_ts(head_blk)
        rate = (head - probe) / max(1.0, float(head_ts - block_ts(probe_blk)))      # blocks per second
        start = max(0, head - int(window_s * rate * 1.15) - 8)
        for _ in range(4):
            budget()
            if block_ts(rpc_call([('eth_getBlockByNumber', [hex(start), False])])[0]) <= head_ts - window_s or start == 0:
                break
            start = max(0, start - int(window_s * rate * 0.25) - 8)
        logs, pons_logs = [], []
        lo = start
        while lo <= head:
            budget()
            hi = min(head, lo + page_blocks - 1)
            logs += rpc_call([('eth_getLogs', [{'fromBlock': hex(lo), 'toBlock': hex(hi), 'address': manager,
                                                'topics': [[V4_SWAP, V4_INITIALIZE]]}])])[0]
            # PONS V2 LAUNCHES (2026-09-23). Only the LAUNCHER can be backfilled: it is a singleton,
            # so this is an address-filtered request of the kind publicnode serves, while the curve
            # trades ride a topic-only subscription that the same endpoint refuses over getLogs --
            # the same asymmetry V3 lives with. Seeding curve -> mint here means a curve that
            # launched during the blind window is attributed the moment its first trade arrives.
            pons_logs += rpc_call([('eth_getLogs', [{'fromBlock': hex(lo), 'toBlock': hex(hi),
                                                     'address': PONS_V2_LAUNCHER,
                                                     'topics': [[PONS_V2_LAUNCHED]]}])])[0] or []
            lo = hi + 1
        logs.sort(key=lambda l: (int(l['blockNumber'], 16), int(l['logIndex'], 16)))
        observed = iso(now)
        rows, inits = [], []
        for log in logs:
            topic = (log.get('topics') or [None])[0]
            if topic == V4_INITIALIZE:
                inits.append(log)
                continue
            row = decode_v4_log(chain, log, observed)
            if row and topic == V4_SWAP and row['block_no'] is not None and row['log_index'] is not None and row['tx_sig']:
                rows.append(row)
        exact = sorted(set(range(start, head + 1, BACKFILL_ANCHOR_BLOCKS)) | {start, head}
                       | set(range(max(start, head - BACKFILL_SEED_HEADERS + 1), head + 1)))
        headers = {}
        for i in range(0, len(exact), 100):
            budget()
            chunk = exact[i:i + 100]
            for block, blk in zip(chunk, rpc_call([('eth_getBlockByNumber', [hex(b), False]) for b in chunk])):
                headers[block] = (block_ts(blk), blk.get('hash'))
        budget()
        anchors = sorted(headers)
        times = {}
        for a, b in zip(anchors, anchors[1:]):
            ta, tb = headers[a][0], headers[b][0]
            for n in range(a, b):
                times[n] = ta if b == a else int(ta + (tb - ta) * (n - a) / (b - a))
        times[head] = head_ts
        if any(r['block_no'] not in times for r in rows):
            raise RuntimeError('a swap lies outside the backfilled block range')
    except Exception as exc:
        print(f'{iso(now)} volume backfill skipped: {type(exc).__name__}: {exc}'[:300], flush=True)
        return None
    # everything is in hand: write pools, block times, ticks, then the window, in one transaction
    conn = tape.conn
    registered = set()
    pons_registered = 0
    for log in pons_logs:
        pair = decode_pons_v2_launched(log)
        if pair:
            tape.register_pons_curve(pair[1], pair[0], now)
            pons_registered += 1
    for log in inits:
        key = decode_initialize_log(log)
        if key:
            pool = log['topics'][1].lower()
            tape.register_pool(pool, key, now)
            registered.add(pool)
    known_pools = {r[0] for r in conn.execute('SELECT pool FROM pool_meta WHERE chain_id=?', (chain,))}
    for pool in {r['pool'] for r in rows} - known_pools - registered:
        key = stored_pool_key(chain, pool)
        if key:
            tape.register_pool(pool, key, now)
            registered.add(pool)
    # The backfill is what repairs a window after a restart: every height it can identify gets its
    # canonical hash written, so the strict join in `_swaps` can verify identity instead of
    # reporting `hash_unverified` (F1).
    #
    # ⚠️ TWO WAYS THIS ERASED IDENTITY BEFORE 2026-09-23 18:xxZ, and the second was the damaging one.
    # `headers` holds only the ANCHOR heights this backfill fetched (~294 of 3,458 in a normal
    # pass) while `times` is interpolated across the whole range, so writing `headers[b]` alone made
    # ~92% of the rows hash-less; and because the write was INSERT OR REPLACE, it also OVERWROTE the
    # hashes the live `_record` had already stamped from its own newHeads frames. Measured damage:
    # 158,981 of 205,977 4663 ticks were sitting on a hash-less header, so the on-chain m5 floor was
    # off on that chain entirely.
    #
    # EVERY BLOCK THAT HAS A TICK HAS A HASH AVAILABLE AT ZERO EXTRA RPC: the log itself carries
    # `blockHash`. A height with neither an anchor header nor a log keeps NULL, which is honest --
    # we do not know it -- and harmless, because no tick sits there.
    log_hashes = {}
    for log in logs + pons_logs:
        try:
            block_no = int(log['blockNumber'], 16)
        except (KeyError, TypeError, ValueError):
            continue
        block_hash = log.get('blockHash')
        if block_hash and block_no not in log_hashes:
            log_hashes[block_no] = str(block_hash).lower()

    def _hash_for(block_no):
        anchor = headers.get(block_no)
        if anchor and anchor[1]:
            return str(anchor[1]).lower()
        return log_hashes.get(block_no)

    # ⚠️ A HEIGHT IS ONLY CANONICAL IF THIS PASS COULD DECIDE IT (2026-09-23 recheck, F1). The
    # COALESCE rule above is right for a writer that LEARNS an identity and wrong for a pass that
    # has just established a stored one is unconfirmed: for an interpolated, non-anchor height with
    # no newly fetched log, the repair has no hash, COALESCE keeps the OLD FORK's, and the orphan
    # tick matches it and passes the canonical join. The review's proof is a $2,400 orphan at 750
    # summing $4,800 over two swaps where the chain holds $2,400 over one.
    #
    # SCOPED TO WHAT THIS PASS ASKED FOR. It fetches V4 swaps on the manager and Pons `Launched`,
    # and nothing else — this endpoint refuses topic-only getLogs, which is why V3 and Pons curve
    # trades ride subscriptions only. So a missing V4 log at a tick-bearing height is EVIDENCE, and
    # a missing V3 or Pons log is nothing at all. Invalidating on the latter would blind the
    # majority population on every backfill (4663 holds 63,978 V3 and 24,909 Pons ticks against
    # 103,058 V4; on 8453 V3 outnumbers V4). The discriminator is pool-id length: 66 is a V4 pool
    # id, 42 a V3 or Pons address, exact on every live row.
    #
    # A HEADER READ, NOT A NULL, because it answers. A canonical tick whose log this pass merely
    # missed is CONFIRMED; a replaced one MISMATCHES and becomes a decided exclusion rather than an
    # unknown. It costs nothing in a healthy pass: every canonical V4 tick-bearing height has a log
    # in the same pass, so the suspect set is empty. Above `repair_cap` the log fetch itself is in
    # doubt, and the honest answer there is NULL — `hash_unverified`, a blind window — never a
    # smaller number and never an assertion that all of them are orphans.
    suspects, invalidated = [], 0
    try:
        suspects = sorted(
            int(row[0]) for row in conn.execute(
                "SELECT DISTINCT block_no FROM pool_ticks WHERE chain_id=? AND block_no>=?"
                " AND block_no<=? AND length(pool)=66", (chain, start, head))
            if row[0] is not None and _hash_for(int(row[0])) is None)
    except Exception:
        logger_print = f'{iso(now)} volume backfill: suspect scan failed'
        print(logger_print[:300], flush=True)
    if suspects:
        if len(suspects) <= int(repair_cap or 0):
            try:
                for i in range(0, len(suspects), 100):
                    budget()
                    chunk = suspects[i:i + 100]
                    for block, blk in zip(chunk, rpc_call(
                            [('eth_getBlockByNumber', [hex(b), False]) for b in chunk])):
                        if blk and blk.get('hash'):
                            headers[block] = (block_ts(blk), blk.get('hash'))
            except Exception as exc:
                print(f'{iso(now)} volume backfill: identity repair failed: '
                      f'{type(exc).__name__}: {exc}'[:300], flush=True)
        still = [b for b in suspects if _hash_for(b) is None]
        if still:
            conn.executemany(
                'INSERT INTO block_times (chain_id, block_no, block_ts, fetched_at, block_hash) '
                'VALUES (?,?,?,?,NULL) ON CONFLICT(chain_id, block_no) DO UPDATE SET '
                'block_hash=NULL',
                [(chain, b, iso(times.get(b, head_ts)), observed) for b in still])
            invalidated = len(still)

    # A WRITE MAY ADD A HASH AND NEVER ERASE ONE. `COALESCE(excluded, existing)` is the rule, not
    # REPLACE: whichever writer learns the identity first keeps it.
    conn.executemany(
        'INSERT INTO block_times (chain_id, block_no, block_ts, fetched_at, block_hash) '
        'VALUES (?,?,?,?,?) ON CONFLICT(chain_id, block_no) DO UPDATE SET '
        'block_ts=excluded.block_ts, fetched_at=excluded.fetched_at, '
        'block_hash=COALESCE(excluded.block_hash, block_times.block_hash)',
        [(chain, b, iso(ts), observed, _hash_for(b)) for b, ts in times.items()])
    inserted = db_ticks.insert_ticks(conn, rows)
    tape.logs_seen += len(rows)
    start_ts = float(headers[start][0])
    tape.since = start_ts if tape.since is None else min(tape.since, start_ts)
    tape.through = float(head_ts) if tape.through is None else max(tape.through, float(head_ts))
    tape.last_block, tape.last_hash = head, headers[head][1]
    tape.hashes = {b: h for b, (_ts, h) in headers.items() if b > head - BACKFILL_SEED_HEADERS}
    conn.execute('UPDATE volume_feed SET since=?,through=?,received=?,logs_seen=? WHERE chain_id=?',
                 (tape.since, tape.through, now, tape.logs_seen, chain))
    conn.commit()
    summary = {'start': start, 'head': head, 'swaps': len(rows), 'inserted': inserted,
               'initializes': len(inits), 'registered': len(registered),
               'pons_launches': pons_registered, 'exact_headers': len(headers),
               'blocks': len(times), 'covered_s': round(float(head_ts - start_ts), 1),
               'identity_repaired': len(suspects) - invalidated,
               'identity_invalidated': invalidated,
               'took_s': round(time.monotonic() - started, 2)}
    print(f'{iso(now)} volume backfill: {summary}', flush=True)
    return summary


def backfill_over_socket(tape, chain, ws_url, now, *, connect_fn, **kwargs):
    """Open a second socket to the tape's own provider for the backfill, then close it."""
    try:
        with connect_fn(ws_url, open_timeout=10, max_size=None) as ws:
            return backfill_window(tape, chain, _ws_caller(ws), now, **kwargs)
    except Exception as exc:
        print(f'{iso(now)} volume backfill socket failed: {type(exc).__name__}: {exc}'[:300], flush=True)
        return None


_V3_THREADS = {}
V3_SEL_TOKEN0, V3_SEL_TOKEN1, V3_SEL_FEE = '0x0dfe1681', '0xd21220a7', '0xddca3f43'


def resolve_v3_pools(conn, chain, rpc_call, now, *, limit=25):
    """Register every V3 pool the tape has ticks for but pool_meta does not know: token0, token1
    and fee, three eth_calls per pool through `rpc_call([(method, params)]) -> results` (the same
    pipelined caller the backfill uses over a second socket to the tape's provider; the custody
    HTTP RPC answered 429 to every batch, 300 and 75 calls alike). Returns the number registered."""
    chain = str(chain)
    from sniper.settings import memecoin as _cfg
    soft_retry_s = float(getattr(_cfg, 'MEMECOIN_ONCHAIN_V3_SOFT_RETRY_S', 0.0) or 0.0)
    # A SOFT NEGATIVE EXPIRES, A REVERT DOES NOT (2026-09-23 review, F5). An empty pair written
    # because the node answered `0x` is `ambiguous`: a contract without token0() answers that way,
    # and so does a node that cannot serve the state. Those rows are re-asked once the retry window
    # has passed. A row from a real revert is `reverted` and is never asked again.
    stale_cutoff = iso(now - soft_retry_s) if soft_retry_s > 0 else None
    # ⚠️ AN UNSTAMPED ROW WOULD STILL SIT UNASKED, and it is left that way deliberately. The
    # expiry needs a `resolved_at` to compare, so an empty-pair row inserted with NULL there would
    # never match. No writer produces one — `memecoin_tick_stream.py:530` stamps it on INSERT OR
    # IGNORE and every volume-writer path stamps it on INSERT OR REPLACE — and verified on the live
    # store 2026-09-24: 107,060 NULL-state rows, 0 with a NULL `resolved_at`. Adding `OR
    # resolved_at IS NULL` would make such a row retry on EVERY pass with no window at all, which
    # is a worse failure than the one it guards. If a writer ever stops stamping it, stamp it there
    # rather than loosening the bound here.
    # ⚠️ A LEGACY ROW CARRIES NO STATE AT ALL, AND NULL MEANS TWO THINGS (2026-09-23 review, F5).
    # `resolve_state` was added by ALTER (`tick_features.py`), so every row written before it has
    # NULL — which is also what a RESOLVED pool carries. Selecting only 'ambiguous' therefore left
    # a legacy empty-pair row unasked forever: it has no pair to price with and no state to expire.
    # The pair itself is the discriminator that NULL is not: a row with no token0 was never
    # resolved, whatever its state column says, so it is an unknown and expires like an ambiguous
    # one. A NULL row WITH a pair is a resolved pool and stays untouched, as does every 'reverted'
    # row, which is a property of the contract and never re-asked.
    retryable = ("SELECT pool FROM pool_meta WHERE chain_id=? AND resolved_at <= ? "
                 "AND (resolve_state='ambiguous' "
                 "     OR (resolve_state IS NULL AND (token0 IS NULL OR token0=''))) ")
    pools = [r[0] for r in conn.execute(
        # `decode_v4_log` labels V4 swaps 'v3_swap' too (same data shape), so the pool id length is
        # the discriminator: a V3 pool is a 20-byte address, a V4 pool id is 32 bytes. Without this
        # the first live cycles spent every batch on unknown V4 ids and resolved no V3 pool at all.
        "SELECT DISTINCT pool FROM pool_ticks WHERE chain_id=? AND kind IN ('v3_swap','v2_swap') AND length(pool)=42 "
        "AND (pool NOT IN (SELECT pool FROM pool_meta WHERE chain_id=?)"
        + (" OR pool IN (" + retryable + ")" if stale_cutoff else "")
        + ") LIMIT ?",
        ((chain, chain, chain, stale_cutoff, int(limit)) if stale_cutoff
         else (chain, chain, int(limit))))]
    if not pools:
        return 0
    # A V2 PAIR HAS NO fee() (2026-09-28): asking it reverts, and a revert marks the pool permanently
    # unresolvable. Pairs are asked token0 and token1 only, and registered as protocol `v2`.
    v2 = {r[0] for r in conn.execute(
        "SELECT DISTINCT pool FROM pool_ticks WHERE chain_id=? AND kind='v2_swap' AND pool IN (%s)"
        % ",".join("?" * len(pools)), (chain, *pools))}
    selectors = {pool: ((V3_SEL_TOKEN0, V3_SEL_TOKEN1) if pool in v2
                        else (V3_SEL_TOKEN0, V3_SEL_TOKEN1, V3_SEL_FEE)) for pool in pools}
    calls = [('eth_call', [{'to': pool, 'data': sel}, 'latest'])
             for pool in pools for sel in selectors[pool]]
    answers = rpc_call(calls)
    tape = VolumeTape.__new__(VolumeTape)
    tape.conn, tape.chain = conn, chain
    done, at = 0, 0
    for pool in pools:
        answer = answers[at:at + len(selectors[pool])]
        at += len(selectors[pool])
        t0, t1 = answer[0], answer[1]
        if not (isinstance(t0, str) and isinstance(t1, str) and len(t0) == 66 and len(t1) == 66):
            # THREE ANSWERS, NOT ONE (2026-09-23 review, F5). This used to cache every
            # non-conforming reply as "not a V3 pool" forever, so a single rate limit or timeout
            # retired a real pool from the m5 sum permanently and left its ticks unpriced.
            if any(isinstance(a, RpcErrorReply) and not a.reverted or a is None for a in answer):
                # TRANSIENT: the link or the node, not the contract. Write NOTHING and let the next
                # pass ask again. This is the fix; it deliberately has no knob.
                continue
            # DETERMINISTIC: the node answered that the contract refuses, which is a property of
            # the contract, so the row is permanent. AMBIGUOUS (`0x` or a short string): a contract
            # without token0() answers that way and so does a node that cannot serve the state, so
            # the row expires and is re-asked.
            reverted = any(isinstance(a, RpcErrorReply) and a.reverted for a in answer)
            conn.execute('INSERT OR REPLACE INTO pool_meta '
                         '(chain_id,pool,token0,token1,quote_side,resolved_at,resolve_state) '
                         'VALUES (?,?,?,?,NULL,?,?)',
                         (chain, pool, '', '', iso(now),
                          'reverted' if reverted else 'ambiguous'))
            conn.commit()
            continue
        tape.register_v3_pool(pool, '0x' + t0[-40:], '0x' + t1[-40:], now,
                              protocol='v2' if pool in v2 else 'v3')
        done += 1
    return done


def _resolve_pool_metadata_pass(conn, chain, ws_url, connect_fn, now, *, due, pauses, period_s):
    """One background pass. Family failures have separate sockets and retry clocks."""
    from sniper.services.memecoin_pons_metadata import resolve_pons_curves
    from sniper.settings import memecoin as _cfg
    from sniper.util.clock import real_utc_now
    families = [('v3', resolve_v3_pools)]
    if _cfg.MEMECOIN_EVM_V4_LAUNCHPADS.get(str(chain)):
        families.append(('pons', resolve_pons_curves))
    for name, resolve in families:
        family_now = max(now, real_utc_now())
        if family_now < due.get(name, 0):
            continue
        try:
            with connect_fn(ws_url, open_timeout=10, max_size=None) as ws:
                n = resolve(conn, str(chain), _ws_caller(ws, tolerate_errors=True), family_now)
            if n:
                print(f'{iso(now)} {name} pools resolved: {n}', flush=True)
            pauses[name] = period_s
        except Exception as exc:
            conn.rollback()
            pauses[name] = min(120.0, pauses.get(name, period_s) * 3)
            print(f'{iso(now)} {name} pool resolve failed: {type(exc).__name__}: {exc}; '
                  f'next try in {pauses[name]:.0f}s'[:300], flush=True)
        due[name] = max(family_now, real_utc_now()) + pauses[name]


def _start_v3_resolver(chain, path, ws_url, *, connect_fn, period_s=5.0):
    """Resolve unknown V3 pools on a daemon thread with its own connection and its own socket."""
    import threading
    key = str(chain)
    thread = _V3_THREADS.get(key)
    if thread is not None and thread.is_alive():
        return thread

    def _loop():
        from sniper.util.clock import real_utc_now
        conn = connect(path)
        try:
            due, pauses = {}, {}
            while True:
                now = real_utc_now()
                _resolve_pool_metadata_pass(conn,key,ws_url,connect_fn,now,
                                             due=due,pauses=pauses,period_s=period_s)
                time.sleep(period_s)
        finally:
            conn.close()

    thread = threading.Thread(target=_loop, name='v3-pools-%s' % key, daemon=True)
    thread.start()
    _V3_THREADS[key] = thread
    return thread


def _start_hop_refresher(chain, path, *, period_s=30.0):
    """Price the equity quote units every `period_s` on a daemon thread with its own connection.

    The observer loop must never wait on the chain: a refresh that takes longer than the head-stall
    rule resets the covered window, and a reset window is a blind gate. This thread owns a separate
    SQLite connection; the loop's connection is never shared across threads.
    """
    import os as _os
    import threading
    key = str(chain)
    thread = _HOP_THREADS.get(key)
    if thread is not None and thread.is_alive():
        return thread
    rpc = (_os.environ.get('MEMECOIN_ONCHAIN_EVM_RPC_URL_%s' % key) or '').strip()
    if not rpc:
        return None

    def _loop():
        from sniper.util.clock import real_utc_now
        from sniper.services import memecoin_equity_hop_price as hop
        conn = connect(path)
        try:
            while True:
                now = real_utc_now()
                try:
                    summary = hop.refresh(conn, key, rpc, now)
                    if summary.get('priced') or summary.get('skipped'):
                        print(f'{iso(now)} hop prices: {len(summary["priced"])} priced, '
                              f'{summary["resolved"]} pools resolved, {summary["reads"]} reads, '
                              f'skipped {summary["skipped"]}'[:400], flush=True)
                except Exception as exc:
                    print(f'{iso(now)} hop prices failed: {type(exc).__name__}: {exc}'[:300], flush=True)
                time.sleep(period_s)
        finally:
            conn.close()

    thread = threading.Thread(target=_loop, name='hop-prices-%s' % key, daemon=True)
    thread.start()
    _HOP_THREADS[key] = thread
    return thread


OPTIONAL_SUBSCRIPTION_IDS = (7, 8)       # the V2 topics: a refusal skips them, never the socket


def subscriptions(chain):
    """[(id, eth_subscribe params)] for one chain's tape socket. Ids 2-6 are today's list, always;
    7 and 8 (Uniswap-V2 and Solidly/Aerodrome Swap, topic-only) only on a chain in
    MEMECOIN_ONCHAIN_V2_SWAP_CHAINS (spec 2026-09-28-memecoin-8453-tape-v2-swaps-design.md)."""
    from sniper.settings import memecoin as cfg
    subs = [(2, ['newHeads']),
            (3, ['logs', {'address': manager_for(chain), 'topics': [[V4_SWAP, V4_INITIALIZE]]}]),
            (4, ['logs', {'topics': [[V3_SWAP, PANCAKE_V3_SWAP]]}]),
            (5, ['logs', {'topics': [[PONS_V2_CURVE_BUY, PONS_V2_CURVE_SELL]]}]),
            (6, ['logs', {'address': PONS_V2_LAUNCHER, 'topics': [[PONS_V2_LAUNCHED]]}])]
    if str(chain) in cfg.memecoin_onchain_v2_swap_chains():
        subs += [(7, ['logs', {'topics': [[V2_SWAP]]}]), (8, ['logs', {'topics': [[SOLIDLY_SWAP]]}])]
    return subs


class SubscriptionWatch:
    """A REQUIRED subscription that went silent on a socket still delivering OTHER logs.

    2026-09-29 01:02Z, 8453: after a reconnect the V4 subscription (id 3) delivered nothing for an
    hour while V3 on the same socket carried 1,100-3,400 swaps a minute. Nothing reconnected, because
    heads kept flowing, and V4's lag expired to NULL rather than growing. Silence alone is no
    evidence on a quiet chain, so a subscription is due only when it has been silent for
    `silence_s` AND the socket carried at least MIN_OTHER_FRAMES log frames of other subscriptions
    in that time. `silent()` marks each due one as just re-opened, so the clock starts again.

    ALL OF THEM SILENT BESIDE HEADS (2026-10-15, 4663, operator yes via Alpha): V4 and V3 stopped
    together from about 05:55:11 to 06:11:48Z while newHeads kept arriving; with both swap topics
    quiet only sparse Pons frames counted as "other", so the re-open came after 997 s (sub 3 silent
    26,055 s that day by 09:40Z). When EVERY required subscription has been silent for the
    threshold and at least MIN_HEAD_FRAMES good heads arrived meanwhile, all are re-opened. The
    threshold starts at `silence_s` and doubles after each such re-open with no swap frame since,
    up to ALL_SILENT_MAX_FACTOR x, so a genuinely quiet chain costs one re-open per 2-16 min (at
    120 s), never a storm; any required frame resets it. `last_all_silent_heads` names the heads
    behind the latest such re-open (None for the per-subscription rule)."""

    REQUIRED = (3, 4)
    MIN_OTHER_FRAMES = 20
    MIN_HEAD_FRAMES = 20
    ALL_SILENT_MAX_FACTOR = 8

    def __init__(self, silence_s):
        self.silence_s = float(silence_s or 0.0)
        self.since = {}          # rid -> its confirmation, last frame or last re-open, the latest
        self.others = {}         # rid -> log frames of other subscriptions since `since`
        self.heads = 0           # good heads since the latest required frame, sent or re-open
        self.all_silent_s = self.silence_s
        self.last_all_silent_heads = None

    def sent(self, rid, now):
        """The subscribe went out: silence is counted from here, answered or not."""
        if rid in self.REQUIRED:
            self.since[rid], self.others[rid] = float(now), 0
            self.heads = 0

    confirm = sent               # a confirmation restarts the same clock

    def frame(self, rid, now):
        for req in self.since:
            if req == rid:
                self.since[req], self.others[req] = float(now), 0
            else:
                self.others[req] += 1
        if rid in self.REQUIRED:
            self.heads, self.all_silent_s = 0, self.silence_s   # swaps flow: no backoff

    def head(self, now):
        """One good head (tape.head accepted it): proof the socket itself is alive."""
        self.heads += 1

    def silent(self, now):
        """[(rid, silent_s, other_frames)] due for a re-open now."""
        self.last_all_silent_heads = None
        if self.silence_s <= 0:
            return []
        due = []
        for rid, since in self.since.items():
            quiet = float(now) - since
            if quiet >= self.silence_s and self.others[rid] >= self.MIN_OTHER_FRAMES:
                due.append((rid, quiet, self.others[rid]))
                self.since[rid], self.others[rid] = float(now), 0
        if (not due and self.since and self.heads >= self.MIN_HEAD_FRAMES
                and all(float(now) - since >= self.all_silent_s for since in self.since.values())):
            self.last_all_silent_heads = self.heads
            for rid, since in self.since.items():
                due.append((rid, float(now) - since, self.others[rid]))
                self.since[rid], self.others[rid] = float(now), 0
            self.heads = 0
            self.all_silent_s = min(self.all_silent_s * 2, self.silence_s * self.ALL_SILENT_MAX_FACTOR)
        return due


PONS_SIDE_QUEUE_MAX = 50_000


def pons_side_url(chain, main_url):
    """MEMECOIN_ONCHAIN_M5_PONS_WS_URL_<chain>, a second provider for the Pons topics only, or None.

    WHY (ULTRONICS, 4663, 2026-10-07): publicnode confirmed the topic-only Pons subscription and
    delivered 0 of 119 chain events from about 00:56Z, while the research QuickNode WS delivered 26
    of 26 in a parallel window. Empty, not wss, or the main socket's own URL: off (today's tape)."""
    url = os.getenv('MEMECOIN_ONCHAIN_M5_PONS_WS_URL_%s' % chain, '').strip()
    if not url.startswith('wss://') or url.rstrip('/') == str(main_url or '').strip().rstrip('/'):
        return None
    return url


def _pons_side_socket(chain, url, out, stop, *, connect_fn, clock, sleep=time.sleep):
    """Put (frame, arrival) for every Pons curve trade and launch on `out`. It NEVER touches the tape:
    the writer's main loop drains `out` on its own thread (drain_pons_side), so the tape, its lag
    samples and its database connection keep a single writer. Reconnects with backoff 1..60 s; a
    full queue drops and counts (a lower bound, logged at most once a minute)."""
    pons = {rid: params for rid, params in subscriptions(chain) if rid in (5, 6)}
    delay, dropped, last_note = 1, 0, 0.0
    while not stop.is_set():
        try:
            with connect_fn(url, open_timeout=10) as ws:
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []}))
                reply = json.loads(ws.recv(timeout=15))
                if reply.get('id') != 1 or int(reply.get('result', '0x0'), 16) != int(chain):
                    raise ValueError('wrong chain')
                for rid, params in pons.items():
                    ws.send(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': 'eth_subscribe',
                                        'params': params}))
                ids = {}
                while not stop.is_set():
                    try:
                        msg = json.loads(ws.recv(timeout=15))
                    except TimeoutError:
                        continue                          # Pons can be quiet; heads are not here
                    if msg.get('id') in pons:
                        if not msg.get('result'):
                            raise ValueError('pons side subscription refused: %s' % msg.get('error'))
                        ids[msg['result']] = msg['id']
                        print(f"{iso(clock())} pons side subscription {msg['id']} confirmed", flush=True)
                        continue
                    params = msg.get('params') or {}
                    if ids.get(params.get('subscription')) is None:
                        continue
                    try:
                        out.put_nowait((params.get('result') or {}, clock()))
                    except queue.Full:
                        dropped += 1
                        if clock() - last_note >= 60:
                            last_note = clock()
                            print(f'{iso(last_note)} pons side queue full: {dropped} frames dropped',
                                  flush=True)
                    delay = 1
        except Exception as exc:
            if stop.is_set():
                return
            print(f'{iso(clock())} pons side socket: {type(exc).__name__}: {exc}'[:200], flush=True)
            sleep(delay)
            delay = min(60, delay * 2)


def drain_pons_side(tape, side_queue):
    """Write every queued side-socket Pons frame through log_pons on the caller's (main) thread."""
    n = 0
    while side_queue is not None:
        try:
            frame, arrival = side_queue.get_nowait()
        except queue.Empty:
            break
        tape.log_pons(frame, arrival)
        n += 1
    return n


RESUBSCRIBE_BASE_ID = 100        # re-open requests; 2-8 are the connect-time list
UNSUBSCRIBE_BASE_ID = 500


def fatal_subscription_error(msg):
    """True when an `error` frame must close the socket (today's rule), False when it answers one
    of the optional V2 subscriptions (logged and skipped) or is no error at all."""
    if not msg.get('error'):
        return False
    return msg.get('id') not in OPTIONAL_SUBSCRIPTION_IDS


def run(chain, ws_url, *, path=None, duration_s=None, connect_fn=None):
    """Receive observations with bounded reconnect delay and no funded writes."""
    from websockets.sync.client import connect as ws_connect
    from sniper.execution.evm_v4_quote import _stored_pool_key
    from sniper.execution.evm_wallet import quote_usd_price
    from sniper.util.clock import real_utc_now
    if not ws_url.startswith('wss://'):
        raise ValueError('a dedicated wss URL is required')
    clock=real_utc_now
    conn=connect(path);tape=VolumeTape(conn,chain,clock());delay=1;known=set();misses={}
    started=time.monotonic()
    active=lambda:duration_s is None or time.monotonic()-started < duration_s
    ws_connect=connect_fn or ws_connect
    side_url=pons_side_url(chain,ws_url);side_queue=side_stop=side_thread=None
    if side_url:
        side_queue,side_stop=queue.Queue(maxsize=PONS_SIDE_QUEUE_MAX),threading.Event()
        side_thread=threading.Thread(target=_pons_side_socket,name=f'pons-side-{chain}',daemon=True,
                                     args=(chain,side_url,side_queue,side_stop),
                                     kwargs=dict(connect_fn=ws_connect,clock=clock))
        side_thread.start()
    try:
        while active():
            tape.reset(clock(),'connect')
            try:
                with ws_connect(ws_url,open_timeout=10) as ws:
                    ws.send(json.dumps({'jsonrpc':'2.0','id':1,'method':'eth_chainId','params':[]}))
                    reply=json.loads(ws.recv(timeout=15))
                    if reply.get('id')!=1 or int(reply.get('result','0x0'),16)!=int(chain):
                        raise ValueError('wrong chain')
                    if _read_stable_decimals(ws,tape,clock()):
                        known.clear()
                    # V3 SWAPS BY TOPIC ALONE (2026-09-22): publicnode accepts a topic-only log
                    # subscription (252 frames in 12 s over 81 pools measured) although it refuses
                    # an address-less eth_getLogs, so V3 has no backfill; its five minutes fill live.
                    # PONS V2 (2026-09-23): the curves are per-token and unverifiable by
                    # address, so their trades ride a topic-only filter like V3's, while the
                    # launcher is a singleton and gets an address filter that names curve -> mint.
                    # SILENT-SUBSCRIPTION WATCH, PER SOCKET (2026-09-29, 8453 V4). See SubscriptionWatch.
                    # Its clock starts at SEND (Gamma LOW 2), so a subscribe never answered at all
                    # is re-opened like one that went quiet.
                    from sniper.settings import memecoin as _cfg
                    watch=SubscriptionWatch(_cfg.onchain_sub_silence_s_for(chain))
                    resub={};unsub=set();next_req=RESUBSCRIBE_BASE_ID;next_unsub=UNSUBSCRIBE_BASE_ID
                    for rid,params in subscriptions(chain):
                        ws.send(json.dumps({'jsonrpc':'2.0','id':rid,'method':'eth_subscribe','params':params}))
                        watch.sent(rid,clock())
                    # FILL THE WINDOW BEFORE THE FIRST SOCKET HEADER (2026-09-22). The subscriptions are
                    # already open, so every header and log the chain produces meanwhile queues on the
                    # socket and drains as duplicates once the loop starts.
                    if _cfg.MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED:
                        if backfill_over_socket(tape,chain,ws_url,clock(),connect_fn=ws_connect):
                            known.update(r[0] for r in conn.execute('SELECT pool FROM pool_meta WHERE chain_id=?',(str(chain),)))
                    subs={};next_price=next_prune=0;head_received=clock();next_ping=head_received+PING_INTERVAL_S;next_backfill=0.0
                    while active():
                        msg=json.loads(ws.recv(timeout=15));now=clock()
                        drain_pons_side(tape,side_queue)
                        if msg.get('id') in unsub:        # a re-open's cleanup answer, whatever it says
                            unsub.discard(msg['id'])
                            continue
                        if fatal_subscription_error(msg):
                            raise ValueError('provider rejected subscription')
                        if msg.get('error'):
                            print(f"{iso(now)} volume optional subscription {msg.get('id')} refused: {str(msg.get('error'))[:160]}",flush=True)
                            continue
                        if msg.get('id') in resub and msg.get('result'):
                            rid=resub.pop(msg['id']);subs[msg['result']]=rid;watch.confirm(rid,now)
                            print(f'{iso(now)} volume subscription {rid} re-confirmed',flush=True)
                            continue
                        if msg.get('id') in (2,3,4,5,6,7,8) and msg.get('result'):
                            subs[msg['result']]=msg['id']
                            # LOGGED PER ID (2026-09-29): the 8453 V4 loss could not be told apart
                            # from a never-confirmed subscription, because nothing recorded either.
                            print(f"{iso(now)} volume subscription {msg['id']} confirmed",flush=True)
                            watch.confirm(msg['id'],now)
                            if msg['id'] in OPTIONAL_SUBSCRIPTION_IDS:
                                tape.confirm_optional(set(subs.values()),now)
                            continue
                        params=msg.get('params') or {};kind=subs.get(params.get('subscription'));item=params.get('result') or {}
                        if len(subs)<2:
                            continue
                        if kind==2:
                            _resets_before=tape.reset_count
                            if tape.head(item,now):          # a skipped frame is no proof of life (T2)
                                head_received=now;delay=1
                                watch.head(now)
                            # A RESET MID-STREAM REFILLS TOO (2026-09-23). publicnode delivers headers
                            # out of order in bursts (70186674 -> 77 -> 75 -> 79 -> 76; 21 head_gap
                            # resets in one hour on 4663), and each reset dropped the window so the
                            # gate read minutes of history instead of five. The backfill ran only at
                            # connect; now it runs after any reset, at most once per
                            # BACKFILL_RESET_THROTTLE_S, so a burst costs seconds of blindness.
                            if tape.reset_count!=_resets_before and _cfg.MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED and now>=next_backfill:
                                next_backfill=now+BACKFILL_RESET_THROTTLE_S
                                if backfill_over_socket(tape,chain,ws_url,clock(),connect_fn=ws_connect):
                                    known.update(r[0] for r in conn.execute('SELECT pool FROM pool_meta WHERE chain_id=?',(str(chain),)))
                                head_received=clock()
                            # A SUBSCRIPTION THAT FALLS BEHIND NEVER RESETS ANYTHING (2026-09-23).
                            # publicnode let the V4 subscription drift to 652.9 s on a live socket
                            # and then dropped 2,754 blocks; there was no disconnect, no head gap
                            # and no reset, so the refill above could not fire and nothing filled
                            # the hole. A MEASURED protocol lag now triggers the same bounded
                            # refill, on this thread, under the same throttle.
                            elif _cfg.MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED and now>=next_backfill:
                                _stalled=tape.stalled_window_s(getattr(_cfg,'MEMECOIN_ONCHAIN_M5_PROTOCOL_LAG_S',0.0), now=now)
                                if _stalled:
                                    next_backfill=now+BACKFILL_RESET_THROTTLE_S
                                    print(f'{iso(now)} volume protocol stall: refilling {_stalled:.0f}s',flush=True)
                                    if backfill_over_socket(tape,chain,ws_url,clock(),connect_fn=ws_connect,window_s=_stalled):
                                        known.update(r[0] for r in conn.execute('SELECT pool FROM pool_meta WHERE chain_id=?',(str(chain),)))
                                    head_received=clock()
                        elif kind==4:
                            tape.log_v3(item,now)
                            _start_v3_resolver(chain,path,ws_url,connect_fn=ws_connect)
                        elif kind in (5,6):
                            tape.log_pons(item,now)
                            _start_v3_resolver(chain,path,ws_url,connect_fn=ws_connect)
                        elif kind in (7,8):
                            tape.log_v2(item,now)
                            _start_v3_resolver(chain,path,ws_url,connect_fn=ws_connect)
                        elif kind==3:
                            tape.log(item,now)
                            pool=(item.get('topics') or [None,None])[1]
                            if pool and pool not in known and now>=misses.get(pool,0):
                                key=_stored_pool_key(chain,pool)
                                if key:
                                    tape.register_pool(pool,key,now);known.add(pool)
                                else:
                                    misses[pool]=now+60
                        if kind in (3,4,5,6,7,8):
                            watch.frame(kind,now)
                        for rid,quiet,others in watch.silent(now):
                            # Re-open on THIS socket: a reconnect would reset the covered window.
                            heads=watch.last_all_silent_heads
                            print(f'{iso(now)} volume subscription {rid} silent {quiet:.0f}s beside '
                                  + (f'{heads} heads, all swap subscriptions silent: re-subscribing'
                                     if heads is not None else f'{others} other log frames: re-subscribing'),flush=True)
                            for sid in [sid for sid,k in subs.items() if k==rid]:
                                ws.send(json.dumps({'jsonrpc':'2.0','id':next_unsub,'method':'eth_unsubscribe','params':[sid]}))
                                unsub.add(next_unsub);next_unsub+=1;del subs[sid]
                            ws.send(json.dumps({'jsonrpc':'2.0','id':next_req,'method':'eth_subscribe',
                                                'params':dict(subscriptions(chain))[rid]}))
                            resub[next_req]=rid;next_req+=1
                            # V4 has a log backfill; refill the silent span (V3 has none, so its hole
                            # stays, as for any span a provider dropped).
                            if rid==3 and _cfg.MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED and now>=next_backfill:
                                next_backfill=now+BACKFILL_RESET_THROTTLE_S
                                if backfill_over_socket(tape,chain,ws_url,clock(),connect_fn=ws_connect,
                                                        window_s=min(quiet,BACKFILL_WINDOW_CAP_S)):
                                    known.update(r[0] for r in conn.execute('SELECT pool FROM pool_meta WHERE chain_id=?',(str(chain),)))
                                head_received=clock()
                        if now-head_received>15:
                            raise TimeoutError('head stream stalled')
                        if now>=next_ping:
                            next_ping=now+PING_INTERVAL_S
                            ws.ping()
                        if now>=next_price:
                            next_price=now+30
                            try:value=quote_usd_price(chain)
                            except Exception:value=None
                            tape.set_quote_price(value,now)
                            # ONE HOP PRICES THE EQUITY QUOTE UNITS (2026-09-22): in its OWN
                            # thread. The first cut ran the refresh inline and its 30-80 eth_calls
                            # took longer than the 15 s head-stall rule, so the observer reset its
                            # window every 30 s (21:03-21:10Z) and every gate read window_incomplete.
                            _start_hop_refresher(chain, path)
                        if now>=next_prune:
                            next_prune=now+60
                            prune(conn,chain,now)
                            misses={p:ts for p,ts in misses.items() if ts>now}
            except Exception as exc:
                now=clock()
                tape.reset(now,f'disconnect:{type(exc).__name__}')
                print(f'{iso(now)} volume stream disconnected: {type(exc).__name__}: {exc}'[:300],flush=True)
                time.sleep(delay);delay=min(60,delay*2)
    finally:
        if side_stop is not None:
            side_stop.set();side_thread.join(timeout=2)
        tape.reset(clock(),'stop');conn.close()


def _gate_source_tx(chain, mint, origin):
    """The source transaction behind an origin id, or None. `swap:raybot:<tx>...` and `onchain:<tx>`
    carry it first; the EVM on-chain lane's `onchain:<chain>:<tx>:<mint>` and the 4663 fast lane's
    `rhfast:<chain>:<tx>:<mint>` count only for this chain and this coin. ONE parse for every chain
    (2026-09-28): the M5 chains used to accept only the first two forms, so the on-chain lane's 4663
    buys lost the tape bound J7 got for the same buy."""
    import re
    match = re.match(r'^(?:swap:raybot:|onchain:)(0x[0-9a-fA-F]{64})(?::|$)', str(origin or ''))
    source_tx = match.group(1) if match else None
    composite = re.fullmatch(
        r'(?:onchain|rhfast):([0-9]+):(0x[0-9a-fA-F]{64}):(0x[0-9a-fA-F]{40})', str(origin or ''))
    if composite and composite[1] == str(chain) and composite[3].lower() == str(mint).lower():
        source_tx = composite[2]
    return source_tx


def prune(conn, chain, now):
    """Drop this chain's swaps and headers older than its KEEP_S horizon (the stream loop, every 60 s)."""
    cutoff = iso(now - KEEP_S.get(str(chain), KEEP_DEFAULT_S))
    conn.execute('DELETE FROM pool_ticks WHERE chain_id=? AND observed_at<?', (chain, cutoff))
    conn.execute('DELETE FROM block_times WHERE chain_id=? AND block_ts<?', (chain, cutoff))
    conn.commit()


def gate_evidence(chain, mint, origin, *, stats=None, source_tokens=None, source_pool=None,
                  source_usd=None):
    """Read an optional lower bound only when the source transaction is identifiable.
    `source_tokens` (whole tokens the source bought, when known) lets the read check that the tape
    holds the source's coin, not just its transaction (rule 3 per venue, ULTRONICS 2026-10-07).

    SOURCE TX UNKNOWN (a J7 swap event; spec 2026-10-15 m5 provider lag, option d): with
    MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND, `source_usd` (the source's paid USD, the size the gate
    excludes) turns the tape's whole m5 window into a bound: max(0, total - source_usd). Whether or
    not the source's swap is in the window, that never overstates organic volume. A blind or
    unreadable tape, or an unknown source size, keeps today's `source_tx_unknown` (the provider
    decides), never a blind reason: that would make the floor undecidable and fail open."""
    from sniper.settings import memecoin as cfg
    if str(chain) == '56':
        # The BSC observer is independent of the V4/V3 writer. A source-venue V2 lower bound
        # helps a sparse provider snapshot without making an RPC call on the entry path.
        source_tx = _gate_source_tx(chain, mint, origin)
        if source_tx is None:
            return {'usd':None,'reason':'source_tx_unknown','pools':[],'coverage':'unknown'}
        # The provider's pair while DexScreener is read for 56; our own source pair (the PancakeSwap-V2
        # mint/WBNB pair, accepted by read_m5 only when the source swapped in it) when the provider
        # gives none or E7:56 is off (entry-gates spec step 4). Measured 10-01..10-08: the provider's
        # pair equals ours on 14 of 73 attempts that had one, and the source's own pool was that
        # pair on 15 of 71 decoded sources, so ours covers fewer coins: it is the no-DexScreener
        # fallback, not an upgrade.
        from sniper.services import memecoin_bsc_v2_live as bsc
        st = stats or {}
        pool, quote = st.get('pair_address'), st.get('quote_address')
        if not pool or cfg.dexscreener_off("E7", "56"):
            # The source receipt's own V2 or V3 pool and the coin's side in it
            # (memecoin_evm_post_trade), whatever its base; else the PancakeSwap-V2 WBNB pair.
            sp = source_pool if isinstance(source_pool, dict) else {}
            if (sp.get("venue") in ("v2", "v3") and str(sp.get("pool") or "").startswith("0x")
                    and sp.get("mint_is_c1") is not None):
                return bsc.read_m5(str(sp["pool"]).lower(), None, mint, source_tx,
                                   coin_is_token1=bool(sp["mint_is_c1"]))
            pool, quote = bsc.source_pair(mint), bsc.WBNB
        return bsc.read_m5(pool, quote, mint, source_tx)
    if str(chain) not in cfg.MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS:
        return None
    source_tx = _gate_source_tx(chain, mint, origin)
    if source_tx is None:
        unknown = {'usd':None,'reason':'source_tx_unknown','pools':[],'coverage':'unknown'}
        if (not getattr(cfg, 'MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND', False)
                or type(source_usd) not in (int, float) or not math.isfinite(source_usd)
                or source_usd < 0):
            return unknown
        total = read_for_gate(chain, mint)
        usd = total.get('usd') if isinstance(total, dict) else None
        if total.get('reason') != 'ok' or type(usd) not in (int, float) or not math.isfinite(usd):
            return unknown
        return {**total, 'usd': max(0.0, float(usd) - float(source_usd)),
                'basis': 'source_unknown_tape_bound', 'tape_total_usd': usd,
                'source_subtracted_usd': float(source_usd)}
    return read_for_gate(chain,mint,exclude_tx=source_tx,source_tokens=source_tokens)
