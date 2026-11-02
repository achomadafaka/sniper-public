#!/usr/bin/env python3
"""Desk INTAKE assembler (step 4, spec 2026-10-27-desk-intake-candidate-path-design.md section 9): the intake twin of
scripts/memecoin_desk_assembler.py, which refuses every PumpSwap emission. Per line of
data/desk/candidate_emissions_intake/<UTC day>.jsonl (the universe worker's IntakeBook):

  1. validate (PumpSwap schema + intake claim) and ingest into the intake cohort (producer U, policy hard_filter.intake);
  2. wait for PumpSwap capture from the raw receipt through the validated producer emission cut,
     at most the cohort's evidence_wait_s;
  3. ONE paced getMultipleAccounts([mint]) for the raw mint account and its context slot (no retry);
  4. sniper.services.memecoin_desk_ps_evidence.pumpswap_evidence (Delta) builds the hard-filter evidence; the
     intake-v1 hard filter decides (desk.evaluate_candidate), the JUDGE worker claims PASSed tickets as usual.

PAPER cohorts only for the first slice (Alpha 2026-10-27). One process, flock; cursor advances only after a line's
ticket is evaluated, so a crash re-reads it and the duplicate ingest resumes the open ticket.
The atomic cursor keeps additive pending_tails (old-day byte cursors): an incomplete line never
blocks newer days or disappears. Deferred day cursors remain after completion because the producer
may append a stored claim later; deleting their sources is an explicit gap, never an empty day.
Without --loop, one complete line is processed. SIGTERM preserves any unfinished offset.
    python3 scripts/memecoin_desk_intake_assembler.py --cohort-id <intake cohort> --loop
Kill knob: MEMECOIN_DESK_INTAKE_ASSEMBLER_ENABLED (default 0).
"""
from __future__ import annotations

import argparse
import fcntl
import copy
import hashlib
import json
import re
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402
import memecoin_desk_assembler as A                                  # noqa: E402

POLL_S = 0.5


class Stopped(Exception):
    """SIGTERM during the evidence wait: the ticket stays unfinished and its byte offset resumable."""


def print_at(cand):
    """Validated producer emission cut; reserve_known_at remains the raw receipt attribution."""
    from sniper.services.memecoin_desk_contract import validate_candidate
    return validate_candidate(cand)["snapshot"]["emission_at"]


def cut_basis(cand):
    """'batch_end' when the emission's cut is the end of its trigger receipt's microsecond bucket (Am36/37), else
    'processing' (a pre-Am36 emission stamped at processing time) or None when unreadable."""
    from sniper.services.memecoin_desk_intake_book import batch_end
    try:
        state = cand["snapshot"]["fields"]["pool_state"]["value"]
        return "batch_end" if cand["snapshot"]["emission_at"] == batch_end(state["reserve_known_at"]) else "processing"
    except (KeyError, TypeError, ValueError):
        return None


def read_mint(cand, rpc, *, deadline):
    """(mint_account with context_slot, completion host time) or (None, None). One paced call, no retry."""
    batch, err = rpc("getMultipleAccounts", [[cand["mint"]], {"encoding": "base64", "commitment": "confirmed"}],
                     deadline=deadline)
    done = rpc.clock()
    if err is not None or not isinstance(batch, dict):
        return None, None
    value, slot = batch.get("value"), (batch.get("context") or {}).get("slot")
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return None, None
    return {**value[0], "context_slot": slot}, done


def _unknown(reason):
    return {"status": "unknown", "reason": reason}


def coin_creator(desk_dir, cand):
    """The trigger print's on-chain coin_creator (swaps_ps), or None. Never a provider or listing field."""
    import sqlite3
    from sniper.services import memecoin_desk_ps_tape as PT
    try:
        conn = PT._ro(desk_dir)
    except PT.TapeUnreadable:
        return None
    if conn is None:
        return None
    try:
        # the trigger print binds the mint either by its mapped mint, or (intake_canonical_pda, as ps_evidence) as an
        # unmapped row on the mint's canonical PumpSwap PDA; live intake triggers are mostly the latter (2026-10-29)
        canonical = cand["pool"] == PT.canonical_pool(cand["mint"])
        rows = conn.execute("SELECT coin_creator FROM swaps_ps WHERE signature=? AND ordinal=? AND pool=? "
                            "AND (mint=? OR (? AND mint IS NULL AND map_status='unmapped')) LIMIT 2",
                            (cand["trigger_signature"], cand["trigger_ordinal"], cand["pool"], cand["mint"],
                             1 if canonical else 0)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return rows[0][0] if len(rows) == 1 and isinstance(rows[0][0], str) and rows[0][0] else None


def read_holders(cand, rpc, *, deadline, supply_atomic, creator):
    """Order B holders fact: getTokenLargestAccounts + one getMultipleAccounts of those accounts, paced, no retry.
    The PumpSwap pool (owner of the base vault) and the incinerator are excluded, as the bonding curve is on Pump.
    A timeout, 429 or malformed reply is unknown with its slug, never an empty holder set."""
    from sniper.services import memecoin_desk_safety as S
    try:
        supply = int(supply_atomic)
    except (TypeError, ValueError):
        return _unknown("supply_unknown")
    if supply <= 0:
        return _unknown("supply_unknown")
    if creator is None:
        return _unknown("creator_unknown")
    largest, err = rpc("getTokenLargestAccounts", [cand["mint"], {"commitment": "confirmed"}], deadline=deadline)
    if err is not None:
        return _unknown(err)
    rows = largest.get("value") if isinstance(largest, dict) else None
    if (not isinstance(rows, list) or not rows or len(rows) > 20
            or any(not isinstance(r, dict) or not isinstance(r.get("address"), str) for r in rows)):
        return _unknown("largest_malformed")
    accounts, err = rpc("getMultipleAccounts", [[r["address"] for r in rows], {"encoding": "base64", "commitment": "confirmed"}],
                        deadline=deadline)
    if err is not None:
        return _unknown(err)
    values = accounts.get("value") if isinstance(accounts, dict) else None
    if not isinstance(values, list) or len(values) != len(rows):
        return _unknown("accounts_malformed")
    return S._holders(cand["mint"], {"supply": supply}, rows, values, cand["pool"], creator, None, {})


def chart_check(facts):
    """Order B chart fact from ps_facts: pass only when flow_60s, both price_vs_60s edges and an age (on-chain coin,
    else Pump.fun provider, else pool creation) are all proven. Absent is a typed unknown, never a pass."""
    if not isinstance(facts, dict):
        return _unknown("facts_unavailable")
    if facts.get("flow_60s") is None:
        return _unknown("flow_60s_%s" % ((facts.get("coverage") or {}).get("60s") or {}).get("reason", "unknown"))
    if facts.get("version") == "pumpswap-facts-v3":
        # Operator 4f5ad1f2: the price path comes from OBSERVED prints when prints_n >= 2, losses counted beside, unseen
        # swaps never a veto. The v3 chart block's 60s window is the gate; the strict top-level edges are not.
        chart = facts.get("chart") or {}
        if chart.get("state") != "known":
            return _unknown("chart_%s" % (chart.get("reason") or "unknown"))
        win = (chart.get("windows") or {}).get("60s") or {}
        if win.get("null_reason") is not None or not isinstance(win.get("prints_n"), int) or win["prints_n"] < 2:
            return _unknown("chart_60s_%s" % (win.get("null_reason") or "unknown"))
    else:
        for key in ("price_vs_60s_low_pct", "price_vs_60s_high_pct"):
            if facts.get(key) is None:
                return _unknown(key + "_unproven")
    ages = facts.get("ages") or {}
    if not any((ages.get(k) or {}).get("state") == "known" for k in ("coin", "provider_reported", "pool")):
        return _unknown("age_unknown")
    return {"status": "pass", "reason": "chart_complete"}


def _at_evidence(cand, evidence):
    """facts_for expects a REFRESHED candidate ("owns FX/decimals/slot"). An initial intake emission has neither the cut
    (learned at evidence time) nor the mint decimals (the paid mint read at evidence time). Return a copy whose
    pool_state takes both from the evidence, re-hashed exactly as build_snapshot hashes it; the claim is untouched."""
    out = copy.deepcopy(cand)
    snap = out["snapshot"]
    state = snap["fields"]["pool_state"]["value"]
    if state.get("cut_slot") is None:
        state["cut_slot"] = evidence["market_cut"]["cut_slot"]
    safety = (evidence.get("checks") or {}).get("token_safety") or {}
    provenance = {}
    if state.get("base_decimals") is None and safety.get("status") == "pass" and type(safety.get("decimals")) is int:
        # Gamma 2026-10-29: a documented exception to the as-of rule for cohort D, not a precedent. SPL Mint.decimals is
        # fixed at InitializeMint (Token-2022 too) and the pool existed at the cut, so the post-cut read equals the value
        # at the cut. ONLY decimals cross: supply, authorities, extensions and every other mutable mint field stay in
        # token_safety and out of the chart; decimals only scale prices; the read time feeds nothing else.
        state["base_decimals"] = safety["decimals"]
        provenance = {"decimals_source": "post_cut_immutable"}   # its read time is the evidence envelope's known_at
    body = {k: v for k, v in snap.items() if k != "hash"}
    snap["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return out, provenance


def _facts_unavailable(exc):
    """'facts_unavailable:<class>[:<message>]' as a machine reason, so a live ticket names its cause (still unknown)."""
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    detail = re.sub(r"[^a-z0-9_.:-]+", "_", str(exc).lower())[:60].strip("_")
    return ("facts_unavailable:%s:%s" % (name, detail) if detail else "facts_unavailable:%s" % name)[:128]


def read_chart(cand, evidence, *, desk_dir, desk_conn, cohort_id, hard_policy, now, ps_version=None):
    """ps_facts at the evidence's own market cut (the JUDGE recomputes it later with our fills excluded)."""
    from sniper.persistence import db_memecoin_desk as store
    from sniper.services import memecoin_desk_ps_facts as F
    cut = evidence.get("market_cut")
    if not isinstance(cut, dict):
        return _unknown("market_cut_unknown")
    try:
        # the assembler holds the RAW emission line (no opportunity_id key); the contract's opportunity id IS the
        # emission's ticket_id (memecoin_desk_contract.validate_candidate), live KeyError 2026-10-29
        opportunity = cand.get("opportunity_id") or cand["ticket_id"]
        source = store.get_intake_source_receipt(desk_conn, opportunity, cohort_id=cohort_id,
                                                 hard_policy=hard_policy, candidate=cand, as_of=now)
        cand, provenance = _at_evidence(cand, evidence)
        facts = F.facts_for(cand, desk_dir=desk_dir, as_of=cand["snapshot"]["emission_at"],
                            receipt_cut=cut["receipt_cut"], cut_slot=cut["cut_slot"],
                            source_metadata=(source or {}).get("source_metadata"),
                            version=ps_version or F.LEGACY_VERSION,       # the cohort's ps_inputs (v3 for cohort D)
                            decision_as_of=now)   # Am22: immutable creation metadata observed by this decision
    except Exception as exc:  # TapeUnreadable, sqlite3.Error, ...: a poison ticket is unknown, never a crash loop
        return _unknown(_facts_unavailable(exc))
    out = chart_check(facts)
    provider = ((facts.get("ages") or {}).get("provider_reported") or {}) if isinstance(facts, dict) else {}
    if out["status"] == "pass" and provider.get("age_clock") == "decision":
        out = dict(out, reason=out["reason"] + "_ageclock_decision")   # Am22 stratum age_clock: decision, never pooled
    if provenance:     # the evidence chart row is closed {status, reason}: the reason carries the decimals provenance
        out = dict(out, reason=out["reason"] + "_decimals_" + provenance["decimals_source"])
    return out


def history_hook(g12, cand, checks, collected, log, required=None):
    """Stage 1b (G1 creator history + G2 coin age): ONLY when the knob is on, only for a coin whose cheap refusing gates and collected soft facts all pass, and only to write history.db and one log row.
    The result is NOT added to the hard-filter checks (no policy or cohort change). Never raises; a Stopped from the caller's loop is not swallowed here because nothing in this path can raise it."""
    if g12 is None or not g12.enabled():
        return None
    try:
        from sniper.services.memecoin_desk_g12_intake import cheap_pass
        if not (cheap_pass(checks, collected) if required is None else cheap_pass(checks, collected, tuple(required))):
            return None
        out = g12.fetch(cand["mint"])
        log({"stage": "g12_history", "ticket_id": cand.get("ticket_id"), "mint": cand["mint"], "inline_spent_today": g12.spent_today(), **out})
        return out
    except Exception as exc:                                  # any fault here is a log row, never a refused or crashed ticket
        log({"stage": "g12_history", "ticket_id": cand.get("ticket_id"), "status": "hook_error", "reason": type(exc).__name__})
        return None


def guarded(name, read):
    """One collected row; any error but Stopped is unknown:<name>:collect_error, never a worker crash."""
    try:
        return read()
    except Stopped:
        raise
    except Exception:
        return _unknown("collect_error")


def evaluate(cand, tid, *, desk_conn, desk_dir, rpc, clip_usd, hard_policy, now_fn=time.time, evidence_fn=None,
             collect=None, precheck_fn=None, on_read=None, after_decision=None):
    """One ingested intake ticket -> its hard-filter stage. Returns (verdict, reason, evidence).
    collect(evidence) -> {check name: row} replaces not-collected soft checks before the filter (order B).
    precheck_fn(cand, **pool inputs) -> refusal reason | None is the free local pool check: a refusal skips the paid
    mint read and the paid holder reads (collect). on_read() is called right before the paid mint read."""
    from sniper.services import memecoin_desk as desk
    if evidence_fn is None:
        from sniper.services.memecoin_desk_ps_evidence import pumpswap_evidence as evidence_fn
    try:
        cut = print_at(cand)
    except (ValueError, TypeError, KeyError) as exc:
        return "REFUSED", str(exc), {}
    state = cand["snapshot"]["fields"]["pool_state"]["value"]
    sol_usd = state.get("sol_usd")
    sol_at = cut - state["sol_age_s"] if state.get("sol_age_s") is not None else None
    strict = hard_policy.get("intake_source_version") == "desk-intake-source-v1"
    integrity = {"integrity": True} if strict else {}
    refused = (precheck_fn(cand, desk_dir=desk_dir, as_of_host=cut, clip_usd=clip_usd, sol_usd=sol_usd, sol_at=sol_at,
                           evaluated_at_host=now_fn(), **integrity) if precheck_fn is not None else None)
    if refused is None:
        if on_read is not None:
            on_read()
        mint_account, mint_known_at = read_mint(cand, rpc, deadline=now_fn() + rpc.timeout_s)
    else:
        mint_account = mint_known_at = None
    now = now_fn()
    evidence = evidence_fn(cand, desk_dir=desk_dir, as_of_host=cut, clip_usd=clip_usd, sol_usd=sol_usd,
                           sol_at=sol_at, mint_account=mint_account, mint_known_at=mint_known_at, evaluated_at_host=now,
                           **integrity)
    md = (evidence.get("checks") or {}).get("market_data") if isinstance(evidence, dict) else None
    if isinstance(md, dict) and md.get("status") == "pass" and cut_basis(cand) == "batch_end":
        # Am36/37 provenance in the closed {status, reason} row: the cut is the trigger receipt's microsecond bucket end
        md["reason"] = (md.get("reason") or "pass") + "_cut_batch_end"
    if refused is not None and isinstance(evidence.get("checks"), dict):
        # the pool's own refusal is the reason; the mint was never read (not a mint-clock unknown)
        evidence["checks"].update(token_safety=_unknown("mint_read_skipped:" + refused),
                                  market_data=_unknown(refused), sell_probe=_unknown(refused))
    elif collect is not None and isinstance(evidence.get("checks"), dict):
        evidence["checks"].update(collect(evidence))
    # Am22/Am25: ONE decision time, sampled after every collection finished and right before the hard filter; checks
    # that bound on it (the chart's age clock) are deferred callables resolved with exactly this value, and it is the
    # persisted hard_filter time (the paper fill anchors after it).
    decision = now_fn()
    resolved = {}
    if isinstance(evidence.get("checks"), dict):
        for name, row in list(evidence["checks"].items()):
            if callable(row):
                evidence["checks"][name] = resolved[name] = row(decision)
        if after_decision is not None and collect is not None and refused is None:
            after_decision(evidence)          # sees only resolved rows (Zeta HOLD 9c70794cd: G12 history hook)
    try:
        filtered = desk.evaluate_candidate(desk_conn, tid, evidence=evidence, policy=hard_policy, now_iso=A.iso(decision))
        return filtered["verdict"], filtered["reason"], evidence
    except ValueError as exc:
        return "REFUSED", str(exc), evidence


def committed_through_print(desk_dir, cand, now, *, integrity=False):
    from sniper.services.memecoin_desk_capture import capture_proof
    try:
        cut = print_at(cand)
        receipt = cand["snapshot"]["fields"]["pool_state"]["value"]["reserve_known_at"]
        if not 0 <= receipt <= cut <= now:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    # The frontier proves commit/continuity; the evidence reader owns the exact reserve dependency interval.
    args = {"pool": cand["pool"], "integrity": True, "window_s": 0} if integrity else {}
    proof = capture_proof(desk_dir, now, venues=("ps",), **args)
    if not (proof is not None and proof["from"] <= receipt and proof["through"] >= cut):
        return False
    if integrity:
        # The batch-end cut (Am36) makes capture commit through the cut sooner than the BLOCK for the print's slot is
        # ingested; ps_evidence then refuses pool_block_cut_unknown. Wait (inside the caller's existing deadline) until a
        # block at or after the state's slot has been received, so the block cut can exceed the state slot.
        try:
            slot = cand["snapshot"]["fields"]["pool_state"]["value"]["slot"]
        except (KeyError, TypeError):
            return False
        return block_seen(desk_dir, slot, now) is not None
    return True


def block_seen(desk_dir, slot, now):
    """Cohort D Amendment 41: the received_at of a PS block with slot > `slot` received before `now`, else None. It is
    a completeness witness that the state's slot has closed, nothing more: no price, flow, reserve or state field is
    read from it. One bounded lookup (newest block per attached day file); unreadable or absent is None."""
    from sniper.services import memecoin_desk_ps_asof as PSA
    from sniper.services import memecoin_desk_ps_tape as PT
    if type(slot) is not int or slot <= 0:
        return None
    try:
        conn = PT._ro(desk_dir)
    except PT.TapeUnreadable:
        return None
    if conn is None:
        return None
    try:
        schemas = PSA._attach_days(conn, desk_dir, now)
        rows = PSA._day_rows(conn, schemas, "blocks",
                             "SELECT slot,received_at FROM {table} WHERE received_at<? ORDER BY slot DESC LIMIT 1", (now,))
        seen = [r[1] for r in rows if type(r[0]) is int and r[0] > slot and type(r[1]) in (int, float)]
        return min(seen) if seen else None
    except Exception:
        return None
    finally:
        conn.close()


def main(argv=None, *, rpc=None, env_root=None, clock=time.time, sleep=time.sleep, stop=None):
    """One bounded pass or the signal-aware worker loop; injected dependencies never change global clocks."""
    import math
    import signal
    import threading
    from datetime import datetime
    from sniper.util.state_paths import require_state_root_outside_release
    root = require_state_root_outside_release({'SNIPER_STATE_ROOT': str(env_root)}) if env_root else require_state_root_outside_release()
    load_env_files(str(root))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--cohort-id', default=os.getenv('MEMECOIN_DESK_INTAKE_COHORT_ID'))
    p.add_argument('--desk-db', default=str(root / 'data/memecoin_desk.db'))
    p.add_argument('--loop', action='store_true')
    p.add_argument('--clip-usd', type=float, default=25.0)
    p.add_argument('--calls-per-day', type=int, default=os.getenv('MEMECOIN_DESK_INTAKE_CALLS_PER_DAY', '2000'))
    a = p.parse_args(argv)
    if os.getenv('MEMECOIN_DESK_INTAKE_ASSEMBLER_ENABLED', '0').strip() != '1':
        print('[desk-intake-assembler] MEMECOIN_DESK_INTAKE_ASSEMBLER_ENABLED is not 1; exiting')
        return 0
    if not a.cohort_id:
        p.error('--cohort-id (MEMECOIN_DESK_INTAKE_COHORT_ID) is required')
    if a.calls_per_day < 0 or not math.isfinite(a.clip_usd) or a.clip_usd <= 0:
        p.error('calls-per-day must be nonnegative and clip-usd must be positive and finite')
    from sniper.persistence import db_memecoin_desk as store
    from sniper.services import memecoin_desk as desk
    from sniper.services import memecoin_desk_contract as K
    stop = stop if stop is not None else threading.Event()
    desk_dir = root / 'data/desk'
    out_dir = desk_dir / 'assembler_intake'
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = open(out_dir / 'assembler.lock', 'a')
    desk_conn = None
    collected = False
    g12 = None
    old_term = None
    owns_signal = False

    def now():
        value = clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('host clock unknown')
        return value

    def log(row, at=None):
        at = now() if at is None else at
        with open(out_dir / (A.utc_day(at) + '.jsonl'), 'a') as fh:
            fh.write(json.dumps({'schema': 'desk-intake-assembler/v1', 'cohort_id': a.cohort_id,
                                 'ts': at, **line_context, **row}, sort_keys=True, default=str) + '\n')
            fh.flush()
            os.fsync(fh.fileno())  # the pre-dispatch charge must survive process death

    def blocked(reason):
        def unavailable(*args, **kwargs):
            return None, reason
        unavailable.clock, unavailable.timeout_s = now, 1.0
        return unavailable

    pending_tails = []
    line_context = {}

    def checkpoint():
        value = {'day': day_, 'offset': offset, 'tries': tries}
        if pending_tails:
            value['pending_tails'] = pending_tails
        tmp = cursor_path.with_suffix('.tmp')
        with tmp.open('w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, cursor_path)
        fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def valid_cursor(value):
        day, at, attempts = value['day'], value['offset'], value.get('tries', 0)
        if (datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day or day > today
                or type(at) is not int or at < 0 or type(attempts) is not int or attempts < 0):
            raise ValueError('invalid cursor fields')
        return day, at, attempts

    def read_line(day, at, *, deferred=False):
        source = emissions / (day + '.jsonl')
        if (deferred or at) and (not source.exists() or source.stat().st_size < at):
            log({'stage': 'coverage', 'verdict': 'ERROR', 'reason': 'source_missing_or_truncated',
                 'source_day': day, 'from_offset': at})
            raise ValueError('cursor source missing or truncated')
        try:
            with source.open('rb') as stream:
                stream.seek(at)
                raw = stream.readline()
                return source, raw, stream.tell()
        except FileNotFoundError:
            return source, b'', at

    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('[desk-intake-assembler] another intake assembler holds the lock; exiting')
            return 0
        desk_conn = store.connect(a.desk_db)
        cohort = store.get_cohort(desk_conn, a.cohort_id)
        if cohort is None or cohort['producer'] != 'U' or 'intake' not in (cohort['policy'].get('hard_filter') or {}):
            p.error('cohort %s is not a provisioned intake cohort (hard_filter.intake)' % a.cohort_id)
        hard_policy = cohort['policy']['hard_filter']
        wait_s = cohort['policy'].get('evidence_wait_s')
        if type(wait_s) is not int or not 1 <= wait_s <= 120:
            p.error('cohort policy evidence_wait_s missing or out of 1..120; never defaulted')
        collected = 'require_collected' in (hard_policy.get('intake') or {})
        if collected:
            from sniper.services.memecoin_desk_dump_group_cache import SnapshotCache
            from sniper.services.memecoin_desk_soft_facts import check_from_row
            dump_cache = SnapshotCache(desk_dir / 'dump_group' / 'cache.json',
                enabled=os.getenv('MEMECOIN_DESK_DUMP_GROUP_BLOCK', '1').strip() != '0').start()
            cluster_seen, clusters = {}, {}
            from sniper.services.memecoin_desk_g12_intake import Intake
            g12 = Intake(desk_dir)         # default OFF (MEMECOIN_DESK_G12_INTAKE != 1): the hook below is then a no-op
        if threading.current_thread() is threading.main_thread():
            old_term = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            owns_signal = True
        cursor_path = out_dir / 'cursor.json'
        today = A.utc_day(now())
        if cursor_path.exists():
            try:
                value = json.loads(cursor_path.read_text())
                day_, offset, tries = valid_cursor(value)
                pending_tails = value.get('pending_tails', [])
                if not isinstance(pending_tails, list):
                    raise ValueError('pending tails must be a list')
                seen = set()
                for tail in pending_tails:
                    tail_day, _, _ = valid_cursor(tail)
                    if set(tail) != {'day', 'offset', 'tries'} or tail_day >= day_ or tail_day in seen:
                        raise ValueError('pending tail ordering or identity')
                    seen.add(tail_day)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError('cursor unreadable or invalid') from exc
        else:
            day_, offset, tries = today, 0, 0
            checkpoint()
            log({'stage': 'coverage', 'verdict': 'FIRST_START', 'from_day': day_, 'from_offset': offset})
        calls_day = today
        calls_today = sum(1 for r in A.tail(out_dir / (today + '.jsonl'), 0)[0] if r.get('stage') == 'mint_read')
        emissions = desk_dir / 'candidate_emissions_intake'
        while not stop.is_set():
            today = A.utc_day(now())
            active = None
            line_context = {}
            for tail in pending_tails:
                source, line, end = read_line(tail['day'], tail['offset'], deferred=True)
                if line.endswith(b'\n'):
                    active = tail
                    break
            else:
                source, line, end = read_line(day_, offset)
            if not line.endswith(b'\n'):
                if day_ < today:
                    if line:
                        pending_tails.append({'day': day_, 'offset': offset, 'tries': tries})
                        log({'stage': 'coverage', 'verdict': 'DEFERRED', 'reason': 'partial_line_at_rollover',
                             'source_day': day_, 'from_offset': offset})
                    elif not source.exists():
                        log({'stage': 'coverage', 'verdict': 'ERROR', 'reason': 'source_missing',
                             'source_day': day_, 'from_offset': offset})
                    days = []
                    for path in emissions.iterdir() if emissions.exists() else ():
                        if not path.is_file() or path.suffix != '.jsonl':
                            continue
                        try:
                            valid_cursor({'day': path.stem, 'offset': 0})
                        except (ValueError, TypeError):
                            continue
                        if day_ < path.stem <= today:
                            days.append(path.stem)
                    days.sort()
                    day_, offset, tries = (days[0] if days else today), 0, 0
                    checkpoint()
                    continue
                if not a.loop:
                    return 0
                sleep(POLL_S)
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                cand = None
            active_day = active['day'] if active is not None else day_
            active_offset = active['offset'] if active is not None else offset
            if active is not None:
                active['tries'] += 1
                active_tries = active['tries']
            else:
                tries += 1
                active_tries = tries
            line_context = {'source_day': active_day, 'from_offset': active_offset, 'to_offset': end}
            checkpoint()
            tid = None
            try:
                if not isinstance(cand, dict) or 'intake' not in cand or cand.get('venue') != 'pumpswap':
                    raise ValueError('not_an_intake_emission')
                K.validate_candidate(cand)
                got = desk.ingest_candidate(desk_conn, cand, cohort_id=a.cohort_id, now_iso=A.iso(now()),
                                            intake_dir=desk_dir / 'intake', source_now_fn=now)
                tid = got['ticket_id']
                resumed = (not got['is_primary'] and got['reason'] == 'duplicate' and got['cohort_id'] == a.cohort_id
                           and got['state'] == 'open' and not any(s['stage'] == 'hard_filter' for s in got['stages']))
                if not got['is_primary'] and not resumed:
                    log({'stage': 'ingest', 'verdict': 'NOT_PRIMARY', 'reason': got['reason'], 'ticket_id': tid})
                    tid = None
            except ValueError as exc:
                log({'stage': 'ingest', 'verdict': 'REFUSED', 'reason': str(exc),
                     'ticket_id': cand.get('ticket_id') if isinstance(cand, dict) else None})
            if tid is not None:
                deadline = cand['snapshot']['emission_at'] + wait_s
                args = ({'integrity': True} if hard_policy.get('intake_source_version') == 'desk-intake-source-v1' else {})
                waited_from = now()
                ready = committed_through_print(desk_dir, cand, waited_from, **args)
                waited = not ready
                while not ready and now() < deadline and not stop.is_set():
                    sleep(POLL_S)
                    ready = committed_through_print(desk_dir, cand, now(), **args)
                # Am41 per-ticket record (assembler log): wait cost and the block witness time (null when none)
                try:
                    witness = block_seen(desk_dir, cand['snapshot']['fields']['pool_state']['value']['slot'], now()) if args else None
                except (KeyError, TypeError):
                    witness = None
                readiness = {'block_wait_ms': round((now() - waited_from) * 1000) if waited else None,
                             'block_witness_known_at': witness, 'ready': bool(ready)}
                if stop.is_set():
                    return 0  # unfinished ticket and byte offset remain resumable
                at = now()   # ONE sample decides the day, the cap and the charge's journal file
                if A.utc_day(at) != calls_day:
                    calls_day = A.utc_day(at)
                    calls_today = sum(1 for r in A.tail(out_dir / (calls_day + '.jsonl'), 0)[0]
                                      if r.get('stage') == 'mint_read')
                paid = False
                if active_tries > A.CRASH_TRIES:
                    log({'stage': 'mint_read_refused', 'ticket_id': tid, 'reason': 'crash_loop'})
                    evidence_rpc = blocked('crash_loop')
                elif not ready:
                    log({'stage': 'mint_read_refused', 'ticket_id': tid, 'reason': 'capture_deadline'})
                    evidence_rpc = blocked('capture_deadline')
                elif calls_today >= a.calls_per_day:
                    log({'stage': 'mint_read_refused', 'ticket_id': tid, 'reason': 'daily_call_cap'})
                    evidence_rpc = blocked('daily_call_cap')
                else:
                    if rpc is None:
                        sys.path.insert(0, str(ROOT / 'scripts/research'))
                        from slot_lane_send_monitor import pick_research_url
                        from sniper.services.memecoin_research_pacer import SharedPacer
                        url = pick_research_url(os.environ)
                        rpc = A.Rpc(url, SharedPacer.for_url(url, gap_s=A.pacer_gap()), clock=now)
                    evidence_rpc = rpc
                    paid = True
                if stop.is_set():
                    return 0
                collect = None
                if collected:
                    def collect(evidence, cand=cand, deadline=deadline, evidence_rpc=evidence_rpc):
                        # order B: holders (2 paced research reads), the bound cluster row (waited for up to the
                        # cohort's evidence_wait_s from the emission) and the dump-group cache; never on a fill path
                        safety = evidence['checks'].get('token_safety') or {}
                        out = {'holders': guarded('holders', lambda: read_holders(
                            cand, evidence_rpc, deadline=now() + evidence_rpc.timeout_s,
                            supply_atomic=safety.get('supply_atomic'), creator=coin_creator(desk_dir, cand)))}
                        def cluster():
                            row = None
                            while not stop.is_set():
                                clusters.update(A.cluster_rows(desk_dir, cluster_seen))
                                row = clusters.get(cand['ticket_id'])
                                row = row if row is not None and A.bound(row, cand) else None
                                if row is not None or now() >= deadline:
                                    break
                                sleep(POLL_S)
                            if stop.is_set():
                                raise Stopped   # a stop mid-wait never persists a refusal; the line is re-read
                            return (check_from_row(row, cohort['policy']) if row is not None
                                    else _unknown('cluster_not_ready'))
                        out['cluster'] = guarded('cluster', cluster)
                        out['dump_group'] = guarded('dump_group', lambda: dump_cache.lookup(
                            mint=cand['mint'], pool=cand['pool'], now=now()))
                        out['chart'] = lambda decision: read_chart(          # resolved at the decision (Am25)
                            cand, evidence, desk_dir=desk_dir, desk_conn=desk_conn, cohort_id=a.cohort_id,
                            hard_policy=hard_policy, now=decision, ps_version=cohort['policy'].get('ps_inputs'))
                        return out
                def after_decision(evidence, cand=cand):
                    # stage 1b: history.db only, never a check; runs after the chart is resolved at the decision
                    collected_rows = {k: evidence['checks'].get(k) for k in ('holders', 'cluster', 'dump_group', 'chart')}
                    history_hook(g12, cand, evidence['checks'], collected_rows, log,
                                 required=(hard_policy.get('intake') or {}).get('require_collected'))
                def on_read(tid=tid, at=at):
                    # logged and counted right BEFORE the paid read (a crash mid-read still spends budget)
                    nonlocal calls_today
                    log({'stage': 'mint_read', 'ticket_id': tid}, at=at)
                    calls_today += 1
                    if stop.is_set():
                        raise Stopped   # a stop after the durable charge never dispatches the read or advances
                from sniper.services.memecoin_desk_ps_evidence import pool_refusal
                try:
                    verdict, reason, evidence = evaluate(cand, tid, desk_conn=desk_conn, desk_dir=desk_dir,
                        rpc=evidence_rpc, clip_usd=a.clip_usd, hard_policy=hard_policy, now_fn=now, collect=collect,
                        precheck_fn=pool_refusal, on_read=on_read if paid else None, after_decision=after_decision)
                except Stopped:
                    return 0
                log({'stage': 'hard_filter', 'ticket_id': tid, 'verdict': verdict, 'reason': reason, 'readiness': readiness,
                     'wait_s': now() - cand['snapshot']['emission_at'],
                     'mint': cand['mint'], 'feed': cand['intake']['feed'], 'filter': cand['intake']['filter'],
                     'checks': {k: '%s:%s' % (v.get('status'), v.get('reason'))
                                for k, v in (evidence.get('checks') or {}).items() if isinstance(v, dict)}})
                if verdict not in {'PASS', 'SKIP', 'KILL'}:
                    raise ValueError('hard_filter stage not persisted: ' + reason)
            if active is not None:
                # Retain the old-day cursor: a restarted emitter can append its claim AFTER closing the damaged line.
                active['offset'], active['tries'] = end, 0
            else:
                offset, tries = end, 0
            checkpoint()
            if not a.loop:
                return 0
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if owns_signal:
            signal.signal(signal.SIGTERM, old_term)
        if desk_conn is not None:
            desk_conn.close()
        if collected:
            dump_cache.close()
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
