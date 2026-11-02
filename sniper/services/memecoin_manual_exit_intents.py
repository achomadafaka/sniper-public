"""The operator's manual SELL crosses to the runner as an INTENT. Only the runner sells.

WHY THIS EXISTS. `memecoin_manual_intents` closed this gap for BUYS on 2026-08-26: the web server
captures settings at import and deploys do not restart it, so money decided in the web process runs
on a stale snapshot (position 44706). The manual close and partial-sell buttons still executed
INSIDE the web process through `make_venue_exit_fn` -- the same class of divergence, on the exit
side, where a stale RPC pool state or exit policy is a stuck bag rather than a wrong entry. Audited
2026-09-05: every rail exit already runs in the runner; only the two operator buttons did not.

AN INTENT CARRIES INPUTS, NEVER A DECISION -- position id, action, fraction, who asked. The runner
re-reads the position at claim time, routes by venue with ITS exit executors, and books the outcome.
The web writes a row and reports `queued`; the dashboard polls the intent for the result.

Exits are never gated by the kill switch, and this queue is not either. The retry ladder the web
used to run in-request (2/5/10/30 s on a stuck ladder with no signature) is kept, but as RE-QUEUES
across runner passes: a funded loop must never sleep for an operator's convenience.
"""
from __future__ import annotations

import logging
import hashlib
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from sniper.services.memecoin_onchain_wake import LaneWake, default_address, send_wake

logger = logging.getLogger(__name__)

QUEUED, CLAIMED, DONE, REFUSED, EXPIRED = "queued", "claimed", "done", "refused", "expired"
TERMINAL = frozenset({DONE, REFUSED, EXPIRED})
ACTIONS = ("close", "partial", "close_any_price")
# The anchor-checked EVM exit (spec 2026-10-01) holds a low sell quote and returns at once. A held
# manual sell is retried on the runner's next pass, never dropped; the executor's own bound (HOLD_S
# and >= 2 held attempts) ends the hold, and the intent's TTL bounds it all.
HELD_RETRY_S = 1.0
DEFAULT_TTL_S = 180.0          # longer than a buy: the retry ladder alone spans 47 s
CLAIM_STALE_S = 90.0
RETRY_AFTER_S = (2.0, 5.0, 10.0, 30.0)     # the web's observed provider-recovery window, unchanged
# A close behind an unknown-confirm partial retries its settle read every HELD_RETRY_S for this long
# after the operator asked, then refuses derisk_unconfirmed (the recovery pass still owns it).
# ponytail: measured from requested_at, so a queue delay counts against it; the first read always runs.
SETTLE_WAIT_S = 3.0
TABLE = "memecoin_manual_exit_intents"


def _source_integer(value):
    if type(value) is int:
        return value
    if type(value) is str and value.isascii() and value.isdecimal():
        return int(value)
    raise ValueError('source quantity must be an integer')


def enqueue_source_partial(conn, *, position_id, network_id, source_owner, mint,
                           source_signature, source_event_index, source_sold_atomic,
                           source_pre_atomic, now_iso, source_proof=None):
    """Freeze one source sale. Duplicates may strengthen proof, never change its ratio or owner."""
    from sniper.persistence import db_memecoin as m
    network = m.normalized_coin_network(network_id, mint)
    sold, pre = _source_integer(source_sold_atomic), _source_integer(source_pre_atomic)
    if not 0 < sold <= pre:
        raise ValueError('source sale requires 0 < sold <= pre')
    if type(source_event_index) is not int or source_event_index < 0:
        raise ValueError('source event index must be a nonnegative integer')
    if any(type(v) is not str or not v.strip() for v in (source_owner, mint, source_signature)):
        raise ValueError('source identity is missing')
    if network != 'solana':
        source_owner, mint = source_owner.lower(), mint.lower()
    position = m.get_position(conn, int(position_id))
    if (not position or position.get('execution_mode') != 'real'
            or m.normalized_coin_network(position.get('network_id'), position.get('mint')) != network
            or (position['mint'] if network == 'solana' else position['mint'].lower()) != mint):
        raise ValueError('source partial position identity mismatch')
    facts = dict(position_id=int(position_id), network_id=network, source_owner=source_owner,
                 mint=mint, source_signature=source_signature, source_event_index=source_event_index,
                 source_sold_atomic=str(sold), source_pre_atomic=str(pre),
                 source_user_id=position.get('source_user_id'))
    key = {k:v for k,v in facts.items() if k not in ('source_sold_atomic','source_pre_atomic','source_user_id')}
    iid = 'source-' + hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    previous = conn.execute(f"SELECT request_json FROM {TABLE} WHERE intent_id=?", (iid,)).fetchone()
    if previous:
        saved = json.loads(previous[0])
        if any(saved.get(k) != v for k,v in facts.items()):
            raise ValueError('source request identity reused with different quantities')
        return iid
    facts['source_proof'] = source_proof
    conn.execute(f"INSERT OR IGNORE INTO {TABLE}(intent_id,requested_at,expires_at,position_id,"
                 "action,fraction,actor,state,attempts,request_json) VALUES(?,?,?,?,?,?,?,?,0,?)",
                 (iid, now_iso, now_iso, position_id, 'source_partial', sold/pre, 'source', QUEUED,
                  json.dumps(facts, sort_keys=True, allow_nan=False)))
    conn.commit()
    # A concurrent insert has the same immutable check as an ordinary duplicate.
    saved = json.loads(conn.execute(f"SELECT request_json FROM {TABLE} WHERE intent_id=?", (iid,)).fetchone()[0])
    if any(saved.get(k) != v for k,v in facts.items() if k != 'source_proof'):
        raise ValueError('source request identity reused with different quantities')
    try:
        send_wake(default_address() + '-source-partials')
    except Exception:
        pass
    return iid


def source_pending_count(conn):
    return conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE action='source_partial' "
                        "AND state IN ('queued','claimed','submitted_unknown')").fetchone()[0]


def drain_source_partials(conn, source_partial_fn, *, now_fn, position_fn, max_per_pass=2):
    """Off-entry source requests. The executor atomically claims; signed ambiguity never requeues."""
    def full_ratio(alias):
        return (f"COALESCE(CASE WHEN json_valid({alias}.request_json) THEN "
                f"json_extract({alias}.request_json,'$.source_sold_atomic')="
                f"json_extract({alias}.request_json,'$.source_pre_atomic') AND "
                f"json_extract({alias}.request_json,'$.source_pre_atomic')<>'0' END,0)")
    is_full, later_full = full_ratio('x'), full_ratio('later')
    done = []
    for _ in range(int(max_per_pass)):
        raw = conn.execute(f"SELECT x.* FROM {TABLE} x WHERE action='source_partial' AND state='queued' "
            "AND (not_before IS NULL OR not_before<=?) AND NOT EXISTS "
            f"(SELECT 1 FROM {TABLE} earlier WHERE earlier.position_id=x.position_id AND earlier.rowid<x.rowid "
            "AND earlier.action='source_partial' AND (earlier.state IN ('claimed','submitted_unknown') "
            f"OR (earlier.state='queued' AND NOT ({is_full})))) "
            # A later full sale achieves zero remainder even when an older trim cannot route.
            # Existing claimed/signed work above remains an absolute exclusion.
            f"AND (({is_full}) OR NOT EXISTS (SELECT 1 FROM {TABLE} later "
            "WHERE later.position_id=x.position_id AND later.rowid>x.rowid "
            "AND later.action='source_partial' AND later.state IN ('queued','claimed','submitted_unknown') "
            f"AND ({later_full}))) "
            "ORDER BY requested_at,rowid LIMIT 1", (now_fn(),)).fetchone()
        if raw is None:
            break
        request = dict(raw)
        pos = position_fn(conn, request['position_id'])
        if not pos or not pos.get('is_open'):
            resolve(conn, request['intent_id'], state=DONE, now_iso=now_fn(), result_status='already_closed')
            done.append((request['intent_id'], DONE))
            continue
        if pos.get('mirror_disabled') or pos.get('tracking_paused_at'):
            conn.execute(f"UPDATE {TABLE} SET not_before=?,reason=? WHERE intent_id=? AND state='queued'",
                         (_plus(now_fn(), HELD_RETRY_S), 'source_mirror_held', request['intent_id']))
            conn.commit()
            done.append((request['intent_id'], QUEUED))
            continue
        try:
            result = source_partial_fn(conn, pos, request, now_fn()) or {}
        except Exception:
            logger.exception('source partial executor failed intent=%s', request['intent_id'])
            result = {'status':'partial_pending'}
        current = read_status(conn, request['intent_id'])
        status, sig = result.get('status'), result.get('sig') or result.get('tx_hash')
        if current and current['state'] == QUEUED and status in ('closed','gone','partial_sold','already_closed'):
            resolve(conn, request['intent_id'], state=DONE, now_iso=now_fn(),
                    result_status=status, sig=sig)
            current = read_status(conn, request['intent_id'])
        elif current and current['state'] == QUEUED and sig:
            fresh = position_fn(conn, request['position_id']) or {}
            claim = fresh.get('close_claim_id') if fresh.get('close_sig') == sig else None
            conn.execute(f"UPDATE {TABLE} SET state='submitted_unknown',sig=?,claim_id=?,"
                         "reason='signed source exit pending recovery' WHERE intent_id=? AND state='queued'",
                         (sig, claim, request['intent_id']))
            conn.commit()
            current = read_status(conn, request['intent_id'])
        # Only the executor/recoverer may release active ownership. A plain exception is not proof.
        if current and current['state'] == QUEUED:
            conn.execute(f"UPDATE {TABLE} SET not_before=?,reason=? WHERE intent_id=? AND state='queued'",
                         (_plus(now_fn(), HELD_RETRY_S), str(result.get('reason') or result.get('status')),
                          request['intent_id']))
            conn.commit()
        done.append((request['intent_id'], current['state'] if current else REFUSED))
    return done


def start_source_partial_worker(*, connect, observe, source_partial_fn, recovery_fn, now_fn,
                                position_fn, max_per_pass=2):
    def drain_fn(conn):
        recovery_fn(conn, now_fn())
        return drain_source_partials(conn, source_partial_fn, now_fn=now_fn,
                                     position_fn=position_fn, max_per_pass=max_per_pass)
    worker = ManualExitWorker(connect=connect, observe=observe, drain_fn=drain_fn,
        pending_fn=source_pending_count, name='source-partials',
        wake=LaneWake(default_address() + '-source-partials'))
    worker.start()
    return worker


class SourcePartialExecution:
    """The partial executors' alternate ledger lifecycle. One-shot defaults remain unchanged."""

    def __init__(self, conn, position, request):
        from sniper.persistence import db_memecoin as m
        self.db, self.conn, self.position = m, conn, position
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE intent_id=? AND action='source_partial' "
                           "AND position_id=?", (request['intent_id'], position['id'])).fetchone()
        if row is None or row['request_json'] != request['request_json']:
            raise ValueError('source request is not the frozen journal row')
        self.request_id, self.facts = row['intent_id'], json.loads(row['request_json'])
        current = m.get_position(conn, position['id'])
        network = m.normalized_coin_network(current.get('network_id'), current.get('mint')) if current else None
        mint = current.get('mint') if current else None
        if network != 'solana' and isinstance(mint, str):
            mint = mint.lower()
        if (not current or network != self.facts.get('network_id') or mint != self.facts.get('mint')
                or current.get('source_user_id') != self.facts.get('source_user_id')):
            raise ValueError('source journal no longer owns this position identity')
        self.position = current
        self.sold, self.pre = int(self.facts['source_sold_atomic']), int(self.facts['source_pre_atomic'])
        if not 0 < self.sold < self.pre:
            raise ValueError('source partial needs a strict partial integer ratio')
        # This float describes the fill only; signing always uses the exact integer ratio.
        self.fraction = min(self.sold / self.pre, math.nextafter(1.0, 0.0))
        self.atomic, self.decimals, self.part = None, None, None

    def read_tokens(self, snapshot_fn):
        if snapshot_fn is None:
            raise ValueError('exact partial balance unavailable')
        snap = snapshot_fn(self.position['mint'])
        if (type(snap.atomic) is not int or snap.atomic < 0 or type(snap.decimals) is not int
                or not 0 <= snap.decimals <= 255):
            raise ValueError('invalid exact partial balance')
        if self.decimals is not None and snap.decimals != self.decimals:
            raise ValueError('partial token decimals changed')
        if self.db.shared_mint_open_real_count(self.conn, self.position['mint'], self.position.get('network_id')) > 1:
            raise ValueError('shared inventory has no exact position allocation')
        return snap.atomic, snap.decimals, snap.human

    def snapshot(self, snapshot_fn):
        self.atomic, self.decimals, human = self.read_tokens(snapshot_fn)
        self.part = self.atomic * self.sold // self.pre
        return human

    def intent_fields(self):
        return dict(source_request_id=self.request_id, balance_snapshot_atomic=str(self.atomic),
                    sell_decimals=self.decimals)

    def receipt(self, fn, sig, mint, quote):
        if fn is None:
            return None
        try:
            proof = fn(sig, mint, {'sell_decimals': self.decimals})
            if (not isinstance(proof, dict) or type(proof.get('sold_atomic')) is not int
                    or proof['sold_atomic'] != int(quote.get('inAmount', quote.get('sellAmount', 0)))
                    or type(proof.get('decimals')) is not int or proof['decimals'] != self.decimals
                    or isinstance(proof.get('received'), bool)
                    or not math.isfinite(float(proof['received'])) or float(proof['received']) <= 0):
                return None
            return proof
        except Exception:
            logger.exception('source partial receipt unavailable sig=%s', sig)
            return None

    def get_position(self, *args, **kwargs):
        return self.db.get_position(*args, **kwargs)

    def bind_exit_intent(self, *args, **kwargs):
        return self.db.bind_exit_intent(*args, **kwargs)

    def set_exit_intent_status(self, *args, **kwargs):
        return self.db.set_exit_intent_status(*args, **kwargs)

    def claim_position_derisk(self, conn, pid, at):
        return self.db.claim_source_partial(conn, pid, self.request_id, at)

    def release_position_derisk_claim(self, *args, **kwargs):
        return self.db.release_source_partial(*args, **kwargs)

    def finish_noop(self, *args, **kwargs):
        return self.db.finish_source_partial_noop(*args, **kwargs)

    def mark_position_derisk_submitted(self, *args, **kwargs):
        return self.db.mark_source_partial_submitted(*args, **kwargs)

    def reset_position_derisk_submission(self, *args, **kwargs):
        return self.db.reset_source_partial_submission(*args, **kwargs)

    def book_derisk_fill(self, *args, **kwargs):
        return self.db.book_source_partial_fill(*args, **kwargs)


def wake_address():
    return default_address() + "-manual-exits"


def _parse(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None


def _plus(iso, seconds):
    t = _parse(iso)
    return (t + timedelta(seconds=float(seconds))).isoformat() if t else iso


def enqueue(conn, *, position_id, action, now_iso, fraction=None, actor="operator",
            ttl_s=DEFAULT_TTL_S, intent_id=None):
    """Record the operator's request. Returns the intent id. Never sells anything."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
    if action == "partial":
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            raise ValueError("partial sell needs a numeric fraction")
        if not (0.0 < fraction < 1.0):
            raise ValueError("fraction must be in (0,1) -- use close for a full exit")
    else:
        fraction = 1.0
    intent_id = intent_id or str(uuid.uuid4())
    conn.execute(
        f"INSERT INTO {TABLE}(intent_id, requested_at, expires_at, position_id, action, fraction, "
        "actor, state, attempts) VALUES (?,?,?,?,?,?,?,?,0)",
        (intent_id, now_iso, _plus(now_iso, ttl_s), int(position_id), action, fraction,
         str(actor or "operator"), QUEUED))
    conn.commit()
    try:
        send_wake(wake_address())
    except Exception:
        pass                                # the worker's poll covers a lost wake
    return intent_id


def claim_next(conn, *, now_iso, claim_id=None):
    claim_id = claim_id or uuid.uuid4().hex
    row = conn.execute(
        f"SELECT * FROM {TABLE} WHERE state=? AND action!='source_partial' AND expires_at>? AND (not_before IS NULL OR not_before<=?) "
        "ORDER BY requested_at LIMIT 1", (QUEUED, now_iso, now_iso)).fetchone()
    if row is None:
        return None
    changed = conn.execute(
        f"UPDATE {TABLE} SET state=?, claim_id=?, claimed_at=? WHERE intent_id=? AND state=? AND expires_at>?",
        (CLAIMED, claim_id, now_iso, row["intent_id"], QUEUED, now_iso)).rowcount
    conn.commit()
    if not changed:
        return None
    return dict(row, state=CLAIMED, claim_id=claim_id, claimed_at=now_iso)


def requeue_for_retry(conn, intent_id, *, now_iso, attempts, until_ttl=False):
    """A stuck ladder with NO signature may be retried on a later pass, on the web's old schedule.
    Returns the not_before stamp, or None when the schedule is exhausted (the caller resolves).
    `until_ttl` keeps the last step instead, so only the intent's TTL ends the order."""
    if attempts >= len(RETRY_AFTER_S) and not until_ttl:
        return None
    not_before = _plus(now_iso, RETRY_AFTER_S[min(attempts, len(RETRY_AFTER_S) - 1)])
    conn.execute(
        f"UPDATE {TABLE} SET state=?, claim_id=NULL, claimed_at=NULL, attempts=?, not_before=? "
        "WHERE intent_id=? AND state=?", (QUEUED, attempts + 1, not_before, intent_id, CLAIMED))
    conn.commit()
    return not_before


def requeue_held(conn, intent_id, *, now_iso):
    """A held sell (`held_anchor`) goes back to the queue for the next pass. It does not spend the
    stuck-ladder retry budget: nothing was signed and the hold is bounded by the executor."""
    conn.execute(
        f"UPDATE {TABLE} SET state=?, claim_id=NULL, claimed_at=NULL, not_before=? "
        "WHERE intent_id=? AND state=?", (QUEUED, _plus(now_iso, HELD_RETRY_S), intent_id, CLAIMED))
    conn.commit()


def resolve(conn, intent_id, *, state, now_iso, result_status=None, reason=None, realized=None, sig=None):
    if state not in TERMINAL:
        raise ValueError(f"{state} is not a terminal intent state")
    changed = conn.execute(
        f"UPDATE {TABLE} SET state=?, resolved_at=?, result_status=?, reason=?, realized=?, sig=? "
        "WHERE intent_id=? AND state NOT IN (?,?,?)",
        (state, now_iso, result_status, reason, realized, sig, intent_id, DONE, REFUSED, EXPIRED)).rowcount
    conn.commit()
    return bool(changed)


def expire_stale(conn, *, now_iso):
    expired = conn.execute(
        f"UPDATE {TABLE} SET state=?, resolved_at=?, reason='not claimed before the intent expired' "
        "WHERE state=? AND action!='source_partial' AND expires_at<=?", (EXPIRED, now_iso, QUEUED, now_iso)).rowcount
    cutoff = _parse(now_iso)
    released = 0
    if cutoff is not None:
        stale = (cutoff - timedelta(seconds=CLAIM_STALE_S)).isoformat()
        expired += conn.execute(
            f"UPDATE {TABLE} SET state=?, resolved_at=?, claim_id=NULL, claimed_at=NULL, "
            "reason='the claiming runner died and the intent expired before another could take it' "
            "WHERE state=? AND action!='source_partial' AND claimed_at<=? AND expires_at<=?", (EXPIRED, now_iso, CLAIMED, stale, now_iso)).rowcount
        released = conn.execute(
            f"UPDATE {TABLE} SET state=?, claim_id=NULL, claimed_at=NULL WHERE state=? AND action!='source_partial' AND claimed_at<=? AND expires_at>?",
            (QUEUED, CLAIMED, stale, now_iso)).rowcount
    conn.commit()
    return {"expired": expired, "released": released}


def read_status(conn, intent_id):
    row = conn.execute(
        f"SELECT intent_id, state, requested_at, expires_at, claimed_at, resolved_at, position_id, action, "
        f"fraction, actor, attempts, not_before, result_status, reason, realized, sig, request_json FROM {TABLE} WHERE intent_id=?",
        (intent_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if (result["state"] == DONE and result["action"] == "partial" and result["sig"]
            and result["result_status"] in ("unknown", "submitted_unknown", "pending", "stuck")):
        # Recovery books the exact signature later; reading status must never requeue a sale.
        fill = conn.execute(
            "SELECT 1 FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
            "WHERE f.position_id=? AND f.tx_sig=? AND f.kind='derisk' "
            "AND f.qty>0 AND f.price>=0 AND f.notional>=0 AND p.execution_mode='real' LIMIT 1",
            (result["position_id"], result["sig"])).fetchone()
        if fill:
            result["result_status"] = "derisked"
    return result


def pending_count(conn):
    row = conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE action!='source_partial' AND state IN (?,?)", (QUEUED, CLAIMED)).fetchone()
    return int(row[0]) if row else 0


def _busy_budget_s():
    """How long one pass may legitimately run (Codex CORE-02): `drain` takes at most 2 intents, each
    a serialized exit that may wait for the wallet lock and then a ~90 s confirm. Default 300 s."""
    try:
        return max(1.0, float(os.getenv("MEMECOIN_MANUAL_EXIT_BUSY_BUDGET_S", "300")))
    except ValueError:
        return 300.0


class ManualExitWorker:
    """Own manual sells independently of feed/maintenance waits; idle reads stay read-only."""

    def __init__(self, *, connect, observe, drain_fn, wake=None, clock=time.monotonic,
                 pending_fn=None, name="manual-exits"):
        self.connect, self.observe, self.drain_fn, self.clock = connect, observe, drain_fn, clock
        self.pending_fn = pending_fn or pending_count
        self.wake = wake if wake is not None else LaneWake(wake_address())
        self.stopping = threading.Event()
        self.pass_lock = threading.Lock()
        self.pass_started = None             # monotonic start of the pass holding pass_lock
        self.last_pass = None
        self.error = None
        self.thread = threading.Thread(target=self.run, name=name, daemon=True)

    def _pass(self, conn):
        if not self.pass_lock.acquire(blocking=False):
            return []
        self.pass_started = self.clock()
        try:
            return self.drain_fn(conn) or []
        finally:
            self.pass_started = None
            self.pass_lock.release()

    def _busy_age(self):
        started = self.pass_started
        return None if started is None else self.clock() - started

    def stalled(self):
        """A pass past its progress budget (or with a negative age). The lock still excludes a
        second drain: this only makes the stall visible, never steals the pass."""
        age = self._busy_age()
        return age is not None and not 0.0 <= age < _busy_budget_s()

    def healthy(self):
        if not (self.thread.is_alive() and self.error is None and self.last_pass is not None):
            return False
        if self._busy_age() is not None:
            return not self.stalled()
        return 0.0 <= self.clock() - self.last_pass < 5.0

    def fallback(self, conn):
        return [] if self.healthy() else self._pass(conn)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        self.thread.join(timeout=3.0)

    def run(self):
        observer = None
        try:
            while not self.stopping.is_set():
                try:
                    if observer is None:
                        observer = self.observe()
                    if self.pending_fn(observer):
                        conn = self.connect()
                        try:
                            self._pass(conn)
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
                        finally:
                            conn.close()         # no writable handle/snapshot survives the wait
                    self.last_pass, self.error = self.clock(), None
                except Exception as exc:
                    self.error = type(exc).__name__
                    logger.exception("manual exit worker failed")
                    if observer is not None:
                        observer.close()
                        observer = None
                self.wake.wait(1.0)
                self.wake.clear()
        finally:
            if observer is not None:
                observer.close()
            if hasattr(self.wake, "close"):
                self.wake.close()

    def status(self):
        return {"alive": self.thread.is_alive(), "healthy": self.healthy(),
                "last_pass_age_s": None if self.last_pass is None else
                round(max(0.0, self.clock() - self.last_pass), 3),
                "busy": self.pass_lock.locked(), "error": self.error,
                "busy_age_s": None if self._busy_age() is None else round(self._busy_age(), 3),
                "stalled": self.stalled(),
                "wake": dict(getattr(self.wake, "stats", {}))}


def make_enqueue_exit_fn(now_fn, *, ttl_s=DEFAULT_TTL_S):
    """The WEB's replacement for the in-process exit executors. Writes a row, reports `queued`."""
    def enqueue_exit_fn(conn, pos, action, fraction=None, *, actor="operator"):
        try:
            intent_id = enqueue(conn, position_id=pos["id"], action=action, fraction=fraction,
                                actor=actor, now_iso=now_fn(), ttl_s=ttl_s)
        except ValueError as exc:
            return {"status": "error", "reason": str(exc)}
        except Exception as exc:
            logger.exception("could not enqueue a manual exit intent for position %s", pos.get("id"))
            return {"status": "error", "reason": f"could not queue the request: {exc}"}
        return {"status": "queued", "intent_id": intent_id, "position_id": pos["id"], "action": action,
                "detail": "the runner sells; poll the intent for the outcome"}
    return enqueue_exit_fn


def drain(conn, exit_fn, partial_fn, *, now_fn, position_fn, max_per_pass=2,
          notify_fn=None, demotion_fn=None, settle_fn=None):
    """Runner side: claim and execute queued exits with the RUNNER's executors. Bounded per pass so
    a backlog never starves the rail sweep it runs beside.

    `settle_fn(conn, pos)` (memecoin_real_runner.settle_derisk_for_manual_close) runs before a full
    close whose position carries a de-risk claim or sig: None proceeds as before, 'confirmed' /
    'failed' re-read the position and close it, 'unknown' waits SETTLE_WAIT_S then refuses."""
    done = []
    expire_stale(conn, now_iso=now_fn())
    for _ in range(int(max_per_pass)):
        intent = claim_next(conn, now_iso=now_fn())
        if intent is None:
            break
        iid, action, pid = intent["intent_id"], intent["action"], int(intent["position_id"])
        pos = position_fn(conn, pid)
        if not pos or not pos.get("is_open"):
            resolve(conn, iid, state=REFUSED, now_iso=now_fn(), result_status="not_open",
                    reason="position is no longer open")
            done.append((iid, REFUSED)); continue
        if (pos.get("execution_mode") or "paper") != "real":
            resolve(conn, iid, state=REFUSED, now_iso=now_fn(), result_status="not_real",
                    reason="not a real position")
            done.append((iid, REFUSED)); continue
        if action == 'known_group_close':
            from sniper.services.memecoin_known_group_canary import _exit_refusal
            try:
                payload = json.loads(intent['request_json'])
                refusal = _exit_refusal(pos,payload)
                from sniper.persistence import db_memecoin as ledger
                if not refusal and not ledger.known_group_exit_source_matches(conn,payload):
                    refusal = 'known_group_exit_source_unbacked'
            except (ValueError, TypeError, KeyError):
                refusal = 'known_group_exit_proof_invalid'
            if refusal:
                resolve(conn, iid, state=REFUSED, now_iso=now_fn(), result_status='rejected', reason=refusal)
                done.append((iid, REFUSED)); continue
        elif action not in ACTIONS:
            resolve(conn, iid, state=REFUSED, now_iso=now_fn(), result_status='rejected', reason='unknown_exit_action')
            done.append((iid, REFUSED)); continue
        res = None
        if (settle_fn is not None and action != "partial"
                and (pos.get("derisk_claim_id") or pos.get("derisk_sig"))):
            try:
                settled = settle_fn(conn, pos)
            except Exception:
                logger.exception("manual exit intent %s: partial settle failed", iid)
                settled = "unknown"           # nothing settled: wait, never sell past it
            if settled == "unknown":
                start, now = _parse(intent.get("requested_at")), _parse(now_fn())
                if start is not None and now is not None and (now - start).total_seconds() < SETTLE_WAIT_S:
                    requeue_held(conn, iid, now_iso=now_fn())
                    done.append((iid, QUEUED)); continue
                res = {"status": "derisk_unconfirmed",
                       "reason": "partial sell %s is unconfirmed; the recovery pass settles it"
                                 % (pos.get("derisk_sig") or "unsigned")}
            elif settled is not None:
                pos = position_fn(conn, pid)
                if not pos or not pos.get("is_open"):
                    resolve(conn, iid, state=REFUSED, now_iso=now_fn(), result_status="not_open",
                            reason="position is no longer open")
                    done.append((iid, REFUSED)); continue
        from sniper.services import memecoin_exit_anchor
        try:
            if res is None and action == "partial":
                res = partial_fn(conn, pos, float(intent["fraction"]), now_fn()) or {}
            elif res is None:
                if action == "close_any_price":
                    memecoin_exit_anchor.request_override(pid)
                res = exit_fn(conn, pos, 'known_group_sell' if action == 'known_group_close' else 'manual', now_fn()) or {}
        except Exception as exc:
            logger.exception("manual exit intent %s failed in the runner", iid)
            resolve(conn, iid, state=REFUSED, now_iso=now_fn(), result_status="error",
                    reason=f"{type(exc).__name__}: {exc}")
            done.append((iid, REFUSED)); continue
        finally:
            if action != 'known_group_close':
                memecoin_exit_anchor.clear_override(pid)     # an unconsumed operator override never lingers
        status = str(res.get("status") or "")
        sig = res.get("sig") or res.get("tx_hash")
        if action != "partial" and status == "already_closing" and sig:
            settled = position_fn(conn, pid)
            if (settled and settled.get("is_open") == 0 and settled.get("closed_at")
                    and settled.get("close_sig") == sig):
                status = "closed"
                res = dict(res, status=status, realized=settled.get("realized_pnl"))
        if status == "deferred" and res.get("reason") == "held_anchor":
            requeue_held(conn, iid, now_iso=now_fn())
            done.append((iid, QUEUED)); continue
        if status == "pending" and not sig:
            # A budgeted exit resumes its journal next pass; expiry still bounds unsigned work.
            requeue_held(conn, iid, now_iso=now_fn())
            done.append((iid, QUEUED)); continue
        if action == "close_any_price" and not sig and status not in (
                "closed", "gone", "unknown", "submitted_unknown"):
            # "Sell at any price" stays the operator's order until a sale lands or the intent's TTL
            # ends it (Gamma LOW 3 on e0faedf70): an attempt that signed nothing is tried again, on
            # the stuck schedule rather than every second (Gamma LOW on d3d96ab9c: up to 180 full
            # ladders on a coin with no route).
            requeue_for_retry(conn, iid, now_iso=now_fn(),
                              attempts=int(intent.get("attempts") or 0), until_ttl=True)
            done.append((iid, QUEUED)); continue
        if status == "stuck" and not sig:
            nb = requeue_for_retry(conn, iid, now_iso=now_fn(), attempts=int(intent.get("attempts") or 0))
            if nb is not None:
                done.append((iid, QUEUED)); continue
        label = f"{pos.get('ticker') or pos.get('mint')} {pos.get('network_id')}"
        if status in ("closed", "gone", "derisked"):
            state, rs = DONE, status
            if demotion_fn is not None and status in ("closed", "gone"):
                try:
                    demotion_fn(conn, pos.get("source_user_id"), network_id=pos.get("network_id"))
                except Exception:
                    logger.exception("wallet demotion check failed after manual close %s", pid)
        elif status in ("unknown", "submitted_unknown") or (status in ("stuck", "pending") and sig):
            state, rs = DONE, status          # a signature can identify a broadcast: never retried
        else:
            state, rs = REFUSED, status or "error"
        reason = res.get("reason") or res.get("error") or res.get("detail")
        resolve(conn, iid, state=state, now_iso=now_fn(), result_status=rs, reason=reason,
                realized=res.get("realized"), sig=sig)
        if notify_fn is not None:
            try:
                notify_fn(f"{'STRATEGY' if action == 'known_group_close' else 'MANUAL'} {action.upper()} pos#{pid}",
                          f"{label} status={status} realized={res.get('realized')} intent={iid[:8]}")
            except Exception:
                pass
        done.append((iid, state))
    return done
