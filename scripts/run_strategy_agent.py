#!/usr/bin/env python3
"""
Strategy Agent CLI runner.

Launches the appropriate strategy agent on-demand or via cron.

Usage:
    python3 scripts/run_strategy_agent.py insider_copy
    python3 scripts/run_strategy_agent.py insider_copy --verbose
    python3 scripts/run_strategy_agent.py insider_copy --prompt "Research wallet 0xABC specifically"
    python3 scripts/run_strategy_agent.py insider_copy --quiet   # cron-friendly

Available agents:
    insider_copy   — Analyzes insider_copy_v1 strategies + researches wallets
    adversarial_reviewer — Reviews open trade ideas before execution

Cron examples (stagger agents to avoid API rate limits):
    # 6 AM and 6 PM daily — insider copy analysis
    0 6,18 * * * /home/sniper/sniper/.venv/bin/python3 /home/sniper/sniper/scripts/run_strategy_agent.py insider_copy --quiet >> /home/sniper/sniper/logs/agent_insider_copy.log 2>&1
"""

import argparse
import importlib
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import anyio

# ── Agent registry ─────────────────────────────────────────────────────────────
# Maps CLI name → (module_path, async_function_name)
AGENT_REGISTRY: dict[str, tuple[str, str]] = {
    "insider_copy": ("agents.insider_copy_agent", "run"),
    "wallet_scout": ("agents.wallet_scout_agent", "run"),
    # "insider_analyst" removed 2026-07-14: insider_score retired per ADR-006 (inversely
    # correlated with lifetime PnL); the analyst had nothing left to calibrate. Source
    # archived under archive/legacy/insider-scoring-analyst-2026-07-14/ for reference.
    "strategy_reviewer": ("agents.strategy_reviewer_agent", "run"),
    "adversarial_reviewer": ("agents.adversarial_reviewer_agent", "run"),
}


def _import_agent(agent_type: str):
    """Import and return the agent's async run function."""
    if agent_type not in AGENT_REGISTRY:
        available = ", ".join(AGENT_REGISTRY.keys())
        print(f"ERROR: Unknown agent '{agent_type}'. Available: {available}", file=sys.stderr)
        sys.exit(1)
    module_path, fn_name = AGENT_REGISTRY[agent_type]
    module = importlib.import_module(module_path)
    return getattr(module, fn_name)


def _setup_logging(verbose: bool, quiet: bool) -> None:
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy SDK internals unless verbose
    if not verbose:
        for noisy in ("httpx", "httpcore", "anthropic", "claude_agent_sdk"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _ensure_log_dir() -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a strategy analysis agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "agent_type",
        choices=list(AGENT_REGISTRY.keys()),
        help="Agent to run",
    )
    parser.add_argument(
        "--prompt",
        help="Custom analysis prompt (overrides the default run workflow)",
        default=None,
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show debug logging and SDK internals",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Minimal output — only print agent result (best for cron logs)",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose, args.quiet)
    _ensure_log_dir()

    # Sanity checks
    db_path = os.getenv("DB_PATH", "sniper_tracker.db")
    if not (ROOT / db_path).exists() and not Path(db_path).exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    run_fn = _import_agent(args.agent_type)

    if not args.quiet:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'═'*60}")
        print(f"  Strategy Agent: {args.agent_type.upper()}")
        print(f"  Started:        {ts}")
        print(f"{'═'*60}\n")

    t0 = time.time()

    try:
        result = anyio.run(run_fn, args.prompt, args.verbose)
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.exception("Agent run failed: %s", exc)
        print(f"\nERROR: Agent failed — {exc}", file=sys.stderr)
        return 1

    elapsed = time.time() - t0

    if not args.quiet:
        print(f"\n{'─'*60}")
        print(f"  RESULT ({elapsed:.0f}s)")
        print(f"{'─'*60}\n")

    print(result)

    if not args.quiet:
        print(f"\n[Done in {elapsed:.0f}s]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
