"""Background-only tape reads; entry consumes immutable, event-time volume snapshots."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType

WINDOW_S = 300.0
MAX_POOLS = 64
MAX_SWAPS = 4096
MAX_TOTAL_SWAPS = 16384
_QUOTE_DECIMALS = {
    "native-sol": 9,
    "So11111111111111111111111111111111111111112": 9,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,
}
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@dataclass(frozen=True, slots=True)
class VolumeDecision:
    state: str
    reason: str
    volume_usd: float | None = None
    required_usd: float | None = None


@dataclass(frozen=True, slots=True)
class _Snapshot:
    quote_mint: str
    total_atomic: int
    by_signature: object
    published_mono: float
    expires_mono: float


_MISSING = VolumeDecision("unknown", "no_complete_coverage")
_STALE = VolumeDecision("unknown", "stale_coverage")
_INVALID = VolumeDecision("unknown", "invalid_identity_or_price")


def _finite(value):
    try:
        return type(value) in (float, int) and math.isfinite(value)
    except OverflowError:
        return False


class VolumeCache:
    def __init__(self, *, max_age_s=5.0, min_ratio=0.02, clock=time.monotonic):
        if not _finite(max_age_s) or max_age_s <= 0:
            raise ValueError("volume max age must be finite and positive")
        if not _finite(min_ratio) or not 0 < min_ratio <= 1:
            raise ValueError("volume ratio must be in (0, 1]")
        self.max_age_s, self.min_ratio, self.clock = max_age_s, min_ratio, clock
        self._snapshots = MappingProxyType({})
        self._status = {"state": "warming", "pools": 0, "last_error": None}

    def check(self, *, mint, venue, market_pool, source_signature, mcap_usd, sol_usd,
              now_mono=None):
        """No I/O or lock, including on a miss. Missing coverage always passes as unknown."""
        if not all(type(x) is str and x for x in (mint, venue, market_pool, source_signature)):
            return _INVALID
        snapshot = self._snapshots.get((mint, venue, market_pool))
        if snapshot is None:
            return _MISSING
        now_mono = self.clock() if now_mono is None else now_mono
        if not _finite(now_mono) or not snapshot.published_mono <= now_mono <= snapshot.expires_mono:
            return _STALE
        if not _finite(mcap_usd) or mcap_usd <= 0:
            return _INVALID
        if snapshot.quote_mint == _USDC:
            quote_usd = 1.0
        elif _finite(sol_usd) and sol_usd > 0:
            quote_usd = sol_usd
        else:
            return _INVALID
        try:
            amount = snapshot.total_atomic - snapshot.by_signature.get(source_signature, 0)
            volume = amount / (10 ** _QUOTE_DECIMALS[snapshot.quote_mint]) * quote_usd
            required = mcap_usd * self.min_ratio
        except (OverflowError, ValueError):
            return _INVALID
        if not math.isfinite(volume) or not math.isfinite(required):
            return _INVALID
        return VolumeDecision("refuse" if volume < required else "allow", "ok", volume, required)

    def refresh(self, conn, *, now, own_owners, published_mono=None):
        """Called only by the background owner; publishes the whole batch atomically."""
        from sniper.persistence import db_sol_volume as db
        snapshots, skipped, consumed = {}, 0, 0
        stamp = self.clock() if published_mono is None else published_mono
        try:
            if (not _finite(now) or not _finite(stamp) or not own_owners
                    or not all(type(owner) is str and owner for owner in own_owners)):
                raise ValueError("missing clock or own wallet identity")
            for coverage in db.coverage_rows(conn, now=now, limit=MAX_POOLS):
                since, through = coverage.get("verified_since"), coverage.get("through")
                quote = coverage.get("quote_mint")
                key = tuple(coverage.get(k) for k in ("mint", "venue", "market_pool"))
                if (coverage.get("complete") != 1 or coverage.get("reason") != "ok"
                        or not _finite(since) or not _finite(through)
                        or not 0 <= now - through <= self.max_age_s
                        or since > through - WINDOW_S or quote not in _QUOTE_DECIMALS
                        or not all(type(x) is str and x for x in key)):
                    skipped += 1
                    continue
                rows = db.swap_rows(conn, coverage["pool"], since=through - WINDOW_S, through=through)
                consumed += len(rows)
                if len(rows) > MAX_SWAPS or consumed > MAX_TOTAL_SWAPS:
                    skipped += 1
                    continue
                total, by_signature, seen = 0, {}, set()
                for row in rows:
                    signature, maker, payer = row.get("signature"), row.get("maker"), row.get("payer")
                    event, block_ts = row.get("event_index"), row.get("block_ts")
                    atomic = row.get("quote_atomic")
                    if (type(signature) is not str or not signature or type(maker) is not str or not maker
                            or type(payer) is not str or not payer
                            or type(event) is not int or event < 0 or (signature, event) in seen
                            or not _finite(block_ts) or not through - WINDOW_S < block_ts <= through
                            or type(atomic) is not str or not 1 <= len(atomic) <= 78
                            or not atomic.isascii() or not atomic.isdecimal()):
                        raise ValueError("invalid verified volume receipt")
                    seen.add((signature, event))
                    if maker not in own_owners and payer not in own_owners:
                        amount = int(atomic)
                        total += amount
                        by_signature[signature] = by_signature.get(signature, 0) + amount
                # Duplicate bindings cannot silently select a different pool after migration.
                if key in snapshots:
                    raise ValueError("ambiguous volume pool identity")
                snapshots[key] = _Snapshot(quote, total, MappingProxyType(by_signature), stamp,
                                           stamp + self.max_age_s - (now - through))
        except Exception as exc:
            self._snapshots = MappingProxyType({})
            self._status = {"state": "unknown", "pools": 0, "last_error": type(exc).__name__}
            return
        self._snapshots = MappingProxyType(snapshots)
        self._status = {"state": "ready" if snapshots else "unknown", "pools": len(snapshots),
                        "skipped": skipped, "last_error": None, "published_mono": stamp}

    def status(self):
        status, snapshots = dict(self._status), self._snapshots
        status.update(enabled=True, min_m5_mcap_ratio=self.min_ratio,
                      max_age_s=self.max_age_s, window_s=WINDOW_S)
        if status["state"] == "ready":
            now = self.clock()
            fresh = sum(s.published_mono <= now <= s.expires_mono for s in snapshots.values())
            status.update(state="ready" if fresh else "stale", pools=fresh)
        return status


def start_volume_cache(*, own_owners, connect=None, clock=time.monotonic):
    """Startup only. The daemon owns a read-only connection; no new provider requests."""
    from sniper.settings import memecoin as cfg
    if not cfg.MEMECOIN_SOLANA_VOLUME_GATE_ENABLED:
        return None
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.persistence.db_ticks import sol_tape_db_path
    from sniper.util.clock import real_utc_now
    cache = VolumeCache(max_age_s=cfg.MEMECOIN_SOLANA_VOLUME_MAX_AGE_S,
                        min_ratio=cfg.MEMECOIN_SOLANA_VOLUME_MIN_M5_MCAP_RATIO, clock=clock)
    owners = frozenset(own_owners)
    connection = connect or (lambda: get_readonly_connection(sol_tape_db_path()))

    def run():
        while True:
            conn = None
            try:
                conn = connection()
                # A single SQLite snapshot keeps coverage and receipts in the same producer state.
                conn.execute("BEGIN")
                cache.refresh(conn, now=real_utc_now(), own_owners=owners)
            except Exception as exc:
                cache._snapshots = MappingProxyType({})
                cache._status = {"state": "unknown", "pools": 0, "last_error": type(exc).__name__}
            finally:
                if conn is not None:
                    conn.close()
            time.sleep(1.0)

    threading.Thread(target=run, name="solana-volume-cache", daemon=True).start()
    return cache
