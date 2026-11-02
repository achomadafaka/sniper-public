"""Cross-chain latency and isolation acceptance gates for the V6 paper owner."""
import ast
import asyncio
from datetime import datetime, timezone
import json
import os
import statistics
import threading
import time
from pathlib import Path

from sniper.services.memecoin_v6_engine import (
    EvidenceIdentity, V6PositionState, make_observation, step,
)
from sniper.services.memecoin_v6_events import V6MarketEvent
from sniper.services.memecoin_v6_fast_runtime import FastRuntimeContext, V6FastRuntime
from sniper.services.memecoin_v6_incremental_state import PoolAccumulator, PoolMeta


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-26T12:00:00+00:00"


def _percentile(values, percentile):
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * percentile / 100) - 1)]


def test_four_position_cross_chain_runtime_isolates_slow_quote_and_meets_local_targets():
    async def case():
        pools = (
            ("4663", "0x" + "11" * 20),
            ("4663", "0x" + "22" * 20),
            ("solana", "VaultA"),
            ("solana", "VaultB"),
        )
        positions = {pool: f"p{index}" for index, pool in enumerate(pools, 1)}
        accumulators = {
            pool: PoolAccumulator(PoolMeta(
                pool[0], pool[1], f"mint-{index}", "v2" if pool[0] == "4663" else "sol",
                0 if pool[0] == "4663" else 1))
            for index, pool in enumerate(pools, 1)
        }
        states = {position: V6PositionState(position, 1_000_000)
                  for position in positions.values()}
        decisions = {position: 0 for position in positions.values()}
        step_ms = []
        latency_ms = {"receive_to_observation": [], "observation_to_decision": []}
        release = threading.Event()

        def quote(position_id, _now):
            if position_id == "p1":
                release.wait(1)
            return "quote"

        def observe(position_id, _facts, _quote, observed_at, evidence_id):
            observation = make_observation(
                observation_id=f"{position_id}:{evidence_id}", observed_at=observed_at,
                network_id="4663" if position_id in {"p1", "p2"} else "solana",
                payload={"executable_price": 1.0, "chain_vol_p90": 0.1,
                         "route_degraded": False, "source_wallet_exited": False,
                         "wallet_tier_adjustment": 0, "is_break": False, "held_s": 1.0,
                         "arrival_rate": 0.1, "buy_share": 0.6, "realized_vol": 0.01,
                         "prior_low": 0.9, "price": 1.0, "net": 1.0},
                evidence=EvidenceIdentity(market=evidence_id),
            )
            started = time.perf_counter_ns()
            decision = step(states[position_id], observation)
            step_ms.append((time.perf_counter_ns() - started) / 1_000_000)
            states[position_id] = decision.state_if_filled
            decisions[position_id] += 1

        def record_latency(stage, duration_us, *_args):
            if stage in latency_ms:
                latency_ms[stage].append(duration_us / 1_000)

        # Warm the pure kernel before measuring the runtime path.
        warm = V6PositionState("warm", 1_000_000)
        for index in range(100):
            observation = make_observation(
                observation_id=f"warm-{index}", observed_at=NOW, network_id="4663",
                payload={"executable_price": 1.0, "price": 1.0, "held_s": 1.0},
                evidence=EvidenceIdentity(market=f"warm-{index}"))
            warm = step(warm, observation).state_if_filled

        context = FastRuntimeContext(
            accumulators=accumulators,
            positions_for_pool=lambda network, pool: (positions[(network, pool)],),
            all_position_ids=lambda: tuple(positions.values()), quote_position=quote,
            observe_position=observe,
            now_iso=lambda: datetime.now(timezone.utc).isoformat(),
            record_latency=record_latency, coalesce_ms=1, max_open=4,
        )
        runtime = V6FastRuntime(context)
        for sequence in range(25):
            for index, ((network, pool), position_id) in enumerate(positions.items(), 1):
                received_at = datetime.now(timezone.utc).isoformat()
                event_number = sequence * len(positions) + index
                event = V6MarketEvent(
                    network_id=network, pool=pool, block_height=100 + event_number,
                    block_hash=f"0xb{event_number}" if network == "4663" else None,
                    tx_id=f"tx-{event_number}", event_index=event_number,
                    source_event_at=received_at, received_at=received_at,
                    kind="v2_swap" if network == "4663" else "sol_swap",
                    payload={"amount0": "100", "amount1": "-50"},
                )
                await runtime.inject(event)
                await runtime.process_one_dirty()
                assert decisions[position_id] == sequence + 1

        assert _percentile(latency_ms["receive_to_observation"], 95) < 100
        assert _percentile(latency_ms["observation_to_decision"], 99) < 25
        assert _percentile(step_ms, 99) < 5
        assert decisions == {"p1": 25, "p2": 25, "p3": 25, "p4": 25}
        assert runtime._action_requests == set()
        if os.getenv("V6_TEST_METRICS"):
            print(json.dumps({stage: {
                "p50_ms": statistics.median(values),
                "p95_ms": _percentile(values, 95),
                "p99_ms": _percentile(values, 99),
            } for stage, values in {
                **latency_ms, "step": step_ms,
            }.items()}, sort_keys=True))
        release.set()
        await runtime.close()

    asyncio.run(case())


def test_transitive_v6_runtime_imports_exclude_funded_execution_modules():
    forbidden = {
        "memecoin_real_runner", "real_trader", "solana_swap_executor",
        "evm_swap_executor", "solana_exit_executor", "evm_exit_executor",
        "submission_backends", "memecoin_reservations", "memecoin_exit_brain",
        "memecoin_v5_replay_gate", "memecoin_v5_feature_study", "memecoin_v5_blind_replay",
    }
    pending = [
        ROOT / "sniper/services/memecoin_v6_fast_runtime.py",
        ROOT / "sniper/services/memecoin_v6_paper_runner.py",
        ROOT / "scripts/memecoin_v6_paper_worker.py",
    ]
    visited = set()
    imported = set()
    while pending:
        path = pending.pop()
        if path in visited or not path.exists():
            continue
        visited.add(path)
        tree = ast.parse(path.read_text(), filename=str(path))
        modules = []
        # Follow imports executed while each module loads. Function-local optional fallbacks in
        # shared quote modules are not reachable because V6 supplies every route/RPC parameter.
        for node in tree.body:
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        imported.update(modules)
        for module in modules:
            if not module.startswith("sniper."):
                continue
            candidate = ROOT.joinpath(*module.split(".")).with_suffix(".py")
            if candidate.exists():
                pending.append(candidate)
    assert not {module for module in imported if forbidden.intersection(module.split("."))}
