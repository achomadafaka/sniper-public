"""Bounded local-clock arrival evidence; never consulted for trade admission."""
from __future__ import annotations

import math
import statistics
import time
from collections import OrderedDict
from itertools import combinations
from types import MappingProxyType


class FeedArrivals:
    """One event-loop writer, immutable rows, snapshots copied by the heartbeat thread.

    Only valid decoded native buys enter this denominator. Missing peers may be
    filtered, disconnected or unseen; an unpaired sample is not proof of feed loss.
    """

    def __init__(self, feeds, *, max_samples=2048, window_s=900.0, grace_s=5.0,
                 scope='valid_decoded_native_buys'):
        self.feeds = tuple(dict.fromkeys(feeds))
        self.scope = scope
        if (not self.feeds or max_samples < 1 or not math.isfinite(window_s)
                or window_s <= 0 or not math.isfinite(grace_s) or grace_s < 0):
            raise ValueError('invalid feed-arrival bounds')
        self.max_samples, self.window_s, self.grace_s = max_samples, window_s, grace_s
        self._rows = {}
        self.evicted_cap = self.evicted_age = self.invalid = 0

    def observe(self, signature, owner, feed, recv_monotonic):
        if (not signature or not owner or feed not in self.feeds
                or not isinstance(recv_monotonic, (int, float))
                or not math.isfinite(recv_monotonic)):
            self.invalid += 1
            return
        key = (signature, owner)
        previous = self._rows.get(key)
        if previous is not None:
            first, winner, arrivals = previous
            if not any(name == feed for name, _ in arrivals):
                self._rows[key] = (first, winner, arrivals + ((feed, recv_monotonic),))
            return
        # Amortized cleanup is bounded; large expiry bursts cannot stall dispatch.
        for _ in range(64):
            if not self._rows:
                break
            oldest = next(iter(self._rows))
            if recv_monotonic - self._rows[oldest][0] <= self.window_s:
                break
            del self._rows[oldest]
            self.evicted_age += 1
        if len(self._rows) >= self.max_samples:
            del self._rows[next(iter(self._rows))]
            self.evicted_cap += 1
        self._rows[key] = (recv_monotonic, feed, ((feed, recv_monotonic),))

    def arrivals(self, signature, owner):
        """((feed, recv_monotonic), ...) for this signature and owner, or () (the rabbit frame line)."""
        row = self._rows.get((signature, owner))
        return row[2] if row is not None else ()

    def snapshot(self, *, now=None):
        now = time.monotonic() if now is None else now
        # dict.copy is atomic under CPython's GIL; the copied values are immutable.
        copied = self._rows.copy()
        rows = [row for row in copied.values() if 0 <= now - row[0] <= self.window_s]
        winners = {feed: 0 for feed in self.feeds}
        for _first, winner, _arrivals in rows:
            winners[winner] += 1
        pairs = {}
        for left, right in combinations(self.feeds, 2):
            deltas, missing_left, missing_right, missing_both, pending = [], 0, 0, 0, 0
            for first, _winner, arrivals in rows:
                by_feed = dict(arrivals)
                if left in by_feed and right in by_feed:
                    deltas.append((by_feed[right] - by_feed[left]) * 1000)
                elif now - first < self.grace_s:
                    pending += 1
                elif left not in by_feed and right not in by_feed:
                    missing_both += 1
                elif left not in by_feed:
                    missing_left += 1
                else:
                    missing_right += 1
            deltas.sort()
            pairs[left + '|' + right] = {
                'left': left, 'right': right, 'n': len(deltas),
                'p50_ms': statistics.median(deltas) if deltas else None,
                'p90_ms': deltas[math.ceil(.9 * len(deltas)) - 1] if deltas else None,
                'missing_left': missing_left, 'missing_right': missing_right,
                'missing_both': missing_both, 'pending': pending}
        return {'clock': 'same_process_monotonic', 'scope': self.scope,
                'delta': 'right_minus_left_ms; positive means left earlier',
                'missing': 'unpaired; feed loss is unproved',
                'window_s': self.window_s, 'grace_s': self.grace_s,
                'max_samples': self.max_samples, 'samples': len(rows),
                'winners': winners, 'pairs': pairs, 'evicted_cap': self.evicted_cap,
                'evicted_age': self.evicted_age, 'expired_retained': len(copied) - len(rows),
                'invalid': self.invalid}


class StampedArrivals:
    """Two populations on one clock (Codex P2 on stage 1, 2026-10-15). `raw`: every owner-signed frame
    each feed received, buy or not. `buys` (what `snapshot` reports): only signatures a decoder
    read as a buy, from ANY feed's decoder (native, routed or RabbitStream), with every feed's
    stamp taken AT RECEIPT. A buy decoded later (a routed buy's metadata worker) still pairs on the
    receipt stamps, and an owner-signed transfer never counts as a rabbit win. One event-loop
    writer, like FeedArrivals."""

    PENDING_MAX = 4096

    def __init__(self, feeds, **bounds):
        self.feeds = tuple(dict.fromkeys(feeds))
        self.raw = FeedArrivals(self.feeds, scope='owner_signed_frames', **bounds)
        self.buys = FeedArrivals(self.feeds, scope='decoded_buys_stamped_at_receipt', **bounds)
        self._stamps = OrderedDict()      # (signature, owner) -> ((feed, mono), ...) not yet a buy
        self._marked = OrderedDict()      # (signature, owner) read as a buy

    def stamp(self, signature, owner, feed, recv_monotonic):
        """One owner-signed frame at receipt."""
        self.raw.observe(signature, owner, feed, recv_monotonic)
        key = (signature, owner)
        if key in self._marked:
            self.buys.observe(signature, owner, feed, recv_monotonic)
            return
        stamps = self._stamps.get(key, ())
        if not any(name == feed for name, _ in stamps):
            self._stamps[key] = stamps + ((feed, recv_monotonic),)
            while len(self._stamps) > self.PENDING_MAX:
                self._stamps.popitem(last=False)

    def mark_buy(self, signature, owner):
        """A decoder read this signature as the owner's buy: its receipt stamps join `buys`."""
        key = (signature, owner)
        if key in self._marked:
            return
        self._marked[key] = None
        while len(self._marked) > self.PENDING_MAX:
            self._marked.popitem(last=False)
        for feed, mono in sorted(self._stamps.pop(key, ()), key=lambda stamp: stamp[1]):
            self.buys.observe(signature, owner, feed, mono)

    def observe(self, signature, owner, feed, recv_monotonic):
        """A decoded buy arriving with its own receipt stamp (the native decoders)."""
        self.stamp(signature, owner, feed, recv_monotonic)
        self.mark_buy(signature, owner)

    def snapshot(self, *, now=None):
        return self.buys.snapshot(now=now)


class ReceiptJournal:
    """One writer; bounded immutable scalar rows copied atomically by the timing drain."""

    def __init__(self, *, producer_run_id, boot_id, clock_domain, max_samples=4096):
        if not producer_run_id or not boot_id or not clock_domain or not 1 <= max_samples <= 4096:
            raise ValueError('invalid receipt journal identity or bound')
        self.producer_run_id, self.boot_id, self.clock_domain = producer_run_id, boot_id, clock_domain
        self.max_samples = max_samples
        self._rows = {}
        self._sequence = self.evicted_cap = self.invalid = 0
        self._evicted_start = self._evicted_end = None
        # Optional getter reads an immutable reference owned by the existing roster worker.
        self.provenance_fn = None

    def identity(self, recv_monotonic, connection_generation):
        provenance = self.provenance_fn() if self.provenance_fn else {}
        return MappingProxyType(dict(provenance, producer_run_id=self.producer_run_id,
            boot_id=self.boot_id, clock_domain=self.clock_domain, recv_monotonic=recv_monotonic,
            connection_generation=connection_generation))

    def _record(self, kind, fields):
        row = dict(fields)
        evidence = row.pop('evidence', None)
        if evidence:
            row.update((key, value) for key, value in evidence.items() if key not in row)
        if any(not isinstance(value, (str, int, float, bool, type(None))) for value in row.values()):
            raise ValueError('receipt fields must be immutable scalars')
        for name in ('recv_monotonic', 'mono_start', 'mono_end'):
            if name not in row:
                continue
            value = row[name]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                row[name] = None
                row['timestamp_state'] = 'invalid_' + name
                self.invalid += 1
        if kind == 'processed' and row.get('recv_monotonic') is None:
            kind = 'gap'
            row.update(component='processed', reason='invalid_recv_monotonic',
                       mono_start=None, mono_end=None, lost_count=None)
        self._sequence += 1
        row.update(kind=kind, producer_run_id=self.producer_run_id, sequence=self._sequence,
                   boot_id=self.boot_id, clock_domain=self.clock_domain)
        if len(self._rows) >= self.max_samples:
            old = self._rows.pop(next(iter(self._rows)))
            mono = old.get('recv_monotonic', old.get('mono_start'))
            if self.evicted_cap == 0:
                self._evicted_start = mono
            self._evicted_end = mono
            self.evicted_cap += 1
        self._rows[self._sequence] = MappingProxyType(row)

    def record_source(self, **fields):
        self._record('source', fields)

    def record_processed(self, **fields):
        self._record('processed', fields)

    def record_gap(self, **fields):
        self._record('gap', fields)

    def snapshot(self):
        copied = self._rows.copy()
        return dict(producer_run_id=self.producer_run_id, rows=tuple(copied.values()),
            first_sequence=next(iter(copied), None), last_sequence=next(reversed(copied), None),
            evicted_cap=self.evicted_cap, invalid=self.invalid,
            evicted_mono_start=self._evicted_start, evicted_mono_end=self._evicted_end)
