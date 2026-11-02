"""WO-6 — immutable ExitIntent binding the decision to the claim it is about to sign.

The close/derisk claim machines (`db_memecoin.claim_position_close`/`claim_position_derisk` +
`solana_exit_executor`) already give one-shot ownership, durable signed identity before broadcast,
and submitted-unknown safety. WO-6 adds the missing link: the immutable snapshot of WHY this claim
was taken, re-verified in the moment BEFORE signing so a decision that has since been superseded (or
a position closed/re-claimed while we walked the slippage ladder) is refused pre-sign instead of
producing a stale on-chain sale.

Pure value + pure verifier — no DB, no network. The executor persists `to_json()` on the position at
claim time and calls `verify_exit_intent` against a fresh re-read right before `sign_fn`.
"""
from __future__ import annotations

import json
import hashlib
import base64
from dataclasses import asdict, dataclass, replace
from typing import Optional

CLOSE, DERISK, PARTIAL = "close", "derisk", "partial"

# Claim-ownership column per action — the live owner token the intent must still match pre-sign.
_CLAIM_COL = {CLOSE: "close_claim_id", DERISK: "derisk_claim_id", PARTIAL: "derisk_claim_id"}
# Fraction of the claim-time balance below which a drop reads as a concurrent sale (double-sell).
_BALANCE_DROP_TOL = 0.02


@dataclass(frozen=True)
class ExitIntent:
    """One exit's immutable decision+claim binding. The decision half (action, fraction,
    controller_revision, decision_state_id, balance_snapshot) is captured at claim; the quote half
    (quote_digest, min_output, quote_time, blockhash, last_valid_height) is filled at sign via
    `with_quote` so rebroadcast can be gated to identical bytes inside the blockhash validity."""
    position_id: int
    claim_id: str
    action: str                                # CLOSE | DERISK
    fraction: float
    controller_revision: int
    balance_snapshot: Optional[float] = None
    decision_state_id: Optional[int] = None
    quote_digest: Optional[str] = None
    min_output: Optional[float] = None
    quote_time: Optional[str] = None
    blockhash: Optional[str] = None
    last_valid_height: Optional[int] = None
    sell_amount_atomic: Optional[int] = None
    min_output_atomic: Optional[int] = None
    route_id: Optional[str] = None
    quote_asset_usd: Optional[float] = None
    signed_payload_digest: Optional[str] = None
    signed_payload_b64: Optional[str] = None
    tx_identity: Optional[str] = None
    nonce: Optional[int] = None
    source_request_id: Optional[str] = None
    balance_snapshot_atomic: Optional[str] = None
    sell_decimals: Optional[int] = None

    def to_json(self) -> str:
        value = asdict(self)
        for key in ('source_request_id', 'balance_snapshot_atomic', 'sell_decimals'):
            if value[key] is None:
                value.pop(key)
        return json.dumps(value, sort_keys=True)

    @staticmethod
    def from_json(raw) -> Optional["ExitIntent"]:
        try:
            d = json.loads(raw)
            if not isinstance(d, dict):
                return None
            known = ExitIntent.__dataclass_fields__
            value = ExitIntent(**{k: v for k, v in d.items() if k in known})
            if value.action not in (CLOSE, DERISK, PARTIAL) or not value.claim_id:
                return None
            if not 0.0 < float(value.fraction) <= 1.0:
                return None
            if value.action == PARTIAL:
                raw = value.balance_snapshot_atomic
                if (not isinstance(value.source_request_id, str) or not value.source_request_id
                        or type(raw) is not str or not raw.isascii() or not raw.isdecimal() or int(raw) <= 0
                        or type(value.sell_decimals) is not int or not 0 <= value.sell_decimals <= 255
                        or not float(value.fraction) < 1.0):
                    return None
            return value
        except (TypeError, ValueError, KeyError):
            return None

    def with_quote(self, *, quote_digest, min_output=None, quote_time=None, blockhash=None,
                   last_valid_height=None, sell_amount_atomic=None, min_output_atomic=None,
                   route_id=None, quote_asset_usd=None) -> "ExitIntent":
        return replace(self, quote_digest=quote_digest, min_output=min_output,
                       quote_time=quote_time, blockhash=blockhash,
                       last_valid_height=last_valid_height,
                       sell_amount_atomic=sell_amount_atomic,
                       min_output_atomic=min_output_atomic, route_id=route_id,
                       quote_asset_usd=quote_asset_usd)

    def with_signed_payload(self, payload, *, tx_identity, nonce=None) -> "ExitIntent":
        if isinstance(payload, bytes):
            raw = payload
        elif isinstance(payload, bytearray):
            raw = bytes(payload)
        else:
            raw = str(payload).encode("utf-8")
        return replace(
            self, signed_payload_digest=hashlib.sha256(raw).hexdigest(),
            signed_payload_b64=base64.b64encode(raw).decode("ascii"),
            tx_identity=str(tx_identity), nonce=(None if nonce is None else int(nonce)))

    def with_validity(self, *, blockhash=None, last_valid_height=None) -> "ExitIntent":
        return replace(
            self, blockhash=(str(blockhash) if blockhash is not None else self.blockhash),
            last_valid_height=(int(last_valid_height) if last_valid_height is not None
                               else self.last_valid_height))


def bind_quote(intent: ExitIntent, quote, *, quote_time=None, quote_asset_usd=None) -> ExitIntent:
    """Bind the canonical quote and exact atomic action amount before signing."""
    try:
        encoded = json.dumps(quote, sort_keys=True, separators=(",", ":"), default=str).encode()
    except (TypeError, ValueError):
        encoded = repr(quote).encode()
    get = quote.get if isinstance(quote, dict) else (lambda _name, default=None: default)

    def _integer(*names):
        for name in names:
            value = get(name)
            if value is not None:
                try:
                    parsed = int(value)
                    return parsed if parsed >= 0 else None
                except (TypeError, ValueError):
                    return None
        return None

    route_id = get("route_id") or get("routeId")
    if route_id is None:
        plan = get("routePlan") or ()
        try:
            route_id = ",".join(
                str((leg.get("swapInfo") or {}).get("ammKey") or "") for leg in plan) or None
        except (TypeError, AttributeError):
            route_id = None
    return intent.with_quote(
        quote_digest=hashlib.sha256(encoded).hexdigest(),
        min_output=_integer("otherAmountThreshold", "minBuyAmount"),
        min_output_atomic=_integer("otherAmountThreshold", "minBuyAmount"),
        sell_amount_atomic=_integer("inAmount", "sellAmount"), route_id=route_id,
        quote_time=quote_time or get("quoted_at") or get("quoteTime"),
        blockhash=get("blockhash") or get("recentBlockhash"),
        quote_asset_usd=quote_asset_usd,
        last_valid_height=_integer("lastValidBlockHeight", "last_valid_height"))


EXIT_FLOOR_RATIO_LIMIT = 20.0


def exit_floor_is_plausible(min_output_usd, reference_value_usd, *,
                            limit: float = EXIT_FLOOR_RATIO_LIMIT) -> bool:
    """Is the signed slippage floor plausible against an independently-derived bag value?

    `quote_binding_complete` only ever asked whether the floor was POSITIVE. UNIFROG
    (position 27097, REAL, 4663, 2026-08-05) shows why that is not enough: the exit quote
    was taken inside a seven-second window in which the tape had collapsed 397x, so
    `min_output_atomic` came out at ~$0.02 and the swap sold a ~$6 bag for $0.02. The floor
    did exactly what it was told; it was told nonsense. A floor derived from a corrupted
    quote is not a floor, and positivity cannot detect that.

    The reference must come from OUTSIDE the quote being checked. Comparing the floor to the
    same quote's own `buyAmount` proves nothing -- the pair stays internally consistent while
    both are wrong together, which is exactly what happened here.

    Returns True (allow) whenever there is no usable reference: a missing reference means we
    cannot judge, and refusing every exit we cannot price would strand positions. The guard
    only ever rejects a floor it can positively show is implausible.
    """
    try:
        reference = float(reference_value_usd)
        floor = float(min_output_usd)
        bound = float(limit)
    except (TypeError, ValueError):
        return True
    if not (reference > 0) or bound <= 1:
        return True
    if floor < 0:
        return True
    return floor >= reference / bound


def exit_floor_refused(intent, quote, position, sell_quantity, *,
                       floor_usd_fn=None, reference_value_usd_fn=None,
                       logger=None, position_id=None) -> bool:
    """Should this signed quote be refused because its floor is implausible?

    One implementation for all four pre-sign gates -- Solana close/de-risk and EVM close/de-risk --
    so a path cannot be protected on one chain and bare on another. Opt-in: with either seam
    missing it returns False and the caller behaves exactly as before.

    `sell_quantity` is the amount ACTUALLY being sold, not the whole bag. On a de-risk only
    `fraction` of the balance goes, so valuing the full balance would compare a partial floor
    against a whole-bag reference and refuse legitimate small de-risks (a 4% partial reads as 25x).

    Never raises: a seam that fails is logged and the exit is allowed, because refusing an exit we
    cannot price strands a funded position, which is worse than the case being guarded.
    """
    if floor_usd_fn is None or reference_value_usd_fn is None:
        return False
    try:
        floor_usd = floor_usd_fn(intent, quote)
        reference_usd = reference_value_usd_fn(position, sell_quantity)
    except Exception:
        if logger is not None:
            logger.exception(
                "exit floor sanity inputs unavailable (pid=%s) — allowing the exit", position_id)
        return False
    if exit_floor_is_plausible(floor_usd, reference_usd):
        return False
    if logger is not None:
        logger.warning(
            "exit floor implausible (pid=%s): floor $%s vs reference $%s — refusing to sign this "
            "quote, retrying next sweep", position_id, floor_usd, reference_usd)
    return True


def quote_binding_complete(intent: ExitIntent) -> bool:
    """True only when the exact action amount and executable minimum output are positive."""
    return bool(
        intent.quote_digest
        and intent.sell_amount_atomic is not None and int(intent.sell_amount_atomic) > 0
        and intent.min_output_atomic is not None and int(intent.min_output_atomic) > 0)


def build_exit_intent(position, *, claim_id, action, fraction, balance=None,
                      decision_state_id=None, source_request_id=None,
                      balance_snapshot_atomic=None, sell_decimals=None) -> ExitIntent:
    """Capture the decision snapshot at claim time. `controller_revision` is the position's WO-5A
    hysteresis revision — if a concurrent reducer write advances it before we sign, the evidence the
    decision rested on has moved and the intent is superseded."""
    get = position.get if hasattr(position, "get") else (lambda k, d=None: position[k])
    return ExitIntent(
        position_id=get("id"), claim_id=claim_id, action=action, fraction=float(fraction),
        controller_revision=int(get("controller_revision") or 0),
        balance_snapshot=(None if balance is None else float(balance)),
        decision_state_id=decision_state_id, source_request_id=source_request_id,
        balance_snapshot_atomic=balance_snapshot_atomic, sell_decimals=sell_decimals)


def verify_exit_intent(intent: ExitIntent, position, *, balance=None,
                       current_decision_state_id=None, balance_atomic=None) -> Optional[str]:
    """Re-verify the intent against a FRESH position read right before signing. Returns None when the
    intent still holds, or a typed refusal string when it does not (never sign on a refusal):

      - position_gone      : the position is closed/absent (a concurrent full exit or reconciliation)
      - claim_lost         : the live claim owner is no longer us (re-claimed / released)
      - decision_superseded: the controller revision advanced -> the decision's evidence has moved
      - balance_changed    : sellable balance moved materially -> stale exact action amount
    """
    if not position or not position.get("is_open"):
        return "position_gone"
    if str(position.get(_CLAIM_COL[intent.action]) or "") != str(intent.claim_id):
        return "claim_lost"
    if intent.action == PARTIAL:
        if position.get('mirror_disabled') or position.get('tracking_paused_at'):
            return 'source_mirror_held'
        if (type(balance_atomic) is not int or intent.balance_snapshot_atomic is None
                or str(balance_atomic) != intent.balance_snapshot_atomic):
            return "balance_changed"
    if int(position.get("controller_revision") or 0) != int(intent.controller_revision):
        return "decision_superseded"
    if intent.decision_state_id is not None:
        if current_decision_state_id is None:
            return "decision_state_unverifiable"
        if int(current_decision_state_id) != int(intent.decision_state_id):
            return "decision_state_superseded"
    if balance is not None and intent.balance_snapshot is not None:
        expected = float(intent.balance_snapshot)
        current = float(balance)
        if expected <= 0 or abs(current - expected) > expected * _BALANCE_DROP_TOL:
            return "balance_changed"
    return None
