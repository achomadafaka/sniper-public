"""The operator's manual buy crosses to the runner as an INTENT. Only the runner builds contracts.

WHY THIS EXISTS. `sniper/settings/memecoin.py` captures every knob from `os.getenv` at IMPORT time,
so a long-lived process holds a snapshot taken when it started. Deploys restart the runner. Nothing
restarts the web server, and the funded manual buy used to execute inside it. Position 44706 opened
at 15:01 under `follow-30-50-v1` / 780s / no silence rail while same-minute feed entries froze
v2 / 14400 / 60 — the timecap lift had activated at 14:33, and the web process had been up since
2026-08-25 10:56. The same staleness refused a manual entry for "insufficient bankroll" against a
wallet holding $108, because the failed balance read was the web's.

`build_funded_contract` already had ONE call site. A single construction POINT is not a single
construction PROCESS, and only the second property makes a frozen contract mean anything.

AN INTENT CARRIES INPUTS, NEVER A DECISION — mint, chain, size, wallet. No policy, no clip, no
gate verdict. If the web could name a rail the divergence would come straight back wearing a
different hat.

REFINED 2026-08-27: an intent MAY carry operator exit rails, under one rule. What crosses is what
the operator TYPED, never a value the web read from its own config. A typed number cannot be
stale; a defaulted one is exactly the 44706 divergence in a new coat. The web sends NULL for every
field the operator left blank, and `rails_mode` names the regime:

    NULL / 'default'  the runner's own policy decides everything (unchanged)
    'custom'          ONLY the named values are armed; every rail not named is switched OFF

'custom' is the operator's explicit choice. It is also the configuration that went 3 of 11 across
the per-position overrides on record, because setting an absolute target writes tp_pct=0 and
removes the default take-profit underneath. The operator was shown that and chose it; manual entry
is a discretionary pick and the operator owns the exit.

DURABLE STATE, NOT A CURSOR. `onchain_wallet_detections` advances past a row whose hook raised,
because "a detection is a moment, not a queue item worth retrying late". Correct there and wrong
here: the operator asked explicitly, so an intent that is never claimed must EXPIRE VISIBLY rather
than disappear. Every intent ends somewhere a person can read.

    queued -> claimed -> filled | refused
    queued -> expired            (never claimed inside its TTL)
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timedelta

from sniper.services.memecoin_onchain_wake import default_address, send_wake

logger = logging.getLogger(__name__)

QUEUED, CLAIMED, FILLED, REFUSED, EXPIRED = "queued", "claimed", "filled", "refused", "expired"
UNRESOLVED = "unresolved"
TERMINAL = frozenset({FILLED, REFUSED, EXPIRED, UNRESOLVED})

# A manual buy is a discretionary pick on a coin that moves. Past this the intent is stale enough
# that filling it is a different trade from the one the operator looked at.
DEFAULT_TTL_S = 120.0

# A claim this old means the runner died holding it. Reclaiming is safe because the funded path is
# idempotent per (coin, wallet) — one-position-per-coin refuses a duplicate — and leaving it
# claimed forever is the silent drop this queue exists to prevent.
CLAIM_STALE_S = 90.0


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None


RAIL_FIELDS = ("tp_pct", "tp_target_mcap", "sl_pct", "sl_target_mcap", "trail_pct", "derisk_pct")
DEFAULT_RAILS, CUSTOM_RAILS = "default", "custom"


DESK = "desk"
SIGNAL_SOURCES = ("manual", DESK)


def wake_address():
    """The intent worker's wake socket (build step 2). A lost datagram costs one worker poll."""
    return default_address() + "-manual-intents"


def validate_desk_rails(rails, bounds):
    """(True, None) when every desk rail is a finite number inside its operator range and every
    required rail is present; else (False, reason). Never clamps: a clamped value is a decision the
    seat did not make (spec desk-execution-plumbing §2.3). A rail with no configured range, or an
    absolute mcap target, may not be set by the seat at all."""
    from sniper.settings.memecoin import DESK_REQUIRED_RAILS
    if not isinstance(rails, dict):
        return False, "desk rails must be a mapping of rail name to number"
    rails = dict(rails)
    if not bounds:
        return False, "desk rail bounds unset or malformed: sl_pct cannot be checked"
    for name in DESK_REQUIRED_RAILS:
        if rails.get(name) is None:
            return False, f"{name} is required on a desk entry"
    for name, value in rails.items():
        if value is None:
            continue
        if name not in bounds:
            return False, f"{name} has no operator range and may not be set by the desk"
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False, f"{name} must be a finite number"
        lo, hi = bounds[name]
        if not lo <= float(value) <= hi:
            return False, f"{name}={value} is outside {lo}-{hi}"
    return True, None


def _desk_refusal(*, network_id, mint, size_usd, size_pct_equity, rails, rails_mode, override,
                  desk_ticket_id, desk_metadata=None):
    """Why a desk intent may not be queued, or None. Checked BEFORE the row exists, so a desk
    intent in the queue always carries its complete frozen policy (R1)."""
    import sniper.settings.memecoin as cfg
    from sniper.persistence.db_memecoin import normalized_coin_network
    from sniper.services.memecoin_origin import desk_lane_open
    if not desk_lane_open(desk_metadata):
        return "desk disabled (MEMECOIN_DESK_ENABLED)"
    if not desk_ticket_id:
        return "a desk intent needs its desk ticket id"
    from sniper.services.memecoin_origin import validate_desk_metadata
    ok, why = validate_desk_metadata(desk_metadata)
    if not ok:
        return why
    if normalized_coin_network(network_id, mint) not in cfg.MEMECOIN_DESK_CHAINS:
        return "chain is not in MEMECOIN_DESK_CHAINS"
    if override:
        return "a desk intent may never carry override"
    if desk_metadata.get('cohort_id') == 'known-group-launch-v1':
        from sniper.settings.known_group import KNOWN_GROUP_LAUNCH_V1 as policy
        if size_usd != policy['clip_usd'] or rails != {'tp_pct':policy['tp_pct'],'sl_pct':policy['sl_pct']}:
            return 'known_group_fixed_policy_required'
    if size_pct_equity is not None or size_usd is None:
        return "a desk intent needs a fixed size_usd"
    if not 0 < size_usd <= float(cfg.MEMECOIN_DESK_CLIP_USD or 0.0):
        return "size_usd is above the desk clip MEMECOIN_DESK_CLIP_USD"
    if rails_mode != CUSTOM_RAILS:
        return "a desk intent must use rails_mode='custom'"
    ok, reason = validate_desk_rails(rails, cfg.MEMECOIN_DESK_RAIL_BOUNDS)
    return None if ok else reason


def _pilot_slots(conn, cohort):
    # Signed/unknown work stays counted; definitive unsigned refusals retain history, not capacity.
    used = conn.execute("SELECT count(*) FROM memecoin_pilot_admissions WHERE cohort_id=?", (cohort,)).fetchone()[0]
    from sniper.persistence.db_memecoin import count_rejected_unsigned_pilot_admissions
    used -= count_rejected_unsigned_pilot_admissions(conn, cohort)
    for table, predicate in (("memecoin_positions", "execution_mode='real'"),
                             ("memecoin_pending_entries", "status='reserved'")):
        used += conn.execute(
            f"SELECT count(*) FROM {table} p WHERE desk_cohort_id=? AND {predicate} "
            "AND NOT EXISTS (SELECT 1 FROM memecoin_pilot_admissions a "
            "WHERE a.cohort_id=p.desk_cohort_id AND a.desk_ticket_id=p.desk_opportunity_id)", (cohort,)).fetchone()[0]
    used += conn.execute(
        "SELECT count(*) FROM memecoin_manual_intents i WHERE signal_source='desk' "
        "AND state IN ('queued','claimed','unresolved','filled') "
        "AND CASE WHEN json_valid(desk_metadata_json) THEN json_extract(desk_metadata_json,'$.cohort_id') END=? "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_pilot_admissions a "
        "WHERE a.cohort_id=? AND a.desk_ticket_id=i.desk_ticket_id) "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p "
        "WHERE p.desk_cohort_id=? AND p.desk_opportunity_id=i.desk_ticket_id)", (cohort,cohort,cohort)).fetchone()[0]
    return used


def enqueue(conn, **request):
    meta=request.get('desk_metadata')
    cohort=meta.get('cohort_id') if isinstance(meta,dict) else None
    if request.get('signal_source')!='desk' or not isinstance(cohort,str) or not cohort.startswith('large-buyer-'):
        return _enqueue(conn,**request)
    import sniper.settings.memecoin as cfg
    cap=cfg.MEMECOIN_LARGE_BUYER_MAX_FILLS
    if type(cap) is not int or not 1<=cap<=30:raise ValueError('pilot admission cap must be 1..30')
    if conn.in_transaction:raise ValueError('pilot admission needs its own transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        ticket=request.get('desk_ticket_id')
        exists=conn.execute('SELECT 1 FROM memecoin_pilot_admissions WHERE cohort_id=? AND desk_ticket_id=?',(cohort,ticket)).fetchone()
        if not exists:
            if _pilot_slots(conn,cohort)>=cap:raise ValueError('pilot admission cap reached')
            conn.execute('INSERT INTO memecoin_pilot_admissions VALUES(?,?,?)',(cohort,ticket,request['now_iso']))
        # _enqueue commits this claim and the queue row together, before its wake.
        return _enqueue(conn,**request)
    except Exception:
        conn.rollback()
        raise


def _enqueue(conn, *, mint, now_iso, network_id=None, source_user_id=None, ticker=None,
            size_usd=None, size_pct_equity=None, origin_event_id=None,
            ttl_s=DEFAULT_TTL_S, intent_id=None, rails=None, rails_mode=None, override=False,
            signal_source="manual", desk_ticket_id=None, desk_metadata=None):
    """Record the operator's request. Returns the intent id. Never executes anything.

    `rails` holds ONLY values the operator typed (see the module docstring): any subset of
    RAIL_FIELDS, with None for anything left blank. `rails_mode` must be 'custom' when the operator
    wants exactly those rails and nothing else, otherwise the runner's own policy applies.

    `override` (2026-09-22, operator order) is an INPUT, same rule as every other field here: what
    the operator asked for, never a decision. The runner resolves it into named policy parameters
    at fill time (real_entry_hook); this module stays as blind to what it means as it already is
    to `size_usd`.
    """
    if not mint:
        raise ValueError("mint required")
    started = _parse(now_iso)
    if started is None:
        raise ValueError("a manual intent needs a usable timestamp")
    intent_id = intent_id or uuid.uuid4().hex
    if size_usd is not None and size_pct_equity is not None:
        raise ValueError("an intent must have one size input")
    if size_usd is not None:
        if isinstance(size_usd, bool):
            raise ValueError("size_usd must be finite and positive")
        size_usd = float(size_usd)
        if not math.isfinite(size_usd) or size_usd <= 0:
            raise ValueError("size_usd must be finite and positive")
    if size_pct_equity is not None:
        if isinstance(size_pct_equity, bool):
            raise ValueError("size_pct_equity must be finite and in (0,1]")
        size_pct_equity = float(size_pct_equity)
        if not math.isfinite(size_pct_equity) or not 0 < size_pct_equity <= 1:
            raise ValueError("size_pct_equity must be finite and in (0,1]")
    rails = dict(rails or {})
    unknown = set(rails) - set(RAIL_FIELDS)
    if unknown:
        # Fail loudly: a silently dropped rail is an operator who thinks a stop is armed.
        raise ValueError("unknown rail field(s): %s" % ", ".join(sorted(unknown)))
    if rails_mode not in (None, DEFAULT_RAILS, CUSTOM_RAILS):
        raise ValueError("rails_mode must be %r or %r" % (DEFAULT_RAILS, CUSTOM_RAILS))
    override = bool(override)
    if signal_source not in SIGNAL_SOURCES:
        raise ValueError("signal_source must be one of %s" % (SIGNAL_SOURCES,))
    if signal_source == DESK:
        refusal = _desk_refusal(network_id=network_id, mint=mint, size_usd=size_usd,
                                size_pct_equity=size_pct_equity, rails=rails, rails_mode=rails_mode,
                                override=override, desk_ticket_id=desk_ticket_id,
                                desk_metadata=desk_metadata)
        if refusal:
            raise ValueError(refusal)
        from sniper.services.memecoin_known_group_canary import _admission_refusal
        refusal = _admission_refusal(conn, desk_metadata, mint=mint, now_iso=now_iso)
        if refusal:
            raise ValueError(refusal)
        origin_event_id = origin_event_id or f"desk:{desk_ticket_id}"
    elif desk_ticket_id is not None:
        raise ValueError("only a desk intent carries a desk ticket id")
    elif desk_metadata is not None:
        raise ValueError("only a desk intent carries desk metadata")
    metadata_json = (json.dumps(desk_metadata, sort_keys=True, separators=(",", ":"))
                     if signal_source == DESK else None)
    vals = [float(rails[k]) if rails.get(k) is not None else None for k in RAIL_FIELDS]
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_manual_intents "
        "(intent_id, requested_at, expires_at, mint, "
        "network_id, source_user_id, ticker, size_usd, size_pct_equity, origin_event_id, state, "
        + ", ".join(RAIL_FIELDS) + ", rails_mode, override, signal_source, desk_ticket_id, desk_metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (intent_id, now_iso, (started + timedelta(seconds=float(ttl_s))).isoformat(),
         mint, network_id, source_user_id, ticker,
         size_usd, size_pct_equity,
         origin_event_id, QUEUED,
         *vals, rails_mode or None, int(override), signal_source, desk_ticket_id, metadata_json))
    if cur.rowcount == 0:
        # Either this intent_id exists, or (desk) this ticket already has an intent. A replay of
        # the same request returns the existing intent; anything else is refused by name, never
        # mapped onto the wrong intent (Codex audit #4).
        columns = ("mint, network_id, source_user_id, ticker, size_usd, size_pct_equity, "
                   "origin_event_id, rails_mode, override, " + ", ".join(RAIL_FIELDS)
                   + ", signal_source, desk_ticket_id, desk_metadata_json, intent_id")
        row = conn.execute(f"SELECT {columns} FROM memecoin_manual_intents WHERE intent_id=?",
                           (intent_id,)).fetchone()
        if row is None and desk_ticket_id is not None:
            row = conn.execute(f"SELECT {columns} FROM memecoin_manual_intents "
                               "WHERE desk_ticket_id=?", (desk_ticket_id,)).fetchone()
        expected = (mint, network_id, source_user_id, ticker,
                    size_usd,
                    size_pct_equity, origin_event_id, rails_mode or None, int(override), *vals,
                    signal_source, desk_ticket_id, metadata_json)
        if row is None or tuple(row)[:-1] != expected:
            if desk_ticket_id is not None:
                raise ValueError(f"desk ticket {desk_ticket_id} already has a different intent")
            raise ValueError("intent_id already used with a different request")
        conn.commit()
        return row[-1]
    conn.commit()
    send_wake(wake_address())   # never raises; the worker's poll covers a lost wake
    return intent_id


def claim_next(conn, *, now_iso, claim_id=None):
    """Take ownership of one queued intent, or None. Atomic: the UPDATE's own WHERE is the lock, so
    two runner passes cannot both claim the same row."""
    claim_id = claim_id or uuid.uuid4().hex
    # THE TTL IS ENFORCED HERE, NOT ONLY BY THE SWEEP. Reading `expires_at` in the claim makes the
    # deadline a local invariant: it holds whatever ran, or failed to run, before this call. The
    # sweep is still the thing that RESOLVES a dead row; this only refuses to start one.
    row = conn.execute(
        # The operator's own buy goes first; a desk intent never makes the operator wait.
        "SELECT * FROM memecoin_manual_intents WHERE state=? AND expires_at>? "
        "ORDER BY CASE COALESCE(signal_source, 'manual') WHEN 'manual' THEN 0 ELSE 1 END, "
        "requested_at LIMIT 1", (QUEUED, now_iso)).fetchone()
    if row is None:
        return None
    changed = conn.execute(
        "UPDATE memecoin_manual_intents SET state=?, claim_id=?, claimed_at=? "
        "WHERE intent_id=? AND state=? AND expires_at>?",
        (CLAIMED, claim_id, now_iso, row["intent_id"], QUEUED, now_iso)).rowcount
    conn.commit()
    if not changed:
        return None
    return dict(row, state=CLAIMED, claim_id=claim_id, claimed_at=now_iso)


def resolve(conn, intent_id, *, state, now_iso, position_id=None, attempt_id=None,
            result_status=None, reason=None, resolved_size_usd=None):
    """Write the outcome. A terminal row is IMMUTABLE — a retry may re-run an intent, never revise
    an observation."""
    if state not in TERMINAL:
        raise ValueError(f"{state} is not a terminal intent state")
    changed = conn.execute(
        "UPDATE memecoin_manual_intents SET state=?, resolved_at=?, position_id=?, attempt_id=?, "
        "result_status=?, reason=?, resolved_size_usd=? "
        "WHERE intent_id=? AND state NOT IN (?, ?, ?, ?)",
        (state, now_iso, position_id, attempt_id, result_status, reason, resolved_size_usd, intent_id,
         FILLED, REFUSED, EXPIRED, UNRESOLVED)).rowcount
    conn.commit()
    return bool(changed)


def expire_stale(conn, *, now_iso):
    """Two sweeps in one, both of which prevent a silent disappearance.

    An intent past its TTL that was never claimed becomes `expired` WITH A REASON. A claim older
    than CLAIM_STALE_S goes back to `queued`: the runner that held it died, and the operator's
    request outlives one process.
    """
    expired = conn.execute(
        "UPDATE memecoin_manual_intents SET state=?, resolved_at=?, "
        "reason='not claimed before the intent expired' WHERE state=? AND expires_at<=?",
        (EXPIRED, now_iso, QUEUED, now_iso)).rowcount
    cutoff = _parse(now_iso)
    released = 0
    if cutoff is not None:
        stale = (cutoff - timedelta(seconds=CLAIM_STALE_S)).isoformat()
        # A DEAD HOLDER DOES NOT REFRESH THE TTL. The release below used to return every stale claim
        # to `queued` without looking at `expires_at`, and `drain` calls this immediately before
        # `claim_next`, so a request the operator made minutes ago was re-queued and executed in the
        # same pass. Measured: a 120s intent released and reclaimed at 200s. The TTL is the whole
        # point of the queue — an operator's buy must not fire after they have stopped expecting it.
        # A stale claim past its TTL is therefore EXPIRED, not re-queued. Only a claim that is both
        # abandoned AND still inside its TTL goes back for another runner.
        expired += conn.execute(
            "UPDATE memecoin_manual_intents SET state=?, resolved_at=?, claim_id=NULL, "
            "claimed_at=NULL, reason='the claiming runner died and the intent expired before "
            "another could take it' WHERE state=? AND claimed_at<=? AND expires_at<=?",
            (EXPIRED, now_iso, CLAIMED, stale, now_iso)).rowcount
        released = conn.execute(
            "UPDATE memecoin_manual_intents SET state=?, claim_id=NULL, claimed_at=NULL "
            "WHERE state=? AND claimed_at<=? AND expires_at>?",
            (QUEUED, CLAIMED, stale, now_iso)).rowcount
    conn.commit()
    return {"expired": expired, "released": released}


def _project_intent(conn, *, attempt_id=None, origin_event_id=None):
    """A display read failure must not release execution ownership."""
    from sniper.services.memecoin_entry_view import entry_view
    try:
        return entry_view(conn, attempt_id=attempt_id, origin_event_id=origin_event_id)
    except Exception:
        logger.exception("manual intent result projection unavailable")
        return {"status": "needs_review", "label": "Needs review", "attempt_id": attempt_id,
                "reason": "Entry evidence is temporarily unavailable."}


def read_status(conn, intent_id):
    """What the UI reads back, so a refusal reaches the person who clicked rather than a log."""
    row = conn.execute(
        "SELECT intent_id, state, requested_at, expires_at, resolved_at, position_id, attempt_id, "
        "result_status, reason, mint, network_id, size_usd, ticker, rails_mode, "
        "size_pct_equity, resolved_size_usd, origin_event_id, override, "
        "signal_source, desk_ticket_id, desk_metadata_json, "
        + ", ".join(RAIL_FIELDS) +
        " FROM memecoin_manual_intents WHERE intent_id=?", (intent_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        metadata = json.loads(result.pop("desk_metadata_json") or "null")
    except (TypeError, ValueError):
        metadata = None
    result["desk_metadata"] = metadata if type(metadata) is dict else None
    result["operation_state"] = result["state"]
    result["operation_reason"] = result.get("reason")
    projected = _project_intent(conn, attempt_id=result.get("attempt_id"),
                           origin_event_id=result.get("origin_event_id"))
    if result["state"] == REFUSED and str(result.get("reason") or "").startswith(
            "desk_rails_not_armed"):
        # The money is spent but the seat's rails never armed, so the runner refused the intent
        # and queued a close. A 'filled' entry projection must not hide that (Codex audit #5).
        result.update(status="desk_rails_not_armed", label="Refused: rails not armed",
                      entry=projected or None)
        return result
    if projected:
        result.update(status=projected["status"], label=projected["label"],
                      entry=projected, reason=(result.get("reason") if str(result.get("reason") or "").startswith("RAILS NOT ARMED:")
                                               else projected["reason"]),
                      position_id=projected.get("position_id") or result.get("position_id"))
        if projected["status"] == "filled":
            result["state"] = FILLED
        elif projected["status"] in ("transaction_unresolved", "submitted_unknown", "needs_review", "retry_pending"):
            result["state"] = UNRESOLVED
    elif result["state"] == UNRESOLVED:
        result.update(status="needs_review", label="Needs review")
    else:
        result["status"] = result.get("result_status") or result["state"]
    return result


def pending_count(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM memecoin_manual_intents WHERE state IN (?, ?)",
        (QUEUED, CLAIMED)).fetchone()
    return int(row[0]) if row else 0


def make_enqueue_buy_fn(now_fn, *, ttl_s=DEFAULT_TTL_S):
    """The WEB's replacement for `make_real_buy_fn`. Same call shape as the executor it replaces,
    so `/real/buy` and `/real/buy_token` need no change, but it only ever writes a row.

    Returning `status='queued'` rather than `'filled'` is deliberate and visible: the routes report
    `ok = status == 'filled'`, so a queued intent reads as not-yet-filled instead of claiming a
    fill that has not happened.
    """
    def enqueue_buy_fn(conn, mint, source_user_id=None, *, ticker=None, network_id=None,
                       size_usd=None, size_pct_equity=None, origin_event_id=None,
                       intent_id=None, rails=None, rails_mode=None, override=False):
        try:
            intent_id = enqueue(
                conn, mint=mint, now_iso=now_fn(), network_id=network_id,
                source_user_id=source_user_id, ticker=ticker, size_usd=size_usd, ttl_s=ttl_s,
                size_pct_equity=size_pct_equity, origin_event_id=origin_event_id,
                intent_id=intent_id, rails=rails, rails_mode=rails_mode, override=override)
        except Exception as exc:
            logger.exception("could not enqueue a manual intent for %s", mint)
            return {"status": "error", "reason": f"could not queue the request: {exc}"}
        row = read_status(conn, intent_id) or {}
        return {"status": row.get("state") or "queued", "intent_id": intent_id,
                "position_id": row.get("position_id"), "reason": row.get("reason"),
                "resolved_size_usd": row.get("resolved_size_usd"),
                "detail": "the runner builds the contract; poll the intent for the outcome"}
    return enqueue_buy_fn


def apply_rails(conn, intent, position_id, *, now_iso, set_override_fn=None):
    """Arm the operator's exit rails on the position their intent just opened.

    Runs in the RUNNER, after the fill, for the same reason the fill does: the rails belong to the
    process whose config a deploy refreshes. Returns the override row id, or None when the runner's
    own policy is what the operator asked for.

    In 'custom' mode every percentage rail the operator did NOT name is written as 0.0, which the
    exit sweep reads as OFF. That is the point of the mode and it is the operator's decision: name
    a take-profit and no stop, and the position has no stop. Absolute mcap targets stay None when
    unnamed, because 0 is not a market cap and the override API refuses it.
    """
    mode = (intent.get("rails_mode") or DEFAULT_RAILS)
    supplied = {k: intent.get(k) for k in RAIL_FIELDS}
    if mode != CUSTOM_RAILS and all(v is None for v in supplied.values()):
        return None
    if mode == CUSTOM_RAILS:
        for k in ("tp_pct", "sl_pct", "trail_pct", "derisk_pct"):
            if supplied[k] is None:
                supplied[k] = 0.0
    if set_override_fn is None:
        from sniper.persistence.db_memecoin import set_position_exit_override as set_override_fn
    # A desk row is the SEAT's decision, not the operator's (R2): its own source ranks below the
    # operator, and the actor names the ticket so a reader can trace the rail to its decision.
    desk = intent.get("signal_source") == DESK
    source = DESK if desk else "operator"
    actor = f"desk:{intent.get('desk_ticket_id')}" if desk else "operator"
    return set_override_fn(
        conn, position_id, source, actor=actor,
        note="%s intent %s (%s rails)" % ("desk" if desk else "manual", intent.get("intent_id"), mode),
        now_iso=now_iso, **supplied)


def _refuse_unarmed_desk_fill(conn, intent, result, now_fn):
    """R1: a desk position whose seat rails did not arm must not stay open as a 'filled' intent.
    The money is spent, so the intent resolves REFUSED and a deterministic full close is queued
    for the runner's own exit executors; nothing here sells. If even the close cannot be queued,
    the reason says so and the runner's alert path reads it."""
    from sniper.services import memecoin_manual_exit_intents as exits
    reason = f"desk_rails_not_armed: {result.get('rails_error')}"
    try:
        exits.enqueue(conn, position_id=int(result["position_id"]), action="close",
                      now_iso=now_fn(), actor=f"desk:{intent.get('desk_ticket_id')}")
    except Exception as exc:
        logger.exception("desk intent %s: close intent for unarmed position %s failed",
                         intent["intent_id"], result.get("position_id"))
        reason += f" | CLOSE NOT QUEUED: {type(exc).__name__}: {exc}"
    resolve(conn, intent["intent_id"], state=REFUSED, now_iso=now_fn(),
            position_id=result.get("position_id"), attempt_id=result.get("attempt_id"),
            result_status=str(result.get("status") or "") or None, reason=reason,
            resolved_size_usd=result.get("resolved_size_usd"))
    return intent["intent_id"], REFUSED


def drain(conn, buy_fn, *, now_fn, max_per_pass=2):
    """Runner side: claim and execute queued intents with the RUNNER's settings and RPC stack.

    Bounded per pass so a queue backlog can never starve the exit sweep — this runs beside a funded
    exit loop, and no measurement or convenience may delay a sell.
    """
    done = []
    expire_stale(conn, now_iso=now_fn())
    for _ in range(int(max_per_pass)):
        intent = claim_next(conn, now_iso=now_fn())
        if intent is None:
            break
        desk_metadata = None
        if intent.get("signal_source") == DESK:
            from sniper.services.memecoin_origin import validate_desk_metadata
            try:
                desk_metadata = json.loads(intent.get("desk_metadata_json") or "null")
            except (TypeError, ValueError):
                pass
            ok, why = validate_desk_metadata(desk_metadata)
            if not ok:
                resolve(conn, intent["intent_id"], state=REFUSED, now_iso=now_fn(),
                        result_status="rejected", reason=f"desk_metadata_invalid: {why}")
                done.append((intent["intent_id"], REFUSED))
                continue
            from sniper.services.memecoin_known_group_canary import _admission_refusal
            refusal = _admission_refusal(conn, desk_metadata, mint=intent['mint'], now_iso=now_fn())
            if refusal:
                resolve(conn, intent['intent_id'], state=REFUSED, now_iso=now_fn(),
                        result_status='rejected', reason=refusal)
                done.append((intent['intent_id'], REFUSED))
                continue
        try:
            result = buy_fn(
                conn, intent["mint"], intent["source_user_id"],
                ticker=intent["ticker"], network_id=intent["network_id"],
                **({"size_usd": intent["size_usd"]} if intent["size_usd"] is not None else {}),
                **({"size_pct_equity": intent["size_pct_equity"]}
                   if intent["size_pct_equity"] is not None else {}),
                **({"origin_event_id": intent["origin_event_id"]}
                   if intent["origin_event_id"] is not None else {}),
                # Same backward-compat rule as every field above: a buy_fn that predates
                # override (a test double, or a not-yet-updated caller) must keep working
                # unchanged when there is nothing new to tell it.
                **({"override": True} if intent["override"] else {}),
                **({"signal_source": DESK, "desk_ticket_id": intent["desk_ticket_id"],
                    "desk_rails": {k: intent[k] for k in RAIL_FIELDS if intent[k] is not None},
                    "desk_metadata": desk_metadata}
                   if intent.get("signal_source") == DESK else {})) or {}
        except Exception as exc:
            logger.exception("manual intent %s failed in the runner", intent["intent_id"])
            projected = _project_intent(conn, origin_event_id=intent.get("origin_event_id"))
            state = UNRESOLVED if not projected or projected["status"] not in (
                "refused", "failed_before_submission", "transaction_failed", "not_evaluated") else REFUSED
            resolve(conn, intent["intent_id"], state=state, now_iso=now_fn(),
                    attempt_id=projected.get("attempt_id") if projected else None,
                    result_status="error", reason=f"{type(exc).__name__}: {exc}")
            done.append((intent["intent_id"], state))
            continue
        status = str(result.get("status") or "")
        projected = _project_intent(conn, attempt_id=result.get("attempt_id"),
                               origin_event_id=intent.get("origin_event_id"))
        unresolved = status in ("submitted_unknown", "unknown", "anomaly", "transaction_unresolved") or (
            projected and projected["status"] in (
                "transaction_unresolved", "submitted_unknown", "retry_pending", "needs_review"))
        state = FILLED if status == "filled" else UNRESOLVED if unresolved else REFUSED
        if state == FILLED and result.get("position_id"):
            # Rails are armed BEFORE the intent resolves, so a reader who sees `filled` sees a
            # position whose exits are already set. A failure here must not turn a real fill into a
            # refusal — the money is already spent — so it is logged loudly and the intent still
            # resolves as filled, carrying the reason.
            try:
                apply_rails(conn, intent, result["position_id"], now_iso=now_fn())
            except Exception as exc:
                logger.exception("manual intent %s filled but its rails did not arm",
                                 intent["intent_id"])
                result = dict(result, rails_error=f"{type(exc).__name__}: {exc}")
                if intent.get("signal_source") == DESK:
                    done.append(_refuse_unarmed_desk_fill(conn, intent, result, now_fn))
                    continue
        # A RAILS FAILURE MUST REACH THE PERSON WHO CLICKED. `rails_error` was set above and then
        # dropped: the reason chain never read it, so the UI showed a bare `filled` for a funded
        # position whose custom exits had NOT armed. It is prefixed so it cannot be mistaken for a
        # refusal reason, and it wins over the fill's own detail because it is the more dangerous
        # fact. Alerting stays with the runner, which owns `_safe_alert`; this module keeps no
        # outbound dependencies, and the persisted reason is what any alert would read.
        rails_error = result.get("rails_error")
        reason = result.get("reason") or result.get("error") or result.get("detail")
        if rails_error:
            reason = f"RAILS NOT ARMED: {rails_error}" + (f" | {reason}" if reason else "")
        # OPERATOR OVERRIDE (2026-09-22): token safety still ran and found a problem, but the
        # override made it a warning instead of a refusal. Same rule as rails_error above -- a
        # real fill must surface this to the person who clicked, not just to a log line.
        safety_warning = result.get("token_safety_warning")
        if safety_warning:
            reason = (f"TOKEN SAFETY WARNING: {safety_warning}"
                     + (f" | {reason}" if reason else ""))
        resolve(conn, intent["intent_id"], state=state, now_iso=now_fn(),
                position_id=result.get("position_id"), attempt_id=result.get("attempt_id"),
                result_status=status or None, reason=reason,
                resolved_size_usd=result.get("resolved_size_usd"))
        done.append((intent["intent_id"], state))
    return done
