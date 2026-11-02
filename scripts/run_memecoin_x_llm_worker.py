#!/usr/bin/env python3
"""Run one disabled-by-default optional memecoin X LLM job."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.persistence.db_core import get_readonly_connection
from sniper.services.memecoin_x_llm_worker import run_once


def _factory(path):
    module_name, separator, name = str(path).partition(":")
    if not separator:
        raise ValueError("provider factory must be module:callable")
    factory = getattr(importlib.import_module(module_name), name)
    return factory()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--provider-factory")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--daily-request-budget", type=int, default=20)
    parser.add_argument("--daily-token-budget", type=int, default=100000)
    parser.add_argument("--max-input-bytes", type=int, default=100000)
    parser.add_argument("--prompt-version", default=os.getenv("MEMECOIN_X_LLM_PROMPT_VERSION"))
    parser.add_argument("--model-version", default=os.getenv("MEMECOIN_X_LLM_MODEL_VERSION"))
    parser.add_argument(
        "--output-schema-version", default=os.getenv("MEMECOIN_X_LLM_OUTPUT_SCHEMA_VERSION"))
    parser.add_argument("--clock-offset-file", default=str(ROOT / "data/clock_offset.json"))
    args = parser.parse_args(argv)
    observed_at, health = x_observed_at_or_none(path=args.clock_offset_file)
    if observed_at is None:
        parser.error(f"healthy corrected clock required: {health.get('reason', 'unknown')}")
    enabled = bool(args.run)
    if enabled and os.getenv("MEMECOIN_X_LLM_ENABLED") != "1":
        parser.error("provider invocation requires MEMECOIN_X_LLM_ENABLED=1")
    if enabled and not args.provider_factory:
        parser.error("provider invocation requires --provider-factory module:callable")
    if enabled and not all((args.prompt_version, args.model_version, args.output_schema_version)):
        parser.error("provider invocation requires prompt, model, and output schema versions")
    provider = _factory(args.provider_factory) if enabled else None
    if enabled:
        conn = sqlite3.connect(Path(args.db).expanduser(), timeout=30)
        conn.row_factory = sqlite3.Row
    else:
        conn = get_readonly_connection(str(Path(args.db).expanduser()))
    try:
        result = run_once(
            conn, provider=provider, observed_at=observed_at, enabled=enabled,
            timeout_s=args.timeout, daily_request_budget=args.daily_request_budget,
            daily_token_budget=args.daily_token_budget,
            max_input_bytes=args.max_input_bytes,
            current_versions={
                "prompt_version": args.prompt_version,
                "model_version": args.model_version,
                "output_schema_version": args.output_schema_version,
            } if enabled else None)
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
