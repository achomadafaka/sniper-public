"""Deterministic Office Trader (pilot seat; Codex T15 call shapes): one adjudicated, Risk-checked decision bundle becomes
at most one idempotent ledger.submit, and every decision gets an immutable receipt in the ledger DB, a veto included,
so a missing order is never read as a veto or a SKIP.

Identity comes from the trusted supervisor Context (role Trader, its book and decision), never from a model. The
order id is the action key. Receipt and submit are one ledger write transaction."""
from __future__ import annotations

import json

from sniper.office import ledger as L

# decision_receipts DDL lives in the ledger SCHEMA (created by open_ledger, never inside a caller transaction)
TRADER_ROLE = "trader"                           # supervisor.Context carries the lower-cased capability role
_FIELDS = ("action_key", "decision_id", "book_id", "bundle_hash", "outcome", "reason", "order_id", "risk_version")


def _receipt(row):
    return {k: row[k] for k in _FIELDS}


def lookup_decision(conn, *, action_key, decision_id, book_id):
    """The committed receipt for an action key, or None (no receipt: nothing was decided, NOT a veto). A receipt under
    that key for another decision or book raises action_key_conflict. SELECT-only."""
    try:
        row = conn.execute("SELECT * FROM decision_receipts WHERE action_key=?", (action_key,)).fetchone()
    except Exception as exc:                          # the table does not exist yet on a fresh read-only handle
        if "no such table" in str(exc):
            return None
        raise
    if row is None:
        return None
    if (row["decision_id"], row["book_id"]) != (decision_id, book_id):
        raise ValueError("action_key_conflict")
    return _receipt(row)


def execute_decision(conn, policy, *, context, action_key, bundle, risk_result, now, sol_usd=None, sol_at=None,
                     order_metadata=None,decision_evidence=None):
    """-> the receipt {action_key, decision_id, book_id, bundle_hash, outcome submitted|refused|vetoed, reason,
    order_id, risk_version}. A retry with the same identity returns the stored receipt with no second effect."""
    if getattr(context, "role", None) != TRADER_ROLE:
        raise ValueError("role_not_trader")
    if not isinstance(bundle, dict) or context.decision_id != bundle.get("decision_id"):
        raise ValueError("context_decision_mismatch")
    if not isinstance(action_key, str) or not action_key:
        raise ValueError("action_key_invalid")
    if risk_result.decision == "allow" and (
            bundle.get("decision") != "ENTER" or risk_result.bundle_hash != bundle.get("bundle_hash")
            or risk_result.decision_id != bundle.get("decision_id") or risk_result.policy_hash != L._hash(policy.frozen())):
        raise ValueError("risk_not_bound")           # an allow is valid only for the exact bundle and policy it judged
    with L._write(conn):
        if conn.execute("SELECT 1 FROM books WHERE book_id=?", (context.book_id,)).fetchone() is None:
            raise ValueError("book_unknown")
        prior = conn.execute("SELECT * FROM decision_receipts WHERE action_key=?", (action_key,)).fetchone()
        if prior is not None:
            if (prior["decision_id"], prior["book_id"], prior["bundle_hash"]) != (
                    context.decision_id, context.book_id, bundle.get("bundle_hash")):
                raise ValueError("action_key_conflict")
            if decision_evidence is not None and (prior['judgements_hash'],prior['decision_label']) != (
                    decision_evidence['judgements_hash'],decision_evidence['decision_label']):
                raise ValueError('action_key_conflict')
            return _receipt(prior)
        if risk_result.decision != "allow":
            outcome, reason, order_id = "vetoed", risk_result.reason, None
        else:
            r = L.submit(conn, policy, book_id=context.book_id, order_id=action_key, network_id=bundle["network_id"],
                         mint=bundle["mint"], pool=bundle["pool"], decision_bundle_hash=bundle["bundle_hash"], now=now,
                         sol_usd=sol_usd, sol_at=sol_at, size_usd=risk_result.clip_usd,metadata=order_metadata)
            outcome, reason = ("submitted", None) if r.state == "pending" else ("refused", r.reason)
            order_id = action_key if outcome == "submitted" else None
        evidence=decision_evidence or {}
        conn.execute("INSERT INTO decision_receipts(action_key,decision_id,book_id,bundle_hash,outcome,reason,order_id,risk_version,actor_id,created_at,epoch_id,judgements_hash,decision_label) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (action_key, context.decision_id, context.book_id, bundle.get("bundle_hash"), outcome, reason,
                      order_id, risk_result.risk_version, context.actor_id, now, getattr(context,'epoch_id',None),
                      evidence.get('judgements_hash'),evidence.get('decision_label')))
        return _receipt(conn.execute("SELECT * FROM decision_receipts WHERE action_key=?", (action_key,)).fetchone())


def bootstrap_pilot(conn, *, epoch_id, book_id, clip_usd, rule_version, capital_usd, actor, now):
    """The pilot's discovery-only book (no evaluated rows, so the verdict scope stays empty): open_epoch, open_book
    kind 'discovery', initial capital. Idempotent with the same values; different values raise
    pilot_bootstrap_conflict."""
    note = json.dumps({"pilot": True, "evaluation": False}, sort_keys=True)
    with L._write(conn):
        epoch = conn.execute("SELECT clip_usd, rule_version FROM ledger_epochs WHERE epoch_id=?", (epoch_id,)).fetchone()
        book = conn.execute("SELECT epoch_id, kind FROM books WHERE book_id=?", (book_id,)).fetchone()
        cap = conn.execute("SELECT usd FROM capital_flows WHERE book_id=? AND kind='initial'", (book_id,)).fetchone()
        if epoch is None:
            L.open_epoch(conn, epoch_id=epoch_id, clip_usd=clip_usd, rule_version=rule_version, actor=actor, note=note,
                         now=now)
        elif tuple(epoch) != (clip_usd, rule_version):
            raise ValueError("pilot_bootstrap_conflict")
        if book is None:
            L.open_book(conn, book_id=book_id, epoch_id=epoch_id, kind="discovery", actor=actor, note=note, now=now)
        elif tuple(book) != (epoch_id, "discovery"):
            raise ValueError("pilot_bootstrap_conflict")
        if cap is None:
            L.fund(conn, book_id=book_id, usd=capital_usd, kind="initial", actor=actor, note=note, now=now)
        elif cap[0] != capital_usd:
            raise ValueError("pilot_bootstrap_conflict")


def pilot_config(path):
    """(OfficeBookPolicy, bootstrap dict) from config/office/book_policy_pilot.json."""
    doc = json.loads(open(path).read())
    if doc.get("label") != "pilot" or doc.get("evaluation") is not False:
        raise ValueError("not_a_pilot_policy")
    return L.OfficeBookPolicy(**{**doc["policy"], "chains": ("solana",)}), doc["bootstrap"]


def bootstrap_from_config(conn, path, *, now):
    """Bootstrap the pilot book with the values frozen in the pilot policy file (Alpha's ruling)."""
    policy, b = pilot_config(path)
    bootstrap_pilot(conn, epoch_id=b["epoch_id"], book_id=b["book_id"], clip_usd=policy.clip_usd,
                    rule_version=b["rule_version"], capital_usd=b["capital_usd"], actor=b["actor"], now=now)
    return policy, b
