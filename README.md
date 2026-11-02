# Sniper

**Wallet intelligence, on-chain launch forensics, and low-latency execution research.**

Sniper is a Python and Rust research platform for understanding how wallets trade, how launch networks prepare and distribute inventory, and whether an observed pattern survives realistic execution constraints. It combines streaming blockchain data, wallet ledgers, statistical analysis, paper experiments, execution infrastructure, and an operator dashboard.

This repository is a **public source snapshot for technical review**, exported on November 2, 2026. It contains current application code and tests, without the private development history, production databases, credentials, operational audit records, or agent handoffs. Exact source revision: [`PUBLIC_SNAPSHOT.json`](PUBLIC_SNAPSHOT.json).

## What is here

| Area | Capabilities | Start reading |
| --- | --- | --- |
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
git clone https://github.com/achomadafaka/sniper-public.git
cd sniper-public
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-review.txt
python -m pytest -n 2 tests/test_env_files.py tests/test_source_guard_helper.py tests/wallet_protocol/test_wallet_protocol_statistics.py -q
```

[`requirements.txt`](requirements.txt) contains the broader application dependencies. The focused review dependencies are separate so a reader can inspect and test core mechanisms without installing the full trading stack.

The public snapshot is not a turnkey deployment. Production state and historical operational artifacts are intentionally absent, and some broader tests require those omitted artifacts or additional dependencies. Deployment scripts and legacy root entrypoints reflect the original operator environment; they are not quick-start commands.

## Validation

The public review workflow runs the same bounded offline tests shown above. Its scope covers environment loading, source inspection helpers, and statistical primitives. It does not certify the entire application, blockchain connectivity, signing, trade execution, or profitability.

## Credentials and security

Environment templates contain placeholders. Never commit filled environment files, signing keys, wallet keypairs, database snapshots, or session tokens. This snapshot was scanned with Gitleaks, including archive and encoded-content checks; reviewed test fixtures contain deliberate dummy credentials and public blockchain identifiers.

Please report a suspected credential exposure privately rather than posting the credential in an issue. See [SECURITY.md](SECURITY.md).

## License

No project-wide open-source license has been granted. The code is published for inspection; do not assume permission to redistribute or reuse it. Included third-party components retain their own licenses.
