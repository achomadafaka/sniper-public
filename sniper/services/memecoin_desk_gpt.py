"""Bounded subscription GPT transport; caller contracts retain semantic validation.

Uses the existing CHECKER jail and guarded auth rotation. The CLI often omits a
served-model echo: model_requested records intent, model_echo stays unknown.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from scripts.research import judge_gpt_replay as transport

MODEL = "gpt-6-luna"
MODELS = (MODEL, "gpt-5.6-luna", "gpt-6.1-sol")
PROVIDER = "codex_cli"
SUPPORTS_OUTPUT_TOKEN_CAP = False
BUNDLED_CLI = Path("/usr/lib/chatgpt/resources/codex")


def _preferred_cli():
    return str(BUNDLED_CLI) if BUNDLED_CLI.is_file() and os.access(BUNDLED_CLI, os.X_OK) else None


def _object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate_json_key")
    return value


def _constant(value):
    raise ValueError("nonfinite_json")


def call_model(system, user, *, deadline_s, model=MODEL, effort="low", schema_obj,
               max_output_tokens=None):
    """One call, no queue or retry. Invalid adapter configuration raises before I/O.

    schema_obj is sent to CLI structured output; callers still validate meaning.
    Actual stream IDs are kept missing when absent, never synthesized for billing.
    """
    if model not in MODELS:
        raise ValueError("gpt_model")
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("gpt_effort")
    if type(deadline_s) not in (int, float) or not math.isfinite(deadline_s) or deadline_s <= 0:
        raise ValueError("gpt_deadline")
    if not isinstance(schema_obj, dict) or schema_obj.get("type") != "object":
        raise ValueError("gpt_schema")
    started = time.monotonic()
    meta = {}
    text, error = None, None
    try:
        # CLI has no supported generation token cap; never silently discard a frozen budget.
        if max_output_tokens is not None:
            error = "output_budget_unsupported"
        else:
            checker = transport._checker()
            prompt = system + "\n\nReturn only the JSON object matching the supplied output schema. " + \
                     "Do not use tools. Input data follows:\n" + user
            text, error, _, meta = transport.codex_json(
                checker, prompt, timeout_s=max(0, deadline_s - (time.monotonic() - started)),
                schema_obj=schema_obj, model=model, effort=effort, slot_timeout_s=0,
                codex_path=_preferred_cli(), page=lambda message: None)
            if error and error.startswith("exit_"):
                meta["exit_code"] = int(error[5:])
                error = "cli_failed"
        if error is None:
            try:
                if not isinstance(json.loads(text, object_pairs_hook=_object, parse_constant=_constant), dict):
                    raise ValueError
            except (TypeError, ValueError):
                error = "invalid_json"
        if error is None and time.monotonic() - started >= deadline_s:
            error = "timeout"
    except Exception as exc:
        error = "transport_error"
        meta["exception"] = type(exc).__name__
    usage = {}
    raw_usage = meta.get("usage")
    for key, value in (raw_usage.items() if isinstance(raw_usage, dict) else ()):
        if key in ("input_tokens", "output_tokens", "cached_input_tokens") and type(value) is int and value >= 0:
            usage[{"cached_input_tokens": "cache_read_input_tokens"}.get(key, key)] = value
    return {"text": text if error is None else None, "call_error": error, "is_error": error is not None,
            "no_model_call": error in {"output_budget_unsupported", "codex_busy", "cli_missing", "no_isolation", "no_codex_login"},
            "provider": PROVIDER, "model_requested": model, "model_echo": meta.get("model_echo"),
            "session_id": meta.get("session_id"), "result_id": meta.get("result_id"),
            "billing_basis": "subscription_flat", "usage": usage, "notional_usd": None,
            "timing": {"wall_ms": round((time.monotonic() - started) * 1000, 1), "transport": PROVIDER,
                       "cli_path": meta.get("cli_path")},
            "transport_meta": {k: meta.get(k) for k in ("tool_events", "tool_event_type", "warnings", "provider_error_kind", "auth", "exception", "exit_code")}}
