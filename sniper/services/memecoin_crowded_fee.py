"""Crowded priority fee on the Solana slot lane (operator YES 2026-10-15 via Alpha, a live test).

THE RULE ("early"): the source's frame reached us while our feed head was still BEFORE its slot S
(receipt_head's head_at_recv_minus_s < 0). Only then does a priority fee reorder anything we can
reach: the leader of S is still building. Our own data before this test: early sources land in S
20% of the time and fill 67% (n=70); late ones 3% and 54%. A bot race in S bids 10M..68M lamports
against our 1.52M fallback. An unknown head is NOT early: the normal bid, fail closed.

THE ARMS: treated (bid MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS) or holdout (the normal bid),
by sha256(attempt_id), one quarter held out, deterministic per attempt, so treated against holdout
compares like with like. Everything else is "not eligible" with its reason.

THE MONEY: the fee is only the compute-unit price (min-out, chase and copy caps, clip and tip are
untouched); priority + tip never exceed MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS
(pump_curve_buy.crowded_priority); a treated send counts its priority fee against
MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS when it goes out, failed or not (a landed failure pays
it), seeded from the ledger at lane start. Zero disables the daily cap; a positive cap pages once
and bids normally until UTC midnight. Unknown spend disables only finite-budget crowded bids.
The lane thread is the only writer; the heartbeat copies `status`.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

RULE_EARLY = "early"
# crowded_priority rounds the CU price UP toward the target, so a raised fee can pass its target by up
# to ceil(cu_limit / 1e6) lamports: 2 at Solana's 1.4M CU maximum (Codex F9 bound).
CU_ROUNDING_SLACK_LAMPORTS = 2
# A treated build that kept the normal bid (crowded_priority) spent nothing crowded: not counted.
NOT_RAISED = ("normal_bid_higher", "no_room")
SUBSTAGE = "crowded_fee"            # the arm, marked before the build
SUBSTAGE_SENT = "crowded_fee_sent"  # a treated send that went out, with its priority fee
HOLDOUT_SHARE = 4            # one attempt in four is the holdout
# The venues in scope: MEMECOIN_SLOT_LANE_CROWDED_VENUES, read per send (default every slot-lane venue
# since 2026-10-16; a routed buy's head is the one its pair completed at, memecoin_slot_lane.receipt_head).


def holdout(attempt_id):
    """Deterministic per attempt: True for the quarter held out."""
    return hashlib.sha256(str(attempt_id).encode("utf-8")).digest()[0] % HOLDOUT_SHARE == 0


class CrowdedFee:
    def __init__(self, *, now_iso_fn, alert_fn=None):
        if alert_fn is None:
            from sniper.services.memecoin_alerts import send_alert as alert_fn
        self.now_iso_fn, self.alert_fn = now_iso_fn, alert_fn
        self.day, self.spent, self.paged = None, 0, False
        self.status = dict(rule=None, target_lamports=None, day=None, spent_lamports=0,
                           daily_cap_lamports=None, capped=False, treated=0, holdout=0, sent_treated=0,
                           not_eligible={}, bound_at=None, accounting_state="known")

    def _roll(self, now_iso):
        day = str(now_iso)[:10]
        if day != self.day:
            self.day, self.spent, self.paged = day, 0, False
            self.status.update(day=day, spent_lamports=0, capped=False, bound_at=None, sent_treated=0,
                               accounting_state="known")

    def _sync_status(self, now_iso):
        from sniper.settings import memecoin as cfg
        target = int(cfg.MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS or 0)
        rule = cfg.MEMECOIN_SLOT_LANE_CROWDED_RULE
        cap = int(cfg.MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS)
        capped = (False if cap == 0 else None if self.spent is None
                  else self.spent >= cap - CU_ROUNDING_SLACK_LAMPORTS)
        self.status.update(rule=rule, target_lamports=target, daily_cap_lamports=cap,
                           spent_lamports=self.spent, accounting_state="unavailable" if self.spent is None else "known",
                           capped=capped, bound_at=(self.status["bound_at"] or now_iso) if capped else None)
        return target, rule, cap

    def seed(self, conn):
        """Today's treated sends already out (a restart must not reset the cap). Lane start only."""
        from sniper.persistence import db_memecoin as m
        now_iso = self.now_iso_fn()
        self._roll(now_iso)
        spent = count = 0
        try:
            for trace_json in m.slot_lane_traces_since(conn, self.day + "T00:00:00+00:00"):
                events = json.loads(trace_json)["substage_events"]
                if not isinstance(events, list):
                    raise ValueError("crowded fee trace events unavailable")
                sent = next((e for e in events if e.get("name") == SUBSTAGE_SENT
                             and e.get("arm") == "treated"), None)
                if sent is None or sent.get("crowded") in NOT_RAISED:
                    continue
                # The SIGNED bid when it was capped at signing: before 2026-10-15's fix every
                # crowded_fee_sent said 10M while 520k / 1.52M went out.
                signed = next((e for e in events if e.get("name") == "entry_bid_signed"), None)
                lamports = (signed or sent).get("priority_fee_lamports" if signed else "sent_lamports")
                if type(lamports) is not int or lamports <= 0:
                    raise ValueError("crowded signed fee unavailable")
                spent += lamports
                count += 1
            self.spent = spent
            self.status["sent_treated"] = count
        except Exception:
            self.spent = self.status["sent_treated"] = None
            logger.exception("[memecoin] crowded fee: seed failed; spend unavailable, finite-budget bids disabled")
        self._sync_status(now_iso)
        return self.spent

    def arm(self, buy, attempt_id, *, head_minus_s):
        """{'arm': 'treated'|'holdout'|None, 'reason', 'target_lamports', ...} for one send."""
        from sniper.settings import memecoin as cfg
        now_iso = self.now_iso_fn()
        self._roll(now_iso)
        target, rule, cap = self._sync_status(now_iso)
        out = dict(rule=rule, target_lamports=target, head_minus_s=head_minus_s, arm=None,
                   rule_version="crowded-early-sha256-q1-v1")
        reason = None
        if target <= 0 or rule != RULE_EARLY:
            reason = "off"
        elif (getattr(buy, "venue", "pump_curve") not in tuple(cfg.MEMECOIN_SLOT_LANE_CROWDED_VENUES)
              and getattr(buy, "graduation", None) is None):
            reason = "venue_not_in_scope"
        elif type(head_minus_s) is not int:
            reason = "head_unknown"
        elif head_minus_s >= 0:
            reason = "late"
        # THE REMAINING ALLOWANCE BOUNDS THE TARGET (Codex F9, 2026-10-17): checking prior spend only let
        # the last treated send of the day pass the cap (144M + 12M = 156M of 150M). Too little left
        # for one raised lamport: today's normal bid, never an entry refusal.
        left = target if cap == 0 or self.spent is None else cap - self.spent - CU_ROUNDING_SLACK_LAMPORTS
        if reason is None and cap:
            if self.spent is None:
                reason = "accounting_unavailable"
            elif left < 1:
                reason = "daily_cap"
        if reason is not None:
            self.status["not_eligible"][reason] = self.status["not_eligible"].get(reason, 0) + 1
            return dict(out, reason=reason)
        arm = "holdout" if holdout(attempt_id) else "treated"
        self.status[arm] += 1
        return dict(out, arm=arm, reason="eligible", target_lamports=min(target, left),
                    bounded_by_daily_cap=left < target)

    def sent(self, lamports):
        """Count a treated send against its arm's decision day, as the restart ledger does."""
        if type(lamports) is not int or lamports <= 0:
            return
        now_iso = self.now_iso_fn()
        if self.spent is not None:
            self.spent += lamports
            self.status["sent_treated"] += 1
        self._sync_status(now_iso)
        if self.status["capped"] and not self.paged:
            self.paged = True
            text = ("crowded priority fee: today's cap reached, %d lamports (%.3f SOL) over %d treated sends; "
                    "normal bids until UTC midnight. In memory, re-seeded from the ledger on restart."
                    % (self.spent, self.spent / 1e9, self.status["sent_treated"]))
            logger.warning("[memecoin] %s", text)
            try:
                self.alert_fn("SLOT-LANE CROWDED FEE CAP", text)
            except Exception:
                logger.exception("[memecoin] crowded fee page failed")
