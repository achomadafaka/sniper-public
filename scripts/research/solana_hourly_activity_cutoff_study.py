#!/usr/bin/env python3
"""Offline cutoff study for the Solana hourly-floor activity exception.

    python3 scripts/research/solana_hourly_activity_cutoff_study.py --roster ROSTER.json --pages DIR

Spec: docs/superpowers/specs/2026-09-17-solana-hourly-floor-activity-design.md, section "Cutoff study".
It reads a FROZEN roster and the raw one-page responses saved for it, and counts each page with the
live helper's `count_page`, so the calibration and the gate cannot disagree about what a count is.
It makes no network call and never opens the funded ledger. It prints one JSON row per roster item.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sniper.services.memecoin_solana_address_activity import (  # noqa: E402
    PAGE_LIMIT, count_page, source_signature)


def decision_ts(iso):
    return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())


def _aware_instant(value):
    """A timezone-aware datetime for `value`, or None for missing, naive or malformed."""
    from datetime import datetime as _dt
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def page_provenance_reason(item, page_doc):
    """Why this saved page does not BELONG to this roster item, or None when it does.

    Added 2026-09-18 beside the eligibility check above, which already refuses an error body, a
    missing result and a malformed document. Eligibility asks "is this an answer"; provenance asks
    "is it an answer to THIS question". A page fetched for another mint, against another `before`
    anchor, or before the decision it is supposed to describe, measures a different window, and
    `count_page` cannot tell: it is handed a list of signatures and nothing else.

    2026-09-23 (inherited review findings W2, W3, W4): an EMPTY page is evidence of zero only when
    the request that produced it is proven -- the saved request must name the method, the mint,
    the before-anchor, the full page size and a commitment (`request_unverified` otherwise); a
    request or params of the wrong shape is `malformed`, never an exception that kills the batch;
    and the fetch time is compared as an aware instant, so `+02:00` and `-04:00` stamps land on
    the right side of the decision (`page_time_unknown` when missing, naive or unparseable).
    """
    request = page_doc.get("request")
    if not isinstance(request, dict):
        return "malformed" if request is not None else "request_unverified"
    params = request.get("params")
    if params is not None and not isinstance(params, list):
        return "malformed"
    params = params or []
    options = params[1] if len(params) > 1 else {}
    if options is not None and not isinstance(options, dict):
        return "malformed"
    options = options or {}
    asked_mint = params[0] if params else None
    if request.get("method") != "getSignaturesForAddress" or asked_mint is None:
        return "request_unverified"
    if item.get("mint") and str(asked_mint) != str(item["mint"]):
        return "mint_mismatch"
    anchor = options.get("before")
    if anchor is None or not options.get("commitment"):
        return "request_unverified"
    if str(anchor) != str(item.get("source_signature")):
        return "anchor_mismatch"
    try:
        limit_ok = int(options.get("limit")) >= PAGE_LIMIT
    except (TypeError, ValueError):
        limit_ok = False
    if not limit_ok:
        return "request_unverified"
    fetched = _aware_instant(page_doc.get("fetched_at"))
    decided = _aware_instant(item.get("decision_at"))
    if fetched is None or decided is None:
        return "page_time_unknown"
    if fetched < decided:
        return "page_before_decision"
    return None


def count_item(item, page_doc):
    """The live count for one roster item, or an unknown row with the reason it cannot be counted.

    Every unknown NAMES ITS REASON (2026-09-18). The three unknown branches were
    indistinguishable in the output, so a reader could not tell a provider fault from a page that
    belongs to another coin, and could not count either.
    """
    out = {"kind": item.get("kind"), "id": item.get("position_id") or item.get("attempt_id"),
           "ticker": item.get("ticker"), "outcome": item.get("outcome"), "mint": item.get("mint"),
           "decision_at": item.get("decision_at"), "source_signature": item.get("source_signature")}
    if not item.get("source_signature") or not item.get("decision_at"):
        out.update(count_state="unknown", unknown_reason="no_source_signature")
        return out
    if not isinstance(page_doc, dict):
        out.update(count_state="unknown",
                   unknown_reason="no_page" if page_doc is None else "malformed")
        return out
    response = page_doc.get("response")
    if not isinstance(response, dict):
        out.update(count_state="unknown", unknown_reason="malformed")
        return out
    if response.get("error") is not None:
        out.update(count_state="unknown", unknown_reason="error_body")
        return out
    if not isinstance(response.get("result"), list):
        out.update(count_state="unknown", unknown_reason="no_result")
        return out
    reason = page_provenance_reason(item, page_doc)
    if reason is not None:
        out.update(count_state="unknown", unknown_reason=reason)
        return out
    counts = count_page(response.get("result"), decision_ts=decision_ts(item["decision_at"]),
                        source_signature=item["source_signature"])
    out.update(counts, count_state="counted", rtt_ms=page_doc.get("rtt_ms"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", required=True)
    ap.add_argument("--pages", required=True)
    args = ap.parse_args(argv)
    roster = json.loads(pathlib.Path(args.roster).read_text())["roster"]
    pages = pathlib.Path(args.pages)
    for item in roster:
        name = "%s_%s.json" % (item["kind"], item.get("position_id") or item["attempt_id"])
        path = pages / name
        # ONE ROW PER ROSTER ITEM, whatever the page holds (W3): a document that does not parse
        # is an unknown row for that item, and the next item still runs.
        try:
            doc = json.loads(path.read_text()) if path.exists() else None
        except (OSError, ValueError):
            doc = "unreadable"
        try:
            row = count_item(item, doc)
        except Exception as exc:                      # a malformed page must not end the batch
            row = {"kind": item.get("kind"),
                   "id": item.get("position_id") or item.get("attempt_id"),
                   "count_state": "unknown", "unknown_reason": "malformed",
                   "error": "%s: %s" % (type(exc).__name__, exc)}
        print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
