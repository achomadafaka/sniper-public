#!/usr/bin/env python3
"""Desk cluster check worker (sniper/services/memecoin_desk_cluster.py). Research Helius key only,
its own store under data/desk, never an entry path. --once runs one pass; --loop keeps passing.

    python3 scripts/memecoin_desk_cluster_worker.py --once
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files  # noqa: E402


def main(argv=None) -> int:
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--sleep-s", type=float, default=10.0)
    args = parser.parse_args(argv)
    load_env_files(os.environ.get("SNIPER_STATE_ROOT", str(ROOT)))
    from sniper.services.memecoin_cluster_rpc import (DESK_MAX_PAGES, DESK_PARSER_VERSION, DESK_PARSER_VERSION_NO_FALLBACK,
                                                      DeskClusterRpc, known_funders)
    from sniper.services.memecoin_cluster_service import ClusterAdmissionService
    from sniper.services.memecoin_cluster_store import ClusterStore
    from sniper.services.memecoin_desk_cluster import metered, run_pass
    from sniper.services.memecoin_research_pacer import SharedPacer
    from scripts.research.slot_lane_send_monitor import pick_research_url
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now_iso
    from urllib.request import urlopen

    try:
        # The plural research key list, refused on any funded/watcher key overlap. The singular
        # MEMECOIN_RESEARCH_HELIUS_RPC_URL is the spent key and is deliberately never used.
        url = pick_research_url(os.environ)
    except ValueError as exc:
        print("refused: %s (research host only)" % exc)
        return 2
    gap_s, workers = cfg.memecoin_desk_cluster_gap_s(), cfg.memecoin_desk_cluster_workers()
    # the desk's OWN gap on the key's shared state file: other readers keep GAP_S (1.1) and a 429 still pauses everyone
    pacer = SharedPacer.for_url(url, gap_s=gap_s)
    desk = Path(os.environ.get("SNIPER_STATE_ROOT", ROOT)) / "data" / "desk"
    store = ClusterStore(desk / "cluster.db")
    store.ensure_source_columns()
    funded_by = cfg.memecoin_desk_cluster_funded_by()
    parser_version = DESK_PARSER_VERSION if funded_by else DESK_PARSER_VERSION_NO_FALLBACK     # the kill knob keeps its own cache key
    non_clusterable = known_funders()["addresses"] if funded_by else frozenset()

    def factory(call_budget):
        meter, opener = metered(urlopen, call_budget, pacer=pacer)
        rpc = DeskClusterRpc(url, http=opener, max_pages=DESK_MAX_PAGES, on_quota=lambda: pacer.pause(60.0), funded_by=funded_by,
                             audit_every=cfg.memecoin_desk_cluster_funded_by_audit(), parser_version=parser_version,
                             calls_left=lambda: call_budget - meter["calls"])
        # frozen with the cohort: the row carries the definition it was made under, the per-source owner counts and the funded-by accounting
        meter["extra"] = rpc.extras
        # `workers` parallel traces behind one pacer; the deadline fits the budget at the desk gap, plus slack.
        return meter, ClusterAdmissionService(
            rpc, store, now_fn=real_utc_now_iso, parser_version=parser_version, non_clusterable=non_clusterable,
            workers=workers, deadline_s=call_budget * gap_s + 15.0, thin_owner_pass=True, early_allow=True,
            holder_failure_reason="cluster_unknown_dependency")
    while True:
        stats = run_pass(factory, desk / "candidate_emissions", desk / "cluster_checks",
                         per_min=cfg.memecoin_desk_cluster_per_min(),
                         calls_per_candidate=cfg.memecoin_desk_cluster_calls("CANDIDATE"),
                         calls_per_min=cfg.memecoin_desk_cluster_calls("MIN"),
                         calls_per_day=cfg.memecoin_desk_cluster_calls("DAY"),
                         now_fn=real_utc_now_iso,
                         max_age_s=cfg.memecoin_desk_cluster_max_age_s(),
                         intake_dir=desk / "candidate_emissions_intake")
        status = dict(stats, at=real_utc_now_iso())
        (desk / "cluster.status.json").write_text(json.dumps(status, sort_keys=True))
        print(json.dumps(status, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(args.sleep_s)


if __name__ == "__main__":
    sys.exit(main())
