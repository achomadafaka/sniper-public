#!/usr/bin/env python3
"""Import changed local memory files as provenance episodes, never facts or status changes.

Each raw file hash names an immutable version. The phase-2 writer resumes missing parts on retry.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MEMORY_DIR = Path.home() / ".claude/projects/-home-bigpapi-Desktop-Sniper/memory"


def run(store, memory_dir, *, secret_values):
    from bot.brain_graph.api import PART
    from bot.brain_graph.gate import redact
    from bot.brain_graph.importer import write_episodes
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        raise FileNotFoundError(memory_dir)
    existing = store.episode_index("import", "memory/")
    result = {"files": 0, "imported": 0, "unchanged": 0, "skipped_index": 0}
    episodes = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name in ("MEMORY.md", "HANDOFF.md") or path.name.startswith("HANDOFF-archive"):
            result["skipped_index"] += 1
            continue
        if path.is_symlink() or not path.is_file():
            continue
        raw = path.read_text(errors="replace")
        key = f"memory/{path.name}#{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
        body, _ = redact(raw, secret_values)
        episodes.append({"key": key, "kind": "legacy_memory", "body": body, "author": "import",
                         "occurred_at": None, "anchor": str(path)})
        result["files"] += 1
        parts = max(1, (len(body) + PART - 1) // PART)
        complete = {r["part_no"] for r in existing.get(key, {}).get("rows", [])} == set(range(1, parts + 1))
        result["unchanged" if complete else "imported"] += 1
    report = {}
    written = write_episodes(store, episodes, report)
    if len(written) != len(episodes):
        raise RuntimeError("Some immutable episode bodies differ; review import redaction before retry")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", type=Path, default=MEMORY_DIR)
    args = parser.parse_args(argv)
    from bot.brain_graph import live
    from sniper.util.envfiles import load_env_files
    from bot.brain_graph.gate import secret_values_from_env, secret_values_from_mapping
    state = live.state_root(ROOT)
    load_env_files(str(state))
    from bot.brain_graph.store import RestStore
    secrets = secret_values_from_env(state / ".env") | secret_values_from_mapping(os.environ)
    print(json.dumps(run(RestStore(timeout=10), args.memory_dir, secret_values=secrets)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
