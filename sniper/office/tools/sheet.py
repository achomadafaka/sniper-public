"""The Office sheet tool (spec §3.5, plan Task 5 as amended by Beta round 1, Zeta and Alpha's clock ruling).

One tool builds the cohort D judge sheet for a coin that passed the intake hard filter, under an office-owned frozen
D policy, and stores it as a hashed receipt in office.db. Every analyst lens is a slice of that one sheet.

Clock basis: every cut a receipt compares is corrected UTC (corrected = host - offset_s, one clock sample). The PS tape's
native host cut is kept as the labelled host_receipt_cut. An unknown or non-finite offset is a typed unknown with NO
source read.

Determinism: the sheet's knowledge time is the hard-filter PASS persist time, not the call time, so the same request
returns the same receipt; the cache key binds network, mint, pool, the candidate snapshot hash, the cuts, the clock
sample and a definition bundle hash (every D component hash + the frozen policy hash + the strategy epoch).
"""
from __future__ import annotations

import hashlib
import json
import time
import math
import re

from sniper.office import safety_evidence as SE
from sniper.office import store as S
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_d_blocks as DB
from sniper.services import memecoin_desk_ps_facts as F
from sniper.services import memecoin_desk_ps_trending as T

PROMPT_VERSION = "desk-judge-v29"                # the D policy contract the office policy must satisfy


def _sha(obj) -> str:
    return hashlib.sha256(S.canonical_json(obj)).hexdigest()


def _finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def definition_bundle_hash(policy: dict, strategy_epoch: str) -> str:
    return _sha({"components": K.d_components(), "policy": _sha(policy), "strategy_epoch": strategy_epoch})


def cache_key(request: dict) -> str:
    """sha256 of the canonical REQUEST: every input the sheet depends on (Zeta 3670eddca): coin, pool, the refreshed
    and the judged snapshot hashes, the PASS cut, the market/slot cut, the sheet as-of, the evidence and source proof
    identity, the offset sample and the definition bundle (D components + policy + strategy epoch)."""
    return _sha(request)


OFFICE_SHEET_CONTRACT = {
    "version": "office-sheet-v1",
    "body": "render_judge_sheet(candidate, ps_facts, safety, safety_proof, d_blocks): the cohort D ps_inputs sheet",
    "lenses": {"ps_trending": "memecoin_desk_ps_trending.trending_for at the sheet as-of, validated"},
    "intentional_diffs_from_the_desk_judge_sheet": [
        "no ctx block: desk-context-v1 narrative/dex/heat-v1 are not exposed (a second heat definition); ps_trending is "
        "a separate lens instead of ctx.trending",
        "no soft / structure / drift blocks (desk-call-time facts, not part of the frozen D ps_inputs)",
        "sheet as-of is the candidate snapshot time, not the judge call time",
        "required / missing_required / optional_unknown added: curve-era desk fields are typed optional unknown",
    ],
}


# Frozen (Am12/Am15 frame): a graduated PumpSwap sheet can never carry the curve-era desk fields, so they are typed optional
# unknown, never "required candidate facts". `required` is what the Judge may WAIT on; `missing` stays the desk contract's
# own list, byte-equal to the worker sheet.
CURVE_ERA = ("age_s", "crawler_flags", "crawler_intent", "creator_prior_launches", "curve_pct", "price_vs_1h_high_pct",
             "quote_vs_roster_pct", "roster", "since_roster", "swaps_since_source_2s", "swaps_since_source_5s")
REQUIRED = ("chart", "flow_60s", "liq_usd", "mcap_usd", "pool_state", "safety", "verdict_price")


def with_required(body):
    miss = sorted(k for k in REQUIRED if body.get(k) is None)
    return dict(body, required=list(REQUIRED), missing_required=miss,
                optional_unknown=sorted(k for k in set(body.get("missing", [])) | set(CURVE_ERA)
                                            if k not in REQUIRED and body.get(k) is None))


def _unknown(reason, **extra):
    return {"kind": "sheet", "state": "unknown", "reason": reason, **extra}


def _row_intact(row) -> bool:
    """The stored row's canonical payload and its chain digest sha256(payload + prev_hash) re-verify."""
    try:
        rec = json.loads(row["payload_json"])
        canon = S.canonical_json(rec)
        return (isinstance(rec, dict) and canon.decode("utf-8") == row["payload_json"]
                and hashlib.sha256(canon + row["prev_hash"].encode("ascii")).hexdigest() == row["row_hash"])
    except (ValueError, TypeError, UnicodeError, AttributeError, KeyError):
        return False


# heat: Office budget 0: it hit its row cap on 39 of 40 sheets, after holding 86 MB, so the read is skipped and stays typed unknown (Alpha 2026-10-30).
TRENDING_BUDGET_S = 6.   # Office path only, streaming: the deadline covers the fold too, about 3 s at Nice 19 on 46,000 rows; the desk keeps 0.5 s


def _verify(hit, key, bundle, epoch, request=None):
    """A cache hit is returned only after its row hash, key, full request, body hash, bundle and epoch re-verify (no
    fallback to a fresh read that would hide a corrupted store); anything malformed is typed receipt_corrupt."""
    try:
        payload = hit["payload_json"]
        rec = json.loads(payload)
        digest = hashlib.sha256(S.canonical_json(rec) + hit["prev_hash"].encode("ascii")).hexdigest()
    except (ValueError, TypeError, UnicodeError, AttributeError, KeyError):
        return _unknown("receipt_corrupt", cache_key=key)
    ok = (isinstance(rec, dict) and S.canonical_json(rec).decode("utf-8") == payload and digest == hit["row_hash"]
          and rec.get("cache_key") == key and _sha(rec.get("request")) == key
          and (request is None or rec.get("request") == request)
          and rec.get("definition_bundle_hash") == bundle and rec.get("strategy_epoch") == epoch
          and rec.get("contract") == OFFICE_SHEET_CONTRACT["version"] and rec.get("state") == "known"
          and _sha(rec.get("sheet")) == rec.get("sheet_hash") and _sha(rec.get("lenses")) == rec.get("lenses_hash"))
    return dict(rec, row_hash=hit["row_hash"]) if ok else _unknown("receipt_corrupt", cache_key=key)


NEAR_PASS_VERSION = "near-pass-v1"     # Gamma prereg docs/audits/2026-10-29-office-near-pass-stratum-preregistration.md
NEAR_PASS_ALLOW_VERSION = "near-pass-allow-v1"  # Amendment 1: new literals require a new freeze.
NEAR_PASS_REQUIRED = ("market_data", "token_safety", "sell_probe")       # section 1.2: priced, tradable, proven round trip
# Amendment 1 freezes complete stored reasons, including the explicitly approved provenance variants.
NEAR_PASS_ALLOWED = {
    "dump_group": ("dump_group_unknown", "partial_history"),
    "cluster": ("cluster_unknown_dependency", "cluster_unknown_dependency:owner_program", "cluster_unknown_dependency:quota"),
    "holders": ("creator_unknown", "supply_unknown"),
    "chart": ("age_unknown", "age_unknown_decimals_post_cut_immutable", "chart_60s_prints_lt_2", "chart_60s_prints_lt_2_decimals_post_cut_immutable"),
}
NEAR_PASS_REFUSED = re.compile(
    r"(?:^|[:_])(?:kill:|facts_unavailable(?:$|[:_])|binding(?:$|[:_])|"
    r"pool_(?:binding_unknown|tip_unproven|snapshot_mismatch|block_cut_unknown|tape_gap)(?:$|[:_])|"
    r"sol_usd_|[a-z0-9_]*_unreadable(?:$|[:_]))")


def _allowed(check, reason):
    return isinstance(reason, str) and reason in NEAR_PASS_ALLOWED.get(check, ())


def near_pass_blockers(hard_filter_row):
    """Section 1 of the near-pass preregistration, exactly: the sorted [{check, status: 'unknown', reason}] blockers of
    an eligible SKIP ticket, or None. Eligible = verdict SKIP whose reason is 'unknown:*' (no kill anywhere),
    market_data / token_safety / sell_probe pass, no kill check, and every other non-pass check an 'unknown' with an
    allowed reason. An absent check, a non-unknown status or any other reason (facts_unavailable, *_unreadable,
    pool_*, sol_usd_*, provenance_unreadable, ...) refuses the ticket."""
    row = hard_filter_row if isinstance(hard_filter_row, dict) else {}
    reason = row.get("reason")
    if (row.get("verdict") != "SKIP" or not isinstance(reason, str)
            or not reason.startswith("unknown:") or NEAR_PASS_REFUSED.search(reason)):
        return None
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    checks = evidence.get("checks") if isinstance(evidence.get("checks"), dict) else None
    # Preregistration 1.5 overrides every allowance, including holders and provenance suffixes.
    if checks is None or set(checks) != set(NEAR_PASS_REQUIRED) | set(NEAR_PASS_ALLOWED) or any(isinstance(v, dict) and (v.get("status") == "kill" or
                            isinstance(v.get("reason"), str) and NEAR_PASS_REFUSED.search(v["reason"]))
                            for v in checks.values()):
        return None
    if any(not isinstance(checks.get(k), dict) or checks[k].get("status") != "pass" for k in NEAR_PASS_REQUIRED):
        return None
    out = []
    for name, c in checks.items():
        if name in NEAR_PASS_REQUIRED or (isinstance(c, dict) and c.get("status") == "pass"):
            continue
        if name not in NEAR_PASS_ALLOWED or not isinstance(c, dict) or c.get("status") != "unknown":
            return None
        if not _allowed(name, c.get("reason")):
            return None
        out.append({"check": name, "status": "unknown", "reason": c["reason"]})
    return sorted(out, key=lambda b: (b["check"], b["reason"]))


def blocker_set_id(blockers):
    return hashlib.sha256(S.canonical_json(blockers)).hexdigest()


def sheet(candidate: dict, *, desk_dir, policy: dict, clock_sample: dict, hard_filter_row: dict, now: float,
          store_conn, buyer_ledger, strategy_epoch: str, feed: str = "pass", source_provenance: dict | None = None, evidence_reader=None) -> dict:
    """A sheet receipt {kind, state known|unknown|refused, ...}; known receipts are appended once per cache key and
    re-verified on every later read. hard_filter_row = {verdict, ts, evidence, market_cut{receipt_cut, cut_slot},
    source_metadata, emission_candidate}: the emission candidate is the snapshot the hard filter judged (it binds the
    safety proof); `candidate` is the refreshed sheet snapshot of the same network/mint/pool. Times: `now` and the PASS row ts are host clock; the sheet as-of is the candidate
    snapshot's emission time (host), which must not be before the PASS. clock_sample {offset_s, sampled_at(host)}
    must be finite, not from the future and no older than the D sheet max age (CLOCK_MAX_AGE_S); otherwise a typed
    unknown with no read. Every cut stored is corrected UTC; the PS tape's host cut is the labelled host_receipt_cut.
    buyer_ledger is injected (the Office never opens the funded DB)."""
    offset, sampled_at = (clock_sample or {}).get("offset_s"), (clock_sample or {}).get("sampled_at")
    if not (_finite(offset) and _finite(sampled_at) and _finite(now) and 0 <= now - sampled_at <= K.CLOCK_MAX_AGE_S):
        return _unknown("clock_unknown")                                       # no read at all
    row = hard_filter_row if isinstance(hard_filter_row, dict) else {}
    blockers = None
    if feed == "near_pass":                    # a separate, labelled admission mode; never mixed with the PASS feed
        blockers = near_pass_blockers(row)
        if blockers is None:
            return {"kind": "sheet", "state": "refused", "reason": "not_near_pass_eligible"}
    elif feed != "pass":
        return {"kind": "sheet", "state": "refused", "reason": "feed_unknown"}
    elif row.get("verdict") != "PASS":
        return {"kind": "sheet", "state": "refused", "reason": "not_hard_filter_pass"}
    try:
        K.validate_ps_inputs(policy, PROMPT_VERSION)
        candidate = K.validate_candidate(candidate)
        judged = K.validate_candidate(row["emission_candidate"])      # the snapshot the hard filter PASSed
        if any(judged[k] != candidate[k] for k in ("network_id", "mint", "pool")):
            raise ValueError("emission_candidate_mismatch")
        cut = row["market_cut"]
        host_cut, slot_cut, pass_at = cut["receipt_cut"], cut["cut_slot"], K._utc(row["ts"])
        as_of = candidate["snapshot"]["emission_at"]
        if not (_finite(host_cut) and type(slot_cut) is int and slot_cut >= 0 and _finite(as_of)):
            raise ValueError("market_cut")
        if not host_cut <= as_of:
            raise ValueError("market_cut_after_sheet")
        if not pass_at <= as_of <= now:
            raise ValueError("snapshot_before_pass_or_future")
    except (ValueError, TypeError, KeyError) as exc:
        return {"kind": "sheet", "state": "refused", "reason": "invalid_input:%s" % str(exc)[:80]}
    bundle = definition_bundle_hash(policy, strategy_epoch)
    cuts = {"basis": "corrected_utc", "receipt_cut": host_cut - offset, "knowledge_cut": as_of - offset,
            "pass_cut": pass_at - offset, "slot_cut": slot_cut, "host_receipt_cut": host_cut}
    request = {"network_id": candidate["network_id"], "mint": candidate["mint"], "pool": candidate["pool"],
               "snapshot_hash": candidate["snapshot"]["hash"], "judged_snapshot_hash": judged["snapshot"]["hash"],
               "cut": cuts, "offset": {"offset_s": float(offset), "sampled_at": float(sampled_at)},
               "evidence_hash": _sha(row["evidence"]), "source_metadata_hash": _sha(row.get("source_metadata")),
               "definition_bundle_hash": bundle, "strategy_epoch": strategy_epoch}
    if source_provenance is not None:
        request['source_provenance'] = source_provenance
    if blockers is not None:          # section 2: feed, blockers and blocker_set_id reach the cache key and the bundle
        request.update(feed="near_pass", near_pass_version=NEAR_PASS_VERSION, allow_version=NEAR_PASS_ALLOW_VERSION, blockers=blockers,
                       blocker_set_id=blocker_set_id(blockers))
    key = cache_key(request)
    # the decision frontier is the trusted call time in corrected UTC; sampled_at only proves the offset's age
    clock = {"corrected_utc": now - offset, "offset_s": float(offset), "sampled_at": float(sampled_at)}
    hit = S.latest(store_conn, "receipts", "cache_key", key)
    if hit is not None:
        return _verify(hit, key, bundle, strategy_epoch, request)
    try:
        facts = F.facts_for(candidate, desk_dir=desk_dir, as_of=as_of, receipt_cut=host_cut, cut_slot=slot_cut,
                            source_metadata=row.get("source_metadata"), ours=frozenset(), ours_sigs=frozenset(),
                            bounds=policy["rail_bounds"], version=F.VERSION3)
        safety = K.safety_from_evidence(judged, row["evidence"], policy=policy["hard_filter"], as_of=as_of)
        flow = facts["flow_60s"]
        safety.update(sell_probe="ok", sells_exist_60s=flow["sell_n"] > 0 if flow is not None else None)
        cluster_hit, lp_burned, evidence_block = SE.build(desk_dir=desk_dir, mint=candidate["mint"], pool=candidate["pool"], as_of=as_of,
                                                          soft=K.soft_facts(row["evidence"].get("checks")), reader=evidence_reader, checks=row["evidence"].get("checks"))
        safety.update(cluster_hit=cluster_hit, lp_locked_or_burned=lp_burned)
        proof = {k: judged[k] for k in ("network_id", "mint", "pool")}
        proof.update(emission_snapshot_hash=judged["snapshot"]["hash"], known_at=K._utc(row["evidence"]["known_at"]),
                     probe_receipt_cut=host_cut, probe_cut_slot=slot_cut)
        d = DB.build_d_blocks(candidate, facts, desk_dir=desk_dir, ours=frozenset(), pass_persist_at=pass_at,
                              decision_at=as_of, clock=(float(offset), float(sampled_at)), ledger=buyer_ledger, unread=frozenset({'heat'}))
        body = K.render_judge_sheet(candidate, ps_facts=facts, safety=safety, safety_proof=proof, d_blocks=d)
        if blockers is not None:       # typed sheet facts: shown as evidence, never a gate (sheet_hash covers them)
            body = dict(body, feed="near_pass", near_pass_version=NEAR_PASS_VERSION, allow_version=NEAR_PASS_ALLOW_VERSION, blockers=blockers,
                        blocker_set_id=blocker_set_id(blockers))
        body = dict(with_required(body), safety_evidence=evidence_block,   # sheet_hash covers it; reported, never a gate
                    price_basis=dict(SE.price_basis(((candidate["snapshot"].get("fields") or {}).get("pool_state") or {}).get("value")),
                                     unpriced_after_last=SE.unpriced_after_last(desk_dir, candidate["pool"], facts.get("structure") if isinstance(facts, dict) else None, as_of)))
        started = time.monotonic()
        trending = T.trending_for(candidate, desk_dir=desk_dir, as_of=as_of, ps_facts=facts, sql_budget_s=TRENDING_BUDGET_S, stream=True)
        trending_elapsed_s = time.monotonic()-started
        T.validate(trending)
    except (ValueError, TypeError, KeyError) as exc:                           # typed, not cached: a later read may succeed
        return _unknown("sheet_unavailable:%s" % str(exc)[:80], cache_key=key)
    lenses = {"ps_trending": trending}
    receipt = {"kind": "sheet", "state": "known", "contract": OFFICE_SHEET_CONTRACT["version"], "cache_key": key,
               "request": request,
               "definition_bundle_hash": bundle, "strategy_epoch": strategy_epoch, "cut": cuts, "clock_sample": clock,
               "sheet": body, "sheet_hash": _sha(body), "lenses": lenses, "lenses_hash": _sha(lenses),
               "read_budget_s": {"ps_trending": TRENDING_BUDGET_S, "heat": 0}, "elapsed_s": {"ps_trending": trending_elapsed_s}}
    if source_provenance is not None:
        receipt['source_provenance'] = source_provenance
    row_hash = S.append(store_conn, "receipts", receipt)
    return dict(receipt, row_hash=row_hash)


def freeze_bundle(store_conn, decision_id: str, cache_key: str, *, definition_bundle_hash: str, strategy_epoch: str) -> str:
    """One frozen bundle per decision (UNIQUE decision_id), bound to the PERSISTED, re-verified sheet receipt's row hash:
    a caller-supplied sheet is never trusted. Lookup and insert are one write transaction, so a legitimate retry
    returns the same hash; another receipt for the same decision is bundle_conflict."""
    with S.write_transaction(store_conn):
        hit = S.latest(store_conn, "receipts", "cache_key", cache_key)
        if hit is None:
            raise ValueError("bundle_receipt_missing")
        rec = _verify(hit, cache_key, definition_bundle_hash, strategy_epoch)
        if rec["state"] != "known":
            raise ValueError("bundle_receipt_corrupt")
        payload = {"decision_id": decision_id, "cache_key": cache_key, "receipt_row_hash": hit["row_hash"],
                   "sheet_hash": rec["sheet_hash"], "lenses_hash": rec["lenses_hash"]}
        prior = S.latest(store_conn, "decision_bundles", "decision_id", decision_id)
        if prior is not None:
            if not _row_intact(prior):                # same per-read integrity as receipts (Zeta dbd6bc386)
                raise ValueError("bundle_corrupt")
            if json.loads(prior["payload_json"]) != payload:
                raise ValueError("bundle_conflict")
            return prior["row_hash"]
        return S.append(store_conn, "decision_bundles", payload)
