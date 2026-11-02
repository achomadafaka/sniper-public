"""Office Scout delivery of near-pass desk candidates (operator yes; Gamma prereg near-pass-v1; Alpha assignment
2026-10-29). Two read-only pieces the broker tools wrap:

- list_near_pass: one bounded rowid tail of desk_tickets (<= 200 + sentinel, all stages, as in the funnel), the
  hard_filter rows at or before `now`, each tested with the sheet tool's frozen near_pass_blockers. Returns ticket ids
  and their blocker sets. Observed lower bounds; `limited` when the tail was cut.
- near_pass_sheet: the Office sheet in feed='near_pass' mode for one listed ticket: the persisted hard_filter row is
  the ticket's own (verdict, reason, ts, evidence, market cut); the sheet's cut is that persisted market cut, never
  the caller's clock; the candidate is the ticket's emission refreshed by memecoin_desk_asof at the sheet call.
Nothing here writes the desk store; the sheet tool appends its receipt to the Office store as usual."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import math
import time
from datetime import datetime

from sniper.office.tools import sheet as SH

SHEET_CODE_VERSION = 'office-near-pass-sheet-v2-gap-rpc'


def _code_hash():
    root = Path(__file__).resolve().parents[1]
    try:
        paths = (Path(__file__), root/'services/memecoin_desk_ps_asof.py',
                 root/'office/tools/sheet.py', root/'office/runtime.py',
                 root/'services/memecoin_desk_ps_losses.py', root/'office/pool_state_rpc.py')
        return hashlib.sha256(b'\0'.join(p.read_bytes() for p in paths)).hexdigest()
    except OSError:
        return None


SHEET_CODE_SHA256 = _code_hash()
_CODE_ROOT = Path(__file__).resolve().parents[2]
PINNED_RELEASE_SHA = _CODE_ROOT.name if len(_CODE_ROOT.name) == 40 and all(c in '0123456789abcdef' for c in _CODE_ROOT.name) else None

LIMIT = 200
MAX_ITEMS = 50
# Am10 (prereg sec 18): the admitted feed 'pass' = core-cohort hard_filter PASS tickets, newest first, at most MAX_ITEMS,
# hard_filter at most PASS_MAX_AGE_S old; frozen constants, the unlisted remainder is counted by reason.
PASS_MAX_AGE_S = 3600.0
# A closed-authority mint read is an immutable fact of the ticket, not a price: the Scout may open a listed ticket long after
# the 120 s SOL bound. Reserve and SOL freshness stay at ps_asof's own bounds.
MINT_MAX_AGE_S = 3600.0


def _finite(value):
    try:
        return type(value) in (int,float) and math.isfinite(value)
    except OverflowError:
        return False


def _stamp(ts):
    try:
        at = datetime.fromisoformat(ts)
        if at.tzinfo is None:
            return None
        s = at.timestamp()
        return s if math.isfinite(s) and s >= 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _row(verdict, reason, inputs_json):
    """The persisted hard_filter row in the shape the sheet tool reads (verdict, reason, evidence)."""
    try:
        inputs = json.loads(inputs_json)
    except (TypeError, ValueError, RecursionError):
        return None
    return {"verdict": verdict, "reason": reason, "evidence": inputs.get("evidence") if isinstance(inputs, dict) else None}


def list_near_pass(conn, *, now, cohort_id=None):
    """{state, limited, items:[{ticket_id, at, feed, blocker_set_id, blockers}], pass_unlisted}; state unknown on a bad clock.
    Feed 'pass' items (Am10) come first; cohort_id (eval-1's frozen cohort) restricts both feeds."""
    if not _finite(now) or now < 0:
        return {"state": "unknown", "reason": "clock_unknown", "limited": None, "items": []}
    rows = conn.execute("SELECT id,ticket_id,cohort_id,stage,verdict,reason,ts FROM desk_tickets ORDER BY rowid DESC LIMIT ?",
                        (LIMIT + 1,)).fetchall()
    limited, rows = len(rows) > LIMIT, rows[:LIMIT]
    if cohort_id is not None:
        rows = [r for r in rows if r["cohort_id"] == cohort_id]
    items, unlisted = [], {"item_limit": 0, "age_bound": 0}
    for r in rows:                                     # rowid DESC = newest first
        if r["stage"] != "hard_filter" or r["verdict"] != "PASS":
            continue
        s = _stamp(r["ts"])
        if s is None or s > now:
            continue
        if now - s > PASS_MAX_AGE_S:
            unlisted["age_bound"] += 1
        elif sum(1 for i in items if i["feed"] == "pass") >= MAX_ITEMS:
            unlisted["item_limit"] += 1
        else:
            items.append({"ticket_id": r["ticket_id"], "at": r["ts"], "feed": "pass"})
    picked = [r for r in rows if r["stage"] == "hard_filter" and r["verdict"] == "SKIP"
              and isinstance(r["reason"], str) and r["reason"].startswith("unknown:")
              and (s := _stamp(r["ts"])) is not None and s <= now]
    near = 0
    for r in picked:
        if near >= MAX_ITEMS:
            break
        got = conn.execute("SELECT substr(inputs_json,1,65536) FROM desk_tickets WHERE id=?", (r["id"],)).fetchone()
        row = _row(r["verdict"], r["reason"], got[0] if got else None)
        blockers = SH.near_pass_blockers(row) if row else None
        if blockers is not None:
            items.append({"ticket_id": r["ticket_id"], "at": r["ts"], "feed": "near_pass", "blocker_set_id": SH.blocker_set_id(blockers),
                          "blockers": blockers, "allow_version": SH.NEAR_PASS_ALLOW_VERSION})
            near += 1
    return {"state": "known", "limited": limited, "items": items, "pass_unlisted": unlisted,
            "coverage": "last %d desk tickets, all stages; near-pass-eligible hard_filter subset" % LIMIT}


# Reasons where the tape tip cannot prove price but an exact on-chain pool read can (a gap after the last tape swap is moot once an
# exact read is newer than it; the others are binding faults).
RPC_FALLBACK = frozenset({"latest_swap_not_buy", "reserve_stale", "virtual_quote_unproven", "tape_gap_since_cut"})


# Heartbeat-freshness reasons that flicker on the live tape (ps_committed_through lags > 5 s about a third of samples, measured
# 2026-10-29): retried a few times on the Office thread, never on the fill path. Any other reason is returned at once.
TRANSIENT = frozenset({"ps_commit_unproven", "tape_stale"})
RETRIES, RETRY_GAP_S = 3, 1.5


def near_pass_sheet(desk_conn, ticket_id, *, store_conn, desk_dir, now, clock_sample, buyer_ledger, refresh, store,
                    pool_state_reader=None, clock=time.time, sleep=time.sleep):
    """The near-pass sheet receipt for one ticket, or {state: refused|unknown, reason}. `store` is
    sniper.persistence.db_memecoin_desk (injected for tests); `refresh` is memecoin_desk_asof.refresh."""
    provenance = dict(code_version=SHEET_CODE_VERSION, code_sha256=SHEET_CODE_SHA256,
        pinned_release_sha=PINNED_RELEASE_SHA,
        rpc_fallback=dict(attempted=False, read_at=None, slot=None, unknown_reason=None, initial_reason=None,
                          clock_basis='host_unix_seconds', read_at_corrected=None))
    rpc = provenance['rpc_fallback']
    def tagged(result):
        return dict(result, source_provenance=provenance)
    ticket = store.get_ticket(desk_conn, ticket_id)
    if ticket is None:
        return tagged({"kind": "sheet", "state": "refused", "reason": "ticket_unknown"})
    stage = next((s for s in ticket["stages"] if s["stage"] == "hard_filter"), None)
    if stage is None:
        return tagged({"kind": "sheet", "state": "refused", "reason": "hard_filter_missing"})
    evidence = (stage.get("inputs") or {}).get("evidence")
    cut = (evidence or {}).get("market_cut") if isinstance(evidence, dict) else None
    if not isinstance(cut, dict):
        return tagged({"kind": "sheet", "state": "unknown", "reason": "market_cut_unknown"})
    cohort = store.get_cohort(desk_conn, ticket["cohort_id"])
    policy = cohort["policy"]
    # A near-pass ticket is a SKIP row, so the judge's PASS-only mint read is absent; its token_safety passed, so the
    # same bound read from its own row is the mint receipt (None stays None: ps_asof then names intake_mint_missing).
    from sniper.services.memecoin_desk_judge import intake_mint_of
    feed = "pass" if stage.get("verdict") == "PASS" else "near_pass"      # Am10: the ticket's own verdict picks the feed
    # integrity=True is the judge's own path for intake (PS facts) cohorts: it carries pending tape gaps into the cut instead
    # of refusing while any gap is pending, which is most of the time on the live tape (heartbeat sampled 2026-10-29).
    args = ({"intake_mint": intake_mint_of(ticket, ("PASS",) if feed == "pass" else ("SKIP",)), "integrity": True}
            if "intake" in ticket["candidate"] else {})
    def with_pending(result, exc):
        """The pending map at a tape-gap failure stays on the result, {} = settled, so the event can check the race reading."""
        return dict(result, ps_pending_gaps=exc.pending_gaps) if hasattr(exc, "pending_gaps") else result
    def named(exc):
        why = getattr(exc, "reason", None)
        return "refresh_failed:%s%s" % (type(exc).__name__, ":%s" % why if isinstance(why, str) else "")
    try:
        for attempt in range(RETRIES + 1):
            try:
                candidate, meta = refresh(ticket["candidate"], desk_dir=desk_dir, now=now, mint_max_age_s=MINT_MAX_AGE_S,
                                           accept_curve_derived=True, resolve_zero_fees=True, **args)
                break
            except Exception as exc:
                pending_gaps = getattr(exc, "pending_gaps", None)
                # A loss awaiting attribution counts on every pool for under a second (Alpha 2026-10-31); a settled gap refuses at once.
                racing = getattr(exc, "reason", None) == "tape_gap_since_cut" and bool(pending_gaps)
                if attempt == RETRIES or not (racing or getattr(exc, "reason", None) in TRANSIENT):
                    raise
                sleep(RETRY_GAP_S)
                now = max(now, clock())
    except Exception as exc:
        if pool_state_reader is None or getattr(exc, "reason", None) not in RPC_FALLBACK:
            return tagged(with_pending({"kind": "sheet", "state": "unknown", "reason": named(exc)}, exc))
        rpc.update(attempted=True, initial_reason=getattr(exc, 'reason', None))
        try:
            got = pool_state_reader.read(ticket["candidate"]["pool"], ticket["candidate"]["mint"])
        except Exception as read_exc:
            got = {'unknown': 'rpc_error:'+type(read_exc).__name__}
        if not isinstance(got, dict):
            got = {'unknown': 'rpc_state_unusable'}
        at, slot = got.get('read_at'), got.get('slot')
        rpc['read_at'] = at if _finite(at) and at >= 0 else None
        rpc['slot'] = slot if type(slot) is int and slot >= 0 else None
        offset = (clock_sample or {}).get('offset_s')
        if rpc['read_at'] is not None and _finite(offset):
            corrected = at-offset
            rpc['read_at_corrected'] = corrected if _finite(corrected) else None
        if 'unknown' not in got and (rpc['read_at'] is None or rpc['slot'] is None):
            got = {'unknown': 'rpc_state_unusable'}
        if 'unknown' in got:
            reason = got['unknown']
            rpc['unknown_reason'] = reason if isinstance(reason,str) and len(reason) <= 160 else 'rpc_unknown'
            return tagged({"kind": "sheet", "state": "unknown", "reason": "%s;%s" % (named(exc), rpc['unknown_reason'])})
        now = max(now, clock())   # the supplied read timestamp cannot make its own future-state check pass
        try:
            candidate, meta = refresh(ticket["candidate"], desk_dir=desk_dir, now=now, mint_max_age_s=MINT_MAX_AGE_S,
                                       accept_curve_derived=True, resolve_zero_fees=True, rpc_state=got, **args)
        except Exception as exc2:
            return tagged(with_pending({"kind": "sheet", "state": "unknown", "reason": named(exc2)}, exc2))
    source = store.get_intake_source_receipt(desk_conn, ticket["opportunity_id"], cohort_id=ticket["cohort_id"],
                                             hard_policy=policy["hard_filter"], candidate=ticket["candidate"],
                                             as_of=now) or {}
    # The facts are read at the cut of the refreshed state: ps_facts needs state.cut_slot == the cut_slot it is given, and the
    # ticket's persisted slot is older than the refresh's. The ticket's own cut stays when the refresh reports none.
    if isinstance(meta, dict) and isinstance(meta.get("as_of"), float):
        now = max(now, meta["as_of"])     # the snapshot as_of can be rounded a microsecond past the call time
    fresh_cut = ({"receipt_cut": meta["receipt_cut"], "cut_slot": meta["cut_slot"]}
                 if isinstance(meta, dict) and "receipt_cut" in meta and "cut_slot" in meta else None)
    row = {"verdict": stage.get("verdict"), "reason": stage.get("reason"), "ts": stage.get("ts"), "evidence": evidence,
           "market_cut": fresh_cut or {"receipt_cut": cut.get("receipt_cut"), "cut_slot": cut.get("cut_slot")},
           "source_metadata": source.get("source_metadata"), "emission_candidate": ticket["candidate"]}
    return tagged(SH.sheet(candidate, desk_dir=desk_dir, policy=policy, clock_sample=clock_sample, hard_filter_row=row, evidence_reader=pool_state_reader,
                    now=now, store_conn=store_conn, buyer_ledger=buyer_ledger, strategy_epoch=ticket["cohort_id"],
                    feed=feed, source_provenance=provenance))
