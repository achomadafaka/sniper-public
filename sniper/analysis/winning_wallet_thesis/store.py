"""Durable per-wallet thesis-profile store. Each analyzer run persists its full
ThesisProfile as JSON so outputs ACCUMULATE — a later tool can read this directory and
compare wallets ("x does better than y at A, y at B"). One wallet at a time is the
analyzer's job; comparison is a separate, future consumer of these stored outputs."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

STORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "runs", "wallet_thesis_profiles")


def persist_profile(profile: Any, *, store_dir: str | None = None) -> str:
    """Write the profile JSON to <store_dir>/<wallet>__<run_id>.json (distinct data ->
    distinct file, since run_id is a deterministic content hash). Returns the path."""
    root = store_dir or STORE_DIR
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{profile.wallet.lower()}__{profile.run_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(profile), f, default=str, indent=2)
    os.replace(tmp, path)
    return path


def load_profiles(store_dir: str | None = None) -> list[dict]:
    """Read all stored profiles (for the future cross-wallet comparison tool)."""
    root = store_dir or STORE_DIR
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if name.endswith(".json"):
            with open(os.path.join(root, name)) as f:
                out.append(json.load(f))
    return out
