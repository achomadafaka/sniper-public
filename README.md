# Sniper

[![Offline review checks](https://github.com/achomadafaka/sniper-research/actions/workflows/review.yml/badge.svg)](https://github.com/achomadafaka/sniper-research/actions/workflows/review.yml)

**Crypto research infrastructure: a multi-agent office, evidence-backed brain, on-chain forensics, and low-latency execution.**

Sniper is a Python and Rust research platform for understanding how wallets trade, how launch networks prepare and distribute inventory, and whether an observed pattern survives realistic execution constraints. It combines streaming blockchain data, wallet ledgers, statistical analysis, paper experiments, execution infrastructure, and an operator dashboard.

This repository is a **public source snapshot for technical review**, exported on November 2, 2026. It contains current application code and tests, without the private development history, production databases, credentials, operational audit records, or agent handoffs. Exact source revision: [`PUBLIC_SNAPSHOT.json`](PUBLIC_SNAPSHOT.json).

## Three systems working together

**The crypto platform** covers Solana/EVM launch research and execution, Hyperliquid market analysis and paper evidence, Polymarket wallet intelligence, and short-horizon crypto research. It includes streaming decoders, causal ledgers, replay tools, route construction, paper lifecycles, and a Rust QUIC submission sidecar.

**The Office** is a research-agent runtime with durable turns, supervisor leases, host-owned capabilities, tool receipts, budgets, memory, paper evaluation, and promotion controls. Research claims and decisions have traceable records; model-generated text does not grant execution authority. Explore [`sniper/office/`](sniper/office/) and the separate [`agents/`](agents/) modules.

**The Brain** stores typed, entity-linked knowledge with provenance, verification states, contradiction candidates, supersession, and task/work records. Retrieval combines lexical and optional semantic signals; evidence checks distinguish a valid reference from an unverified or broken one. The engine, CLI, and schema are included; private memory contents and access credentials are not. Explore [`bot/brain_graph/`](bot/brain_graph/), [`scripts/brain_cli.py`](scripts/brain_cli.py), and [`migrations/`](migrations/).

### Explore the work

- [Engineering highlights](docs/ENGINEERING_HIGHLIGHTS.md): durable execution, Rust transport, evidence-preserving streams, profiling, and immutable releases.
- [Wallet universe and forensics](docs/WALLET_UNIVERSE_AND_FORENSICS.md): capital, transaction flow, funding graphs, launch preparation, supply distribution, and settlement.
- [Providers and software](docs/PROVIDERS_AND_STACK.md): commercial on-chain services, market APIs, model subscriptions/transports, and the software stack.
- [Architecture tour](docs/ARCHITECTURE.md), [database structure](docs/DATABASES.md), and [full repository map](docs/REPOSITORY_MAP.md).

## Wallet universe and launch forensics

A major part of the work is following **where money and inventory come from, how wallets prepare a launch, and where value goes afterward**. Wallet-universe observations measure capital and transaction-bound flow across supported venues. Campaign research connects funding, creation signers, fee beneficiaries, early buyers, token transfers, and later settlement with explicit evidence and knowledge times.

The research questions include identifying repeat launch operators through wallet rotation, recognizing preparation before a launch, finding recurring early participants in runners, and separating independent demand from activity linked to the launch operation. That requires failed-launch controls, coverage accounting, fee/inventory reconciliation, and forward executable testing. [Read the full forensics guide](docs/WALLET_UNIVERSE_AND_FORENSICS.md).

## Data infrastructure and integrations

Implemented integrations include **Shyft/RabbitStream, Helius, dRPC, Alchemy, Chainstack, J7Tracker, Jupiter, LI.FI, Relay, KyberSwap, DexScreener, GeckoTerminal, CoinGecko, Goldsky, Polymarket, Hyperliquid, Binance, and Kraken**, with source-specific roles and boundaries. The Office supports **Claude and Codex** model transports; the Brain supports optional local embeddings. Recorded commercial-account usage is dated in [the provider inventory](docs/PROVIDERS_AND_STACK.md); service integration does not imply a current paid subscription.

The software spans Python/Rust, gRPC/Protobuf, WebSockets, QUIC, SQLite/WAL, DuckDB, PostgreSQL/Supabase, browser capture, and an operator UI. Provider budgets, authenticated sessions, chain identity, original source time, and explicit unknown/failure states are part of the implementation.

## What is here

| Area | Capabilities | Start reading |
| --- | --- | --- |
| Research Office | Durable agent orchestration, bounded tools, capability checks, memory, paper trials, and evaluation | [`sniper/office/`](sniper/office/) |
| Knowledge Brain | Entity-linked facts, evidence validation, contradiction candidates, retrieval, and durable work records | [`bot/brain_graph/`](bot/brain_graph/) |
| Agent modules | Wallet scouting, archetype analysis, strategy review, and adversarial review | [`agents/`](agents/) |
| Hyperliquid | Market microstructure, copy decomposition, paper evidence, and operator research surfaces | [`sniper/hyperlive/`](sniper/hyperlive/), [`sniper/algos/hl_microstructure/`](sniper/algos/hl_microstructure/) |
| Short-horizon crypto | BTC 5-minute and Polymarket crypto research code | [`sniper/algos/btc5m/`](sniper/algos/btc5m/), [`sniper/algos/polymarket_crypto5m/`](sniper/algos/polymarket_crypto5m/) |
| Wallet intelligence | Position reconstruction, outcome attribution, statistical controls, and behavioral thesis extraction | [`sniper/analysis/winning_wallet_thesis/`](sniper/analysis/winning_wallet_thesis/) |
| Launch forensics | Funding and wallet relationships, creation proofs, launch sequences, and campaign histories | [`sniper/office/`](sniper/office/) |
| Streaming data | Solana transaction decoding, source evidence, and bounded background collectors | [`sniper/feeds/`](sniper/feeds/), [`sniper/services/`](sniper/services/) |
| Paper research | Versioned candidate policies, replay, paired controls, and outcome measurement | [`sniper/research/memecoin_strategy/`](sniper/research/memecoin_strategy/) |
| Execution systems | Solana/EVM routing, durable transaction identities, admission controls, and exit recovery | [`sniper/execution/`](sniper/execution/) |
| Rust transport | A local QUIC transaction-submission sidecar with a Python client | [`rust/solana-tpu-sidecar/`](rust/solana-tpu-sidecar/) |
| Operator UI | Flask routes, dashboard assets, and runtime health surfaces | [`sniper/web/`](sniper/web/), [`sniper/routes/`](sniper/routes/) |

Here, **TPU means Solana's Transaction Processing Unit**, not Google's machine-learning accelerator. This project does not claim JAX/XLA or Google TPU training work.

## Architecture and evidence

Feed adapters decode source transactions. Services attach identity, provenance, and timing evidence. SQLite-backed ledgers support research, paper lifecycles, operator views, and separate funded execution paths. Background collectors and notification workers keep slow aggregation and delivery away from order handling.

The wallet analyzer produces research hypotheses, not copy-trading recommendations. A chart move is not an executable fill; a wallet link is not proof of common ownership; passing tests are not proof of a profitable strategy. Missing observations remain unknown. Experimental strategies, including recent known-network work, require their own validation and explicit activation.

For a closer tour, see [the review guide](docs/REVIEW_GUIDE.md). Design contracts are retained under [`docs/superpowers/specs/`](docs/superpowers/specs/); older contracts may be superseded by later ones and current code.

## Review locally

Use Python 3.13. The commands below install a small dependency set and run offline checks; they do not launch the dashboard, collectors, or trading services.

```bash
git clone https://github.com/achomadafaka/sniper-research.git
cd sniper-research
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-review.txt
python -m pytest -n 2 \
  tests/test_env_files.py \
  tests/test_source_guard_helper.py \
  tests/wallet_protocol/test_wallet_protocol_statistics.py \
  tests/office/test_store.py \
  tests/office/test_broker.py \
  tests/test_brain_graph_contradict.py \
  tests/test_wallet_universe_flow.py \
  tests/test_wallet_universe_capital_reads.py \
  tests/test_wallet_universe_source.py \
  tests/office/test_wallet_network.py \
  tests/office/test_wallet_launch_proof.py \
  tests/office/test_wallet_campaign_history.py -q
```

[`requirements.txt`](requirements.txt) contains the broader application dependencies. The focused review dependencies are separate so a reader can inspect and test core mechanisms without installing the full trading stack.

The public snapshot is not a turnkey deployment. Production state and historical operational artifacts are intentionally absent, and some broader tests require those omitted artifacts or additional dependencies. Deployment scripts and legacy root entrypoints reflect the original operator environment; they are not quick-start commands.

## Validation

The public review workflow runs the same bounded offline tests shown above. Its scope covers environment loading, source inspection helpers, statistical primitives, Office storage and tool brokerage, Brain contradiction candidates, wallet-universe flow/capital/source recovery, relationship graphs, launch proofs, and campaign history. It does not certify the entire application, blockchain connectivity, signing, trade execution, or profitability.

## Credentials and security

Environment templates contain placeholders. Never commit filled environment files, signing keys, wallet keypairs, database snapshots, or session tokens. This snapshot was scanned with Gitleaks, including archive and encoded-content checks; reviewed test fixtures contain deliberate dummy credentials and public blockchain identifiers.

Please report a suspected credential exposure privately rather than posting the credential in an issue. See [SECURITY.md](SECURITY.md).

## License

No project-wide open-source license has been granted. The code is published for inspection; do not assume permission to redistribute or reuse it. Included third-party components retain their own licenses.
