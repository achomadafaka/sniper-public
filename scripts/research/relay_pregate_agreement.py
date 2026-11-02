#!/usr/bin/env python3
"""Agreement check for the Relay pre-gate (spec 2026-10-09 relay deposit proof, section 5; Alpha's
condition 1): replay recorded EVM entry attempts through `memecoin_relay_pregate.decision` and require
that wherever it labels, the hook's own terminal reason has the same class, and that it labels no
admitted attempt.

Inputs come from each attempt's own record: the mcap and its basis (classification facts), the source
size and prior net (the `source_buy_floor` substage), the big-cap lift's verdict (`bigcap_lift`
substage) and the waived families (`exemptions_applied`). Only attempts that reached the mcap stage
are replayed (the hook's roster gate already passed, so `armed` holds). The configuration is TODAY's
(.env under --env-root). A floor label on an attempt whose recorded ratio (`configured_mcap_ratio`)
differs from today's ratio, resolved here with the hook's own functions, is `config_drift`, listed
with both ratios and not counted as agreement or failure.

Read-only on the ledger; no network. Exit 1 on any disagreement.

    python3 scripts/research/relay_pregate_agreement.py --since 2026-10-01 --env-root /home/bigpapi/Desktop/Sniper
    python3 scripts/research/relay_pregate_agreement.py --live --since <shadow start> --env-root ...

--live grades MEMECOIN_RELAY_PREGATE=shadow labels on watcher rows against the runner's own terminal
reason for the same origin, on proved rows only. Activation rule (Gamma M2): `ready_for_on` needs
>= 30 graded labels and none that refuse what the runner admitted.
"""
import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EVM = ("4663", "8453", "56", "1", "5042")


def rows(ledger, since):
    return ledger.execute(
        "SELECT attempt_id, network_id, source_user_id, terminal_status, terminal_reason, "
        "json_extract(classification_json,'$.facts.effective_mcap'), "
        "json_extract(classification_json,'$.facts.mcap_basis'), exemptions_applied, trace_json "
        "FROM memecoin_entry_attempts WHERE network_id IN (%s) AND decision_started_at>=? "
        "AND json_extract(classification_json,'$.facts.mcap_basis') IS NOT NULL" % ",".join("?" * len(EVM)),
        (*EVM, since)).fetchall()


def recorded_inputs(trace_json, exemptions):
    trace = json.loads(trace_json or "{}")
    subs = {s.get("name"): s for s in trace.get("substage_events", []) if isinstance(s, dict)}
    floor = subs.get("source_buy_floor") or {}
    lift = subs.get("bigcap_lift")
    lifted = (lift.get("lift_max") if lift and lift.get("decision") == "lifted" else None)
    exempt = frozenset(part.strip().rstrip("?") for part in str(exemptions or "").split(",") if part.strip())
    return dict(their_usd=floor.get("source_size_usd"), prior_net_usd=floor.get("prior_net_usd"),
                lift_fn=(lambda mc, ceiling, _v=lifted: _v), exempt=exempt), floor


def hook_ratio_today(chain, uid, mcap):
    """Today's source-buy mcap ratio for this wallet, chain and mcap, resolved with the HOOK's own
    functions here and not read back from the pre-gate: a pre-gate that resolves it wrong must show
    as a failure, never as configuration drift."""
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    ratio = max(0.0, float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO or 0.0))
    band_ratios = getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", None) or {}
    try:
        if band_ratios:
            ratio = rr._band_source_buy_ratio(band_ratios, str(chain), float(mcap), ratio)
        return rr._wallet_source_buy_ratio(str(chain), uid, ratio)
    except (TypeError, ValueError):
        return None


def agreement(records, decide):
    """records: (attempt_id, chain, uid, status, reason, mcap, basis, exemptions, trace). Returns the
    report dict; `failures` lists every disagreement."""
    from sniper.services.memecoin_relay_pregate import hook_class
    out = collections.Counter()
    failures, drift = [], []
    for aid, chain, uid, status, reason, mcap, basis, exemptions, trace in records:
        inputs, floor = recorded_inputs(trace, exemptions)
        bars = {}
        cls, text = decide(chain=chain, source_user_id=uid, mcap=mcap, mcap_basis=basis, armed=True,
                           info=bars, **inputs)
        hook = hook_class(reason) if status == "refused" else "admitted"
        out["replayed"] += 1
        if cls is None:
            out["silent:" + text.split(":")[0]] += 1
            continue
        out["labelled:%s" % cls] += 1
        then = floor.get("configured_mcap_ratio")
        now = hook_ratio_today(chain, uid, mcap) if cls == "source_buy_floor" else None
        if (cls == "source_buy_floor" and hook != cls and then is not None and now is not None
                and abs(float(then) - float(now)) > 1e-12):
            # DECIDED UNDER ANOTHER RATIO: the knob changed since; today's label describes today.
            out["config_drift:%s" % cls] += 1
            drift.append(dict(attempt_id=aid, chain=chain, ratio_then=then, ratio_now=now,
                              hook_reason=reason, status=status))
            continue
        if hook != cls:
            failures.append(dict(attempt_id=aid, chain=chain, label=cls, label_text=text, hook=hook,
                                 hook_reason=reason, status=status, mcap=mcap, basis=basis))
        else:
            out["agreed:%s" % cls] += 1
    return dict(counts=dict(sorted(out.items())), failures=failures, config_drift=drift)


PROVED = ("relay-origin-paid-v1", "relay-origin-bound-v1", "relay-deposit-processed-v1")
MIN_GRADED_FOR_ON = 30


def grade_live(records):
    """Grade shadow labels against the runner (Gamma M2's activation rule). records: (pregate dict,
    the row's proof_version, the runner's terminal status, its terminal reason). Only PROVED rows grade:
    on an unproved row the runner refused on initiation first and never reached the gates."""
    from sniper.services.memecoin_relay_pregate import hook_class
    out, admitted, other, silences = collections.Counter(), [], [], collections.Counter()
    for pregate, proof_version, status, reason in records:
        out["rows"] += 1
        cls = (pregate or {}).get("cls")
        if not cls:
            why = str((pregate or {}).get("text") or (pregate or {}).get("silent"))
            out["silent:%s" % why] += 1
            silences[why] += 1
            # A QUIET SHADOW IS NOT AN AGREEING ONE (Alpha 2026-10-09): every operator .env edit
            # silences the pre-gate until the next watcher restart, so the stale share is reported
            # on its own line beside the graded count.
            out["config_stale"] += why.startswith("config_stale") or why == "runner_start_unknown"
            continue
        if proof_version not in PROVED:
            out["unproved"] += 1
            continue
        out["graded"] += 1
        runner = hook_class(reason) if status == "refused" else "admitted"
        if runner == "admitted":
            admitted.append(dict(label=cls, runner=runner, status=status, reason=reason))
        elif runner == cls:
            out["agreed:%s" % cls] += 1
        else:
            out["other_reason"] += 1
            other.append(dict(label=cls, runner=runner, reason=reason))
    return dict(counts=dict(sorted(out.items())), label_refuses_admitted=admitted, other_reason=other,
                rows=out["rows"], graded=out["graded"], config_stale=out["config_stale"],
                silences=dict(silences.most_common()),
                config_stale_share=(out["config_stale"] / out["rows"]) if out["rows"] else None,
                ready_for_on=out["graded"] >= MIN_GRADED_FOR_ON and not admitted)


def live_rows(ledger, since):
    """(pregate, proof_version, status, reason) for every watcher row carrying a pre-gate verdict, with
    the runner's own terminal for the same origin (its latest attempt)."""
    out = []
    for signature, valuation in ledger.execute(
            "SELECT signature, source_valuation_json FROM onchain_wallet_detections WHERE detected_at>=? "
            "AND json_extract(source_valuation_json,'$.relay_pregate') IS NOT NULL", (since,)):
        val = json.loads(valuation or "{}")
        attempt = ledger.execute(
            "SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts WHERE origin_event_id=? "
            "ORDER BY decision_started_at DESC LIMIT 1", ("onchain:" + signature,)).fetchone()
        if attempt is None:
            continue
        out.append((val.get("relay_pregate"), (val.get("source_initiation") or {}).get("proof_version"),
                    attempt[0], attempt[1]))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ledger", default=None)
    p.add_argument("--env-root", default=str(REPO))
    p.add_argument("--since", required=True)
    p.add_argument("--live", action="store_true",
                   help="grade the shadow labels on watcher rows against the runner instead of replaying")
    a = p.parse_args()
    sys.path.insert(0, str(REPO))
    from sniper.util.envfiles import load_env_files
    load_env_files(a.env_root)
    from sniper.services.memecoin_relay_pregate import decision
    ledger_path = a.ledger or str(Path(a.env_root) / "sniper_tracker.db")
    ledger = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True, timeout=30)
    if a.live:
        report = grade_live(live_rows(ledger, a.since))
        json.dump(report, sys.stdout, indent=1, default=str)
        print()
        sys.exit(1 if report["label_refuses_admitted"] else 0)
    report = agreement(rows(ledger, a.since), decision)
    json.dump(report, sys.stdout, indent=1, default=str)
    print()
    sys.exit(1 if report["failures"] else 0)


if __name__ == "__main__":
    main()
