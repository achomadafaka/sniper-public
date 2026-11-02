#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _read_event_payload() -> str:
    chunks: list[str] = []
    try:
        chunks.append(sys.stdin.read())
    except Exception as exc:
        print(f"[targeted-test-hook] warning: could not read stdin: {exc}", file=sys.stderr, flush=True)
    env_payload = os.environ.get("TOOL_INPUT", "")
    if env_payload:
        chunks.append(env_payload)
    return "\n".join(part for part in chunks if part).strip()


def _collect_strings(obj) -> list[str]:
    results: list[str] = []
    if isinstance(obj, dict):
        for value in obj.values():
            results.extend(_collect_strings(value))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_collect_strings(item))
    elif isinstance(obj, str):
        results.append(obj)
    return results


def _extract_candidate_paths(payload: str) -> list[Path]:
    if not payload:
        return []

    strings: list[str] = []
    try:
        strings.extend(_collect_strings(json.loads(payload)))
    except (json.JSONDecodeError, ValueError):
        strings.append(payload)

    pattern = re.compile(r"[\w./-]+\.(?:py|js|css|html|json|md)")
    candidates: list[Path] = []
    seen: set[Path] = set()
    for text in strings:
        for token in pattern.findall(text):
            path = (ROOT / token).resolve() if not token.startswith("/") else Path(token).resolve()
            try:
                path.relative_to(ROOT)
            except ValueError:
                continue
            if path.exists() and path.is_file() and path not in seen:
                seen.add(path)
                candidates.append(path)
    return candidates


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _tests_for_path(rel: str) -> list[str]:
    if rel.startswith("tests/") and rel.endswith(".py"):
        return [rel]

    exact_map: dict[str, list[str]] = {
        "db.py": [
            "tests/test_db.py",
            "tests/test_mcp_tools.py",
            "tests/test_proof_bar.py",
            "tests/test_classification_diagnostics.py",
            "tests/test_replay_engine.py",
            "tests/test_context_layer.py",
            "tests/test_commodities_feed.py",
            "tests/test_news_feed.py",
            "tests/test_oil_momentum.py",
        ],
        "paper_trader.py": [
            "tests/test_source_exit_correctness.py",
            "tests/test_critical_bugs.py",
            "tests/test_paper_trader_sizing.py",
        ],
        "alerter.py": [
            "tests/test_alerter_commands.py",
            "tests/test_alerter_refactor.py",
            "tests/test_alerter_thesis.py",
        ],
        "strategy_runner.py": [
            "tests/test_runner_integration.py",
            "tests/test_strategy_framework.py",
            "tests/test_risk_policy.py",
        ],
        "replay_engine.py": [
            "tests/test_replay_engine.py",
            "tests/test_replay_api.py",
            "tests/test_strategy_plugin_contract.py",
            "tests/test_replay_harness.py",
        ],
        "instance_profile.py": [
            "tests/test_instance_profile.py",
            "tests/test_instance_tag_schema.py",
            "tests/test_runner_integration.py",
        ],
        "health_check.py": [
            "tests/test_health_check_import.py",
            "tests/test_proof_bar.py",
            "tests/test_mcp_tools.py",
        ],
        "polymarket_api.py": [
            "tests/test_polymarket_api.py",
            "tests/test_oil_momentum.py",
            "tests/test_weather_strategy.py",
        ],
    }
    if rel in exact_map:
        return exact_map[rel]

    if rel.startswith("feeds/"):
        name = Path(rel).stem
        tests = ["tests/test_feed_registry.py", "tests/test_runner_integration.py"]
        if name == "commodities":
            tests += ["tests/test_commodities_feed.py", "tests/test_oil_momentum.py"]
        elif name == "news":
            tests += ["tests/test_news_feed.py", "tests/test_context_layer.py"]
        elif name == "weather":
            tests += ["tests/test_weather_strategy.py"]
        elif name == "polymarket":
            tests += ["tests/test_polymarket_api.py"]
        return tests

    if rel.startswith("strategies/"):
        name = Path(rel).stem
        tests = ["tests/test_strategy_framework.py", "tests/test_strategy_plugin_contract.py"]
        if name == "oil_momentum_v1":
            tests += ["tests/test_oil_momentum.py", "tests/test_context_layer.py"]
        elif name == "weather_v1":
            tests += ["tests/test_weather_strategy.py"]
        elif name in {"insider_copy_v1", "fade_v1"}:
            tests += ["tests/test_replay_engine.py", "tests/test_replay_api.py"]
        return tests

    if rel.startswith("replay/"):
        return [
            "tests/test_replay_engine.py",
            "tests/test_replay_api.py",
            "tests/test_strategy_plugin_contract.py",
            "tests/test_replay_harness.py",
        ]

    if rel.startswith("risk/"):
        return [
            "tests/test_risk_policy.py",
            "tests/test_strategy_framework.py",
            "tests/test_runner_integration.py",
        ]

    if rel.startswith("context/"):
        return ["tests/test_context_layer.py", "tests/test_oil_momentum.py"]

    if rel.startswith("routes/"):
        stem = Path(rel).stem
        route_map: dict[str, list[str]] = {
            "api_strategies": [
                "tests/test_replay_api.py",
                "tests/test_strategy_framework.py",
                "tests/test_followable_leaderboard.py",
            ],
            "api_core": [
                "tests/test_classification_diagnostics.py",
                "tests/test_followable_leaderboard.py",
            ],
            "api_misc": [
                "tests/test_critical_bugs.py",
                "tests/test_mcp_tools.py",
            ],
        }
        return route_map.get(stem, [])

    base = Path(rel).stem
    conventional = ROOT / "tests" / f"test_{base}.py"
    if conventional.exists():
        return [f"tests/test_{base}.py"]

    return []


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen and (ROOT / item).exists():
            seen.add(item)
            out.append(item)
    return out


def main() -> int:
    payload = _read_event_payload()
    paths = _extract_candidate_paths(payload)

    tests: list[str] = []
    for path in paths:
        tests.extend(_tests_for_path(_relative(path)))
    tests = _dedupe(tests)

    if tests:
        cmd = ["python3", "-m", "pytest", *tests, "-x", "-q", "--tb=line"]
        print(
            f"[targeted-test-hook] running targeted tests for: {', '.join(_relative(p) for p in paths[:3])}",
            flush=True,
        )
        print(f"[targeted-test-hook] pytest {' '.join(tests)}", flush=True)
    else:
        cmd = [
            "python3",
            "-m",
            "pytest",
            "tests/",
            "-x",
            "-q",
            "--tb=line",
        ]
        if paths:
            print(
                f"[targeted-test-hook] no repo-aware mapping for: {', '.join(_relative(p) for p in paths[:3])}",
                flush=True,
            )
        else:
            print("[targeted-test-hook] no edited file path detected from hook payload", flush=True)
        print("[targeted-test-hook] falling back to full suite", flush=True)

    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
