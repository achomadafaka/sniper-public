"""Main collector loop — WebSocket → parser → writer."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from sniper.sim.collector.config import CollectorConfig
from sniper.sim.collector.writer import RawWriter
from sniper.sim.collector.ws import parse_ws_message
from sniper.sim.ids import make_market_id

LOG = logging.getLogger(__name__)


class Collector:
    """Polymarket WebSocket collector.

    Subscribes to market channels, parses messages into RawEvents,
    writes to gzipped JSONL via RawWriter.
    """

    def __init__(self, config: CollectorConfig):
        self._config = config
        self._writer = RawWriter(
            raw_dir=config.raw_dir,
            venue=config.venue,
            rotation_seconds=config.rotation_seconds,
        )
        self._ingest_seq = 0
        self._running = False
        self._markets: dict[str, dict] = {}  # condition_id -> market info
        self._asset_to_market: dict[str, str] = {}  # asset_id -> condition_id
        self._last_snapshot_mtime: float = 0.0
        self._last_snapshot_check: float = 0.0

    def _next_seq(self) -> int:
        self._ingest_seq += 1
        return self._ingest_seq

    @staticmethod
    def _capture_ts() -> str:
        from sniper.sim.schema import capture_ts_now
        return capture_ts_now()

    def register_markets(self, markets: list[dict]) -> None:
        """Register markets from metadata sync. Called externally."""
        for m in markets:
            cid = m.get("condition_id", "")
            if not cid:
                continue
            self._markets[cid] = m
            for token in m.get("tokens", []):
                tid = token.get("token_id", "")
                if tid:
                    self._asset_to_market[tid] = cid

    def load_markets_snapshot(self) -> bool:
        """Reload the latest market universe snapshot if it changed on disk."""
        now = time.monotonic()
        if now - self._last_snapshot_check < self._config.market_reload_seconds:
            return False
        self._last_snapshot_check = now
        path = self._config.snapshot_path
        if not os.path.exists(path):
            return False
        mtime = os.path.getmtime(path)
        if mtime <= self._last_snapshot_mtime:
            return False
        with open(path, "r", encoding="utf-8") as f:
            markets = json.load(f)
        if not isinstance(markets, list):
            raise ValueError("markets snapshot must be a JSON list")
        self._markets.clear()
        self._asset_to_market.clear()
        self.register_markets(markets)
        self._last_snapshot_mtime = mtime
        LOG.info("Loaded %d markets from %s", len(markets), path)
        return True

    def get_asset_ids(self) -> list[str]:
        """Return all asset IDs to subscribe to."""
        return list(self._asset_to_market.keys())

    async def run(self) -> None:
        """Main loop — connect, subscribe, receive, parse, write."""
        self._running = True
        delay = self._config.reconnect_base_delay

        while self._running:
            try:
                await self._connect_and_stream()
                delay = self._config.reconnect_base_delay
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                LOG.warning("WS disconnected: %s. Reconnecting in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.reconnect_max_delay)
            except Exception:
                LOG.exception("Unexpected error in collector loop")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.reconnect_max_delay)

    async def _connect_and_stream(self) -> None:
        self.load_markets_snapshot()
        asset_ids = self.get_asset_ids()
        if not asset_ids:
            LOG.info("No markets registered yet. Waiting for snapshot...")
            await asyncio.sleep(30)
            return

        LOG.info("Connecting to %s with %d assets", self._config.ws_url, len(asset_ids))
        async with websockets.connect(
            self._config.ws_url,
            ping_interval=30,
            ping_timeout=60,
            close_timeout=10,
            max_size=10 * 1024 * 1024,  # 10MB — initial book snapshots can be >1MB for 400 assets
        ) as ws:
            # Subscribe
            subscribe_msg = json.dumps({
                "type": "market",
                "assets_ids": asset_ids,
                "custom_feature_enabled": self._config.custom_feature_enabled,
            })
            await ws.send(subscribe_msg)
            LOG.info("Subscribed to %d assets", len(asset_ids))

            async for raw_text in ws:
                if not self._running:
                    break

                capture_ts = self._capture_ts()
                self.load_markets_snapshot()

                try:
                    messages = json.loads(raw_text)
                except json.JSONDecodeError:
                    LOG.warning("Invalid JSON from WS: %s", raw_text[:200])
                    continue

                if not isinstance(messages, list):
                    messages = [messages]

                for msg in messages:
                    asset_id = msg.get("asset_id", "")
                    condition_id = msg.get("market", "") or self._asset_to_market.get(asset_id, "")
                    if not condition_id:
                        continue

                    market_info = self._markets.get(condition_id, {})
                    market_id = make_market_id("polymarket", condition_id)
                    market_type = market_info.get("market_type", "other")

                    events = parse_ws_message(
                        msg,
                        market_id=market_id,
                        venue_market_id=condition_id,
                        market_type=market_type,
                        next_seq=self._next_seq,
                        capture_ts=capture_ts,
                    )

                    for evt in events:
                        self._writer.write(evt)

                if self._ingest_seq % 100 == 0:
                    self._writer.flush()

    def stop(self) -> None:
        self._running = False
        self._writer.close()
