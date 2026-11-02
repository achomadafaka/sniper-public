#!/usr/bin/env python3
"""Grade the Nozomi slot-lane A/B per Gamma's pre-registration
(docs/audits/2026-10-09-nozomi-slot-lane-ab-preregistration.md, f7760ad8b).
The hash-assigned fee-split cohort includes ordinary Solana entry lanes.

Ledger only, read-only, no network. Default mode prints the population and the early-stop checks,
never a landing outcome. `--read` adds the provisional ledger landing table (slot_resend.landing_slot);
the pre-registered read re-checks every landing on chain before a verdict.

    python3 scripts/research/nozomi_ab_grade.py [--since 2026-10-09T12:29:03+00:00] [--read]
    python3 scripts/research/nozomi_ab_grade.py --selftest
"""
import argparse
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys

ACTIVATION = "2026-10-09T12:29:03+00:00"      # Alpha: release de5e17cd5, runner pid 416889
# The original three (f7760ad8b); amendment 1 widens the list to whatever .env names (Alpha sets it).
WALLETS = {"fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb": "Degen",
           "ff03cead-db17-5f20-b8aa-b2cae7e15d6a": "Resell",
           "aefe2ddd-c580-5245-a2f5-e4ed62f7ef10": "Frank"}
SPEND_CAP_LAMPORTS = 80_000_000      # amendment 1: 0.08 SOL (16 landed tips) before the read


def env_wallets(db):
    """Configured scope, or historical Solana attempt UIDs for '*'; original three when absent."""
    ids = []
    try:
        for line in open(".env"):
            if line.startswith("MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS="):
                ids = [w.strip() for w in line.split("=", 1)[1].strip().strip("'\"").split(",") if w.strip()]
    except OSError:
        pass
    if "*" in ids:
        ids = [row[0] for row in db.execute(
            "SELECT DISTINCT source_user_id FROM memecoin_entry_attempts "
            "WHERE network_id = 'solana' AND source_user_id IS NOT NULL "
            "ORDER BY source_user_id") if str(row[0] or "").strip()]
        if not ids:
            return {}
    elif not ids:
        return dict(WALLETS)
    names = dict(db.execute("SELECT source_user_id, handle FROM memecoin_wallets WHERE source_user_id IN (%s)"
                            % ",".join("?" * len(ids)), ids).fetchall())
    return {w: names.get(w) or w[:8] for w in ids}
NOZOMI_TIP = 5_000_000
ALL_B_RULE = "nozomi-all-b-v1"      # MEMECOIN_NOZOMI_ALL_B (amendment 2, before/after read)
AB_RULE = "nozomi-sha256-attempt-odd-v1"
CAPPED_RULE = "nozomi-capped-bid-v1"
FEE_SPLIT_RULE = "nozomi-fee-split-v1"
TREATMENT_TIP = 6_000_000
TREATMENT_PRIORITY_CAP = 520_000
MIN_NOZOMI_TIP = 1_000_000
COMBINED_FEE_CAP = 6_520_000
SENDER_TIP = 5_000


def arm_of(attempt_id):
    d = hashlib.sha256(str(attempt_id).encode("utf-8")).digest()
    return "B" if int.from_bytes(d[:4], "big") % 2 == 1 else "A"


def fee_assignment(attempt_id):
    digest = hashlib.sha256((FEE_SPLIT_RULE + ":" + str(attempt_id)).encode()).digest()
    return "T" if int.from_bytes(digest[:4], "big") % 2 else "C", digest[:4].hex()


def _nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _source_origin(trace, source_user_id):
    source = trace.get("source_evidence") or {}
    initiation = source.get("source_initiation") or {}
    # Opaque feed IDs cannot supply an exact origin. Conflicting canonical identities censor it.
    from solders.signature import Signature
    signatures = set()
    for value in (source.get("source_sig"), source.get("signature"), initiation.get("tx_hash")):
        if isinstance(value, str):
            try:
                if str(Signature.from_string(value)) == value:
                    signatures.add(value)
            except ValueError:
                pass
    sig = next(iter(signatures)) if len(signatures) == 1 else None
    mint = source.get("mint") or initiation.get("mint")
    if all(isinstance(v, str) and v.strip() for v in (source_user_id, sig, mint)):
        return source_user_id, sig, mint
    return None


def _execution_outcome(trace, subs, tx_sig, source_slot, landing_slot):
    """Receipt-derived atoms qualify; a booked fill or same-slot failure alone never does."""
    terminal = trace.get("terminal_status")
    if terminal == "failed" and type(landing_slot) is int:
        return "chain_failed", False
    evidence = (subs.get("receipt_execution_evidence") or {}).get("evidence") or {}
    source = trace.get("source_evidence") or {}
    atoms = [evidence.get(k) for k in ("acquired_atomic", "payment_atomic")]
    slot = evidence.get("receipt_slot")
    if (terminal == "filled" and evidence.get("state") == "verified"
            and evidence.get("tx_sig") == tx_sig and evidence.get("network_id") == "solana"
            and isinstance(source.get("mint"), str) and evidence.get("mint") == source["mint"]
            and type(slot) is int and slot >= 0
            and (landing_slot is None or slot == landing_slot)
            and type(evidence.get("acquired_decimals")) is int
            and 0 <= evidence["acquired_decimals"] <= 255
            and all(isinstance(v, str) and v.isascii() and v.isdigit() and int(v) > 0 for v in atoms)):
        return "acquired", type(source_slot) is int and slot == source_slot
    return ("acquisition_unknown" if terminal == "filled" else "execution_unknown"), False


def receipt_bin(minus_s, age_ms):
    if minus_s is None:
        return "unknown"
    if minus_s >= 1:
        return ">150"
    if age_ms is None:
        return "unknown"
    return "<=30" if age_ms <= 30 else "30-80" if age_ms <= 80 else "80-150" if age_ms <= 150 else ">150"


def pct(values, q):
    v = sorted(x for x in values if x is not None)
    return None if not v else v[min(len(v) - 1, int(q * len(v)))]


def grade_row(attempt_id, source_user_id, trace, wallets=None):
    """One signed send's facts; ordinary lanes need an identity from terminal/provider evidence."""
    subs = {}
    for s in trace.get("substage_events") or []:
        subs.setdefault(s.get("name"), s)
    send = subs.get("slot_send_call") or {}
    noz, noz_send = subs.get("nozomi_ab"), subs.get("nozomi_ab_send")
    rule = (noz or {}).get("rule")
    tx_sig = send.get("tx_sig")
    if not tx_sig and rule == FEE_SPLIT_RULE:
        fields = trace.get("fields") or {}
        tx_sig = fields.get("tx_sig") or fields.get("sig")
        if (not tx_sig and (noz_send or {}).get("nozomi_payload_sha_match") is True
                and (noz_send or {}).get("nozomi_sig_match") is True):
            tx_sig = noz_send.get("nozomi_returned_sig")
    if not isinstance(tx_sig, str) or not tx_sig.strip():
        return None
    tip = (noz or {}).get("tip_lamports")
    fallback = "nozomi_ab_fallback" in subs
    signed_tip_expected = 0 if (subs.get("nozomi_ab_fallback") or {}).get("sent") == "untipped" else tip
    sender = subs.get("sender_ab")
    rec = subs.get("slot_receipt") or {}
    arm = arm_of(attempt_id)
    fee_arm, signed_bid_state = None, None
    bid = subs.get("entry_bid_signed") or {}
    priority, signed_tip, combined = (bid.get(k) for k in
        ("priority_fee_lamports", "tip_lamports", "combined_fee_lamports"))
    problems = []
    if noz is None:
        problems.append("no_nozomi_ab_substage")
    else:
        if rule in (ALL_B_RULE, CAPPED_RULE, FEE_SPLIT_RULE):
            arm = "B"                    # amendment 2: every send is B, no control half
        if (noz.get("arm") != arm
                or rule not in (AB_RULE, ALL_B_RULE, CAPPED_RULE, FEE_SPLIT_RULE)):
            problems.append("arm_or_rule_mismatch")
        if rule == FEE_SPLIT_RULE:
            fee_arm, stamp = fee_assignment(attempt_id)
            if noz.get("fee_arm") != fee_arm or noz.get("fee_assignment_hash") != stamp:
                problems.append("FEE_assignment_mismatch")
            if (type(tip) is not int or (tip != TREATMENT_TIP if fee_arm == "T"
                                        else not MIN_NOZOMI_TIP <= tip <= NOZOMI_TIP)):
                problems.append("TIP_fee_allocation_violation")
        if (rule == CAPPED_RULE and (type(tip) is not int or not MIN_NOZOMI_TIP <= tip <= NOZOMI_TIP)
                or rule not in (CAPPED_RULE, FEE_SPLIT_RULE) and tip != (NOZOMI_TIP if arm == "B" else 0)):
            problems.append("nozomi_tip_%s" % noz.get("tip_lamports"))
        if rule in (CAPPED_RULE, FEE_SPLIT_RULE):
            if any(type(v) is not int for v in (priority, signed_tip, combined)):
                problems.append("signed_bid_unknown")
                upper_priority, upper_combined = (bid.get(k) for k in
                    ("priority_fee_upper_bound_lamports", "combined_fee_upper_bound_lamports"))
                if (rule == FEE_SPLIT_RULE and type(signed_tip) is int
                        and type(upper_priority) is int and type(upper_combined) is int):
                    if (signed_tip != signed_tip_expected or upper_priority < 0
                            or upper_combined != upper_priority + signed_tip
                            or upper_combined > COMBINED_FEE_CAP
                            or fee_arm == "T" and signed_tip and upper_priority > TREATMENT_PRIORITY_CAP):
                        problems.append("BID_signed_upper_bound_violation")
                    else:
                        signed_bid_state = "bounded_unknown"
            elif (priority < 0 or signed_tip != signed_tip_expected or combined != priority + signed_tip
                  or combined > COMBINED_FEE_CAP
                  or rule == FEE_SPLIT_RULE and fee_arm == "T" and signed_tip
                     and priority > TREATMENT_PRIORITY_CAP):
                problems.append("BID_signed_budget_violation")
            else:
                signed_bid_state = "exact"
    if arm == "A":
        if noz_send is not None:
            problems.append("nozomi_send_on_arm_A")
        if sender is None or sender.get("tip_lamports") != SENDER_TIP:
            problems.append("arm_A_not_sender_tipped")
    else:
        if sender is not None:
            problems.append("sender_arm_on_arm_B")
        if noz_send is None and not fallback:
            problems.append("no_nozomi_ab_send")
        if noz_send is not None:
            if noz_send.get("nozomi_payload_sha_match") is False:
                problems.append("BYTES_payload_sha_mismatch")
            if noz_send.get("nozomi_sig_match") is False:
                problems.append("BYTES_returned_sig_mismatch")
            if (rule in (CAPPED_RULE, FEE_SPLIT_RULE) and noz_send.get("tip_lamports") != tip
                    or rule not in (CAPPED_RULE, FEE_SPLIT_RULE)
                       and noz_send.get("tip_lamports") not in (None, NOZOMI_TIP)):
                problems.append("TIP_send_tip_%s" % noz_send.get("tip_lamports"))
            if rule == FEE_SPLIT_RULE:
                returned = noz_send.get("nozomi_returned_sig")
                if returned is not None and returned != tx_sig:
                    problems.append("BYTES_returned_identity_mismatch")
                start, finish = (noz_send.get(k) for k in
                    ("nozomi_post_start_ms", "nozomi_post_finish_ms"))
                if start is not None or finish is not None:
                    if not _nonnegative(start) or not _nonnegative(finish) or finish < start:
                        problems.append("POST_timing_invalid")
                if (noz_send.get("nozomi_state") == "accepted"
                        and (not returned or noz_send.get("nozomi_payload_sha_match") is not True
                             or noz_send.get("nozomi_sig_match") is not True
                             or start is None or finish is None)):
                    problems.append("provider_evidence_unknown")
    source = trace.get("source_evidence") or {}
    source_slot = rec.get("source_slot", source.get("source_slot"))
    landing_slot = (subs.get("slot_resend") or {}).get("landing_slot")
    if landing_slot is None:
        landing_slot = next((s.get("landing_slot") for s in reversed(trace.get("submission_events") or [])
                             if s.get("stage") == "chain_confirmation"), None)
    outcome, same_slot = _execution_outcome(trace, subs, tx_sig, source_slot, landing_slot)
    return {"attempt_id": attempt_id, "wallet": (wallets or WALLETS).get(source_user_id, source_user_id), "arm": arm,
            "rule": rule, "tip_lamports": tip, "fee_arm": fee_arm,
            "source_origin": _source_origin(trace, source_user_id), "signed_bid_state": signed_bid_state,
            "priority_fee_lamports": priority, "signed_tip_lamports": signed_tip,
            "combined_fee_lamports": combined, "execution_outcome": outcome, "same_slot_acquired": same_slot,
            "send_ms": send.get("elapsed_ms"), "tx_sig": tx_sig,
            "bin": receipt_bin(rec.get("head_at_recv_minus_s"), rec.get("head_at_recv_age_ms")),
            "source_slot": source_slot, "landing_slot": landing_slot,
            "nozomi_state": (noz_send or {}).get("nozomi_state"), "nozomi_ms": (noz_send or {}).get("nozomi_ms"),
            "fallback": fallback, "terminal": trace.get("terminal_status"),
            "reason": trace.get("terminal_reason"), "problems": problems}


def early_stops(rows):
    """The pre-registered early stops this ledger can check (1, 2, 3 trace part, 5, 6)."""
    a = [r for r in rows if r["arm"] == "A" and r["rule"] == AB_RULE]
    b = [r for r in rows if r["arm"] == "B"]
    ab_b = [r for r in b if r["rule"] == AB_RULE]
    stops = []
    if len(a) >= 5 and len(ab_b) >= 5:
        pa, pb = pct([r["send_ms"] for r in a], 0.9), pct([r["send_ms"] for r in ab_b], 0.9)
        if pa is not None and pb is not None and pb > pa + 10.0:
            stops.append("1 speed: B p90 %.1f ms > A p90 %.1f + 10" % (pb, pa))
    for r in rows:
        for p in r["problems"]:
            if p.startswith(("BYTES", "TIP", "BID", "FEE_assignment", "POST", "arm_or_rule", "nozomi_tip",
                             "nozomi_send_on", "sender_arm_on")):
                stops.append("%s: %s" % (r["attempt_id"], p))
    run = 0
    for r in b:                                   # decision order
        state = r["nozomi_state"]
        if state in (None, "pending") and not r["fallback"]:
            continue                              # unknown, neither breaks nor extends a run
        run = 0 if state == "accepted" else run + 1
        if run >= 3:
            stops.append("5 instrument: 3 consecutive arm-B sends not accepted (to %s)" % r["attempt_id"])
            break
    landed_b = [r for r in b if r["terminal"] == "filled" and not r["fallback"]]
    spent = sum(r["tip_lamports"] for r in landed_b if type(r["tip_lamports"]) is int)
    if spent > SPEND_CAP_LAMPORTS:
        stops.append("6 spend: %d landed tips > %.2f SOL" % (len(landed_b), SPEND_CAP_LAMPORTS / 1e9))
    return stops


def fee_split_population(rows):
    """Decision-ordered first submissions per exact source; retries never add independent samples."""
    first, retries, unknown, cross = {}, [], [], set()
    for row in rows:
        if row["rule"] != FEE_SPLIT_RULE:
            continue
        origin = row["source_origin"]
        if origin is None:
            unknown.append(row)
        elif origin not in first:
            first[origin] = row
        else:
            retries.append(row)
            if row["fee_arm"] != first[origin]["fee_arm"]:
                cross.add(origin)
    for row in (*first.values(), *retries):
        if row["source_origin"] in cross and "FEE_cross_arm_origin" not in row["problems"]:
            row["problems"].append("FEE_cross_arm_origin")
    return {"primary": [r for origin, r in first.items() if origin not in cross],
            "retries": retries, "unknown_origins": unknown, "cross_arm_origins": cross}


def key_hits(db, since):
    """Stop 4: where the API key value appears (counts only, never the value)."""
    key = ""
    for name in (".env",):
        try:
            for line in open(name):
                if line.startswith("MEMECOIN_NOZOMI_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
    if not key:
        return None
    # Read locally: a grep pattern in argv exposes the credential to process inventories.
    needle = key.encode()
    log_files = unreadable = 0
    for directory, _, names in os.walk("logs", followlinks=False):
        for name in names:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                continue
            try:
                with open(path, "rb") as fh:
                    tail = b""
                    while chunk := fh.read(1024 * 1024):
                        data = tail + chunk
                        if needle in data:
                            log_files += 1
                            break
                        tail = data[-(len(needle) - 1):] if len(needle) > 1 else b""
            except OSError:
                unreadable += 1
    traces = db.execute("SELECT COUNT(*) FROM memecoin_entry_attempts WHERE decision_started_at >= ? "
                        "AND instr(trace_json, ?) > 0", (since, key)).fetchone()[0]
    subs = db.execute("SELECT COUNT(*) FROM memecoin_submission_attempts WHERE created_at >= ? AND "
                      "(instr(coalesce(reason_code,''), ?) > 0 OR instr(coalesce(response_code,''), ?) > 0)",
                      (since, key, key)).fetchone()[0]
    return {"log_files": log_files, "unreadable_log_files": unreadable,
            "traces": traces, "submission_rows": subs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=ACTIVATION)
    ap.add_argument("--read", action="store_true", help="also print provisional ledger landings")
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--selftest", action="store_true")
    ns = ap.parse_args()
    if ns.selftest:
        return selftest()
    db = sqlite3.connect("file:%s?mode=ro" % ns.db, uri=True, timeout=30)
    wallets = env_wallets(db)
    q = ("SELECT attempt_id, source_user_id, trace_json, delivery_lane FROM memecoin_entry_attempts WHERE network_id = "
         "'solana' AND decision_started_at >= ? AND "
         "source_user_id IN (%s) ORDER BY decision_started_at" % ",".join("?" * len(wallets)))
    rows, unsent, ordinary_ungraded = [], 0, 0
    slot_lanes = ('slot_lane_processed', 'slot_lane_confirmed_receipt')
    for attempt_id, uid, tj, lane in db.execute(q, (ns.since, *wallets)):
        trace = json.loads(tj or "{}")
        if lane not in slot_lanes and not any(s.get('name') == 'nozomi_ab'
                and s.get('rule') == FEE_SPLIT_RULE for s in trace.get('substage_events') or []):
            continue
        r = grade_row(attempt_id, uid, trace, wallets)
        if r is None:
            if lane not in slot_lanes:
                ordinary_ungraded += 1
            else:
                unsent += 1
        else:
            r["delivery_lane"] = lane
            rows.append(r)
    graded = [r for r in rows if "no_nozomi_ab_substage" not in r["problems"]]
    print("since %s: %d in-scope attempts never sent; %d signed sends; %d without a nozomi_ab substage "
          "(out of population)" % (ns.since, unsent, len(rows), len(rows) - len(graded)))
    print("signed sends by source lane: %s" % {lane: sum(r['delivery_lane'] == lane for r in rows)
          for lane in sorted(set(slot_lanes) | {r['delivery_lane'] for r in rows}, key=str)})
    print("ordinary ungraded/missing signed proof: %d" % ordinary_ungraded)
    split = fee_split_population(graded)
    for rule in sorted({r["rule"] for r in graded}, key=str):
        cohort = [r for r in graded if r["rule"] == rule]
        print("rule %s:" % rule)
        for lane in sorted(set(slot_lanes) | {r['delivery_lane'] for r in cohort}, key=str):
            for arm in "AB":
                part = [r for r in cohort if r["delivery_lane"] == lane and r["arm"] == arm]
                print("source lane %s arm %s: n=%d; decide-to-send p50/p90 %s/%s ms" % (
                    lane, arm, len(part), pct([r["send_ms"] for r in part], 0.5),
                    pct([r["send_ms"] for r in part], 0.9)))
        for arm in "AB":
            part = [r for r in cohort if r["arm"] == arm]
            by_w = {w: sum(1 for r in part if r["wallet"] == w) for w in wallets.values()}
            bins = {}
            for r in part:
                bins[r["bin"]] = bins.get(r["bin"], 0) + 1
            print("arm %s: n=%d by wallet %s; receipt bins %s; decide-to-send p50/p90 %s/%s ms" % (
                arm, len(part), by_w, bins, pct([r["send_ms"] for r in part], 0.5),
                pct([r["send_ms"] for r in part], 0.9)))
        b = [r for r in cohort if r["arm"] == "B"]
        states = {}
        for r in b:
            states[r["nozomi_state"]] = states.get(r["nozomi_state"], 0) + 1
        print("arm B nozomi states %s; nozomi_ms p50/p90 %s/%s; fallbacks %d" % (
            states, pct([r["nozomi_ms"] for r in b], 0.5), pct([r["nozomi_ms"] for r in b], 0.9),
            sum(r["fallback"] for r in b)))
        if rule == FEE_SPLIT_RULE:
            print("fee split: retries=%d; unknown origins=%d; cross-arm origins=%d" % (
                len(split['retries']), len(split['unknown_origins']), len(split['cross_arm_origins'])))
            for fee_arm in ('C', 'T'):
                primary = [r for r in split['primary'] if r['fee_arm'] == fee_arm]
                summary = "fee %s: primary=%d; untipped fallbacks=%d; signed bid unknown=%d" % (
                    fee_arm, len(primary), sum(r['fallback'] for r in primary),
                    sum(r['signed_bid_state'] != 'exact' for r in primary))
                if ns.read:
                    summary += "; successful same-slot acquisitions=%d; chain failures=%d; acquisition/execution unknown=%d" % (
                        sum(r['same_slot_acquired'] for r in primary),
                        sum(r['execution_outcome'] == 'chain_failed' for r in primary),
                        sum(r['execution_outcome'] in ('acquisition_unknown', 'execution_unknown') for r in primary))
                print(summary)
    problems = [(r["attempt_id"], r["problems"]) for r in graded if r["problems"]]
    print("problems: %s" % (problems or "none"))
    print("key (stop 4): %s" % key_hits(db, ns.since))
    stops = early_stops(graded)
    print("EARLY STOPS: %s" % (stops or "none"))
    if ns.read:
        print("PROVISIONAL ledger landings (chain re-check before any verdict):")
        for r in graded:
            k = (None if r["landing_slot"] is None or r["source_slot"] is None
                 else r["landing_slot"] - r["source_slot"])
            print("  %s %s %s %-6s bin=%-7s k=%s %s %s%s" % (r["rule"], r["arm"], r["attempt_id"][:12], r["wallet"], r["bin"],
                k, r["terminal"], (r["reason"] or "")[:40],
                " fee=%s outcome=%s" % (r['fee_arm'], r['execution_outcome']) if r['rule'] == FEE_SPLIT_RULE else ""))
    return 1 if stops else 0


def selftest():
    assert arm_of("x") in "AB"
    d = hashlib.sha256(b"abc").digest()
    assert arm_of("abc") == ("B" if int.from_bytes(d[:4], "big") % 2 else "A")
    assert receipt_bin(0, 12) == "<=30" and receipt_bin(0, 80) == "30-80" and receipt_bin(0, 81) == "80-150"
    assert receipt_bin(1, 5) == ">150" and receipt_bin(None, 5) == "unknown" and receipt_bin(0, None) == "unknown"
    aid_b = next(i for i in ("t%d" % n for n in range(100)) if arm_of(i) == "B")
    aid_a = next(i for i in ("t%d" % n for n in range(100)) if arm_of(i) == "A")

    def tr(arm, **send):
        ev = [{"name": "slot_receipt", "source_slot": 10, "head_at_recv_minus_s": 0, "head_at_recv_age_ms": 20},
              {"name": "nozomi_ab", "arm": arm, "rule": "nozomi-sha256-attempt-odd-v1",
               "tip_lamports": NOZOMI_TIP if arm == "B" else 0},
              {"name": "slot_send_call", "elapsed_ms": 5.0, "tx_sig": "sig"}]
        ev.append({"name": "nozomi_ab_send", "tip_lamports": NOZOMI_TIP, **send} if arm == "B"
                  else {"name": "sender_ab", "tip_lamports": SENDER_TIP})
        return {"substage_events": ev, "terminal_status": "filled"}
    good_b = grade_row(aid_b, "x", tr("B", nozomi_state="accepted", nozomi_payload_sha_match=True,
                                      nozomi_sig_match=True))
    good_a = grade_row(aid_a, "x", tr("A"))
    assert good_b["problems"] == [] and good_a["problems"] == [], (good_b, good_a)
    assert early_stops([good_a, good_b]) == []
    bad = grade_row(aid_b, "x", tr("B", nozomi_state="accepted", nozomi_payload_sha_match=False))
    assert any("BYTES" in s for s in early_stops([bad]))                     # stop 2 fires
    lost = [grade_row(aid_b, "x", tr("B", nozomi_state="submitted_unknown")) for _ in range(3)]
    assert any(s.startswith("5 ") for s in early_stops(lost))                 # stop 5 fires
    pend = [grade_row(aid_b, "x", tr("B", nozomi_state="pending")) for _ in range(3)]
    assert early_stops(pend) == []                                            # pending is unknown
    wrong = grade_row(aid_a, "x", tr("B", nozomi_state="accepted"))           # recorded B, rule says A
    assert any("arm_or_rule" in s for s in early_stops([wrong]))
    slow = [dict(good_a, send_ms=5.0) for _ in range(5)] + [dict(good_b, send_ms=30.0) for _ in range(5)]
    assert any(s.startswith("1 ") for s in early_stops(slow))                 # stop 1 fires
    assert not any(s.startswith("6 ") for s in early_stops([good_b] * 16))   # 16 tips = 0.08 SOL
    assert any(s.startswith("6 ") for s in early_stops([good_b] * 17))       # stop 6 fires
    def tr_all(**send):
        t = tr("B", **send)
        t["substage_events"][1]["rule"] = ALL_B_RULE
        return t
    all_b = grade_row(aid_a, "x", tr_all(nozomi_state="accepted", nozomi_payload_sha_match=True,
                                         nozomi_sig_match=True))      # hash says A, all-B says B
    assert all_b["arm"] == "B" and all_b["problems"] == [], all_b
    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
