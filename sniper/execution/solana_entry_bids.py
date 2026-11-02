"""Bounded entry bids; providers run only on the single paced refresh thread.

Agave runtime/src/prioritization_fee_cache.rs feeds compute_unit_price_in_microlamports()
into get_prioritization_fees(): RPC prioritizationFee is a unit price, not total lamports.
Nozomi /nozomi/tip-stream reports SOL percentiles; /nozomi/tipping-and-faq sets 0.001 SOL minimum.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import math
from threading import Event, Thread
import time
from types import MappingProxyType

from solders.pubkey import Pubkey


MAX_TOTAL_LAMPORTS = 6_520_000
FALLBACK_PRIORITY_LAMPORTS = 1_520_000
PRIORITY_FLOOR_MICRO_LAMPORTS = 3_800_000
MIN_TIP_LAMPORTS = 1_000_000
MAX_TIP_LAMPORTS = 5_000_000


def _warm_floor_at_fallback():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_WARM_FLOOR_AT_FALLBACK", False))


def fallback_priority_lamports():
    """The normal bid on a cold or stale pool, in total lamports: MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS,
    read at call time; a non-int or a value <= 0 is FALLBACK_PRIORITY_LAMPORTS (today's 1,520,000)."""
    from sniper.settings import memecoin as cfg
    value = getattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", None)
    return value if type(value) is int and value > 0 else FALLBACK_PRIORITY_LAMPORTS


@dataclass(frozen=True)
class _Sample:
    value: int | None = None
    observed_at: float | None = None
    attempted_at: float | None = None
    reason: str | None = None
    count: int = 0
    source_age_s: float = 0.0
    source_slot: int | None = None


class _InvalidSample(ValueError):
    pass


def _json_result(result):
    if result is None:
        raise _InvalidSample("unavailable")
    if isinstance(result, dict) and "error" in result:
        error = result["error"]
        code = error.get("code") if isinstance(error, dict) else None
        raise _InvalidSample("rate_limit" if code in (429, -32429) else "rejected")
    if not isinstance(result, list) or len(result) > 150:
        raise _InvalidSample("malformed")
    if not result:
        raise _InvalidSample("empty")
    return result


def _priority_sample(result):
    rows = _json_result(result)
    values, slots = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise _InvalidSample("malformed")
        slot, price = row.get("slot"), row.get("prioritizationFee")
        if (type(slot) is not int or not 0 <= slot < 2**64 or slot in slots
                or type(price) is not int or not 0 <= price < 2**64):
            raise _InvalidSample("malformed")
        slots.add(slot)
        values.append(price)
    values.sort()
    return values[(95 * len(values) + 99) // 100 - 1], len(values), max(slots)


def _tip_sample(result, wall_now, ttl_s):
    rows = _json_result(result)
    samples = []
    for row in rows:
        if not isinstance(row, dict):
            raise _InvalidSample("malformed")
        value, stamp = row.get("landed_tips_95th_percentile"), row.get("time")
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                or not isinstance(stamp, str) or len(stamp) > 64):
            raise _InvalidSample("malformed")
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("missing timezone")
            source_at = dt.timestamp()
        except (ValueError, OverflowError):
            raise _InvalidSample("malformed") from None
        age = wall_now - source_at
        if age < 0:
            raise _InvalidSample("future_source")
        # Decimal(str(...)) avoids binary-float rounding below the requested tip.
        lamports = int((Decimal(str(value)) * 1_000_000_000).to_integral_value(rounding=ROUND_CEILING))
        samples.append((source_at, lamports, age))
    _, lamports, age = max(samples)
    if age >= ttl_s:
        raise _InvalidSample("stale_source")
    return lamports, len(samples), age


def _failure_reason(error):
    if isinstance(error, _InvalidSample):
        return str(error)
    reason = getattr(error, "reason", None)
    if type(reason) is str and reason in ("timeout", "transport", "rejected", "rate_limit",
                                         "malformed", "stale_source", "future_source"):
        return reason
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "rejected"
    kind = type(error).__name__.lower()
    if isinstance(error, TimeoutError) or "timeout" in kind:
        return "timeout"
    if isinstance(error, ConnectionError) or "connection" in kind:
        return "transport"
    return "provider_error"


def _age(sample, now):
    return None if sample.observed_at is None else max(0.0, now - sample.observed_at) + sample.source_age_s


class EntryBidCache:
    """HTTP adapters must set a timeout. A stalled adapter leaves entry reads unaffected."""

    def __init__(self, priority_fee_fn, tip_floor_fn, *, clock=time.monotonic,
                 wall_clock=time.time, enabled=True, max_pools=32,
                 priority_ttl_s=30.0, tip_ttl_s=30.0,
                 refresh_interval_s=1.0, tip_refresh_s=10.0):
        if type(enabled) is not bool or type(max_pools) is not int or not 1 <= max_pools <= 32:
            raise ValueError("invalid entry bid cache bounds")
        for value, minimum in ((priority_ttl_s, 1), (tip_ttl_s, 1),
                               (refresh_interval_s, 1), (tip_refresh_s, 10)):
            if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
                raise ValueError("invalid entry bid cache cadence")
        self.enabled = enabled
        self._priority_fee_fn, self._tip_floor_fn = priority_fee_fn, tip_floor_fn
        self._clock, self._wall_clock = clock, wall_clock
        self._max_pools = max_pools
        self._priority_ttl_s, self._tip_ttl_s = priority_ttl_s, tip_ttl_s
        self._refresh_interval_s, self._tip_refresh_s = refresh_interval_s, tip_refresh_s
        self._pending = deque(maxlen=max_pools)
        self._pools = MappingProxyType({})
        self._registered_at = {}
        self._tip = _Sample()
        self._next_call_at = float("-inf")
        self._next_tip_at = float("-inf")
        self._stop = Event()
        self._thread = None

    def register(self, pool):
        """Best-effort bounded registration; CPython deque append/pop are atomic."""
        if not self.enabled or self._stop.is_set() or not isinstance(pool, str):
            return False
        try:
            Pubkey.from_string(pool)
        except ValueError:
            return False
        if pool in self._pending:
            return True
        if len(self._pending) >= self._max_pools:
            return False
        # maxlen also bounds simultaneous registrations; an evicted request uses fallback.
        self._pending.append(pool)
        return True

    def select(self, *, pool, cu_limit, tip_lamports=0):
        if type(cu_limit) is not int or not 1 <= cu_limit <= 1_400_000:
            raise ValueError("invalid compute unit limit")
        from sniper.execution.solana_sender_ab import valid_nozomi_tip_budget
        if not valid_nozomi_tip_budget(tip_lamports):
            raise ValueError("invalid tip budget")
        snapshot = self._pools
        sample = snapshot.get(pool, _Sample()) if isinstance(pool, str) else _Sample()
        age = _age(sample, self._clock())
        fresh = self.enabled and age is not None and age < self._priority_ttl_s
        if fresh:
            # A WARM POOL BIDS ITS MEASURED FEE (operator 2026-10-23, reversing Alpha 2026-10-16's floor at the cold
            # fallback): the fallback floor made every entry pay >= 5M lamports (~$0.90, 3.6% of a $25 clip) whatever
            # the pool's real fee. A quiet pool now bids max(PRIORITY_FLOOR_MICRO_LAMPORTS, its p95); a busy one more.
            # MEMECOIN_SOLANA_ENTRY_BID_WARM_FLOOR_AT_FALLBACK=1 restores the old floor.
            floor = PRIORITY_FLOOR_MICRO_LAMPORTS
            if _warm_floor_at_fallback():
                floor = max(floor, fallback_priority_lamports() * 1_000_000 // cu_limit)
            desired = max(floor, sample.value)
            source = "account_filtered_p95"
        else:
            desired = fallback_priority_lamports() * 1_000_000 // cu_limit
            source = "fallback_" + ("disabled" if not self.enabled else "cold" if age is None else "stale")
        remaining = MAX_TOTAL_LAMPORTS - tip_lamports
        unit_price = min(desired, remaining * 1_000_000 // cu_limit)
        priority = (unit_price * cu_limit + 999_999) // 1_000_000
        return {"micro_lamports_per_cu": unit_price, "priority_fee_lamports": priority,
                "tip_lamports": tip_lamports, "combined_fee_lamports": priority + tip_lamports,
                "priority_source": source, "priority_sample_micro_lamports_per_cu": sample.value,
                "priority_sample_age_s": age, "priority_failure": sample.reason,
                "priority_sample_count": sample.count, "priority_source_slot": sample.source_slot,
                "cap_saturated": desired > unit_price, "pool": pool, "cu_limit": cu_limit}

    def tip_lamports(self):
        sample = self._tip
        age = _age(sample, self._clock())
        return (min(MAX_TIP_LAMPORTS, max(MIN_TIP_LAMPORTS, sample.value))
                if self.enabled and age is not None and age < self._tip_ttl_s
                else MAX_TIP_LAMPORTS)

    def status(self):
        from sniper.execution.solana_sender_ab import (
            nozomi_fee_split_on, NOZOMI_FEE_SPLIT_RULE, NOZOMI_TREATMENT_TIP_LAMPORTS)
        fee_split = nozomi_fee_split_on()
        now, tip, pools = self._clock(), self._tip, self._pools
        tip_age = _age(tip, now)
        tip_fresh = self.enabled and tip_age is not None and tip_age < self._tip_ttl_s
        return {"enabled": self.enabled, "running": self._thread is not None and self._thread.is_alive(),
                "pending_pools": len(self._pending), "max_pools": self._max_pools,
                "max_total_lamports": MAX_TOTAL_LAMPORTS,
                "fee_split_enabled": fee_split,
                "fee_split_rule": NOZOMI_FEE_SPLIT_RULE if fee_split else None,
                "fee_split_control_tip_max_lamports": MAX_TIP_LAMPORTS,
                "fee_split_treatment_tip_lamports": NOZOMI_TREATMENT_TIP_LAMPORTS if fee_split else None,
                "fee_split_treatment_priority_max_lamports":
                    MAX_TOTAL_LAMPORTS - NOZOMI_TREATMENT_TIP_LAMPORTS if fee_split else None,
                "tip_lamports": min(MAX_TIP_LAMPORTS, max(MIN_TIP_LAMPORTS, tip.value)) if tip_fresh
                                else MAX_TIP_LAMPORTS,
                "tip_source": "nozomi_p95" if tip_fresh else "fallback",
                "tip_sample_lamports": tip.value,
                "tip_cap_saturated": tip_fresh and tip.value > MAX_TIP_LAMPORTS,
                "tip_sample_age_s": tip_age, "tip_failure": tip.reason,
                "pools": {pool: {"sample_micro_lamports_per_cu": sample.value,
                                 "sample_age_s": _age(sample, now), "failure": sample.reason,
                                 "sample_count": sample.count, "source_slot": sample.source_slot}
                          for pool, sample in pools.items()}}

    def _refresh_once(self):
        if not self.enabled or self._stop.is_set():
            return
        now = self._clock()
        pools = dict(self._pools)
        for _ in range(len(self._pending)):
            try:
                pool = self._pending.popleft()
            except IndexError:
                break
            if pool not in pools and len(pools) >= self._max_pools:
                oldest = min(self._registered_at, key=self._registered_at.get)
                pools.pop(oldest)
                self._registered_at.pop(oldest)
            pools.setdefault(pool, _Sample())
            self._registered_at[pool] = now
        self._pools = MappingProxyType(pools)
        if now < self._next_call_at:
            return
        is_tip = self._tip_floor_fn is not None and now >= self._next_tip_at
        if not is_tip and (not pools or self._priority_fee_fn is None):
            return
        self._next_call_at = now + self._refresh_interval_s
        if is_tip:
            previous = self._tip
            self._next_tip_at = now + self._tip_refresh_s
        else:
            pool = min(pools, key=lambda key: float("-inf") if pools[key].attempted_at is None
                       else pools[key].attempted_at)
            previous = pools[pool]
        try:
            if is_tip:
                value, count, source_age = _tip_sample(self._tip_floor_fn(), self._wall_clock(), self._tip_ttl_s)
                sample = _Sample(value, self._clock(), now, None, count, source_age)
            else:
                value, count, slot = _priority_sample(self._priority_fee_fn(pool))
                if previous.source_slot is not None and slot <= previous.source_slot:
                    raise _InvalidSample("stale_source")
                sample = _Sample(value, self._clock(), now, None, count, source_slot=slot)
        except Exception as error:
            sample = replace(previous, attempted_at=now, reason=_failure_reason(error))
        self._next_call_at = self._clock() + self._refresh_interval_s
        if is_tip:
            self._tip = sample
        else:
            pools[pool] = sample
            self._pools = MappingProxyType(pools)

    def run(self):
        while not self._stop.is_set():
            self._refresh_once()
            self._stop.wait(self._refresh_interval_s)

    def start(self):
        if self.enabled and self._thread is None and not self._stop.is_set():
            self._thread = Thread(target=self.run, name="solana-entry-bids", daemon=True)
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
