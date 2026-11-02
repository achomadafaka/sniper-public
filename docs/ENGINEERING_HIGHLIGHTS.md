# Engineering highlights

These are implemented mechanisms worth inspecting. They are not claims that every component is enabled, every strategy is profitable, or every historical performance target was achieved.

## A research Office with a host-owned control plane

The Office separates model proposals from capabilities, authenticates model transports, bounds tool work, persists turns and receipts, fences stale supervisor generations, and separates ordinary records from private evaluation outcomes. This makes orchestration reviewable and recoverable rather than depending on an uninterrupted chat session.

Explore [`sniper/office/`](../sniper/office/), especially `supervisor.py`, `broker.py`, `capabilities.py`, `store.py`, `models.py`, and the paper/training modules.

## A knowledge Brain that can disagree with its own history

The Brain keeps typed facts, provenance, verification states, contradictions, supersession, and task/work records. It distinguishes “not checked” from “checked and broken,” and a plausible semantic match does not automatically outrank a relevant factual reference.

Explore [`bot/brain_graph/`](../bot/brain_graph/), [`scripts/brain_cli.py`](../scripts/brain_cli.py), and the Brain migrations.

## Cross-chain observations without flattening away evidence

Wallet-universe observations preserve chain, venue, exact source transaction, atomic units, capital age, coverage, and provider-read budgets. Canonical block/log recovery is labelled differently from a receipt fetch. That is important when providers expose inconsistent histories.

Explore [wallet universe and forensics](WALLET_UNIVERSE_AND_FORENSICS.md).

## Recoverable financial execution

The execution stack persists transaction identity before broadcast, distinguishes accepted/ambiguous/rejected/not-sent outcomes, protects durable close ownership, and uses executable quantity-bound evidence for exit decisions. Provider failure and token/route failure are different states.

Explore [`sniper/execution/`](../sniper/execution/), [`memecoin_real_runner.py`](../sniper/services/memecoin_real_runner.py), and [`db_memecoin.py`](../sniper/persistence/db_memecoin.py).

## A keyless Rust QUIC sidecar

Python hands already-signed wire bytes over a private local socket. The sidecar checks payload identity and submits through Solana/Agave QUIC tooling; it never needs a wallet signing key. Its Cargo dependency pins and vendored patch are visible.

Explore [`rust/solana-tpu-sidecar/`](../rust/solana-tpu-sidecar/) and [`solana_tpu_sidecar.py`](../sniper/execution/solana_tpu_sidecar.py). Solana TPU is transaction-processing infrastructure, not Google's TPU accelerator.

## Stream retention and single-writer archival

Research keeps retained transaction payloads useful for later exact decoding. Archive preparation and live-tape replacement have separate owners, and the implementation handles committed markers, restart recovery, and archive payload identity rather than assuming a successful file write proves a database transition completed.

Explore [`wallet_flow_archive.py`](../sniper/office/wallet_flow_archive.py), [`wallet_stream_proof.py`](../sniper/office/wallet_stream_proof.py), and their tests.

## Reusable wallet theses instead of a single ranking score

The wallet-analysis pipeline reconstructs positions, separates structural activity, attributes outcomes, applies statistical controls, slices behavior, and assembles candidate theses with explicit missing-data limits. A display explanation must not silently become a funded gate.

Explore [`wallet_protocol/`](../sniper/analysis/wallet_protocol/), [`winning_wallet_thesis/`](../sniper/analysis/winning_wallet_thesis/), and [`strategy_pattern_audit/`](../sniper/analysis/strategy_pattern_audit/).

## Broader market research and replay

Hyperliquid code covers market microstructure, funding, copy decomposition, paper evidence, calibration, and research surfaces. BTC/Polymarket 5-minute work, lead-lag studies, and a separate canonical event/replay platform provide additional research paths.

Explore [`sniper/hyperlive/`](../sniper/hyperlive/), [`sniper/algos/`](../sniper/algos/), [`sniper/research/leadlag/`](../sniper/research/leadlag/), and [`sniper/sim/`](../sniper/sim/).

## Performance measured at the right boundary

Frame-level offline benchmarks and execution traces distinguish decoding, CPU work, queueing, persistence, provider round trips, submission, and chain confirmation. Percentile timings and original monotonic boundaries are useful; subtracting unrelated wall clocks or summing overlapping stages is not.

Explore [`txshape_latency_bench.py`](../scripts/research/txshape_latency_bench.py), [`solana_feed_host_benchmark.py`](../scripts/research/solana_feed_host_benchmark.py), and the trace/transport tests.

## Immutable runtime and measurement boundaries

Deployment tooling builds pinned releases and verifies runtime identity independently of the mutable checkout. Research, paper measurement, funded authority, authenticated web wiring, and organic post-deploy effectiveness are separate verdicts.

Explore [`deploy_memecoin_runtime.sh`](../scripts/deploy_memecoin_runtime.sh), [`memecoin_runtime_verify.py`](../scripts/memecoin_runtime_verify.py), and [`memecoin_runtime_processes.py`](../scripts/memecoin_runtime_processes.py). These are operational source references, not recommended quick-start commands for this public snapshot.

## Read the tests as executable design notes

The snapshot contains thousands of test and fixture files. Public CI intentionally runs a named subset; file counts are not a passing-test count. Tests cover ordinary outcomes as well as ambiguous submission, stale clocks, broken source identity, missing data, duplicate ownership, concurrency, and restart recovery.

Production records and old audit logs remain excluded. The repository demonstrates inspectable engineering work; it does not substitute source volume for validation.
