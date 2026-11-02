"""Funded large-buyer pilot, deterministic, no LLM. The trigger is the Office arm's frozen rule (docs/audits/2026-10-31-large-buyer-operator-scorecard-preregistration.md), read from the arm's own file, with the pool floor of 40 SOL before the buy.

It enters only through the desk queue (memecoin_manual_intents): the runner's desk worker, token safety, impact, bankroll, the desk cap and daily halt all apply there. Its process never touches a copy lane.
Every skip is named and counted. Absent data skips the entry; nothing is guessed. Default off: MEMECOIN_LARGE_BUYER_ENABLED."""
import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from sniper.util.clock import real_utc_now

PRODUCER = "H"            # a fixed-rule producer; the cohort id is what marks the pilot and selects its knob
COHORT_ID = "large-buyer-v1"  # legacy callers; run() always selects the new frozen CROWD identity
CROWD_COHORT_ID = "large-buyer-crowd-v1"
PROMPT_VERSION = "large-buyer-crowd-v1"
POOL_FLOOR_LAMPORTS = 40 * 10**9
CLIP_USD = 25
RAILS = {"tp_pct": 40.0, "sl_pct": 30.0}
TIME_STOP_S = 300.0
LATE_S = 5.0               # past entry_due + this the signal is stale: skipped, never bought late
INTENT_TTL_S = 5.0
TOLERANCE_PCT = 15.0       # build-day tape: p90 price at +10 s is +9.1% over the trigger print
FEE_PCT = 1.25
SOL_MAX_AGE_S = 120.0
POLICY = {"rule": "first observed reach of 25 SOL, no earlier observed sell, pool 40+ SOL, enter +10 s", "rails": RAILS, "time_stop_s": TIME_STOP_S,
          "clip_usd": CLIP_USD, "tolerance_pct": TOLERANCE_PCT, "min_others": 1, "history_basis": "observed_tape", "capture_complete": False}


def pump_decimals(mint):
    """A name/suffix is never an account read. Retained fail-closed for old callers."""
    return None


def mint_proof(conn, mint, *, now):
    from sniper.services import memecoin_desk_ps_metadata as md
    try:
        fact=md.read_metadata(conn,mint,as_of=now)
        if not fact or fact['metadata_source']!=md.SOURCE or fact['token_program'] not in md.OWNER_OF:return None
        if type(fact['decimals']) is not int or not 0<=fact['decimals']<=18:return None
        if type(fact['metadata_slot']) is not int or fact['metadata_slot']<=0:return None
        at=fact['metadata_known_at']
        if type(at) not in (int,float) or not math.isfinite(at) or not 0<at<=now:return None
        return dict(fact,mint=mint)
    except (sqlite3.Error,ValueError,TypeError,KeyError):return None


def read_sol_usd(path, *, now):
    try:
        beat=json.loads(Path(path).read_text())
        values=[beat[k] for k in ('sol_usd','sol_age_s','ts')]
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):return None
        price,age,at=values
        if price<=0 or age<0 or at>now or now-at+age>SOL_MAX_AGE_S:return None
        return price
    except (OSError,ValueError,KeyError,TypeError):return None


def queue_request(request, *, host_now):
    """Host receipts schedule; the runner's queue uses corrected UTC. Never refresh entry due."""
    proof=request.get('desk_metadata',{}).get('admission_proof',{})
    due=proof.get('entry_due_host')
    if type(due) not in (int,float) or not math.isfinite(due) or not due<=host_now<due+LATE_S:return None
    utc=real_utc_now()
    ttl=min(INTENT_TTL_S,due+LATE_S-host_now)
    proof['entry_deadline_utc']=iso(utc+ttl)
    return dict(request,now_iso=iso(utc),ttl_s=ttl)


def verdict_price(trigger, *, decimals, sol_usd, now):
    """The executor's price cap. Priced from real reserves after the trigger buy. Real reserves read low on a virtual-quote pool, so that error refuses rather than overpays."""
    quote, base = int(trigger["post_quote"]) / 1e9, int(trigger["post_base"]) / 10**decimals
    if quote <= 0 or base <= 0:
        return None
    ref = quote / base * sol_usd
    impact = CLIP_USD / sol_usd / quote * 100
    cap = TOLERANCE_PCT + FEE_PCT + impact
    return {"ref_price_usd": ref, "mcap_usd": 0.0, "liq_usd": quote * sol_usd, "supply_tokens": 0.0, "clip_usd": CLIP_USD, "tolerance_pct": TOLERANCE_PCT,
            "fee_pct": FEE_PCT, "impact_pct": impact, "cap_pct": cap, "cap_price_usd": ref * (1 + cap / 100), "snapshot_at": now, "source": "large_buyer_tape", "basis": "asof"}


def request_for(trigger, *, cohort_id, decimals, sol_usd, now, mint_metadata=None):
    ticket = "lb:%s:%s" % (trigger["pool"], trigger["wallet"])
    price = verdict_price(trigger, decimals=decimals, sol_usd=sol_usd, now=real_utc_now() if cohort_id==CROWD_COHORT_ID else now)
    if price is None:
        return None
    identity={k:trigger[k] for k in ('pool','wallet','mint','signature','trigger_at')}
    if cohort_id==CROWD_COHORT_ID:
        identity['crowd_proof']={k:trigger[k] for k in ('crowd_others_60s','source_slot','source_tx_index','source_ordinal','source_signature','detect_lag_s')}
        identity['mint_metadata']=mint_metadata
        identity['history_state']=trigger.get('history_state')
        identity['history_reason']=trigger.get('history_reason')
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    extra={}
    if cohort_id==CROWD_COHORT_ID:
        if not mint_metadata or mint_metadata.get('decimals')!=decimals:return None
        extra=dict(min_others_60s=1,history_basis="observed_tape",crowd_proof=identity['crowd_proof'],admission_proof=dict(mint=trigger['mint'],decimals=decimals,
            metadata_slot=mint_metadata['metadata_slot'],metadata_known_at_host=mint_metadata['metadata_known_at'],
            metadata_source=mint_metadata['metadata_source'],token_program=mint_metadata['token_program'],
            entry_due_host=trigger['entry_due'],entry_deadline_utc=iso(real_utc_now()+max(0,trigger['entry_due']+LATE_S-now)),clock_domain='corrected_utc'))
    return {"mint": trigger["mint"], "network_id": "solana", "size_usd": CLIP_USD, "rails": dict(RAILS), "rails_mode": "custom", "signal_source": "desk",
            "desk_ticket_id": ticket, "origin_event_id": "desk:" + ticket, "ttl_s": INTENT_TTL_S,
            "desk_metadata": {"cohort_id": cohort_id, "producer": PRODUCER, "opportunity_id": ticket, "snapshot_hash": digest, "verdict_price": price, "time_stop_s": TIME_STOP_S, **extra}}


def fills_and_queued(ledger, cohort_id):
    fills = ledger.execute("SELECT count(*) FROM memecoin_positions WHERE desk_cohort_id=? AND execution_mode='real'", (cohort_id,)).fetchone()[0]
    reserved = ledger.execute("SELECT count(*) FROM memecoin_pending_entries WHERE desk_cohort_id=? AND status='reserved'", (cohort_id,)).fetchone()[0]
    queued = ledger.execute("SELECT count(*) FROM memecoin_manual_intents WHERE desk_ticket_id LIKE 'lb:%' AND state IN ('queued','claimed')").fetchone()[0]
    return fills + reserved + queued


def cycle(arm, ledger, *, cohort_id, decimals_fn, sol_fn, enqueue, now, max_fills, seen, counts, min_others=0, proof_fn=None):
    """One pass. Advisory prechecks; shared enqueue atomically owns the durable admission cap."""
    rows = arm.execute("SELECT * FROM triggers WHERE entry_due<=? AND entry_due>=? ORDER BY entry_due", (now, now - LATE_S - 1)).fetchall()
    for row in rows:
        t = dict(row)
        ticket = "lb:%s:%s" % (t["pool"], t["wallet"])
        if ticket in seen:
            continue
        fact=proof_fn(t['mint']) if proof_fn else None
        decimals=fact.get('decimals') if isinstance(fact,dict) else None if cohort_id==CROWD_COHORT_ID else decimals_fn(t['mint'])
        sol=sol_fn(now)
        why = _skip(t, ledger, cohort_id, lambda _:decimals, lambda _:sol, now, max_fills, min_others)
        if why:
            if why not in ('decimals_unknown','sol_unknown'):seen.add(ticket)
            counts[why] = counts.get(why, 0) + 1
            continue
        seen.add(ticket)  # All enqueue attempts and non-transient decisions are final for this receipt.
        request = request_for(t, cohort_id=cohort_id, decimals=decimals, sol_usd=sol, now=now, mint_metadata=fact)
        if request is None:
            counts["reserves_invalid"] = counts.get("reserves_invalid", 0) + 1
            continue
        try:
            enqueue(ledger, **request)
            counts["sent"] = counts.get("sent", 0) + 1
        except (ValueError, sqlite3.Error) as exc:
            key = "enqueue_refused:" + type(exc).__name__
            counts[key] = counts.get(key, 0) + 1


def _skip(t, ledger, cohort_id, decimals_fn, sol_fn, now, max_fills, min_others=0):
    if type(t.get("entry_due")) not in (int,float) or not math.isfinite(t["entry_due"]) or not 0<=now-t["entry_due"]<LATE_S:
        return "late"
    if cohort_id==CROWD_COHORT_ID:
        trigger_at,detected_at=t.get('trigger_at'),t.get('detected_at')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (trigger_at,detected_at)):return 'crowd_proof_unknown'
        if t['entry_due']!=trigger_at+10 or not 0<=detected_at-trigger_at<=5:return 'crowd_proof_unknown'
        lag=t.get('detect_lag_s')
        if (min_others!=1 or t.get('crowd_state')!='known' or type(t.get('crowd_others_60s')) is not int or t['crowd_others_60s']<1
                or type(lag) not in (int,float) or not math.isfinite(lag) or not 0<=lag<=5 or abs(lag-(detected_at-trigger_at))>1e-6):return 'crowd_proof_unknown'
        if (type(t.get('source_slot')) is not int or t['source_slot']<=0 or type(t.get('source_tx_index')) is not int or t['source_tx_index']<=0
                or type(t.get('source_ordinal')) is not int or t['source_ordinal']<0 or not isinstance(t.get('source_signature'),str) or not 0<len(t['source_signature'])<=128 or t['source_signature']!=t['signature']):return 'crowd_order_unknown'
    if min_others > 0 and cohort_id!=CROWD_COHORT_ID and (type(t.get("others_60s")) is not int or t["others_60s"] < min_others):
        return "too_few_others"            # an unknown count never passes a requirement
    if not str(t["pool_quote_before"] or "").isdigit() or int(t["pool_quote_before"]) < POOL_FLOOR_LAMPORTS:
        return "pool_under_floor_or_unknown"
    if not str(t["post_quote"] or "").isdigit() or not str(t["post_base"] or "").isdigit():
        return "reserves_unknown"
    decimals = decimals_fn(t["mint"])
    if type(decimals) is not int or not 0 <= decimals <= 18:
        return "decimals_unknown"
    sol = sol_fn(now)
    if type(sol) not in (int, float) or not math.isfinite(sol) or sol <= 0:
        return "sol_unknown"
    if fills_and_queued(ledger, cohort_id) >= max_fills:
        return "max_fills"
    return None


def iso(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def run(*, arm_path, ledger_path, desk_dir, period_s=1.0, until=None, log=print, metadata_path=None):
    from pathlib import Path
    import sniper.settings.memecoin as cfg
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_manual_intents as intents
    if not cfg.MEMECOIN_LARGE_BUYER_ENABLED:
        log("large buyer pilot: MEMECOIN_LARGE_BUYER_ENABLED is off")
        return 0
    ledger = sqlite3.connect(ledger_path, timeout=2)
    cohort_id = m.register_desk_cohort(ledger, producer=PRODUCER, policy=POLICY, prompt_version=PROMPT_VERSION, created_at=iso(real_utc_now()), cohort_id=CROWD_COHORT_ID)
    ledger.commit()
    arm = sqlite3.connect(Path(arm_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    arm.row_factory = sqlite3.Row

    metadata=None
    try:metadata=sqlite3.connect(Path(metadata_path or Path(desk_dir,'pumpswap/metadata.db')).resolve().as_uri()+'?mode=ro',uri=True,timeout=0.1)
    except sqlite3.Error:pass

    def proof(mint):
        nonlocal metadata
        if metadata is None:
            try:metadata=sqlite3.connect(Path(metadata_path or Path(desk_dir,'pumpswap/metadata.db')).resolve().as_uri()+'?mode=ro',uri=True,timeout=0.1)
            except sqlite3.Error:return None
        return mint_proof(metadata,mint,now=time.time())

    def decimals(mint):
        fact=proof(mint)
        return fact['decimals'] if fact else None

    def sol(now):
        return read_sol_usd(Path(desk_dir,'universe.heartbeat'),now=now)

    def enqueue(conn, **request):
        queued=queue_request(request,host_now=time.time())
        if queued is None:raise ValueError('pilot entry window elapsed')
        return intents.enqueue(conn,**queued)

    seen, counts = set(), {}
    log('large buyer pilot: started cohort=%s min_others=%s max_admissions=%s' %
        (cohort_id,cfg.MEMECOIN_LARGE_BUYER_MIN_OTHERS,cfg.MEMECOIN_LARGE_BUYER_MAX_FILLS))
    next_report=monotonic()+30
    while until is None or time.time() < until:
        now = time.time()
        try:
            cycle(arm, ledger, cohort_id=cohort_id, decimals_fn=decimals, sol_fn=sol, enqueue=enqueue, now=now, max_fills=cfg.MEMECOIN_LARGE_BUYER_MAX_FILLS, min_others=cfg.MEMECOIN_LARGE_BUYER_MIN_OTHERS, proof_fn=proof, seen=seen, counts=counts)
        except sqlite3.Error as exc:
            log("large buyer pilot: read failed %s" % type(exc).__name__)
        if monotonic()>=next_report:
            log('large buyer pilot: heartbeat '+json.dumps(dict(cohort_id=cohort_id,host_now=time.time(),counts=counts,count_basis='attempts'),sort_keys=True))
            next_report=monotonic()+30
        time.sleep(period_s)
    arm.close()
    if metadata is not None:metadata.close()
    ledger.close()
    log("large buyer pilot: %s" % counts)
    return 0
