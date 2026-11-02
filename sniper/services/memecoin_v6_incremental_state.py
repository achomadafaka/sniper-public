"""Bounded shared per-pool market facts for the V6 live-paper runtime.

The accumulator ingests normalized read-only chain events once per pool. Position observations use
its immutable snapshot instead of rescanning the chain tape for every open position. It owns no
position economics, quote, policy, or action state.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Iterable, Mapping

from sniper.services.memecoin_exit_market_state import compute_state
from sniper.services.memecoin_v6_engine import POLICY_ID
from sniper.services.memecoin_v6_events import (
    V6FeedCursor, V6FeedHealth, V6MarketEvent, event_identity,
)
from sniper.settings.memecoin_v6_paper import MEMECOIN_V6_V3_DEPTH_VERIFIED_CHAINS


_DEFAULT_MAX_EVENTS = 100_000


def _epoch(value: str) -> float:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return parsed.timestamp()


@dataclass(frozen=True)
class PoolMeta:
    network_id: str
    pool: str
    mint: str
    pool_kind: str
    quote_side: int
    chain_vol_p90: float | None = None
    tape_complete: bool = True
    tape_checked_at: str | None = None
    quote_asset: str | None = None

    def __post_init__(self):
        if self.network_id not in {"4663", "solana"}:
            raise ValueError(f"unsupported network_id: {self.network_id}")
        if not self.pool or not self.mint or not self.pool_kind:
            raise ValueError("pool metadata identities must not be empty")
        if self.quote_side not in (0, 1):
            raise ValueError("quote_side must be 0 or 1")


@dataclass(frozen=True)
class PoolFacts:
    network_id: str
    pool: str
    mint: str
    market_evidence_id: str | None
    market_event_kind: str | None
    source_event_time: str | None
    capture_status: str
    capture_version: str
    tape_complete: bool
    price: float | None = None
    prior_low: float | None = None
    net: float | None = None
    arrival_rate: float | None = None
    buy_share: float | None = None
    realized_vol: float | None = None
    chain_vol_p90: float | None = None
    buy_n: int | None = None
    sell_n: int | None = None
    buy_vol_quote: str | None = None
    sell_vol_quote: str | None = None
    depth_pull_flag: bool | None = None
    liquidity_removed_fraction: float | None = None
    reserve_quote: str | None = None
    reserve_delta: str | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)
    quote_asset: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    def scored_fields(self) -> dict:
        return {
            "price": self.price, "prior_low": self.prior_low, "net": self.net,
            "arrival_rate": self.arrival_rate, "buy_share": self.buy_share,
            "realized_vol": self.realized_vol, "buy_n": self.buy_n, "sell_n": self.sell_n,
            "buy_vol_quote": self.buy_vol_quote, "sell_vol_quote": self.sell_vol_quote,
            "depth_pull_flag": self.depth_pull_flag,
            "liquidity_removed_fraction": self.liquidity_removed_fraction,
            "reserve_quote": self.reserve_quote, "reserve_delta": self.reserve_delta,
        }

    def to_observation_facts(self) -> dict:
        return {
            **self.scored_fields(), "chain_vol_p90": self.chain_vol_p90,
            "tape_complete": self.tape_complete, "capture_status": self.capture_status,
            "capture_version": self.capture_version,
            "block_identity": self.market_evidence_id,
            "market_event_kind": self.market_event_kind,
            "source_event_time": self.source_event_time, "pool": self.pool,
            "market_quote_asset": self.quote_asset,
            "provenance": dict(self.provenance),
        }


class PoolAccumulator:
    """One bounded causal event buffer shared by every V6 position in a pool."""

    def __init__(self, meta: PoolMeta, *, max_events: int = _DEFAULT_MAX_EVENTS):
        if not isinstance(meta, PoolMeta):
            raise TypeError("meta must be PoolMeta")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        self.meta = meta
        self.max_events = max_events
        self._events: deque[V6MarketEvent] = deque()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._block_hashes: OrderedDict[int, str] = OrderedDict()
        self._block_times: OrderedDict[int, str] = OrderedDict()
        self._gap = not meta.tape_complete
        self._retry_gap = False
        self._integrity_gap = False
        self._last_received_at: str | None = None
        self._cursor: V6FeedCursor | None = None
        self._cursor_key: tuple | None = None
        self._snapshot_cache_key: tuple | None = None
        self._snapshot_cache: PoolFacts | None = None

    def _invalidate_snapshot(self) -> None:
        self._snapshot_cache_key = None
        self._snapshot_cache = None

    @property
    def health(self) -> V6FeedHealth:
        return V6FeedHealth(
            mode="incremental", current=not (self._gap or self._retry_gap),
            gap=self._gap or self._retry_gap,
            last_received_at=self._last_received_at, reconnect_count=0,
        )

    @property
    def cursor(self) -> V6FeedCursor | None:
        return self._cursor

    def mark_gap(self) -> None:
        if not self._gap:
            self._invalidate_snapshot()
        self._gap = True

    def mark_retry_gap(self) -> None:
        if not self._retry_gap:
            self._retry_gap = True
            self._invalidate_snapshot()

    def clear_retry_gap(self) -> None:
        if self._retry_gap:
            self._retry_gap = False
            self._invalidate_snapshot()

    def restore_feed_cursor(self, cursor: V6FeedCursor, *, current: bool,
                            checked_at: str) -> bool:
        """Restore/advance a fully scanned provider frontier without inventing market events."""
        pool_matches = (cursor.pool.lower() == self.meta.pool.lower()
                        if self.meta.network_id == "4663" else cursor.pool == self.meta.pool)
        if cursor.network_id != self.meta.network_id or not pool_matches:
            raise ValueError("feed cursor does not belong to accumulator pool")
        if self._cursor is not None and cursor.height < self._cursor.height:
            self._integrity_gap = True
            self._gap = True
            self._invalidate_snapshot()
            return False
        self._cursor = cursor
        self._cursor_key = ((cursor.height, (1 << 63) - 1, "\uffff")
                            if cursor.network_id == "4663" else
                            (cursor.height, "\uffff", (1 << 63) - 1))
        self.advance_tape_check(checked_at)
        if current and not self._integrity_gap:
            self._gap = False
        self._invalidate_snapshot()
        return True

    def advance_tape_check(self, checked_at: str) -> bool:
        """Advance a durable proof that the provider head was checked with no cursor hole."""
        checked_epoch = _epoch(checked_at)
        current_epoch = (_epoch(self.meta.tape_checked_at)
                         if self.meta.tape_checked_at is not None else None)
        if current_epoch is not None and checked_epoch <= current_epoch:
            return False
        self.meta = replace(self.meta, tape_checked_at=checked_at)
        self._invalidate_snapshot()
        return True

    def apply(self, event: V6MarketEvent) -> bool:
        pool_matches = (event.pool.lower() == self.meta.pool.lower()
                        if self.meta.network_id == "4663" else event.pool == self.meta.pool)
        if event.network_id != self.meta.network_id or not pool_matches:
            raise ValueError("event does not belong to accumulator pool")
        identity = event_identity(event)
        if identity in self._seen:
            return False
        order_key = ((event.block_height, event.event_index, event.tx_id)
                     if event.network_id == "4663" else
                     (event.block_height, event.tx_id, event.event_index))
        if self._cursor_key is not None and order_key < self._cursor_key:
            self._integrity_gap = True
            self._gap = True
            self._invalidate_snapshot()
            return False

        known_hash = self._block_hashes.get(event.block_height)
        if known_hash is not None and event.block_hash is not None and known_hash != event.block_hash:
            self._integrity_gap = True
            self._gap = True
            self._invalidate_snapshot()
            return False
        if event.block_hash is not None:
            self._block_hashes[event.block_height] = event.block_hash
            self._block_hashes.move_to_end(event.block_height)

        if event.source_event_at is None:
            self._integrity_gap = True
            self._gap = True
        else:
            known_time = self._block_times.get(event.block_height)
            if known_time is not None and known_time != event.source_event_at:
                self._integrity_gap = True
                self._gap = True
                return False
            self._block_times[event.block_height] = event.source_event_at
            self._block_times.move_to_end(event.block_height)
        self._events.append(event)
        self._seen[identity] = None
        self._last_received_at = event.received_at
        self._cursor = V6FeedCursor(
            event.network_id, self.meta.pool, event.block_height, identity)
        self._cursor_key = order_key

        if len(self._events) > self.max_events:
            self._events.popleft()
            self._integrity_gap = True
            self._gap = True
        seen_bound = self.max_events * 2
        while len(self._seen) > seen_bound:
            self._seen.popitem(last=False)
        while len(self._block_hashes) > self.max_events:
            self._block_hashes.popitem(last=False)
        while len(self._block_times) > self.max_events:
            self._block_times.popitem(last=False)
        self._invalidate_snapshot()
        return True

    def snapshot(self, now_iso: str) -> PoolFacts:
        now_ts = _epoch(now_iso)
        cache_key = (
            self._cursor.event_identity if self._cursor else None,
            self._cursor.height if self._cursor else None,
            self._gap, self._retry_gap, self.meta.tape_checked_at, int(now_ts),
        )
        if cache_key == self._snapshot_cache_key and self._snapshot_cache is not None:
            return self._snapshot_cache
        rows = []
        for event in self._events:
            rows.append({
                "kind": event.kind, "pool": event.pool, "block_no": event.block_height,
                "block_hash": event.block_hash, "tx_sig": event.tx_id,
                "log_index": event.event_index, **dict(event.payload),
            })
        block_times = dict(self._block_times)

        state = compute_state(
            rows, block_times,
            {"chain_id": self.meta.network_id, "mint": self.meta.mint,
             "pool": self.meta.pool, "pool_kind": self.meta.pool_kind,
             "position_id": 0, "quote_side": self.meta.quote_side,
             "tape_incomplete": self._gap or self._retry_gap, "engine_version": POLICY_ID,
             "capture_watermark_at": self.meta.tape_checked_at,
             "v3_depth_verified": MEMECOIN_V6_V3_DEPTH_VERIFIED_CHAINS},
            now_ts=now_ts,
        )
        total = (state.buy_n or 0) + (state.sell_n or 0)
        buy_share = (state.buy_n / total) if state.buy_n is not None and total else None
        net = float(state.net_flow_quote) if state.net_flow_quote is not None else None
        latest_identity = self._cursor.event_identity if self._cursor else None
        latest_kind = self._events[-1].kind if self._events else None
        serialized = state.to_dict()
        facts = PoolFacts(
            network_id=self.meta.network_id, pool=self.meta.pool, mint=self.meta.mint,
            market_evidence_id=latest_identity, market_event_kind=latest_kind,
            source_event_time=state.source_event_time,
            capture_status=state.state.value,
            capture_version=f"v6-incremental-{state.feature_version}",
            tape_complete=not (self._gap or self._retry_gap), price=state.nominal_price,
            prior_low=state.prior_low,
            net=net, arrival_rate=state.arrival_rate, buy_share=buy_share,
            realized_vol=state.realized_vol, chain_vol_p90=self.meta.chain_vol_p90,
            buy_n=state.buy_n, sell_n=state.sell_n,
            buy_vol_quote=state.buy_vol_quote, sell_vol_quote=state.sell_vol_quote,
            depth_pull_flag=state.depth_pull_flag,
            liquidity_removed_fraction=state.liquidity_removed_fraction,
            reserve_quote=state.reserve_quote, reserve_delta=state.reserve_delta,
            provenance=serialized.get("provenance") or {}, quote_asset=self.meta.quote_asset,
        )
        self._snapshot_cache_key = cache_key
        self._snapshot_cache = facts
        return facts


def rebuild_accumulator(events: Iterable[V6MarketEvent], cursor: V6FeedCursor | None,
                        meta: PoolMeta | None = None, *,
                        max_events: int = _DEFAULT_MAX_EVENTS) -> PoolAccumulator:
    ordered = tuple(sorted(events, key=lambda event: (
        (event.block_height, event.event_index, event.tx_id)
        if event.network_id == "4663" else
        (event.block_height, event.tx_id, event.event_index))))
    if meta is None:
        if not ordered:
            raise ValueError("meta is required when rebuilding an empty accumulator")
        first = ordered[0]
        try:
            meta = PoolMeta(
                network_id=first.network_id, pool=first.pool,
                mint=str(first.payload["mint"]), pool_kind=str(first.payload["pool_kind"]),
                quote_side=int(first.payload["quote_side"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("meta is required unless events carry mint/pool_kind/quote_side") from exc
    acc = PoolAccumulator(meta, max_events=max_events)
    for event in ordered:
        acc.apply(event)
    if cursor is not None:
        pool_matches = (cursor.pool.lower() == meta.pool.lower()
                        if meta.network_id == "4663" else cursor.pool == meta.pool)
        if cursor.network_id != meta.network_id or not pool_matches:
            raise ValueError("cursor does not belong to accumulator pool")
        if acc.cursor is None or cursor.height < acc.cursor.height:
            acc.mark_gap()
        elif cursor.event_identity != acc.cursor.event_identity:
            acc.mark_gap()
        else:
            acc._cursor = cursor
    return acc


def bootstrap_accumulators(positions, tape_conn, state_conn=None, *,
                           max_events: int = _DEFAULT_MAX_EVENTS):
    """Bounded startup rebuild from read-only tapes. Returns ``(accumulators, position_pools)``."""
    from sniper.persistence import db_exit_state, db_ticks
    from sniper.services.memecoin_v6_observations import _active_tape, _pool_for_position

    accumulators = {}
    position_pools = {}
    groups = {}
    for raw_position in positions:
        position = dict(raw_position)
        network_id = str(position["network_id"])
        tc = _active_tape(tape_conn, network_id)
        if tc is None:
            continue
        pool = _pool_for_position(tc, position)
        if pool is None:
            continue
        key = (network_id, pool.lower() if network_id == "4663" else pool)
        position_pools[str(position["position_id"])] = key
        group = groups.setdefault(key, {"tc": tc, "pool": pool, "positions": []})
        group["positions"].append(position)

    for key, group in groups.items():
        network_id, _normalized_pool = key
        tc, pool = group["tc"], group["pool"]
        grouped_positions = group["positions"]
        position = grouped_positions[0]
        opened = []
        for candidate in grouped_positions:
            value = candidate.get("opened_at")
            try:
                opened.append((_epoch(value), str(value)))
            except (TypeError, ValueError):
                pass
        opened_epoch, _opened_raw = min(opened) if opened else (None, None)
        opened_at = (datetime.fromtimestamp(opened_epoch, timezone.utc).isoformat()
                     if opened_epoch is not None else None)
        route = position.get("route_binding")
        is_v4 = bool(isinstance(route, Mapping)
                     and route.get("protocol") == "uniswap_v4"
                     and isinstance(route.get("pool_key"), Mapping))
        kinds = {row[0] for row in tc.execute(
            "SELECT DISTINCT kind FROM pool_ticks WHERE chain_id=? AND pool=?",
            (network_id, pool))}
        pool_kind = ("v3" if is_v4 else
                     "sol" if any(kind.startswith("sol_") for kind in kinds) else
                     "v2" if kinds & {"v2_swap", "v2_sync"} else
                     "v3" if "v3_swap" in kinds else "unknown")
        meta_row = tc.execute(
            "SELECT quote_side,token0,token1 FROM pool_meta WHERE chain_id=? AND pool=?",
            (network_id, pool)).fetchone()
        if is_v4:
            pool_key = route["pool_key"]
            mint = str(position["mint"]).lower()
            currency0 = str(pool_key.get("currency0") or "").lower()
            currency1 = str(pool_key.get("currency1") or "").lower()
            quote_side = 1 if currency0 == mint else 0 if currency1 == mint else None
            quote_asset = currency1 if quote_side == 1 else currency0 if quote_side == 0 else None
        else:
            quote_side = meta_row[0] if meta_row is not None else None
            quote_asset = (meta_row[1] if quote_side == 0 else meta_row[2]
                           if quote_side == 1 else None) if meta_row is not None else None
        if quote_side not in (0, 1):
            continue
        tape_complete = not is_v4
        if network_id != "solana":
            gap_table = tc.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tick_gaps'").fetchone()
            if gap_table is not None and tc.execute(
                    "SELECT 1 FROM tick_gaps WHERE chain_id=? AND resolved_at IS NULL "
                    "AND (? IS NULL OR detected_at>=?) LIMIT 1",
                    (network_id, opened_at, opened_at)).fetchone() is not None:
                tape_complete = False
        if network_id == "solana":
            quote_row = tc.execute(
                "SELECT quote_mint FROM sol_pool_vaults WHERE pool=?", (pool,)).fetchone()
            quote_asset = quote_row[0] if quote_row is not None else quote_asset
            cursor_row = tc.execute(
                "SELECT frontier_before,updated_at FROM sol_sig_cursor WHERE vault=?",
                (pool,)).fetchone()
            has_tick = tc.execute(
                "SELECT 1 FROM pool_ticks WHERE chain_id='solana' AND pool=? LIMIT 1",
                (pool,)).fetchone()
            # No cursor is not proof of a complete walk. A newly resolved V6 venue must remain
            # warming until the pull tape has established a durable cursor and decoded history.
            tape_complete = bool(cursor_row is not None and not cursor_row[0] and has_tick)
        meta = PoolMeta(
            network_id=network_id, pool=pool, mint=str(position["mint"]),
            pool_kind=pool_kind, quote_side=int(quote_side),
            chain_vol_p90=position.get("chain_vol_p90"), tape_complete=tape_complete,
            quote_asset=quote_asset,
            tape_checked_at=(str(cursor_row[1]) if network_id == "solana" and cursor_row
                             and cursor_row[1] else None),
        )
        acc = PoolAccumulator(meta, max_events=max_events)
        event_order = ("tx_sig DESC,log_index DESC" if network_id == "solana"
                       else "log_index DESC,tx_sig DESC")
        rows = tc.execute(
            "SELECT kind,block_no,tx_sig,log_index,maker,amount0,amount1,tick,tick_lower,"
            "tick_upper,liquidity,sqrt_price_x96,block_hash,observed_at FROM pool_ticks "
            "WHERE chain_id=? AND pool=? AND block_no IS NOT NULL "
            "AND (? IS NULL OR observed_at>=?) "
            f"ORDER BY block_no DESC,{event_order} LIMIT ?",
            (network_id, pool, opened_at, opened_at, max_events)).fetchall()
        rows = list(reversed(rows))
        blocks = {row["block_no"] for row in rows}
        block_times = (db_ticks.block_ts_map(tc, network_id, blocks)
                       if network_id == "solana" else
                       db_exit_state.merged_block_ts_map(tc, state_conn, network_id, blocks))
        for row in rows:
            source_at = block_times.get(row["block_no"])
            source_epoch = None
            if source_at is not None:
                try:
                    source_epoch = _epoch(source_at)
                except (TypeError, ValueError):
                    pass
            # V6 owns only post-fill behavior. Historical tape rows are useful for other systems,
            # but cannot become evidence for this position or poison it because an older capture
            # predates block-hash/time provenance upgrades.
            if opened_epoch is not None and source_epoch is not None \
                    and source_epoch < opened_epoch:
                continue
            if not row["tx_sig"] or row["log_index"] is None \
                    or (network_id == "4663" and not row["block_hash"]):
                acc.mark_gap()
                continue
            payload = {
                name: row[name] for name in (
                    "maker", "amount0", "amount1", "tick", "tick_lower", "tick_upper",
                    "liquidity", "sqrt_price_x96") if row[name] is not None
            }
            try:
                event = V6MarketEvent(
                    network_id=network_id, pool=pool, block_height=int(row["block_no"]),
                    block_hash=row["block_hash"], tx_id=str(row["tx_sig"]),
                    event_index=int(row["log_index"]), source_event_at=source_at,
                    received_at=str(row["observed_at"]), kind=str(row["kind"]), payload=payload,
                )
            except ValueError:
                acc.mark_gap()
                continue
            acc.apply(event)
        accumulators[key] = acc
    return accumulators, position_pools
