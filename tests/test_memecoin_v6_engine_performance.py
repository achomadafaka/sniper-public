"""Warm V6 kernel latency and funded-import boundary gates."""
import ast
import statistics
import time
from pathlib import Path

from sniper.services.memecoin_v6_engine import (
    EvidenceIdentity, V6PositionState, make_observation, step,
)

ROOT = Path(__file__).resolve().parents[1]


def test_warm_v6_kernel_p99_is_below_five_ms():
    payload = {"executable_price": 1.0, "price": 1.0, "held_s": 1.0}
    observations = [
        make_observation(
            observation_id=f"perf-{index}", observed_at="2026-07-26T12:00:00+00:00",
            network_id="solana", payload=payload,
            evidence=EvidenceIdentity(market=f"slot:{index}"),
        )
        for index in range(10_001)
    ]
    state = V6PositionState(position_id="perf", remaining_qty_atomic=1_000_000)
    state = step(state, observations[0]).state_if_filled
    samples_ns = []
    for observation in observations[1:]:
        started = time.perf_counter_ns()
        decision = step(state, observation)
        samples_ns.append(time.perf_counter_ns() - started)
        state = decision.state_if_filled

    ordered = sorted(samples_ns)
    p50_ms = statistics.median(ordered) / 1_000_000
    p95_ms = ordered[int(len(ordered) * 0.95) - 1] / 1_000_000
    p99_ms = ordered[int(len(ordered) * 0.99) - 1] / 1_000_000
    assert p99_ms < 5.0, f"warm latency p50={p50_ms:.3f} p95={p95_ms:.3f} p99={p99_ms:.3f} ms"


def test_v6_runtime_modules_do_not_import_funded_execution_surfaces():
    # Resolve each subject wherever it lives: part of the V6 set moved to
    # archive/legacy/memecoin-v6-2026-08-30 on 2026-08-30, and a guard must not stop guarding
    # merely because its subject was archived.
    _archive = ROOT / "archive/legacy/memecoin-v6-2026-08-30"

    def _subject(rel):
        live = ROOT / rel
        if live.exists():
            return live
        archived = _archive / rel
        assert archived.exists(), f"guard subject vanished from both roots: {rel}"
        return archived

    files = tuple(_subject(r) for r in (
        "sniper/services/memecoin_v6_engine.py",
        "sniper/services/memecoin_v6_position_policy.py",
        "sniper/persistence/db_memecoin_v6_paper.py",
        "sniper/services/memecoin_v6_paper_runner.py",
        "scripts/memecoin_v6_paper_worker.py",
    ))
    forbidden = (
        "memecoin_real_runner", "real_trader", "solana_swap_executor", "evm_swap_executor",
        "solana_exit_executor", "evm_exit_executor", "submission_backends",
    )
    imported = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
    for module in imported:
        assert not any(name in module for name in forbidden), module
