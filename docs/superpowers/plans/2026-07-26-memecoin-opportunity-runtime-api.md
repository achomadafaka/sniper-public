# Memecoin Opportunity Runtime and API Plan

**Goal:** Run the capture/state pipeline as an isolated default-OFF worker and expose a read-only event-driven Radar API.

**Architecture:** `scripts/memecoin_opportunity_worker.py` owns the single WAL writer. Source tasks push bounded envelopes to one state task. Solana uses program-log subscriptions. Robinhood uses an explicit provider JSON-RPC WebSocket when configured and otherwise polls bounded canonical `eth_getLogs` ranges from the read-only RPC; the public sequencer feed is not treated as an `eth_subscribe` endpoint. The worker refreshes SOL/ETH USD conversion inputs in-process from a read-only public source while retaining explicit network-cost assumptions; a failed refresh keeps the last value only until its normal freshness deadline. Flask opens the dedicated DB read-only and serves snapshot, launch detail, source-health, evidence, and SSE delta endpoints. The worker heartbeat/version is the stream cursor.

**Constraints:** `MEMECOIN_OPPORTUNITY_CAPTURE=0` by default; atomic singleton lock; corrected UTC; no production DB access; no orders; no LLM/X/wallet-primary input; no service restart in this change.

## Tasks

1. Add validated opportunity settings for enable flag, isolated DB path, source URLs, queue bounds, staleness, recovery limits, and clip sizes.
2. Implement deterministic `process_envelope()` and restart reconstruction before adding the supervisor loop.
3. Implement the worker and watchdog with default-OFF startup refusal, singleton lock, graceful shutdown, source-specific failure isolation, heartbeat, and gap creation on overflow.
4. Add a dedicated Flask blueprint: `/api/memecoin/opportunity-radar`, `/launch/<network>/<venue>/<token>`, `/health`, `/evidence`, and `/stream`.
5. Register the read-only blueprint in the web server. A missing or old DB returns explicit `disabled/unavailable`, not a fabricated empty healthy market.
6. Verify default OFF, restart, bounded queue, health/stale behavior, read-only routes, SSE delta framing, and structural absence of funded writes.
7. Refresh native USD conversion inputs on a bounded cadence; stale or unavailable pricing degrades only the affected venue and never fabricates quote completeness.

## Verification

```bash
python3 -m pytest tests/test_memecoin_opportunity_settings.py tests/test_memecoin_opportunity_runtime.py tests/test_memecoin_opportunity_api.py -q
bash -n scripts/memecoin_opportunity_watchdog.sh
```
