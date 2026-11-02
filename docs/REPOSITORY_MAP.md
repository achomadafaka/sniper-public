# Repository map and publication scope

This map comes from a comparison of the tracked local source and the public snapshot, not just the original README. On November 2, 2026, the application code under `sniper/`, `bot/`, `agents/`, `scripts/`, `rust/`, and the configuration and migration trees was included verbatim from the source revision in `PUBLIC_SNAPSHOT.json`.

| Directory | What it contains |
| --- | --- |
| `sniper/office/` | Research Office, model/tool boundaries, memory, paper/evaluation mechanics, wallet networks, campaigns, and forensics |
| `bot/brain_graph/` | Entity-linked facts, evidence checking, contradiction candidates, retrieval, persistence, and work records |
| `agents/` | Wallet scouting, archetype analysis, strategy review, adversarial review, and tools |
| `sniper/analysis/` | Wallet protocol, thesis extraction, statistical controls, trade/exit research, and explanatory surfaces |
| `sniper/hyperlive/` | Hyperliquid data, trader and market research, paper evidence, and execution-related modules |
| `sniper/algos/` | BTC/Polymarket short-horizon and Hyperliquid microstructure research |
| `sniper/feeds/` | Market, wallet, subgraph, WebSocket, and transaction-feed adapters |
| `sniper/services/` | Capture, materialization, wallet-universe observations, source proof, execution coordination, and background services |
| `sniper/execution/` | Route/quote integrations, custody/signing boundaries, RPC pools, submission transport, and recovery |
| `sniper/research/` | Versioned paper candidates, opportunity studies, wallet execution, and lead-lag research |
| `sniper/sim/` | Canonical events, collection, compaction, episode handling, and replay machinery |
| `sniper/persistence/` | SQLite schema ownership and data-access modules |
| `sniper/routes/`, `sniper/web/`, `static/` | Operator API and dashboard implementation |
| `rust/solana-tpu-sidecar/` | Keyless QUIC transport and its Rust manifests/vendor patch |
| `scripts/` | Research entrypoints, capture workers, benchmarks, deployment, schema validation, and operational tools |
| `migrations/` | PostgreSQL/Supabase schema and migration/rollback definitions |
| `config/`, `systemd/`, `deploy/` | Configuration examples and original operator deployment/service machinery |
| `tests/` | Source contracts, fixtures, regression checks, and offline evaluation tests |
| `docs/superpowers/specs/`, `docs/superpowers/plans/` | Design contracts and implementation plans, including historical/superseded work |
| `data/` Python modules | A small set of importable data-client/schema modules; not a publication of collected data |

## What was intentionally not exported

- Original Git history containing historical credentials.
- Filled environment files, signing keys, sessions, private model/account access, and runtime state.
- Production databases, private Brain contents, local wallet dossiers, and collected research corpora.
- Agent handoffs, personal notes, historical operational logs, and raw audit/evidence bundles.
- Retired `archive/` implementation trees and old one-off audit scripts.
- A March 2026 file named `docs/current_schema.sql`, which is not a complete current bootstrap schema.

The omitted research and audit records are not evidence that their experiments succeeded or failed; they are simply outside this source-review export. The public architecture, provider inventory, forensics guide, and engineering highlights summarize the implemented mechanisms without exposing private runtime material.

## Start with a question

- **How does the Office control agents?** Read `supervisor.py`, `broker.py`, `capabilities.py`, and `store.py` under `sniper/office/`.
- **How is knowledge maintained?** Read `bot/brain_graph/` and the Brain migrations.
- **How are wallet identities and money flows established?** Read [the forensics guide](WALLET_UNIVERSE_AND_FORENSICS.md).
- **What providers and software are used?** Read [the stack inventory](PROVIDERS_AND_STACK.md).
- **Where is the strongest systems work?** Read [engineering highlights](ENGINEERING_HIGHLIGHTS.md).
- **Where are database definitions?** Read [the database map](DATABASES.md).
