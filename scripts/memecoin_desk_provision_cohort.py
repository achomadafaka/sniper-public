#!/usr/bin/env python3
"""Provision desk cohort U (operator, explicit; never per emission). DRY RUN by default: prints the
exact frozen policy and the MEMECOIN_DESK_U_COHORT_ID it resolves to, and writes nothing. --confirm
writes the cohort to both stores (the desk DB and the funded ledger's memecoin_desk_cohorts) through
sniper.services.memecoin_desk.provision_cohort; a re-run with the same policy reports it already provisioned.

The cohort id is sha256 of the frozen body (producer, prompt version, policy), so any policy change is
a new cohort, never an edit. Versions come from the code that runs them (ranker, selection rule,
label, SR p, crawler), so the policy cannot name a version the release does not run; with no CRAWLER
in the release it refuses. judge_p_admit, the
hard-filter thresholds and the hook allowlist have no defaults: they are passed explicitly.

    python3 scripts/memecoin_desk_provision_cohort.py --judge-p-admit <P> \\
        --round-trip-loss-pct-max <R>          # dry run
    ... --confirm                                         # the operator only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROMPT_VERSION = "desk-judge-v18"
PLAYBOOK = ROOT / "docs" / "superpowers" / "specs" / "2026-10-21-desk-judge-playbook.md"
D_COMPONENTS = ('buyers', 'history', 'heat', 'list_claims', 'd_sheet', 'chart', 'ps_trending')


def _budget_for_cohort(a):
    if not a.freeze_answer_budget and not a.ps_inputs:
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from memecoin_desk_judge_worker import answer_budget
    value = answer_budget()
    if value is None and a.freeze_answer_budget:
        raise SystemExit("refused: --freeze-answer-budget needs MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS set")
    return value


def frozen_policy(*, judge_p_admit, round_trip_loss_pct_max, hook_allowlist, rail_bounds, venue="pumpfun", trigger=False,
                  winner_reentry=None, verdict_tolerance_pct=None, mcap_band=None, claim_margin_pct=None,
                  moved_max_pct=None, standing_orders=False, v23=False, max_output_tokens=None, drift_requeue=False,
                  judge_model=None, exit_observer=None, entry_gate_wait=False,
                  px_hold_max_wait_s=60, graduation_guard_pct=90.0,
                  standing_v2=False, chase_limit=False, cluster=False, watch_list=False,
                  structure_sheet=None, intake=None, preregistration=None, ps_inputs=False,
                  ps_inputs_version=None, judge_playbook_sha256=None, d_definitions=None):
    """Cohort U's frozen policy (Alpha 2026-10-21, ranker pre-registration amendments 1-4 and 8). Hard filter hf2
    (operator order 2026-10-22): exit and pricing blockers only; cluster, top10, creator and crawler are JUDGE facts,
    so the frozen hard_filter carries no top10/creator ceiling and a different policy hash (a new cohort)."""
    from sniper.services import memecoin_desk_judge as J
    from sniper.services.memecoin_desk_contract import HARD_FILTER_VERSION, judge_defer_max_s
    from sniper.services.memecoin_desk_structure import AGENTS as STRUCTURE_AGENTS, VERSION as STRUCTURE_VERSION, WINDOWS as STRUCTURE_WINDOWS_S
    from sniper.services import memecoin_desk_ranker as R
    from sniper.services.memecoin_desk_context_facts import (BOARD_FRESH_S, CONTEXT_VERSION, THEME_FRESH_S,
                                                             THEME_WINDOW_S)
    from sniper.services.memecoin_desk_safety import FEE_AGREE_MIN, FEE_OF_NEWEST, REGULAR_FEE_TRADES
    from sniper.services import memecoin_desk_ranker_label as L
    from sniper.services.memecoin_cluster_rpc import desk_definition as CLUSTER_DEFINITION
    crawler_version = _crawler_version()
    if type(judge_p_admit) not in (int, float) or not 0 < judge_p_admit <= 1:
        raise ValueError("judge_p_admit must be in (0, 1]")
    if not rail_bounds or "sl_pct" not in rail_bounds:
        raise ValueError("rail_bounds: MEMECOIN_DESK_RAIL_BOUNDS must parse with an sl_pct range")
    if venue not in ("pumpfun", "pumpswap"):
        raise ValueError("venue")
    if ps_inputs and (venue != "pumpswap" or intake is None):
        raise ValueError("ps_inputs requires intake PumpSwap")
    ps_version = ("pumpswap-facts-v1" if ps_inputs_version is None else ps_inputs_version) if ps_inputs else None
    if ((ps_inputs_version is not None and not ps_inputs)
            or ps_version not in (None, "pumpswap-facts-v1", "pumpswap-facts-v2", "pumpswap-facts-v3")):
        raise ValueError("ps_inputs_version")
    if ps_version in ("pumpswap-facts-v2", "pumpswap-facts-v3"):
        import re
        if not isinstance(judge_playbook_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", judge_playbook_sha256):
            raise ValueError("judge_playbook_sha256")
    elif judge_playbook_sha256 is not None:
        raise ValueError("judge_playbook_sha256 requires ps_inputs v2 or v3")
    if d_definitions is not None and ps_version != 'pumpswap-facts-v3':
        raise ValueError('ps_inputs_v3_definitions require v3')
    d_tags = {}
    if ps_version == 'pumpswap-facts-v3':
        if (not isinstance(intake, dict)
                or intake.get('require_collected') not in (['chart', 'cluster', 'dump_group', 'holders'], ['chart', 'holders'])
                or not cluster):
            raise ValueError('ps_inputs_v3 requires collected evidence and cluster')
        if (not isinstance(preregistration, dict) or set(preregistration) != {'path', 'commit'}
                or not isinstance(preregistration['path'], str) or not preregistration['path']
                or not isinstance(preregistration['commit'], str)
                or not re.fullmatch('[0-9a-f]{40}', preregistration['commit'])):
            raise ValueError('ps_inputs_v3 preregistration')
        if not isinstance(d_definitions, dict) or set(d_definitions) != set(D_COMPONENTS):
            raise ValueError('ps_inputs_v3_definitions')
        for name, tag in d_definitions.items():
            if (not isinstance(tag, dict) or set(tag) != {'version', 'definition_sha256'}
                    or not isinstance(tag['version'], str) or not re.fullmatch('[a-z][a-z0-9-]*', tag['version'])
                    or not isinstance(tag['definition_sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', tag['definition_sha256'])):
                raise ValueError('ps_inputs_v3_definitions:' + name)
            d_tags[name] = dict(tag)
    if ps_inputs:
        structure_sheet = "facts-v2"
    if trigger and venue != "pumpfun":
        raise ValueError("the trigger selection rule is cohort U on the Pump.fun curve only")
    if v23 and (not standing_orders or venue != "pumpfun"):
        raise ValueError("v23 (forecast-v1 + v23 facts) needs --standing-orders on the Pump.fun curve")
    if intake is not None and (venue != "pumpswap" or mcap_band or trigger or standing_orders or watch_list):
        # first intake slice (Alpha 2026-10-27): graduated PumpSwap coins, ENTER/SKIP recorded, no band, no orders/watch
        raise ValueError("intake: --venue pumpswap only, and never with --mcap-band-*, --trigger, --standing-orders, --watch-list")
    if ps_version == 'pumpswap-facts-v3':
        if judge_model not in (None, 'claude-opus-5-5'):
            raise ValueError('cohort_policy:d_judge_model')
        judge_model = 'claude-opus-5-5'
    if intake is not None and judge_model is None:
        judge_model = J.GPT_MODEL
    if intake is not None:
        from sniper.services.memecoin_desk_contract import intake_feeds
        if not isinstance(intake, dict):
            raise ValueError("cohort_policy:intake")
        intake = dict(intake)
        if "feeds" in intake:
            feeds = intake["feeds"]
            if not isinstance(feeds, list) or any(not isinstance(f, str) for f in feeds):
                raise ValueError("cohort_policy:intake")
            intake["feeds"] = sorted(feeds)
        intake_feeds({"intake": intake})
    policy = {
        **({"venue": "pumpswap"} if venue == "pumpswap" else {}),
        **({"ps_inputs": ps_version} if ps_inputs else {}),
        **({"judge_playbook_sha256": judge_playbook_sha256}
           if ps_version in ("pumpswap-facts-v2", "pumpswap-facts-v3") else {}),
        "clip_usd": 25, "max_open": 5, "daily_halt_usd": 200, "cumulative_kill_usd": -200,
        "enter_ttl_max_s": 30, "boundary_max_s": 120, "wait_max_s": 300,
        "judge_p_admit": judge_p_admit, "judge_defer_max_s": judge_defer_max_s(), "evidence_wait_s": 60, "model_billing_basis": "subscription_flat",
        "hard_filter": {"version": HARD_FILTER_VERSION, "round_trip_loss_pct_max": round_trip_loss_pct_max,
                        "transfer_hook_allowlist": sorted(hook_allowlist),
                        # the admission band (contract.mcap_band); absent = the original 30k-100k, byte-identical
                        **({"mcap_band": [float(mcap_band[0]), float(mcap_band[1])]} if mcap_band else {}),
                        **({"mcap_band_claim_margin_pct": float(claim_margin_pct)} if claim_margin_pct is not None else {}),
                        **({"judge_moved_max_pct": float(moved_max_pct)} if moved_max_pct is not None else {}),
                        # intake-v1 (operator 2026-10-27): no band; market cap only a safety bound; liquidity floor in SOL
                        **({"intake": intake} if intake is not None else {}),
                        **({"intake_source_version": "desk-intake-source-v1"} if ps_inputs else {})},
        # watch v22 (operator 2026-10-23): JUDGE may WAIT on a price trigger that code fires; part of the cohort identity
        # px_hold shape from Gamma's pullback discovery read (2026-10-23): <= 2 x range5 deep, 5 s hold, 60 s max wait
        **({"standing_orders": {"version": "standing-v2" if standing_v2 else "watch-v1",
                                **({"order_max_s": 7200, "book_max": 50} if standing_v2 else {}), "watch_max_s": 1800, "graduation_guard_pct": graduation_guard_pct,
                                "dead_tape_noise_k": 3.0, "px_hold_max_noise": 2.0, "px_hold_hold_s": 5,
                                "px_hold_max_wait_s": px_hold_max_wait_s}} if standing_orders else {}),
        "rail_bounds": {k: [v[0], v[1]] for k, v in sorted(rail_bounds.items())},
        "ranker_version": R.INLINE_RANKER_VERSION if trigger else R.RANKER_VERSION,
        "select_version": J.TRIGGER_SELECT_VERSION if trigger else J.SELECT_VERSION,
        "label_version": L.LABEL_VERSION, "sr_post_p": R.SR_POST_P, "sr_all_p": R.SR_ALL_P,
        "crawler_version": crawler_version,
        # hf4: the market-structure block on the JUDGE sheet; its version, windows and bar width are part of the cohort identity
        **({"structure": {"version": STRUCTURE_VERSION, "windows_s": list(STRUCTURE_WINDOWS_S), "bar_s": 5, "chart_s": 120,
                          "read_margin_s": 2.0, "agents": sorted(STRUCTURE_AGENTS),
                          # facts-v2 (Lambda, structure-v2): the sheet the v25 playbook describes; absent = v1
                          **({"sheet": structure_sheet} if structure_sheet else {})},
            # the sell probe prices the fee from NON-agent trades (the Mayhem agent is fee-free): part of the cohort identity
            "sell_probe": {"fee_basis": "non_agent_trades", "agree_min": FEE_AGREE_MIN, "of_newest": FEE_OF_NEWEST,
                           "window": REGULAR_FEE_TRADES},
            # as-of collector facts on the sheet (operator 2026-10-22): their version and freshness are cohort identity
            "context": {"version": CONTEXT_VERSION, "board_fresh_s": BOARD_FRESH_S, "theme_fresh_s": THEME_FRESH_S,
                        "theme_window_s": THEME_WINDOW_S, **({"pumpswap": True} if ps_inputs else {})}}
           if venue == "pumpfun" or ps_inputs else {}),
        # the cluster fact's definition is part of the cohort (Gamma): the desk funder parser version and its signature page cap
        # the cluster parser contract the JUDGE sheet's soft facts read (Delta 10-26: without it the sheet shows cluster
        # unknown, cluster_parser_mismatch); --trigger implies it, --cluster freezes it alone
        **({"cluster": CLUSTER_DEFINITION()} if trigger or cluster else {}),
        # winner re-entry (operator 2026-10-23): the store reads exactly {"version", "per_hour", "window_s"}; absent = off
        **({"winner_reentry": winner_reentry} if winner_reentry else {}),
        # v23 (operator 2026-10-23): forecast-v1 "f" on every answer (record only) and the qualify / reentry sheet facts
        **({"forecast": "forecast-v1", "facts": "v23-facts-v1"} if v23 else {}),
        # v23 answer budget: the worker's MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS must equal it (absent = no cap)
        **({"judge_max_output_tokens": max_output_tokens} if max_output_tokens is not None else {}),
        # fill-drift re-judge: a drift refusal opens ONE fresh JUDGE ticket (needs Lambda's drift gate in the executor)
        **({"drift_requeue": "drift-requeue-v1"} if drift_requeue else {}),
        # entry-gate-v1 (Alpha GO 2026-10-26): every E-now becomes a px_hold WAIT at the frozen shape (no instant buy)
        **({"entry_gate": "entry-gate-v1"} if entry_gate_wait else {}),
        # watch-list-v1 (operator 10-26, Gamma prereg 6c9b1f37f): H = WATCH re-looked in arm W (bit 2), final in arm C
        **({"watch_list": {"version": "watch-list-v1", "max_looks": 6, "life_s": 900, "cadence_s": [60, 180, 300, 600],
                           "relook_seats_per_h": 20}} if watch_list else {}),
        # chase-limit-v1 (Alpha 10-26 option 3): a skipped_chase ENTER in arm B -> 2 h standing limit at the verdict price
        **({"chase_limit": {"version": "chase-limit-v1", "order_max_s": 7200, "book_max": 50,
                            "graduation_guard_pct": graduation_guard_pct}} if chase_limit else {}),
        # the JUDGE model is cohort identity (operator 2026-10-23); absent = claude-haiku-5-5, byte-identical
        **({"judge_model": judge_model} if judge_model else {}),
        **({"judge_provider": "codex_cli", "judge_effort": "low"} if judge_model == J.GPT_MODEL else {}),
        # record-only exit observer (CORTEX-root; Gamma: all four k logged, the population is a per-record label)
        **({"exit_observer": {"version": exit_observer, "grid": [2, 3, 4, 6], "baseline_drop_pct": 10.0}}
           if exit_observer else {}),
        # the verdict-price tolerance the cap uses (MEMECOIN_DESK_VERDICT_TOLERANCE_PCT): cohort identity, so a change is a new cohort
        **({"verdict_tolerance_pct": verdict_tolerance_pct} if verdict_tolerance_pct is not None else {}),
        **({"ranker_version": "pumpswap-features-v1", "select_version": "pumpswap-unregistered",
            "label_version": "pumpswap-unregistered", "sr_post_p": 0, "sr_all_p": 0,
            "judge_p_admit": 0, "admission_status": "unregistered"} if venue == "pumpswap" and intake is None else {}),
        # intake: no ranker; every PASS ticket competes for the JUDGE seat at the frozen judge_p_admit (paper, Alpha)
        **({"ranker_version": "none", "select_version": "intake-v1", "label_version": "intake-v1",
            "sr_post_p": 0, "sr_all_p": 0, "admission_status": "paper",
            # The intake seat remains 40/h; the new GPT model/provider/LOW identity is frozen above.
            "judge_calls_per_hour": 40, "judge_model": judge_model or "claude-opus-5-5",
            # Gamma Amendment 2 (19bdbce4a): the outcome-blind seat order (age-out = judge_defer_max_s, frozen above)
            "judge_order": J.INTAKE_ORDER} if intake is not None else {}),
        **({"preregistration": preregistration} if preregistration is not None else {}),
    }
    if d_tags:
        policy.update({name: tag for name, tag in d_tags.items() if name != 'ps_trending'})
        policy['context']['ps_trending'] = d_tags['ps_trending']
    return policy


def _load_d_definitions():
    from sniper.services.memecoin_desk_contract import d_components
    from sniper.services.memecoin_desk_ps_trending import definition
    return {**d_components(), 'ps_trending': definition()}


def _verified_prereg(spec):
    """{"path", "commit"} for '<path>@<commit>' when that file exists at that commit in this repo (full sha), else None."""
    import subprocess
    if not isinstance(spec, str) or "@" not in spec:
        return None
    path, ref = spec.rsplit("@", 1)
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", ref + "^{commit}"], capture_output=True,
                             text=True, timeout=10, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", "%s:%s" % (sha, path)], capture_output=True,
                       timeout=10, check=True)
    except (subprocess.SubprocessError, OSError):
        return None
    return {"path": path, "commit": sha}


def _crawler_version():
    """The CRAWLER changes the JUDGE's inputs (crawl fields go from always-NULL to sometimes-set), so it
    is live before cohort U is provisioned and its version is frozen in the policy, or turning it on
    later is a new cohort (Gamma). Until Lambda's seat lands there is no version, and provisioning
    refuses rather than freeze a cohort without one."""
    try:
        from sniper.services.memecoin_desk_crawler import CRAWLER_VERSION
    except ImportError:
        raise ValueError("crawler_version: sniper.services.memecoin_desk_crawler.CRAWLER_VERSION does not exist "
                         "yet; provision after the CRAWLER is live") from None
    if not isinstance(CRAWLER_VERSION, str) or not CRAWLER_VERSION:
        raise ValueError("crawler_version: CRAWLER_VERSION must be a non-empty string")
    return CRAWLER_VERSION


def cohort_id(policy, *, producer="U", prompt_version=PROMPT_VERSION):
    """The id register_desk_cohort derives (db_memecoin.register_desk_cohort), without a write."""
    from sniper.services.memecoin_desk_contract import cohort_policy_hash
    return "desk-cohort:%s:%s" % (producer, cohort_policy_hash(policy, producer=producer, prompt_version=prompt_version))


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--judge-p-admit", type=float, required=True,
                   help="share of shortlisted PASS tickets the JUDGE samples (Gamma amendment 5)")
    p.add_argument("--top10-pct-max", type=float, default=None, help="REMOVED in hard filter hf2; refused if given")
    p.add_argument("--creator-pct-max", type=float, default=None, help="REMOVED in hard filter hf2; refused if given")
    p.add_argument("--round-trip-loss-pct-max", type=float, required=True)
    p.add_argument("--hook-allow", action="append", default=[], help="a transfer-hook program to allow")
    p.add_argument("--venue", choices=("pumpfun", "pumpswap"), default="pumpfun")
    p.add_argument("--trigger", action="store_true",
                   help="the trigger selection rule (pre-registration amendment 11: ranker_version u-inline-v1, select_version "
                        "desk-judge-select-v2): a new cohort, Pump.fun curve only")
    p.add_argument("--desk-db", default=None, help="default: <state root>/data/memecoin_desk.db")
    p.add_argument("--ledger-db", default=None, help="default: the funded ledger DB_PATH")
    p.add_argument("--expected-cohort-id", default=None, help="refuse policy/config drift before either registry write")
    p.add_argument("--playbook", default=str(PLAYBOOK),
                   help="the JUDGE playbook this cohort runs; its prompt_version line names the cohort's version")
    p.add_argument("--winner-reentry-per-hour", type=int, default=None,
                   help="freeze winner re-entry (winner-reentry-v1) with this rolling-hour admission cap; absent = off")
    p.add_argument("--winner-reentry-window-s", type=float, default=1800.0)
    p.add_argument("--winner-reentry-loss-soft", action="store_true",
                   help="winner_reentry v2: a desk_risk_exit / real_trail loss also re-arms on a reclaim of our entry "
                        "fill with net buy flow; the loss re-entry tripwire refuses it once tripped")
    p.add_argument("--mcap-band-lo-usd", type=float, default=None,
                   help="freeze an admission band [lo, --mcap-band-hi-usd] in hard_filter.mcap_band; the producer env "
                        "MEMECOIN_DESK_UNIVERSE_BAND_LO_USD must match at cutover")
    p.add_argument("--mcap-band-hi-usd", type=float, default=100000.0)
    p.add_argument("--standing-orders", action="store_true",
                   help="freeze policy.standing_orders watch-v1 (watch_max_s 1800, graduation_guard_pct 90, "
                        "dead_tape_noise_k 3): JUDGE WAIT price triggers fired by code; needs playbook v22")
    p.add_argument("--v23", action="store_true",
                   help="freeze policy.forecast forecast-v1 and policy.facts v23-facts-v1 (needs --standing-orders and "
                        "playbook v23); with --winner-reentry-per-hour it freezes winner-reentry-v3 (re-entry as a WATCH; "
                        "the universe worker needs MEMECOIN_DESK_REENTRY_WATCH=1 at the cutover)")
    p.add_argument("--judge-model", default=None, choices=("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-luna"),
                   help="freeze policy.judge_model (new intake defaults to GPT; non-intake absent = claude-haiku-5-5); historical Opus needs MEMECOIN_DESK_JUDGE_CLI_PATH "
                        "to a CLI that runs it (the SDK-bundled 2.1.209 cannot)")
    p.add_argument("--exit-observer", default=None, metavar="VERSION",
                   help="freeze policy.exit_observer {version VERSION (exit-observer-vN), grid [2,3,4,6], baseline_drop_pct 10}")
    p.add_argument("--structure-sheet", default=None, choices=("facts-v2",),
                   help="freeze policy.structure.sheet (Lambda's structure-v2 facts sheet; the v25 playbook describes it)")
    p.add_argument("--watch-list", action="store_true",
                   help="freeze policy.watch_list watch-list-v1 (6 looks, 900 s life, cadence 60/180/300/600 s, 20 re-looks/h); "
                        "needs a playbook with the H answer (v25), no --standing-orders")
    p.add_argument("--cluster", action="store_true",
                   help="freeze policy.cluster (the current cluster parser contract) without --trigger, so the JUDGE sheet "
                        "carries cluster facts (Delta 10-26: 61eee222 had none)")
    p.add_argument("--chase-limit", action="store_true",
                   help="freeze policy.chase_limit chase-limit-v1 (works WITHOUT --standing-orders, prompt unchanged): an ENTER "
                        "refused skipped_chase in arm B (bit 1) becomes a 7200 s standing limit at the verdict price, book 50, "
                        "guard = --graduation-guard-pct (Gamma prereg b7d133fc8)")
    p.add_argument("--standing-v2", action="store_true",
                   help="freeze standing_orders version standing-v2 (needs --standing-orders): arm B (bit 1 of sha256(ticket_id)) "
                        "turns a JUDGE px_hold WAIT into one standing limit order, life 7200 s, book bound 50 (Gamma prereg 7e0e59d41)")
    p.add_argument("--graduation-guard-pct", type=float, default=90.0,
                   help="freeze standing_orders.graduation_guard_pct (50..100; operator 10-26: 97 for v23, every fire at the "
                        "90 guard was refused because coins pass 90%% curve near 26k mcap)")
    p.add_argument("--px-hold-max-wait-s", type=int, default=60,
                   help="freeze standing_orders.px_hold_max_wait_s (10..600; operator 10-26: 600 for the v23 cutover, all "
                        "60 s px_hold WAITs expired untouched)")
    p.add_argument("--entry-gate-wait", action="store_true",
                   help="freeze policy.entry_gate entry-gate-v1: every JUDGE E-now becomes a px_hold WAIT at the frozen shape "
                        "(needs --standing-orders); kill knob MEMECOIN_DESK_ENTRY_GATE=0 restores enter-now")
    p.add_argument("--drift-requeue", action="store_true",
                   help="freeze policy.drift_requeue drift-requeue-v1: a fill-drift refusal opens one fresh JUDGE ticket")
    p.add_argument("--freeze-answer-budget", action="store_true",
                   help="freeze MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS (as set in the env) into policy.judge_max_output_tokens; "
                        "the JUDGE worker refuses a cohort whose frozen value differs from its own")
    p.add_argument("--judge-moved-max-pct", type=float, default=None,
                   help="freeze hard_filter.judge_moved_max_pct: a ticket whose claim-time cap moved more than this %% "
                        "from its emission (either way) is a no-call moved_since_emission; absent = off")
    p.add_argument("--mcap-band-claim-margin-pct", type=float, default=None,
                   help="freeze hard_filter.mcap_band_claim_margin_pct: at the JUDGE claim the floor is lowered by this "
                        "%% (the emission-time crossing stays the proof); absent = no margin")
    p.add_argument("--freeze-verdict-tolerance", action="store_true",
                   help="freeze MEMECOIN_DESK_VERDICT_TOLERANCE_PCT (as set in the env) into the policy")
    p.add_argument("--intake", action="store_true",
                   help="intake-v1 cohort (graduated coins from the intake feeds, no band): needs --venue pumpswap")
    p.add_argument("--ps-inputs", action="store_true", help="versioned PumpSwap facts and source-integrity cohort")
    p.add_argument("--intake-feeds", nargs="+", choices=("pumpfun", "dexscreener", "fomo"),
                   help="freeze enrolled feeds; omitted preserves legacy Pump.fun-only policy")
    p.add_argument("--intake-prereg", default=None, metavar="PATH@COMMIT",
                   help="the frozen pre-registration this intake cohort binds, e.g. "
                        "docs/audits/2026-10-27-feed-intake-base-rate-preregistration.md@61de43949")
    p.add_argument("--intake-require-collected", action="store_true",
                   help="order B (2026-10-28): refuse an intake ticket whose holders, cluster or dump_group fact is still "
                        "unknown after evidence_wait_s; freezes the cluster definition too (implies --cluster)")
    p.add_argument("--intake-require-collected-core", action="store_true",
                   help="2026-10-29: like --intake-require-collected but only chart and holders are required; an unknown "
                        "dump_group or cluster stays a fact for the JUDGE (implies --cluster)")
    p.add_argument("--intake-mcap-min-usd", type=float, default=5000.0)
    p.add_argument("--intake-mcap-max-usd", type=float, default=50_000_000.0)
    p.add_argument("--intake-liq-min-quote-sol", type=float, default=40.0,
                   help="real SOL in the pool vault (operator 2026-10-27: 40 SOL, not a USD floor)")
    p.add_argument("--confirm", action="store_true", help="write the cohort (the operator only)")
    a = p.parse_args(argv)
    if a.intake_feeds is not None and not a.intake:
        p.error("--intake-feeds requires --intake")
    if a.intake_require_collected and a.intake_require_collected_core:
        p.error("--intake-require-collected and --intake-require-collected-core are exclusive")
    a.intake_require_collected_any = a.intake_require_collected or a.intake_require_collected_core
    if a.intake_require_collected_any and not a.intake:
        p.error("--intake-require-collected requires --intake")
    if a.intake_require_collected_any:
        a.cluster = True        # the cluster rows the assembler binds must match a frozen definition
    if a.ps_inputs and (not a.intake or a.venue != "pumpswap"):
        p.error("--ps-inputs requires --intake --venue pumpswap")
    prereg = None
    if a.intake:
        # Alpha 2026-10-27: an intake cohort binds its frozen pre-registration (doc path + commit, verified in git)
        prereg = _verified_prereg(a.intake_prereg)
        if prereg is None:
            p.error("--intake needs --intake-prereg <path>@<commit> naming a pre-registration that exists in git")
    if a.confirm and a.venue == "pumpswap" and prereg is None:
        p.error("PumpSwap admission is unregistered; preview only until its own pre-registration is frozen")
    if a.top10_pct_max is not None or a.creator_pct_max is not None:
        print("refused: --top10-pct-max / --creator-pct-max were removed in hard filter hf2 (they are JUDGE facts now)")
        return 2
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root, state_path
    load_env_files(str(state_root()))
    import re
    from sniper.settings.memecoin import parse_desk_rail_bounds
    text = Path(a.playbook).read_text()
    found = re.search(r"prompt_version: `([^`]+)`", text)
    if not found or not re.fullmatch(r"desk-judge-v\d+", found.group(1)):
        print("refused: playbook %s has no desk-judge-vNN prompt_version" % a.playbook)
        return 2
    prompt_version = found.group(1)
    partial_inputs = a.ps_inputs and prompt_version == "desk-judge-v28"
    d_inputs = a.ps_inputs and prompt_version == 'desk-judge-v29'
    if prompt_version == 'desk-judge-v29' and not a.ps_inputs:
        p.error('v29 playbook requires --ps-inputs')
    playbook_sha256 = None
    if partial_inputs or d_inputs:
        parts = text.split("\n---\n", 1)
        if len(parts) != 2:
            p.error('%s playbook requires the system-prompt delimiter' % prompt_version)
        playbook_sha256 = hashlib.sha256(parts[1].encode()).hexdigest()
    policy = frozen_policy(judge_p_admit=a.judge_p_admit,
                           round_trip_loss_pct_max=a.round_trip_loss_pct_max, hook_allowlist=a.hook_allow,
                           rail_bounds=parse_desk_rail_bounds(os.getenv("MEMECOIN_DESK_RAIL_BOUNDS", "")), venue=a.venue,
                           trigger=a.trigger,
                           winner_reentry=(({"version": "winner-reentry-v3", "per_hour": a.winner_reentry_per_hour,
                                             "window_s": a.winner_reentry_window_s, "loss_soft": True}
                                            if a.v23 else
                                            {"version": "winner-reentry-v2", "per_hour": a.winner_reentry_per_hour,
                                             "window_s": a.winner_reentry_window_s, "loss_soft": True}
                                            if a.winner_reentry_loss_soft else
                                            {"version": "winner-reentry-v1", "per_hour": a.winner_reentry_per_hour,
                                             "window_s": a.winner_reentry_window_s}) if a.winner_reentry_per_hour else None),
                           verdict_tolerance_pct=(float(os.getenv("MEMECOIN_DESK_VERDICT_TOLERANCE_PCT", "3"))
                                                  if a.freeze_verdict_tolerance else None),
                           mcap_band=((a.mcap_band_lo_usd, a.mcap_band_hi_usd) if a.mcap_band_lo_usd is not None else None),
                           claim_margin_pct=a.mcap_band_claim_margin_pct, moved_max_pct=a.judge_moved_max_pct,
                           standing_orders=a.standing_orders, v23=a.v23,
                           max_output_tokens=_budget_for_cohort(a), drift_requeue=a.drift_requeue,
                           judge_model=a.judge_model, exit_observer=a.exit_observer,
                           entry_gate_wait=a.entry_gate_wait, px_hold_max_wait_s=a.px_hold_max_wait_s,
                           graduation_guard_pct=a.graduation_guard_pct, standing_v2=a.standing_v2,
                           chase_limit=a.chase_limit, cluster=a.cluster,
                           watch_list=a.watch_list, structure_sheet=a.structure_sheet,
                           intake=({"version": "intake-v1", "mcap_safety": [a.intake_mcap_min_usd, a.intake_mcap_max_usd],
                                    "liq_min_quote_sol": a.intake_liq_min_quote_sol,
                                    **({"feeds": a.intake_feeds} if a.intake_feeds is not None else {}),
                                    **({"require_collected": ["chart", "cluster", "dump_group", "holders"]}
                                       if a.intake_require_collected else {}),
                                    **({"require_collected": ["chart", "holders"]}
                                       if a.intake_require_collected_core else {})} if a.intake else None),
                           preregistration=prereg, ps_inputs=a.ps_inputs,
                           ps_inputs_version=('pumpswap-facts-v3' if d_inputs else
                                              'pumpswap-facts-v2' if partial_inputs else None),
                           judge_playbook_sha256=playbook_sha256,
                           **({'d_definitions': _load_d_definitions()} if d_inputs else {}))
    from sniper.services.memecoin_desk_contract import validate_ps_inputs
    validate_ps_inputs(policy, prompt_version)
    if a.standing_orders:
        from sniper.services.memecoin_desk_contract import validate_standing
        validate_standing(policy["standing_orders"])        # 10 <= px_hold_max_wait_s <= 600 etc.: refuse before printing
    cid = cohort_id(policy, prompt_version=prompt_version)
    if a.expected_cohort_id is not None and cid != a.expected_cohort_id:
        print("refused: expected cohort %s, computed %s" % (a.expected_cohort_id, cid))
        return 2
    print(json.dumps({"producer": "U", "prompt_version": prompt_version, "policy": policy}, indent=2,
                     sort_keys=True))
    print("MEMECOIN_DESK_U_COHORT_ID=%s" % cid)
    if not a.confirm:
        print("dry run: nothing written (add --confirm to provision)")
        return 0
    from sniper.persistence import db_memecoin_desk as store
    from sniper.persistence.db_core import get_connection
    from sniper.services import memecoin_desk as desk
    desk_conn = store.connect(a.desk_db or str(state_path("data", "memecoin_desk.db")))
    if store.get_cohort(desk_conn, cid) is not None:
        # the id is the policy's hash, so an existing row IS this policy; a second provision would
        # only differ in created_at, which the desk store refuses as a conflict
        desk_conn.close()
        print("already provisioned %s" % cid)
        return 0
    ledger_conn = get_connection(a.ledger_db) if a.ledger_db else get_connection()
    try:
        got = desk.provision_cohort(desk_conn, ledger_conn, producer="U", policy=policy,
                                    prompt_version=prompt_version,
                                    created_at=datetime.now(timezone.utc).isoformat())
    finally:
        ledger_conn.close()
        desk_conn.close()
    if got != cid:
        print("refused: provisioned %s, expected %s" % (got, cid))
        return 3
    print("provisioned %s" % got)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
