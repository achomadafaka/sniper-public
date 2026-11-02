#!/usr/bin/env python3
"""Minimal test: open 1 WebSocket to Polymarket CLOB, subscribe to 5 active
markets, and print raw messages for 60 seconds.

Isolates whether the WS feed is delivering data at all.

Usage:
    python3 scripts/test_ws_connection.py
"""
import asyncio
import json
import time

import websockets

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# 5 known high-volume asset_ids — we'll fetch fresh ones from the API
# but as a fallback, these are hardcoded from recent market index.


async def fetch_some_asset_ids(n: int = 5):
    """Grab a few active asset_ids from the Polymarket gamma API."""
    import urllib.request
    url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=10&ascending=false&order=volume24hr"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper-test/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        asset_ids = []
        for market in data:
            raw_ids = market.get("clobTokenIds") or []
            if isinstance(raw_ids, str):
                try:
                    raw_ids = json.loads(raw_ids)
                except json.JSONDecodeError:
                    continue
            for tid in raw_ids:
                if tid:
                    asset_ids.append(str(tid))
                    if len(asset_ids) >= n:
                        return asset_ids
        return asset_ids
    except Exception as e:
        print(f"Failed to fetch asset_ids from API: {e}")
        return []


async def main():
    print("Fetching active asset_ids from Polymarket API...")
    asset_ids = await fetch_some_asset_ids(5)
    if not asset_ids:
        print("ERROR: Could not fetch any asset_ids. Check network/API.")
        return

    print(f"Got {len(asset_ids)} asset_ids: {asset_ids[:3]}...")
    print(f"Connecting to {WS_URL} ...")

    msg_count = 0
    start = time.time()
    duration = 60  # seconds

    try:
        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=60,
            close_timeout=10,
        ) as ws:
            sub_msg = json.dumps({"type": "market", "asset_ids": asset_ids})
            await ws.send(sub_msg)
            print(f"Subscribed to {len(asset_ids)} assets")
            print(f"Listening for {duration}s...\n")

            while time.time() - start < duration:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    msg_count += 1
                    if msg_count <= 20:
                        # Print first 20 messages in detail
                        try:
                            parsed = json.loads(raw)
                            # Compact print
                            if isinstance(parsed, list):
                                for item in parsed[:3]:
                                    et = item.get("event_type", "?")
                                    price = item.get("price", "?")
                                    side = item.get("side", "?")
                                    asset = str(item.get("asset_id", ""))[:16]
                                    print(f"  [{msg_count}] event_type={et} side={side} price={price} asset={asset}...")
                            elif isinstance(parsed, dict):
                                et = parsed.get("event_type", "?")
                                print(f"  [{msg_count}] dict: event_type={et} keys={list(parsed.keys())[:6]}")
                            else:
                                print(f"  [{msg_count}] type={type(parsed).__name__}: {str(parsed)[:120]}")
                        except json.JSONDecodeError:
                            print(f"  [{msg_count}] raw (not JSON): {raw[:120]}")
                    elif msg_count % 50 == 0:
                        elapsed = time.time() - start
                        print(f"  ... {msg_count} messages in {elapsed:.0f}s ({msg_count/elapsed:.1f}/s)")
                except asyncio.TimeoutError:
                    elapsed = time.time() - start
                    print(f"  [timeout] No message in 10s (total: {msg_count} msgs in {elapsed:.0f}s)")

    except websockets.exceptions.ConnectionClosed as e:
        print(f"\nConnection closed: {e}")
    except Exception as e:
        print(f"\nError: {e}")

    elapsed = time.time() - start
    print(f"\n=== RESULTS ===")
    print(f"Duration: {elapsed:.0f}s")
    print(f"Messages received: {msg_count}")
    print(f"Rate: {msg_count/max(elapsed,1):.1f} msgs/sec")
    if msg_count == 0:
        print("VERDICT: WebSocket is NOT delivering any data. Issue is likely server-side or subscription format.")
    else:
        print("VERDICT: WebSocket IS delivering data. Issue is in stream_low_buys.py processing pipeline.")


if __name__ == "__main__":
    asyncio.run(main())
