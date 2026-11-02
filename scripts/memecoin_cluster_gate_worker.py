#!/usr/bin/env python3
"""Run the pinned funded cluster admission service."""
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc
from sniper.services.memecoin_cluster_service import (
    ClusterAdmissionService,
    ClusterCacheWorker,
    bind_server,
    serve_one,
)
from sniper.services.memecoin_cluster_store import ClusterStore
from sniper.util.clock import real_utc_now_iso
from sniper.util.envfiles import load_env_files


HEARTBEAT_INTERVAL_S = 1.0


import logging

logger = logging.getLogger(__name__)
CACHE_IDLE_S = 2.0


def _state_paths():
    state_root = Path(os.environ["SNIPER_STATE_ROOT"])
    data = state_root / "data"
    data.mkdir(parents=True, exist_ok=True)
    return (
        data / "memecoin_cluster_gate.db",
        data / "memecoin_cluster_gate.sock",
        data / "memecoin_cluster_gate.heartbeat",
        data / "memecoin_cluster_gate.status.json",
    )


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    # SNIPER_STATE_ROOT is REQUIRED, never defaulted to the code root. The worker runs from
    # the pinned release directory, so falling back to ROOT would put the funder cache, the
    # socket, and the heartbeat inside a release that the next deploy replaces — silently
    # discarding every resolved funder and stranding the socket the funded client dials.
    configured_root = os.environ.get("SNIPER_STATE_ROOT", "").strip()
    if not configured_root:
        parser.error("SNIPER_STATE_ROOT is required (never defaults to the release directory)")
    state_root = Path(configured_root)
    load_env_files(state_root)
    url = os.environ.get("MEMECOIN_RESEARCH_HELIUS_RPC_URL", "").strip()
    if not url:
        parser.error("MEMECOIN_RESEARCH_HELIUS_RPC_URL is required")
    db_path, socket_path, heartbeat, status_path = _state_paths()
    store = ClusterStore(db_path)
    rpc = ResearchClusterRpc(url)
    service = ClusterAdmissionService(rpc, store)
    cache = ClusterCacheWorker(store, rpc.trace_first_funder)
    if args.once:
        _atomic_write(heartbeat, real_utc_now_iso() + "\n")
        _atomic_write(status_path, json.dumps(store.status_summary(), sort_keys=True))
        return 0
    server = bind_server(socket_path)
    server.settimeout(HEARTBEAT_INTERVAL_S)
    # Cache warming runs on its OWN thread. Inline, a pass of 4 cold traces holds the loop
    # for up to ~14 s while the funded client's deadline is 13 s, so cache work could itself
    # produce cluster_store_unavailable blocks on live entries. The socket loop must never
    # wait on RPC.
    stop_cache = threading.Event()

    def cache_loop():
        while not stop_cache.is_set():
            try:
                cache.cache_pass(limit=4)
            except Exception:
                logger.exception("cluster cache pass failed")
            stop_cache.wait(CACHE_IDLE_S)

    cache_thread = threading.Thread(target=cache_loop, name="cluster-cache", daemon=True)
    cache_thread.start()
    try:
        while True:
            _atomic_write(heartbeat, real_utc_now_iso() + "\n")
            _atomic_write(
                status_path,
                json.dumps(store.status_summary(), sort_keys=True),
            )
            try:
                serve_one(server, service)
            except socket.timeout:
                continue
    finally:
        stop_cache.set()
        server.close()
        socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
