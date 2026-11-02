"""Durable notification state. Only the Telegram worker calls these functions."""
from __future__ import annotations

import hashlib
import logging
import os
import json
import re
import sqlite3
import time
from datetime import datetime

logger = logging.getLogger(__name__)

UNRESOLVED = frozenset({"transaction_unresolved", "submitted_unknown", "needs_review"})
PENDING = frozenset({"source_received", "evaluating", "not_evaluated"})
TRACKED = PENDING | UNRESOLVED | {"retry_pending"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_notification_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    activation_cutoff TEXT NOT NULL,
    last_scan_at TEXT,
    scan_cursors_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS memecoin_notification_transitions (
    transition_key TEXT PRIMARY KEY NOT NULL,
    origin_event_id TEXT, attempt_id TEXT, network_id TEXT,
    position_id TEXT, tx_sig TEXT, fill_id TEXT,
    message_kind TEXT NOT NULL, execution_state TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    event_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN
        ('baseline','queued','sending','delivered','failed','delivery_unknown','superseded','missed')),
    rendered_text_redacted TEXT, message_sha256 TEXT, error_class TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_mc_notification_delivery
    ON memecoin_notification_transitions(delivery_state,event_at);
CREATE INDEX IF NOT EXISTS idx_mc_notification_active
    ON memecoin_notification_transitions(is_current,execution_state,origin_event_id);
CREATE INDEX IF NOT EXISTS idx_mc_notification_attempt
    ON memecoin_notification_transitions(network_id,attempt_id);
CREATE INDEX IF NOT EXISTS idx_mc_notification_updated
    ON memecoin_notification_transitions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mc_notification_origin
    ON memecoin_notification_transitions(network_id,origin_event_id);
"""


def _epoch(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _source_hold_s():
    """Seconds an undecided source alert waits for its decision. 0 = send at once."""
    from sniper.settings import memecoin as cfg
    try:
        return max(0.0, float(getattr(cfg, "MEMECOIN_TG_SOURCE_HOLD_S", 4.0)))
    except (TypeError, ValueError):
        return 4.0


def _unresolved_grace_s():
    """Seconds a submitted-but-unconfirmed entry waits before it pages as unresolved."""
    from sniper.settings import memecoin as cfg
    try:
        return max(0.0, float(getattr(cfg, "MEMECOIN_TG_UNRESOLVED_GRACE_S", 30.0)))
    except (TypeError, ValueError):
        return 30.0


def _decision_expected(view):
    """True when the runner will decide this signal: its chain is funded. A signal on an unfunded
    chain never gets a decision, so holding its alert would only delay it."""
    try:
        from sniper.services.memecoin_chains import funded_chain_keys, resolve_chain
        return resolve_chain(view.get("network_id")) in funded_chain_keys()
    except Exception:
        return False


def transition_key(view):
    """Bind a result to its exact source, attempt, and execution identity."""
    from sniper.services.memecoin_chains import resolve_chain
    kind = view.get("message_kind") or ("source" if view.get("origin_event_id") and view.get("status") in PENDING else "entry")
    identity = [resolve_chain(view.get("network_id")) or str(view.get("network_id") or "unknown"),
                str(view.get("origin_event_id") or ""), kind]
    if kind != "source":
        identity.extend(str(view.get(k) or "") for k in
                        ("attempt_id", "status", "tx_sig", "position_id", "fill_id"))
    return "v1:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def _automatic_refusal(view):
    """True for a refusal the runner decided on its own before any money moved."""
    return (view.get("status") == "refused" and bool(view.get("source_user_id"))
            and view.get("message_kind") != "exit"
            and not any(view.get(k) for k in ("tx_sig", "submitted_at", "fill_id", "position_id")))


def _silent_mcap_refusal(view):
    """Keep automatic market-cap ceiling refusals in Activity without a Telegram message."""
    if not _automatic_refusal(view):
        return False
    reason = str(view.get("reason") or "")
    return reason == "over_ceiling" or bool(re.fullmatch(
        r"mcap \$[\d,]+ over ceiling \$[\d,]+ \(over_ceiling\)", reason))


def _silent_duplicate_refusal(view):
    """A refusal that only says another lane already took this wallet's buy of this coin.

    One alert per source buy (2026-09-27, operator): the lane holding the reservation or position
    pages its own outcome, BOUGHT or its real refusal. Paging the duplicate put a second card on the
    same buy and, through the per-coin collapse, muted the real answer (GAY, round trip over cap).
    `duplicate_same_source` requires the exact chain, wallet, mint and source transaction."""
    return _automatic_refusal(view) and bool(view.get("duplicate_same_source"))


def _silent_j7_fast_lane_refusal(view):
    """A J7 buy (j7_wallets or j7_shadow) refused only because fast-lane-only funds Solana through
    the native lanes. It says nothing about the coin: AUTON f139fd7a (2026-10-06) paged
    "Why: native_fast_lane_unavailable" while the coin sat over the mcap ceiling. Operator order:
    no card. The native lanes page their own result for the same buy."""
    return (_automatic_refusal(view) and view.get("signal_source") == "follow_activity"
            and view.get("reason") == "native_fast_lane_unavailable")


def _refusal_collapse_s():
    """Seconds one coin's refusal notice silences its own repeats. 0 = send every one."""
    from sniper.settings import memecoin as cfg
    try:
        return max(0.0, float(getattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 3600.0)))
    except (TypeError, ValueError):
        return 3600.0


def _source_cards_enabled():
    """Whether the undecided "EVALUATING" card pages at all."""
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", False))


def _refusal_collapsed(conn, view, now):
    """True when a refusal notice for this coin already went out inside the collapse window.

    EVIDENCE. Gamma read `memecoin_telegram_delivery_audit` and found 1,927 of the 3,027 Telegram
    messages in seven days were entry REFUSED notices over 630 coins, one coin 38 times
    (docs/audits/2026-09-22-research-lanes-and-alerts-review.md). This table, a different writer,
    records 1,531 delivered refused entries in the same seven days over 555 coins, worst coin 24. A
    one-hour window replayed over those rows suppresses 732 of the 1,504 whose coin still resolves
    (49%); 30 min suppresses 44% and 2 h suppresses 52%.

    A collapsed refusal is still written as a transition with delivery_state='baseline', the same way
    a silent retry and a silent ceiling refusal are, so Activity loses nothing.

    The window reads this table's own delivery record, so it counts only refusals the operator could
    have seen. `julianday` is used on both sides because it normalizes every stamp shape this column
    has ever carried (17,643 of 17,643 rows parse, all `+00:00`); an unparseable stamp yields NULL
    and the refusal pages (fail open). Rows written before this gate shipped carry no mint in their
    evidence, so the first refusal per coin after a deploy still pages -- the window fills itself and
    needs no backfill.

    ponytail: the mint lives in evidence_json, so the worst case scans one network's refusals
    (6.7 ms warm at 17,643 rows, once per new refusal, on the notifier thread and not the entry
    path). If the table grows an order of magnitude, add a mint column with an index.
    """
    window, mint = _refusal_collapse_s(), view.get("mint")
    # A refusal with no origin event is a manual or unattributed attempt: the operator is waiting on
    # an answer to their own request, so it always pages. Only a copy signal's repeats collapse.
    if not window or not mint or not view.get("origin_event_id") or not _automatic_refusal(view):
        return False
    return bool(conn.execute(
        "SELECT 1 FROM memecoin_notification_transitions WHERE network_id=? "
        "AND message_kind='entry' AND execution_state='refused' "
        "AND delivery_state IN ('queued','sending','delivered','delivery_unknown') "
        "AND julianday(updated_at)>=julianday(?,?) "
        "AND json_extract(evidence_json,'$.mint')=? LIMIT 1",
        (str(view.get("network_id") or "unknown"), now, "-%f seconds" % window,
         str(mint))).fetchone())


def _source_card_delivered(conn, view):
    """True when an EVALUATING card for this signal already reached the operator (or may have).

    GCAT (BSC, 2026-09-17): the source hold expired, EVALUATING was sent, and the over-ceiling
    refusal that followed was muted, so the card never resolved. A delivered card owns its result."""
    row = conn.execute(
        "SELECT 1 FROM memecoin_notification_transitions WHERE network_id=? "
        "AND origin_event_id=? "
        "AND message_kind='source' AND delivery_state IN ('delivered','delivery_unknown') "
        "UNION ALL SELECT 1 FROM memecoin_notification_transitions WHERE network_id=? "
        "AND attempt_id=? AND origin_event_id IS NULL "
        "AND message_kind='source' AND delivery_state IN ('delivered','delivery_unknown') LIMIT 1",
        (str(view.get("network_id") or "unknown"), view.get("origin_event_id"),
         str(view.get("network_id") or "unknown"), view.get("attempt_id"))).fetchone()
    return bool(row)


class NotificationLifecycle:
    """Reconcile durable views; an ambiguous send owns at most one explicitly marked resend."""

    def __init__(self, conn, *, read_view, send_fn, render_fn, eligible_fn, now_fn, audit_fn=None):
        self.conn = conn
        self.read_view = read_view
        self.send_fn = send_fn
        self.render_fn = render_fn
        self.eligible_fn = eligible_fn
        self.now_fn = now_fn
        self.audit_fn = audit_fn
        self._refresh_offset = 0
        conn.executescript(SCHEMA)

    def activate(self, now=None):
        self.conn.execute("INSERT OR IGNORE INTO memecoin_notification_state(singleton,activation_cutoff) VALUES(1,?)",
                          (now or self.now_fn(),))
        self.conn.commit()
        return self.conn.execute("SELECT activation_cutoff FROM memecoin_notification_state WHERE singleton=1").fetchone()[0]

    def recover_interrupted(self):
        self.conn.execute("UPDATE memecoin_notification_transitions SET delivery_state='delivery_unknown', "
                          "error_class='InterruptedSend',updated_at=?,evidence_json=json_set(evidence_json,"
                          "'$.delivery_retry_allowed',CASE WHEN json_extract(evidence_json,'$.delivery_attempt')=1 "
                          "THEN 1 ELSE 0 END) WHERE delivery_state='sending'", (self.now_fn(),))
        self.conn.commit()

    def observe(self, view, *, eligible=None):
        if not view or not (view.get("origin_event_id") or view.get("attempt_id") or view.get("fill_id")):
            return None
        from sniper.services.memecoin_chains import canonical_chain_id
        view = dict(view)
        view["network_id"] = canonical_chain_id(view.get("network_id")) or str(view.get("network_id") or "unknown")
        key = transition_key(view)
        if self.conn.execute("SELECT 1 FROM memecoin_notification_transitions WHERE transition_key=?", (key,)).fetchone():
            return key
        now = self.now_fn()
        cutoff = self.activate()
        kind = view.get("message_kind") or ("source" if view.get("origin_event_id") and view.get("status") in PENDING else "entry")
        prior = self.conn.execute(
            "SELECT execution_state,event_at,evidence_json FROM memecoin_notification_transitions WHERE network_id=? AND (origin_event_id=? OR (origin_event_id IS NULL AND attempt_id=?)) "
            "AND is_current=1 AND message_kind IN ('source','entry') "
            # A silent retry (below) still counts as this signal's notice, so its final result
            # pages even when it concludes late -- but only a retry from after activation, or an
            # old retry record would re-page history.
            "AND (delivery_state NOT IN ('baseline','missed') "
            "     OR (execution_state='retry_pending' AND julianday(event_at)>=julianday(?))) LIMIT 1",
            (str(view.get("network_id") or "unknown"), view.get("origin_event_id"), view.get("attempt_id"),
             cutoff)).fetchone()
        authorized = (view.get("status") not in {"shadow", "funding_handoff"}
                      and (bool(prior) or (self.eligible_fn(view) if eligible is None else eligible)))
        silent = (_silent_mcap_refusal(view) or _silent_duplicate_refusal(view)
                  or _silent_j7_fast_lane_refusal(view) or _refusal_collapsed(self.conn, view, now))
        authorized = authorized and not (view.get("native_owned_secondary_refusal")
            or (silent and not _source_card_delivered(self.conn, view)))
        if not authorized and not view.get("attempt_id"):
            return None
        stamp = (view.get("observed_at") if kind == "source" else
                 view.get("status_updated_at") or view.get("completed_at") or view.get("observed_at"))
        transition_observed_at = None
        prior_epoch = _epoch(prior[1]) if prior else None
        historical_review = bool(prior and prior[0] == "needs_review"
                                 and (prior_epoch is None or prior_epoch < _epoch(cutoff)))
        try:
            prior_evidence = json.loads(prior[2] or "{}") if prior else {}
        except (ValueError, TypeError):
            prior_evidence = {}
        if not isinstance(prior_evidence, dict):
            prior_evidence = {}
        terminal_facts = ("reservation_status", "reservation_resolution_reason", "tx_sig",
                          "completed_at", "status_updated_at")
        label_correction = bool(prior and prior[0] != view.get("status")
            and prior[0] in UNRESOLVED and view.get("status") not in UNRESOLVED
            and prior_evidence.get("reservation_status") in {"failed", "confirmed"}
            and all(prior_evidence.get(k) for k in terminal_facts)
            and all(prior_evidence.get(k) == view.get(k) for k in terminal_facts))
        legacy_correction = bool(prior and prior[0] == "needs_review" and not label_correction
            and not prior_evidence.get("reservation_status")
            and view.get("reservation_status") in {"failed", "confirmed"}
            and view.get("status") not in UNRESOLVED and stamp == prior[1]
            and prior_epoch is not None and _epoch(now) - prior_epoch > 300)
        # A corrected historical review is not a new execution event.
        if (prior and prior[0] in (UNRESOLVED | {"retry_pending"})
                and prior[0] != view.get("status") and not historical_review and not label_correction):
            transition_observed_at = now
            stamp = now
        event_epoch, now_epoch, cutoff_epoch = _epoch(stamp), _epoch(now), _epoch(cutoff)
        state = "queued" if authorized else "baseline"
        # RETRIES ARE SILENT (2026-09-11, operator: "why is the system retrying so often"). Every
        # retry attempt is a new transition, so one decision paged "RETRY SCHEDULED" up to three
        # times in 20 s before its result (EPI 17:37Z; 14 retry pages that afternoon). The retry is
        # recorded; the operator hears the final outcome once.
        if view.get("status") == "retry_pending":
            state = "baseline"
        # AGE GATE FOR UNRESOLVED (2026-09-11, Beta's review). Unresolved statuses skip the cutoff
        # below by design, so a genuinely open old transaction still pages. But the unresolved scan
        # walks ALL history, and a terminal row the view cannot yet classify also reads unresolved:
        # that is how 21 July/August records paged at activation. First sight of an unresolved view
        # from before the cutoff stays quiet only when the ledger RECORDS a terminal reservation
        # (failed or confirmed). An unknown reservation state is not "closed": it still pages.
        if (view.get("status") in UNRESOLVED and not prior and event_epoch is not None
                and event_epoch < cutoff_epoch
                and view.get("reservation_status") in ("failed", "confirmed")):
            state = "baseline"
        if view.get("status") not in UNRESOLVED:
            if event_epoch is None:
                state = "baseline"
            elif event_epoch < cutoff_epoch:
                state = "baseline"
            elif authorized and now_epoch - event_epoch > 300:
                state = "missed"
        if label_correction:
            stamp = prior[1]
            state = "baseline"
        evidence = {k: view.get(k) for k in ("source_created_at", "observed_at", *terminal_facts)}
        if legacy_correction:
            evidence["historical_result_correction"] = True
        evidence["eligible"] = bool(authorized)
        if view.get("mint"):
            evidence["mint"] = str(view["mint"])
        if transition_observed_at:
            evidence["transition_observed_at"] = transition_observed_at
        fields = [key] + [str(view[k]) if view.get(k) is not None else None for k in
                         ("origin_event_id", "attempt_id", "network_id", "position_id", "tx_sig", "fill_id")]
        if kind in {"source", "entry"}:
            self.conn.execute(
                "UPDATE memecoin_notification_transitions SET is_current=0, "
                "delivery_state=CASE WHEN delivery_state IN ('queued','failed') THEN 'superseded' ELSE delivery_state END "
                "WHERE network_id=? AND (origin_event_id=? OR (origin_event_id IS NULL AND attempt_id=?)) "
                "AND is_current=1 AND message_kind IN ('source','entry')",
                (view["network_id"], view.get("origin_event_id"), view.get("attempt_id")))
        self.conn.execute("INSERT OR IGNORE INTO memecoin_notification_transitions "
                          "(transition_key,origin_event_id,attempt_id,network_id,position_id,tx_sig,fill_id,"
                          "message_kind,execution_state,event_at,created_at,updated_at,delivery_state,evidence_json) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          fields + [kind, str(view.get("status") or "needs_review"), stamp or now, now, now, state,
                                    json.dumps(evidence, separators=(",", ":"))])
        self.conn.commit()
        return key

    def claim(self, key, payload):
        """Claim one send in a short transaction before the network call."""
        with self.conn:
            result = self.conn.execute(
                "UPDATE memecoin_notification_transitions SET delivery_state='sending',updated_at=?,"
                "rendered_text_redacted=?,message_sha256=?,evidence_json=json_set(evidence_json,"
                "'$.delivery_attempt',COALESCE(json_extract(evidence_json,'$.delivery_attempt'),0)+1,"
                "'$.delivery_retry_allowed',0) WHERE transition_key=? AND is_current=1 AND ("
                "(delivery_state IN ('queued','failed') AND COALESCE(json_extract(evidence_json,'$.delivery_attempt'),0)=0) OR "
                "(delivery_state='delivery_unknown' AND json_extract(evidence_json,'$.delivery_attempt')=1 "
                "AND json_extract(evidence_json,'$.delivery_retry_allowed')=1 "
                "AND julianday(updated_at)<=julianday(?,'-5 seconds')))",
                (self.now_fn(), payload.get("rendered_text_redacted"), payload.get("message_sha256"), key, self.now_fn()))
        return result.rowcount == 1

    def _state(self, key, state, *, error=None, payload=None, retry_allowed=False):
        payload = payload or {}
        self.conn.execute("UPDATE memecoin_notification_transitions SET delivery_state=?,updated_at=?,"
                          "error_class=?,rendered_text_redacted=COALESCE(?,rendered_text_redacted),"
                          "message_sha256=COALESCE(?,message_sha256),"
                          "evidence_json=json_set(evidence_json,'$.delivery_retry_allowed',?) WHERE transition_key=?",
                          (state, self.now_fn(), error, payload.get("rendered_text_redacted"),
                           payload.get("message_sha256"), int(retry_allowed), key))
        self.conn.commit()

    def refresh_tracked(self, *, limit=100):
        """Refresh a bounded page. The cursor wraps across unresolved origins of every age."""
        placeholders = ",".join("?" for _ in TRACKED)
        rows = self.conn.execute(
            "SELECT origin_event_id,attempt_id FROM memecoin_notification_transitions "
            f"WHERE is_current=1 AND execution_state IN ({placeholders}) "
            "AND (origin_event_id IS NOT NULL OR attempt_id IS NOT NULL) "
            "ORDER BY origin_event_id,attempt_id LIMIT ? OFFSET ?", (*sorted(TRACKED), limit, self._refresh_offset)).fetchall()
        self._refresh_offset = self._refresh_offset + len(rows) if len(rows) == limit else 0
        for row in rows:
            current = self.read_view(**({"origin_event_id": row[0]} if row[0] else {"attempt_id": row[1]}))
            self.observe(current)

    def deliver(self, *, limit=20):
        from sniper.services.memecoin_telegram import telegram_retryable_error, telegram_audit_payload
        self._missed_summary()
        # Preparation can retry; a network attempt cannot repeat without a recorded ambiguous receipt.
        rows = self.conn.execute(
            "SELECT * FROM memecoin_notification_transitions WHERE delivery_state='queued' OR "
            "(delivery_state='failed' AND COALESCE(json_extract(evidence_json,'$.delivery_attempt'),0)=0 "
            "AND julianday(updated_at)<julianday(?)-30.0/86400) OR "
            "(delivery_state='delivery_unknown' AND json_extract(evidence_json,'$.delivery_attempt')=1 "
            "AND json_extract(evidence_json,'$.delivery_retry_allowed')=1 "
            "AND julianday(updated_at)<=julianday(?,'-5 seconds')) "
            "ORDER BY event_at,rowid LIMIT ?", (self.now_fn(), self.now_fn(), limit)).fetchall()
        columns = [r[1] for r in self.conn.execute("PRAGMA table_info(memecoin_notification_transitions)")]
        for raw in rows:
            row = dict(zip(columns, raw))
            key = row["transition_key"]
            attempt = int(json.loads(row["evidence_json"] or "{}").get("delivery_attempt", 0)) + 1
            try:
                if row["message_kind"] == "missed_summary":
                    current = {"message_kind": "missed_summary", "status": "missed_summary"}
                    count = json.loads(row["evidence_json"])["missed_count"]
                    text = f"Telegram missed {count} result(s). Open Activity for the durable outcomes."
                else:
                    kwargs = ({"origin_event_id": row["origin_event_id"]} if row["origin_event_id"]
                              else {"attempt_id": row["attempt_id"]})
                    if row["message_kind"] == "exit":
                        kwargs = {"fill_id": row["fill_id"]}
                    current = self.read_view(**kwargs)
                    if not current:
                        raise ValueError("ProjectionUnavailable")
                    new_key = transition_key(current)
                    if new_key != key:
                        self._state(key, "superseded")
                        self.observe(current, eligible=True)
                        continue
                    if (current.get("native_owned_secondary_refusal") or ((_silent_mcap_refusal(current) or _silent_duplicate_refusal(current)
                         or _silent_j7_fast_lane_refusal(current))
                            and not _source_card_delivered(self.conn, current))):
                        self._state(key, "baseline")
                        continue
                    if json.loads(row["evidence_json"] or "{}").get("historical_result_correction"):
                        current = dict(current, historical_result_correction=True)
                    age = (_epoch(self.now_fn()) or 0) - (_epoch(row["event_at"]) or 0)
                    # ONE MESSAGE PER SIGNAL (2026-09-11). A source alert sent before its decision is
                    # followed a second later by the result, so every signal paged twice. Hold a
                    # still-undecided source briefly: when the decision lands, the source is
                    # superseded above and only the result goes out. After the hold it sends.
                    if (row["message_kind"] == "source" and current.get("status") in PENDING
                            and 0 <= age < _source_hold_s() and _decision_expected(current)):
                        continue
                    # THE MUTE SILENCES THE DELIVERY, NEVER THE INSTRUMENT (2026-09-22, Alpha's
                    # decision). Three things make this the right place rather than observe():
                    #  1. the muted population is then exactly the cards that WOULD have sent -- a
                    #     card superseded inside the hold recorded nothing before and records nothing
                    #     now, so the sample does not change shape;
                    #  2. `render_fn` runs first because the renderer is also what writes the
                    #     alert-intent row (`_source_intent` -> `record_alert` ->
                    #     memecoin_alert_latency), which is the ONLY live writer behind that
                    #     instrument -- the legacy `_prepare_alert` path has no production caller;
                    #  3. the rendered text is then discarded, so nothing reaches Telegram.
                    # CAUTION for a consumer: `alert_sent_at` stays null on a muted card, because
                    # nothing was sent. Anchor on `detected_at`, not on the send time.
                    if row["message_kind"] == "source" and not _source_cards_enabled():
                        self.render_fn(current)
                        self._state(key, "baseline")
                        continue
                    # A BUY MID-CONFIRMATION IS NOT AN ALARM (2026-09-11). Every buy passes through
                    # "submitted, confirmation unknown" for 1-9 s, and all 19 such pages that day
                    # were followed within seconds by BOUGHT (or one TRANSACTION FAILED). Page the
                    # unknown state only when it outlives the grace; a resolution inside it
                    # supersedes this row above and only the outcome is sent.
                    if (current.get("status") in {"submitted_unknown", "transaction_unresolved"}
                            and 0 <= age < _unresolved_grace_s()):
                        continue
                    if age > 300 and current.get("status") not in UNRESOLVED:
                        self._state(key, "missed")
                        continue
                    text = self.render_fn(current)
                    if not text:
                        raise ValueError("EmptyMessage")
                if attempt == 2:
                    text = "RESENT (may be a duplicate)\n" + text
                payload = telegram_audit_payload(text)
            except Exception as exc:
                self._state(key, "failed", error=type(exc).__name__)
                continue
            if not self.claim(key, payload):
                continue
            started = self.now_fn()
            elapsed = 0.0
            error = None
            retry_allowed = False
            state = "failed"
            started_mono = time.perf_counter()
            try:
                result = self.send_fn(text)
                ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
                elapsed = float(result[1] or 0) if isinstance(result, tuple) else 0.0
                state = "delivered" if ok else "failed"
                error = None if ok else "TelegramRejected"
            except Exception as exc:
                # The sender crossed its network boundary. An unexpected exception is ambiguous.
                state, error = "delivery_unknown", type(exc).__name__
                elapsed = (time.perf_counter() - started_mono) * 1000.0
                retry_allowed = attempt == 1 and telegram_retryable_error(exc)
                import urllib.error
                if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                    state = "failed"
            self._state(key, state, error=error, payload=payload, retry_allowed=retry_allowed)
            if self.audit_fn:
                self.audit_fn({"message_kind": row["message_kind"], "source_id": row["origin_event_id"] or row["position_id"],
                    "queued_at": row["created_at"], "send_started_at": started, "completed_at": self.now_fn(),
                    "send_ok": state == "delivered", "send_ms": elapsed, "error_class": error,
                    "attempt": attempt, **payload})
        # A superseded source produces its current result in this same worker cycle.
        if any(dict(zip(columns, r))["delivery_state"] == "queued" for r in rows):
            remaining = self.conn.execute("SELECT COUNT(*) FROM memecoin_notification_transitions WHERE delivery_state='queued'").fetchone()[0]
            if remaining and limit > len(rows):
                self.deliver(limit=limit - len(rows))

    def _missed_summary(self):
        rows = self.conn.execute("SELECT transition_key,evidence_json FROM memecoin_notification_transitions "
                                 "WHERE delivery_state='missed' AND message_kind!='missed_summary' "
                                 "AND json_extract(evidence_json,'$.summary_key') IS NULL LIMIT 100").fetchall()
        pending = [(r[0], json.loads(r[1])) for r in rows if not json.loads(r[1]).get("summary_key")]
        if not pending:
            return
        now = self.now_fn()
        key = "summary:" + hashlib.sha256("|".join(r[0] for r in pending).encode()).hexdigest()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO memecoin_notification_transitions "
                              "(transition_key,message_kind,execution_state,event_at,created_at,updated_at,delivery_state,evidence_json) "
                              "VALUES(?,'missed_summary','missed_summary',?,?,?,'queued',?)",
                              (key, now, now, now, json.dumps({"missed_count": len(pending)})))
            for origin_key, evidence in pending:
                evidence["summary_key"] = key
                self.conn.execute("UPDATE memecoin_notification_transitions SET evidence_json=? WHERE transition_key=?",
                                  (json.dumps(evidence), origin_key))


def _unpriced_close_created_at(read_conn, fill_id):
    """created_at of an unpriced confirmed close, else None."""
    row = read_conn.execute("SELECT created_at FROM memecoin_fills WHERE id=? AND price IS NULL "
                            "AND notional IS NULL AND fill_source='chain_confirmed_unpriced'",
                            (fill_id,)).fetchone()
    return row[0] if row else None


# How long after a close the re-check below still looks at it: any scanner gap shorter than this
# still pages a deferred close (Gamma LOW, case B). Rows are re-read only inside this window.
_UNPRICED_EXIT_RECHECK_S = 6 * 3600.0


def _unpriced_exit_wait_s():
    try:
        return max(0.0, float(os.getenv("MEMECOIN_EXIT_CARD_UNPRICED_WAIT_S", "120")))
    except ValueError:
        return 120.0


def scan_notification_views(lifecycle, read_conn, read_view, *, limit=100):
    """Scan append-only identities in bounded pages. Refresh mutable attempts separately."""
    state = lifecycle.conn.execute(
        "SELECT scan_cursors_json FROM memecoin_notification_state WHERE singleton=1").fetchone()
    cursors = json.loads(state[0])
    # THE CLOSES STILL OWED A CARD (review C05, 2026-10-08). The fill cursor moves past a deferred
    # unpriced close, so its id is kept here, in the same state row, and re-read below until it is
    # observed or ends with a named reason. Bounded by the wait: an id leaves at most one wait after
    # it was deferred. Old code reading this row keeps the key (it rewrites the whole dict).
    deferred = cursors.setdefault("deferred_fills", [])
    tables = ("memecoin_events", "memecoin_entry_attempts", "memecoin_fills")
    for table in tables:
        if table not in cursors:
            cursors[table] = read_conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0]
            continue
        if table == "memecoin_events":
            query = "SELECT rowid,fomo_event_id FROM memecoin_events WHERE rowid>? ORDER BY rowid LIMIT ?"
        elif table == "memecoin_entry_attempts":
            query = "SELECT rowid,attempt_id FROM memecoin_entry_attempts WHERE rowid>? ORDER BY rowid LIMIT ?"
        else:
            query = "SELECT rowid,id FROM memecoin_fills WHERE rowid>? ORDER BY rowid LIMIT ?"
        for cursor, identity in read_conn.execute(query, (cursors[table], limit)).fetchall():
            cursors[table] = cursor
            if table == "memecoin_fills":
                created = _epoch(_unpriced_close_created_at(read_conn, identity))
                now = _epoch(lifecycle.now_fn())
                if created is not None and now is not None and now - created < _unpriced_exit_wait_s():
                    if identity not in deferred:
                        deferred.append(identity)
                    continue               # paged below, once priced (or after the wait)
            kwargs = ({"origin_event_id": identity} if table == "memecoin_events" else
                      {"attempt_id": identity} if table == "memecoin_entry_attempts" else {"fill_id": identity})
            current = read_view(**kwargs)
            lifecycle.observe(current)
    # AN UNPRICED CLOSE PAGES WHEN IT IS PRICED (2026-10-07, 71670/71561 'Unknown @ Unknown').
    # Recovery books a confirmed close without proceeds and a later sweep prices the SAME fill row
    # (chain_confirmed_receipt_reconciled). Re-found by query on every scan, so no stored state can
    # be lost between workers; observe() pages a fill once (its transition key has no price). The
    # card waits for the price, or MEMECOIN_EXIT_CARD_UNPRICED_WAIT_S, then sends what it has.
    top = cursors.get("memecoin_fills", 0)
    wait_s, now = _unpriced_exit_wait_s(), _epoch(lifecycle.now_fn())
    still = []
    for fill_id in deferred:
        created = _epoch(_unpriced_close_created_at(read_conn, fill_id))
        if created is not None and (now is None or now - created < wait_s):
            still.append(fill_id)          # still unpriced and inside the wait
            continue
        view = read_view(fill_id=fill_id)
        if view:
            lifecycle.observe(view)        # priced, or past the wait: what it has (pages once)
        else:
            terminal = cursors.setdefault("deferred_terminal", {})
            if str(fill_id) not in terminal:
                logger.warning("notifications: deferred close fill=%s ended view_unavailable", fill_id)
            terminal[str(fill_id)] = "view_unavailable"
            for old_key in list(terminal)[:-100]:
                terminal.pop(old_key)      # the newest 100 reasons stay inspectable
    deferred[:] = still
    for fill_id, priced, created_at in read_conn.execute(
            "SELECT id, price IS NOT NULL, created_at FROM memecoin_fills WHERE rowid>? AND rowid<=? "
            "AND fill_source IN ('chain_confirmed_unpriced','chain_confirmed_receipt_reconciled')",
            (max(0, top - 500), top)).fetchall():
        since = _epoch(created_at)
        if now is None or since is None or now - since > max(wait_s, _UNPRICED_EXIT_RECHECK_S):
            continue                       # long settled: paged then, never re-read
        if not priced and now - since < wait_s:
            continue
        lifecycle.observe(read_view(fill_id=fill_id))
    with lifecycle.conn:
        lifecycle.conn.execute("UPDATE memecoin_notification_state SET scan_cursors_json=?,last_scan_at=? WHERE singleton=1",
                               (json.dumps(cursors), lifecycle.now_fn()))


# memecoin_rabbit_signal.EXIT_REASONS (named here so the persistence layer imports no service).
RABBIT_EXIT_REASONS = ("source_failed", "source_not_landed")


def _sold_share(conn, row):
    """This sale's share of the position by quantity, so a partial reads "sold 75%" and the close
    after it "sold remaining 25%" (73882: fractions 0.75 and 1.0 have different bases). None for a
    lone full exit or an unknown quantity."""
    from sniper.persistence.db_memecoin import ENTRY_KINDS, EXIT_KINDS
    try:
        qty = float(row.get("qty")) if row.get("qty") is not None else None
        marks = ",".join("?" for _ in ENTRY_KINDS)
        entered = conn.execute(f"SELECT SUM(qty), COUNT(*) - COUNT(qty) FROM memecoin_fills "
                               f"WHERE position_id=? AND kind IN ({marks})",
                               (row["position_id"], *ENTRY_KINDS)).fetchone()
        if not qty or qty <= 0 or not entered or entered[1] or not entered[0] or entered[0] <= 0:
            return None
        share = round(qty / float(entered[0]), 4)
        if row.get("kind") in ("derisk", "partial_exit"):
            return {"share": share, "remaining": False}
        marks = ",".join("?" for _ in EXIT_KINDS)
        earlier = conn.execute(f"SELECT 1 FROM memecoin_fills WHERE position_id=? AND id<? "
                               f"AND kind IN ({marks}) LIMIT 1",
                               (row["position_id"], row["id"], *EXIT_KINDS)).fetchone()
        return {"share": share, "remaining": True} if earlier else None
    except (TypeError, ValueError, sqlite3.Error):
        return None


def notification_exit_view(conn, fill_id):
    """Read an exact funded sale, including sales from positions that predate activation."""
    row = conn.execute("SELECT f.*,p.network_id AS position_network,p.mint,p.ticker,p.source_handle,"
                       "p.source_user_id,p.origin_event_id AS position_origin,p.funded_contract_json,p.exit_reason,"
                       "p.entry_notional AS position_entry_notional,p.realized_pnl AS position_realized_pnl,"
                       "p.is_open AS position_is_open "
                       "FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
                       "WHERE f.id=? AND p.execution_mode='real'", (fill_id,)).fetchone()
    if not row:
        return None
    row = dict(row)
    from sniper.persistence.db_memecoin import EXIT_KINDS
    if row.get("kind") not in EXIT_KINDS:
        return None
    try:
        contract = json.loads(row.get("funded_contract_json") or "{}")
        policy = contract.get("policy") or {}
    except (TypeError, ValueError):
        policy = {}
    view = {"message_kind": "exit", "status": "sold", "fill_id": row["id"],
            "origin_event_id": row.get("origin_event_id") or row.get("position_origin"),
            "attempt_id": row.get("attempt_id"), "position_id": row["position_id"],
            "network_id": row.get("network_id") or row["position_network"], "mint": row["mint"],
            "ticker": row["ticker"], "source_handle": row["source_handle"],
            "source_user_id": row["source_user_id"], "tx_sig": row.get("tx_sig"),
            "filled_notional": row["notional"], "filled_price": row["price"],
            "position_policy_id": policy.get("policy_id") if isinstance(policy, dict) else None,
            "reason": (row["kind"] if row["kind"] in ("derisk", "partial_exit") else row.get("exit_reason") or row["kind"]),
            "completed_at": row["created_at"], "status_updated_at": row["created_at"],
            "entry_notional": row.get("position_entry_notional"),
            "realized_pnl": row.get("position_realized_pnl"),
            "position_closed": row.get("position_is_open") == 0}
    view["sold_share"] = _sold_share(conn, row)
    if view["position_closed"]:
        # What decided the exit, so the line leads with the exact quote, not a stale mid (display).
        from sniper.persistence.db_memecoin import exit_decisions
        view["exit_decision"] = exit_decisions(conn, [row["position_id"]]).get(row["position_id"])
    if view["reason"] in RABBIT_EXIT_REASONS:
        # A rabbit-led fire whose source did not land: the verdict and its lead from the exit audit.
        audit = conn.execute("SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=? "
                             "AND reason=? ORDER BY id DESC LIMIT 1",
                             (row["position_id"], view["reason"])).fetchone()
        try:
            detail = json.loads(audit[0]) if audit and audit[0] else {}
        except (TypeError, ValueError):
            detail = {}
        view["rabbit_landing"] = {"verdict": view["reason"], "lead_ms": detail.get("rabbit_lead_ms"),
                                  "age_ms": detail.get("age_ms"), "basis": detail.get("basis")}
    if view["reason"] == "mirror":
        from sniper.persistence.db_memecoin import latest_mirror_decisions
        decision = latest_mirror_decisions(conn, [row["position_id"]]).get(row["position_id"]) or {}
        view["mirror_sold_fraction"] = decision.get("sold_fraction")
    return view


def notification_fill_identity(conn, fill_id):
    """Resolve a new entry fill to its exact source and attempt."""
    row = conn.execute("SELECT f.origin_event_id,f.attempt_id,p.origin_event_id FROM memecoin_fills f "
                       "JOIN memecoin_positions p ON p.id=f.position_id WHERE f.id=? "
                       "AND p.execution_mode='real' AND f.kind IN ('entry','manual_buy')", (fill_id,)).fetchone()
    if not row:
        return None
    return {"origin_event_id": row[0] or row[2], "attempt_id": row[1]}


def scan_unresolved_notification_views(lifecycle, read_conn, read_view, *, limit=100):
    """Seed old unresolved transactions and refresh them with bounded cursor pages."""
    cursors = json.loads(lifecycle.conn.execute(
        "SELECT scan_cursors_json FROM memecoin_notification_state WHERE singleton=1").fetchone()[0])
    queries = {
        "unresolved_pending": "SELECT rowid,origin_event_id,attempt_id FROM memecoin_pending_entries "
            "WHERE rowid>? AND (status='reserved' OR recovery_status='orphaned') ORDER BY rowid LIMIT ?",
        "unresolved_attempt": "SELECT rowid,origin_event_id,attempt_id FROM memecoin_entry_attempts "
            "WHERE rowid>? AND terminal_status IN ('unknown','anomaly','transaction_unresolved','submitted_unknown') "
            "ORDER BY rowid LIMIT ?",
    }
    for name, query in queries.items():
        rows = read_conn.execute(query, (cursors.get(name, 0), limit)).fetchall()
        for _, origin, attempt in rows:
            current = read_view(origin_event_id=origin, attempt_id=attempt)
            if current and current.get("status") in UNRESOLVED | {"filled"}:
                lifecycle.observe(current, eligible=True)
        cursors[name] = rows[-1][0] if len(rows) == limit else 0
    with lifecycle.conn:
        lifecycle.conn.execute("UPDATE memecoin_notification_state SET scan_cursors_json=? WHERE singleton=1",
                               (json.dumps(cursors),))


def notification_delivery_view(conn, *, network_id, origin_event_id=None, attempt_id=None):
    """Return exact durable delivery evidence without creation or migration of tables."""
    from sniper.services.memecoin_chains import canonical_chain_id
    available = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memecoin_notification_transitions'").fetchone()
    result = {"available": bool(available), "source": None, "result": None, "history": []}
    if not available or not (origin_event_id or attempt_id):
        return result
    chain = canonical_chain_id(network_id) or str(network_id or "unknown")
    columns = ("message_kind", "execution_state", "delivery_state", "attempt_id", "position_id", "fill_id",
               "updated_at", "error_class", "origin_event_id", "network_id")
    where = "origin_event_id=?" if origin_event_id else "attempt_id=?"
    rows = conn.execute("SELECT " + ",".join(columns) + " FROM memecoin_notification_transitions "
                        f"WHERE network_id=? AND {where} ORDER BY rowid DESC LIMIT 20",
                        (chain, origin_event_id or attempt_id)).fetchall()
    for row in rows:
        item = dict(zip(columns, row))
        result["history"].append(item)
        if item["message_kind"] not in {"source", "entry"}:
            continue
        kind = "source" if item["message_kind"] == "source" else "result"
        if result[kind] is None:
            result[kind] = item
    return result


def notification_delivery_revision(conn):
    """Return a bounded revision for the UI when only delivery state changes."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memecoin_notification_transitions'").fetchone():
        return None
    recent = conn.execute("SELECT transition_key,delivery_state,updated_at FROM memecoin_notification_transitions "
                          "ORDER BY updated_at DESC,rowid DESC LIMIT 200").fetchall()
    active = conn.execute("SELECT transition_key,delivery_state,updated_at FROM memecoin_notification_transitions "
                          "WHERE delivery_state IN ('queued','sending','failed') ORDER BY event_at LIMIT 200").fetchall()
    values = sorted(set(tuple(row) for row in recent + active))
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
