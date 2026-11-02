# Providers, subscriptions, and third-party stack

Sniper connects commercial blockchain infrastructure, public market APIs, authenticated relays, model transports, and local storage. The interesting engineering is how those sources are isolated, budgeted, reconciled, and measured—not just the number of API integrations.

This is an inventory of implemented integrations and recorded operator usage. A module's presence does not mean it is enabled, currently subscribed, or required for every workflow. No provider endorses this project.

## Commercial on-chain infrastructure

The operator's account reference dated **October 15, 2026** records **Shyft Build with RabbitStream included**, **paid Helius research access**, and **Alchemy and Chainstack account integrations**. These are historical account facts, not a fresh billing audit. Plan prices, current entitlements, credentials, account identifiers, and private endpoint URLs are intentionally not published.

| Service | Role in the project | Inspect |
| --- | --- | --- |
| Shyft / RabbitStream / Yellowstone-compatible gRPC | Solana transaction and account streams; processed and confirmed evidence paths; subscription readiness and source-health handling | `sniper/services/memecoin_shyft_*.py`, `sniper/feeds/yellowstone/`, `sniper/services/memecoin_solana_provider.py` |
| Helius | Research transaction/history reads and source-evidence collection; provider-specific evidence parsing | `sniper/services/memecoin_helius_evidence.py`, `sniper/office/chain_data.py` |
| dRPC | Configurable research/RPC infrastructure with shared pacing and credit accounting; separate from funded custody | `sniper/office/chain_data.py`, `sniper/services/wallet_universe_metrics.py`, research scripts |
| Alchemy / Chainstack | Configurable RPC infrastructure and historical provider evaluation | `sniper/execution/evm_rpc_pool.py`, `sniper/execution/solana_rpc_pool.py`, provider configuration and preflight scripts |
| Official chain RPCs / PublicNode | Chain-bound public reads and selected submission/fallback paths | `sniper/execution/evm_rpc_pool.py`, `sniper/execution/solana_rpc_pool.py`, `sniper/settings/memecoin.py` |

RPC access and stream access are different products. A healthy endpoint does not prove remaining credits, archive availability, the correct chain, or timely data. Research traffic must not silently consume the funded lane's capacity.

## Trading, discovery, and market data

| Integration | Purpose and boundary |
| --- | --- |
| J7Tracker | Authenticated wallet-event relay, roster synchronization, and session renewal. A relay hint alone is not on-chain initiation proof. See `sniper/feeds/j7_feed.py`, `j7_auth.py`, and `sniper/services/memecoin_j7_backup.py`. |
| Fomo / Axiom | Wallet activity normalization, identity/intake adapters, and optional browser-based observations. Historical and current roles differ. See `sniper/feeds/fomo_*`, `axiom_browser.py`, and `axiom_parse.py`. |
| Jupiter | Solana route and quote infrastructure. Quotes, rate limits, exact atomic quantities, and execution identity are handled separately. See `sniper/execution/jupiter_route.py`. |
| LI.FI | Manifest-bound EVM routing and execution components, including explicit runtime/quota ownership. See `sniper/execution/lifi_*.py`. |
| Relay / KyberSwap | Additional routing, quote, and bridge-related integrations in the execution stack. Their presence does not imply a route is universally admissible. |
| DexScreener / GeckoTerminal / CoinGecko | Discovery, display, and research pricing/enrichment. A cached display price is not an executable price. See `sniper/office/market_data.py` and provider adapters. |
| Polymarket | CLOB, Gamma, Data API, and WebSocket capture for wallets, positions, markets, order books, and short-horizon research. See `sniper/markets/`, `sniper/feeds/`, and `sniper/algos/polymarket_crypto5m/`. |
| Goldsky | Public order-book subgraph fills used for wallet-identified historical execution evidence. See `sniper/feeds/goldsky_fills.py`. |
| Hyperliquid | Market streams, fills, funding, trader research, paper evidence, and separate execution adapters. See `sniper/hyperlive/` and `sniper/feeds/hyperliquid*`. |
| Binance / Kraken | Reference market data and cross-market research inputs. See `sniper/research/leadlag/` and related feed adapters. |
| X and RSS/news sources | Context and source-graph research, including browser capture and structured news feeds. Context remains distinct from transaction proof. |
| Telegram | Operator notifications through bounded asynchronous delivery. Notification HTTP must not delay order handling. |

Venue decoders/builders also cover distinct on-chain mechanisms: Pump bonding curves and PumpSwap, Raydium, Meteora, Uniswap variants, and PancakeSwap/Infinity. These are protocol integrations, not a claim of paid subscriptions. Chain and venue support is specific to the relevant module and gate.

## Models and the research Office

- **OpenAI Codex** and **Anthropic Claude** have typed, authenticated model transports in the Office. Model output, served-model identity, usage, and billing basis are represented separately; unknown identity is not promoted to a verified model claim. See `sniper/office/models.py`, `model_child.py`, and the authentication/diagnostics modules.
- **Ollama** appears in local-model integrations and research tooling. Availability and model choices depend on host configuration.
- **FastEmbed / ONNX Runtime** support optional local embeddings for Brain retrieval. An embedding similarity is a retrieval signal, not evidence that a claim is true.
- Subscription transport code does not publish or transfer the operator's subscriptions to users of this repository. Reviewers need their own authorized access for any service they choose to enable.

## Software foundations

| Layer | Main components |
| --- | --- |
| Python services and UI | Python, Flask, Flask-Sock, HTTPX, Requests, aiohttp, WebSocket clients |
| Stream encoding | gRPC, Protocol Buffers, Yellowstone-compatible schemas |
| Persistence and research | SQLite/WAL, DuckDB, PostgreSQL/Supabase, schema migrations, optional vector retrieval |
| Chain tooling | solders, base58, Web3.py, eth-account, eth-utils, eth-abi, chain-specific transaction/receipt decoders |
| Rust transport | Tokio, Serde, SHA-256, Solana/Agave client and QUIC libraries |
| Browser and tool interfaces | Playwright, Beautiful Soup, MCP interfaces, structured JSON/schema validation |
| Operations and validation | Docker builds, systemd/cron tooling, immutable Git release paths, pytest/xdist, offline fixtures and replay benchmarks |

The authoritative dependency manifests are [`requirements.txt`](../requirements.txt), [`requirements-review.txt`](../requirements-review.txt), and [`Cargo.toml`](../rust/solana-tpu-sidecar/Cargo.toml). Optional integrations may need additional packages. This inventory is not an exhaustive transitive-dependency or license audit; third-party components retain their own terms.

## Why multiple providers?

Discovery, low-latency observation, historical evidence, executable quotes, submission, and confirmation need different guarantees. One provider being fast at one job does not make it authoritative at all of them. The code therefore tracks source identity, original time, coverage, budgets, and explicit failure states instead of treating every response as interchangeable.
