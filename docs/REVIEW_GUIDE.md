# Technical review guide

This guide describes the November 2, 2026 public source snapshot. It is an architecture map, not a report of a running installation.

## Suggested reading order

1. **Wallet analysis:** `sniper/analysis/wallet_protocol/types.py` defines report surfaces. `sniper/analysis/winning_wallet_thesis/cli.py` composes ledger enrichment, position building, slicing, shuffle baselines, clustering, and thesis assembly. Display-only explanations must not silently change verdicts.
2. **Network research:** `sniper/office/wallet_campaign.py`, `wallet_network.py`, and `wallet_launch_proof.py` show campaign construction and evidence binding. Distinguish a signing developer from a fee beneficiary, a shared service, or a later settlement counterparty.
3. **Paper boundaries:** `sniper/research/memecoin_strategy/` separates candidate evaluation from funded execution. Research results need causal timestamps, coverage labels, and realistic execution evidence before promotion.
4. **Execution correctness:** `sniper/services/memecoin_real_runner.py`, `sniper/persistence/db_memecoin.py`, and `sniper/execution/` contain admission, persistence, transport, and exit handling. Focus on signed/submitted ambiguity, duplicate ownership, atomic claims, and original source age.
5. **Transport and performance:** `sniper/execution/solana_tpu_sidecar.py` connects Python to `rust/solana-tpu-sidecar/`. The Rust process submits already-signed bytes over QUIC; this is blockchain transport, not an ML kernel. `scripts/research/txshape_latency_bench.py` demonstrates offline frame-level timing.
6. **Tests:** begin with the bounded README command, then inspect the matching tests for a subsystem before attempting broader runs.

## Useful review questions

- Does a claim have an exact source identity and an original observation time?
- Are unknown data, valid zeros, and confirmed absence represented separately?
- Does a retry preserve ownership after signing or an ambiguous broadcast?
- Are slow provider calls and research work kept off entry and exit paths?
- Do paper measurements include missing coverage, fees, slippage, and failed exits?
- Does a performance claim separate local computation from upstream latency?

## Snapshot boundaries

Application source, test code and fixtures, migrations, configuration templates, dashboard assets, and design contracts are included. Private runtime state, credentials, agent notes, historical audit records, and the original Git history are excluded. Historical `archive/` code is also omitted.

Some tests inspect the complete private repository or rely on its operational artifacts. Their presence is useful as a contract, but it does not imply they can all pass in this export. The public CI deliberately names its limited scope.
