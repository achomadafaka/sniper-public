"""hf2 soft facts for the JUDGE sheet, as of the call (operator order 2026-10-22).

The hard filter keeps only exit and pricing blockers. Cluster and holder evidence reach the JUDGE as typed facts
(contract.soft_facts): the stored hard-filter evidence supplies holders and whatever cluster row existed then, and
the bound desk-cluster/v1 row that landed since replaces the cluster part. No bound row at call time is the fact
'unknown:not_yet_ready'. Total: a read or parse problem is an unknown fact, never an exception, never a kill, and
nothing here touches the network or a lock.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sniper.services import memecoin_desk_contract as contract
from sniper.services.memecoin_cluster_admission import MIN_ELIGIBLE_OWNERS

MAX_BYTES = 8_000_000
SCHEMA = "desk-cluster/v1"
CHECK_KEYS = ("status", "reason")


def _epoch(value):
    try:
        return contract._utc(value)
    except ValueError:
        return None


def _tail_lines(path):
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > MAX_BYTES:
            fh.seek(size - MAX_BYTES)
            fh.readline()                                 # drop the partial first line
        return fh.read().decode("utf-8", "replace").splitlines()


FUNDED_PARSER_VERSION = 1       # the funded gate's trace: a row from a worker that predates cluster_parser_version, or a cohort with no cluster block
FUNDED_MAX_PAGES = 20
MISMATCH = "cluster_parser_mismatch"
UNPROVEN = "cluster_definition_unproven"


FUNDED_DEFINITION = {"parser_version": FUNDED_PARSER_VERSION, "max_pages": FUNDED_MAX_PAGES}


def row_definition(row):
    """The definition a cluster row was made under: the stamped whole dict (cluster_definition); a row without it is the funded
    trace (parser 1, 20 pages), or an older partial stamp (parser/pages only), which can never equal a full cohort block."""
    if not isinstance(row, dict):
        return None
    if isinstance(row.get("cluster_definition"), dict):
        return row["cluster_definition"]
    if "cluster_parser_version" in row:
        return {"parser_version": row.get("cluster_parser_version"), "max_pages": row.get("max_pages")}
    return dict(FUNDED_DEFINITION)


def cohort_definition(policy):
    """The definition the cohort froze: its whole cluster block; a policy with no cluster block froze the funded definition."""
    block = (policy or {}).get("cluster")
    return dict(block) if isinstance(block, dict) else dict(FUNDED_DEFINITION)


def check_from_row(row, cohort_policy=None):
    """The cluster check dict for the JUDGE from a desk-cluster/v1 row: its status / reason plus the two percentages the
    cluster worker keeps in row['metrics'] (top_funder_of_attempted, clustered_of_attempted, fractions of attempted
    supply). Exact percentages require a positive denominator and full integer supply resolution. Thin-book top-funder
    metrics omit singleton groups, so they cannot describe the largest funder. Invalid or partial values stay absent."""
    if not isinstance(row, dict) or not isinstance(row.get("check"), dict):
        return {"status": "unknown", "reason": "malformed"}
    if cohort_policy is not None and row_definition(row) != cohort_definition(cohort_policy):
        # the cluster worker is global: a row made under another definition than the one this ticket's cohort froze is never
        # mixed in and never a kill; it is an unknown fact the study counts per day (it is stored on the ticket's evidence)
        return {"status": "unknown", "reason": MISMATCH}
    check = {k: row["check"][k] for k in CHECK_KEYS if k in row["check"]}
    status, reason = check.get("status"), check.get("reason")
    if (not isinstance(status, str) or status not in ("pass", "kill", "unknown")
            or (reason is not None and (not isinstance(reason, str) or not contract._MACHINE.fullmatch(reason)))):
        return {"status": "unknown", "reason": "malformed"}
    metrics = row.get("metrics")
    if status in ("pass", "kill") and isinstance(metrics, dict):
        attempted, resolved = metrics.get("attempted_supply"), metrics.get("resolved_supply")
        eligible = metrics.get("eligible")
        if (not contract._number(attempted, integer=True, minimum=1)
                or not contract._number(resolved, integer=True, minimum=1) or resolved != attempted
                or not contract._number(eligible, integer=True, minimum=1)
                or reason in ("allowed_bound", "pass_thin_bound")):
            return check
        # Shared-infrastructure funders (frozen list) resolve their owners but join no group: the share of attempted supply held by such
        # owners is a plain fact beside the percentages, so a low top-funder number is never read as clean without it. Same exactness
        # rule as the other percentages: only here, past the early return, i.e. a fully resolved trace; on a partial or bound trace it
        # would be a floor (unresolved owners could be exchange-funded too), so it stays null.
        excluded = metrics.get("excluded_known_funders_of_attempted")
        if contract._number(excluded, minimum=0, maximum=1):
            check["excluded_known_funders_pct"] = round(excluded * 100, 4)
        for out_key, key in (("top_funder_pct", "top_funder_of_attempted"), ("clustered_pct", "clustered_of_attempted")):
            if out_key == "top_funder_pct" and (eligible < MIN_ELIGIBLE_OWNERS or reason == "pass_thin_owners"):
                continue
            value = metrics.get(key)
            if contract._number(value, minimum=0, maximum=1):
                check[out_key] = round(value * 100, 4)
    return check


def bound_cluster_check(candidate, *, desk_dir, now, cohort_policy=None):
    """The newest desk-cluster/v1 check bound to this candidate and known at or before `now`, or None.
    Raises OSError / ValueError on an unreadable file (the caller turns it into an unknown fact)."""
    if not contract._number(now, minimum=0):
        raise ValueError("clock_invalid")
    emitted = candidate["snapshot"]["emission_at"]
    if not contract._number(emitted, minimum=0):
        raise ValueError("candidate_clock_invalid")
    today = datetime.fromtimestamp(now, timezone.utc).date()
    found, found_at = None, None
    for day in ((today - timedelta(days=1)).isoformat(), today.isoformat()):
        path = Path(desk_dir) / "cluster_checks" / ("%s.jsonl" % day)
        if not path.exists():
            continue
        for line in _tail_lines(path):
            try:
                row = json.loads(line)
            except ValueError:
                continue                                  # a torn line: skip, a later line may still bind
            if (not isinstance(row, dict) or row.get("schema") != SCHEMA or not isinstance(row.get("check"), dict)
                    or any(row.get(k) != candidate.get(k) for k in ("ticket_id", "network_id", "mint", "pool"))
                    or row.get("snapshot_hash") != candidate["snapshot"]["hash"]):
                continue
            known = _epoch(row.get("known_at"))
            if known is None or not emitted <= known <= now or (found_at is not None and known < found_at):
                continue
            found, found_at = check_from_row(row, cohort_policy), known
    return found


def for_ticket(ticket, *, desk_dir, now, cohort_fn=None):
    """contract.soft_facts for one stored ticket (store.get_ticket) as of `now` (epoch seconds).
    cohort_fn(ticket) -> the ticket's cohort record. OMITTING it keeps the legacy, unguarded API (tests and old callers only: the
    judge worker always passes it, and a test pins that). With it the cluster fact is fail-closed:
      * a callback that yields no cohort/policy                          -> unknown / cluster_definition_unproven;
      * a bound row made under another definition than the cohort froze -> unknown / cluster_parser_mismatch (check_from_row);
      * a cohort that froze a "cluster" block and no bound row at call time: the stored hard-filter evidence is NOT inherited
        (it carries no definition stamp, so it cannot prove the frozen definition)  -> unknown / cluster_definition_unproven.
    A legacy cohort (no cluster block) keeps the stored evidence exactly as before."""
    checks = {}
    try:
        stage = next((s for s in ticket.get("stages") or [] if s.get("stage") == "hard_filter"), None)
        evidence = ((stage or {}).get("inputs") or {}).get("evidence") or {}
        checks = dict(evidence.get("checks") or {})
    except (AttributeError, TypeError, ValueError):
        checks = {}
    try:
        candidate = ticket["candidate"]
        if candidate.get("venue") == "pumpswap" or candidate.get("schema") == contract.PUMPSWAP_SCHEMA:
            checks["cluster"] = {"status": "unknown", "reason": "not_applicable"}
        else:
            policy = None
            if cohort_fn is not None:
                cohort = cohort_fn(ticket)
                policy = cohort.get("policy") if isinstance(cohort, dict) else None
            if cohort_fn is not None and not isinstance(policy, dict):
                checks["cluster"] = {"status": "unknown", "reason": UNPROVEN}
            else:
                row = bound_cluster_check(candidate, desk_dir=desk_dir, now=now, cohort_policy=policy)
                if row is not None:
                    checks["cluster"] = row
                elif isinstance(policy, dict) and isinstance(policy.get("cluster"), dict) and "cluster" in checks:
                    checks["cluster"] = {"status": "unknown", "reason": UNPROVEN}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        checks["cluster"] = {"status": "unknown", "reason": "unreadable"}
    return contract.soft_facts(checks)
