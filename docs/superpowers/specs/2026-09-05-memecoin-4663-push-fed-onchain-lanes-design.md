# Memecoin 4663 push-fed on-chain lanes — design

Date: 2026-09-05. Owner: Alpha (sniper-fc). Status: operator asked "why did the feed lag" and to
fix what we find; this is the fix. Builder: sniper-f0.

## Problem, measured 2026-09-05

Feed lag is entirely upstream. For every funded attempt today, our decision timestamp equals our
receipt timestamp (median 0.00 s). The lag sits between the trade time Fomo stamps and Fomo's
delivery: armed-wallet lane median 3.3 s, p90 19 s, 9% of buys over 20 s, maxima 35-103 s, in
every hour of the day. The global firehose is never earlier than the armed-wallet lane for the
same trade (12 of 12 pairs today), so there is no faster Fomo lane.

The one source that beats Fomo is our own chain reader: `scripts/research/onchain_evm_wallet_watch.py`
detected 6 armed-wallet buys today at 1.4 s block-to-detection and 1.6 s ahead of Fomo's receipt.
It saw 6 of 435 because it polls range `eth_getLogs`, which only the public chain-owner endpoint
serves, and that endpoint fails and rate-limits under load: the watcher log holds 12,142 failed
getLogs calls and 14 "blocks behind, skipping" events. Its polls are also part of the load that
429s the endpoint the runner needs for pool-key reads.

Pool discovery has the same dependence: the 38-61 s "log index lag" that makes a fresh pool
invisible at the first signal is a getLogs index lag.

Probe, 2026-09-05 17:2x UTC, QuickNode WebSocket (`wss://` of the custody URL), read-only:
- `eth_subscribe("logs", {topics: [Transfer, null, [20 armed addresses]]})` accepted.
- `eth_subscribe("logs", {address: PoolManager, topics: [Initialize]})` delivered 8 events in 30 s,
  first after 1.6 s, every one in the same block as the concurrent `newHeads` head (lag 0 blocks).

## Outcome

1. The wallet watcher receives armed wallets' token transfers by push, at block time, with no
   range getLogs. Its detections feed the existing on-chain lane unchanged, so the funded
   admission (same `real_hook`, `max_age` 45 s, Fomo dedup) does not move.
2. Pool keys for new V4 pools are stored the moment `Initialize` is emitted, so a fresh coin's
   real pool is a known key at the first signal without the launchpad getter, the indexers or the
   chain scan.

## Design

### Transport

One process, the existing watcher script (launched by `memecoin_watchdog.sh` from the pinned
release, 24-hour duration, relaunched by the watchdog). It opens ONE WebSocket to
`MEMECOIN_EVM_WS_URL_4663` (default: derived by walking `MEMECOIN_EVM_RPC_POOL_URLS_4663`, taking the
first custody host with `https://` replaced by `wss://`, and skipping the chain-owner public host by
name; None when no custody socket is configured, in which case the watcher keeps polling), using
`websockets` as `memecoin_volume_death.run_robinhood_volume_stream` already does. Subscriptions:

- `newHeads` — liveness only. If no head arrives for `MEMECOIN_EVM_WS_STALE_S` (default 15 s),
  reconnect with backoff 1, 2, 4, 8 s (cap 30 s). Same rule the volume stream uses.
- `logs` filter A: `topics = [Transfer, null, [armed addresses]]`. Re-subscribed whenever the
  armed address set changes (the existing `sync_addresses` result), by unsubscribing the old id
  and subscribing the new filter.
- `logs` filter B: `address = PoolManager, topics = [Initialize]`.

A subscription id is not delivery (2026-08-07 finding on publicnode). The watcher writes its
heartbeat only on a delivered head, and reports `last_log_at` per filter.

### Filter A: transfers -> detections

On a delivered Transfer log to an armed address: fetch the transaction and receipt (two reads,
through the pool's read layer), decode the buy with the existing `direct_buy_from_receipt`, and
write one `onchain_wallet_detections` row exactly as `_record` does today, with
`detect_latency_s` = now minus the block timestamp. Fetch first, write last (one short write per
detection; never a network call with a write pending). The on-chain lane in the runner consumes
the table unchanged.

### Filter B: Initialize -> pool keys

On a delivered Initialize log: decode `(pool_id, currency0, currency1, fee, tickSpacing, hooks)`
from topics and data (the decoder in `evm_v4_quote._onchain_pools_for_mint` / `_pool_key`), and
store the key through the existing `_store_pool_key`. No quote, no confirmation: an Initialize is
the chain's own statement that the pool exists. Only pools on our PoolManager; foreign managers
are ignored as today.

### Fallback

If the WebSocket cannot be established or stays stale past `MEMECOIN_EVM_WS_FALLBACK_S` (default
120 s), the watcher runs today's polling loop until the socket returns. The poll uses the
existing endpoint list; nothing about it changes.

#### 2026-10-10 refusal cooldown amendment

This transport is now shared by the EVM watchers. A subscription refusal previously ran one
bounded poll callback, then slept for the complete 60-to-600-second policy backoff. Ethereum's
`-32005` refusal reached 480 seconds at 19:43:40 UTC; the 180-second heartbeat guard restarted
the watcher at 19:48:01 UTC with `beat_stale:231`.

During refusal backoff, run the existing bounded fallback repeatedly. Space callback starts by
at least 30 seconds within each cooldown, count callback runtime toward its monotonic deadline,
and never reconnect before the original policy or transport cooldown expires. A callback that
overruns the deadline adds no sleep. Check shutdown before and after polling and between bounded
sleeps. Without a fallback callback, preserve the existing single backoff sleep.

Only a delivered head may mark push delivery. Fallback heartbeats remain actual poll progress or
typed poll errors; waiting alone writes nothing. Source proof, recovery frontier, roster, RPC
selection, rate limiting and the 180-second watchdog guard remain unchanged. A blocked poll can
still trip the guard; each bounded poll invocation retains its existing local retry streak.
This amendment fixes the blind cooldown, not provider quota refusal or inline Ethereum decoding.

Regression: `python3 -m pytest -n 2 tests/test_memecoin_onchain_push_lanes.py -q`.

#### 2026-10-11 Ethereum Infura rotation amendment

Ethereum uses the explicit WebSocket primary followed by the Infura members of its existing
HTTP pool, deduplicated by URL. Public HTTP fallbacks are not automatically subscription providers.
Other chains retain their single configured socket. Only one socket subscribes at a time;
healthy delivery does not rotate on a timer. A pooled connection proves `eth_chainId` on that
same socket before subscribing. A wrong chain is excluded; unavailable identity remains unknown.

A refusal immediately tries a ready backup. Explicit daily-quota refusals park that member until
the next UTC day; other policy refusals retain the existing backoff. Transport failures escalate
5/15/60 seconds per member. When every member is cooling down, bounded fallback polling continues
at most every 30 seconds. Rotation increments reconnect accounting so existing gap recovery runs.
The heartbeat reports only slot indexes and per-slot attempts, heads, logs and refusals.

HTTP and WebSocket primaries may use different operator-owned accounts to share the workload.
V3 scalar/batch reads rotate on Infura HTTP 402 as well as 429. No entry, exit, signing or sizing
policy changes. Two keys do not establish two quotas; daily capacity requires account-plan and
usage evidence, including HTTP requests and every subscription's per-block charge.

Regression: `python3 -m pytest -n 2 tests/test_memecoin_infura_ws_rotation.py
tests/test_memecoin_v3_rpc_quota_failover.py`.

### Settings

- `MEMECOIN_EVM_WS_URL_4663` (default derived from the custody pool list; never the chain-owner host.
  `MEMECOIN_EVM_RPC_URL_4663` is the chain-owner endpoint and must not be the source).
- `MEMECOIN_EVM_WS_STALE_S` (15), `MEMECOIN_EVM_WS_FALLBACK_S` (120).
- `MEMECOIN_ONCHAIN_PUSH_ENABLED` (default 1 on 4663).

### Cost

One WebSocket on the paid custody endpoint, two reads per armed-wallet transfer (a few per hour),
zero range getLogs. The public chain-owner endpoint loses the watcher's failing polls.

## Testing

Unit (`tests/test_memecoin_onchain_push_lanes.py`):

1. A Transfer log to an armed address yields one detection row through the existing `_record`
   shape; a transfer to a non-armed address yields none.
2. An Initialize log stores a pool key equal to the log's decoded key; a log from a foreign
   PoolManager stores nothing.
3. Stale heads trigger reconnect with the backoff sequence; a subscription reply without heads
   does not write a heartbeat.
4. An armed-set change re-subscribes filter A with the new address list.
5. WebSocket failure past the fallback window runs the polling loop; recovery returns to push.
6. No test opens a network connection.

Canary (read-only, the QuickNode WebSocket only): 10 minutes, count Initialize events and
transfers to armed addresses, report the head-minus-log-block distribution and the
block-time-to-delivery latency.

## Canary result (sniper-f0, 2026-09-05, 540 s, custody WebSocket only)

heads 1056; Initialize events 28 (~4.5k/day); head-minus-log block min -1, median 0, max 0;
block time to delivery p50 0.63 s, p90 1.03 s, max 1.22 s.

## Rollout

Ship behind `MEMECOIN_ONCHAIN_PUSH_ENABLED=1`. The watcher restarts on its 24-hour cycle or on the
next runner deploy (it is release-pinned). Report: detections per hour before and after, the share
of armed buys the lane sees, and `feed_lag_vs_onchain_s` on rows where both sources saw the trade.

## 2026-09-16 identity and receipt amendment

The poll and push paths now share the same net receipt classifier.
A supported purchase requires net token acquisition and a related payment to the token counterparty.
Direct native payment remains supported when the signer and transaction recipient match that relationship.
A transfer sender alone does not establish a purchase. Unsupported indirect payments remain unknown.
A successful empty address refresh now revokes the last subscription; only a read error retains the previous set.
Axiom proofs require `wallet-paid-v2`. Independent Fomo ownership and funded approval remain authoritative.
See `2026-09-16-axiom-observation-lanes-design.md` for the current proof and observation limits.
