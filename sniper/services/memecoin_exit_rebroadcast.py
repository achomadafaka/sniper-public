"""Re-broadcast a pinned exit's OWN signed bytes, so an unmined sell resolves in seconds.

WHY. When a funded sell is broadcast and the node answers neither confirmed nor failed, the close
claim is held and nothing else may sell. That hold is correct -- an ambiguous sell might land in
ten seconds, and a second one sells the bag twice. But it is passive: we wait for the account nonce
to move on its own before the claim can be proven dead and released.

Measured 2026-09-02 over 7 days: 8 exits entered `submitted_unknown` against 448 confirmed, and
they stayed unresolved 0 to 30 minutes, median ~9. Position 48595 spent 17-30 minutes pinned across
four attempts while the price moved.

WHAT THIS DOES, AND WHY IT CANNOT DOUBLE-SELL. It re-sends the EXACT signed payload already stored
on the intent. Identical bytes are the identical transaction with the identical hash and the
identical nonce, so the chain can include it at most once. This is not a replacement, not a
re-quote, and not a new signature -- it is the same sell, offered to the network again because the
first offer appears to have been dropped.

That is the whole safety argument, and it is why this is the right first move rather than a
same-nonce replacement at higher gas: a replacement is a DIFFERENT transaction and reasoning about
which one lands is a harder argument to be sure of on a funded path.

WHAT IT DOES NOT DO. It never signs, never re-quotes, never mutates the intent, never releases the
claim, and never closes a position. Resolution stays with the existing recovery, which reads the
receipt and applies its own proofs. This only makes the transaction more likely to be seen.

IT REFUSES TO ACT WHEN THE ANSWER IS ALREADY KNOWN. If the receipt exists -- confirmed or reverted
-- there is nothing to rebroadcast and the recovery owns it. Fail-closed on every unreadable input.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

COUNTS = {"eligible": 0, "resent": 0, "receipt_present": 0, "too_young": 0,
          "no_payload": 0, "send_failed": 0, "exhausted": 0, "not_due": 0, "in_flight": 0,
          "too_late": 0, "late": 0, "release_deferred": 0}

# Re-offers already made per SUBMISSION, keyed on tx_identity. Idempotent is not the same as free.
#
# THE CASE THIS BOUND EXISTS FOR, measured 2026-09-02: three bags pinned tonight were not lost in
# transit at all. A node refuses a transaction when `gasLimit * gasPrice` exceeds the wallet
# balance whatever the swap consumes -- 900,000 * 0.6628 gwei = 0.00059648 ETH against a wallet
# holding 0.00051674 -- so every sell was signed, broadcast, and rejected BEFORE the mempool.
# Re-offering identical bytes to a node that rejected them for insufficient funds earns the
# identical rejection, forever. Unbounded retries there are the 2026-08-21 quota finding wearing a
# new hat.
_ATTEMPTS = {}
# WHEN EACH COPY MAY GO (2026-09-30). Two copies: the first at 20 s of age, the second at least
# 15 s after it (35 s for a sell watched from its submission). The gap is on the caller's clock,
# so a sell first seen old (a restart forgets the count) still gets no burst. There is no third
# copy: a copy near EVM's 60 s release buys nothing worth racing it (8 dropped closes in 14 days).
FIRST_AT_S = 20.0
MIN_GAP_S = 15.0
MAX_REOFFERS = 2
# The per-request timeouts of a copy's own receipt read and send, under both venues' own.
SEND_TIMEOUT_S = 2.0
RECEIPT_TIMEOUT_S = 2.0
# A COPY MUST NEVER ARRIVE AFTER ITS CLAIM WAS RELEASED (Gamma, three rounds on this one race).
# On EVM the never-admitted proof releases a claim whose nonce is still FREE, and the re-armed
# exit signs a new sell on that nonce. Old bytes sent after that compete with it: if they mine,
# the bag is sold by a transaction the ledger has forgotten. A per-request timeout does not bound
# a copy (the RPC layer rotates across pool members, each with its own timeout), and the sweep's
# `now_iso` is not the time (its own reads run before the dispatch). So:
#   - ONE absolute deadline for the whole copy: COPY_DEADLINE_S after its dispatch, and never
#     later than the release age minus RELEASE_CLEARANCE_S after the submission, both on the
#     real clock. With the 60 s release that is 43 s: 17 s clear.
#   - every request the copy makes goes through `short_timeout_http`, which refuses to START a
#     request that could outlast that deadline (less than its own timeout left): rotations and
#     identity probes included. So no request of a copy is in the air after the deadline, and
#     none is cut short. The refusal is RequestNotSent: the RPC pool stops rotating and does not
#     quarantine a member for OUR deadline (the token-safety scope would: it cuts a request in
#     flight and its error is a TimeoutError, which the pool reads as the member's failure);
#   - the release path does not release a claim while a copy of that sell is in flight.
# Solana has no such release (past its blockhash a copy cannot land): no deadline there.
COPY_DEADLINE_S = 8.0
RELEASE_CLEARANCE_S = 17.0
MIN_COPY_BUDGET_S = 4.0           # less than one receipt read and one send at their caps: do not start
_LAST_AT = {}                     # tx_identity -> caller-clock epoch of its latest copy
_IN_FLIGHT = {}                   # tx identity -> its copy's monotonic deadline (None: no deadline)
_LOCK = threading.Lock()          # the sweep thread and the detached sends share this state
_monotonic = time.monotonic       # this module's own name, so a test never patches the global clock


_COPY = threading.local()         # .deadline: the running copy's absolute monotonic deadline


def _wall():
    """Real UTC now as an epoch (the corrected clock the executors stamp submissions with)."""
    from sniper.util.clock import real_utc_now
    return real_utc_now()


def reset_state():
    """Test hook. Production never calls this."""
    with _LOCK:
        for k in COUNTS:
            COUNTS[k] = 0
        _ATTEMPTS.clear()
        _LAST_AT.clear()
        _IN_FLIGHT.clear()


def _count(name):
    with _LOCK:
        COUNTS[name] += 1


def _epoch(iso):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _age_s(submitted_at, now_iso):
    from datetime import datetime
    try:
        a = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        return (b - a).total_seconds()
    except (TypeError, ValueError):
        return None


def _not_due(tx_hash, age, now, first_at):
    """The schedule's verdict for this submission's NEXT copy, or None when it may go now.
    No I/O: the sweep calls this on its own thread."""
    with _LOCK:
        made, last = _ATTEMPTS.get(tx_hash, 0), _LAST_AT.get(tx_hash)
    if made >= MAX_REOFFERS:
        return "reoffers_exhausted"       # a node that keeps refusing will keep refusing
    if age < first_at:
        return "too_young"                # it may simply confirm
    if last is not None and (now is None or now - last < MIN_GAP_S):
        return "not_due"
    return None


def _read_intent(intent_json, submitted_at, now_iso):
    """(intent, age, reason): the reason names why nothing can be re-offered."""
    try:
        intent = json.loads(intent_json) if isinstance(intent_json, str) else dict(intent_json or {})
    except Exception:
        return None, None, "unreadable_intent"
    age = _age_s(submitted_at, now_iso)
    if age is None:
        return intent, None, "unreadable_timestamp"
    if not intent.get("tx_identity") or not intent.get("signed_payload_b64"):
        _count("no_payload")
        return intent, age, "no_signed_payload"   # nothing to re-offer; never synthesise one
    return intent, age, None


def rebroadcast_pinned_exit(intent_json, submitted_at, *, now_iso, receipt_fn, send_fn,
                            min_age_s=FIRST_AT_S, send_before=None):
    """Re-send one pinned exit's own bytes. Returns the reason string for what happened.

    `receipt_fn(tx_hash)` -> a receipt dict, or None when the chain has never seen it.
    `send_fn(raw_bytes)`  -> whatever the chain seam returns; only truthiness is read.
    `min_age_s` is the age of the first copy; later copies follow MIN_GAP_S apart.

    SYNCHRONOUS: it reads the receipt and sends on the calling thread. The exit sweep must use
    `rebroadcast_pinned_exit_detached`, which never waits for either.
    `send_before`: a monotonic instant, the copy's absolute deadline. A copy whose receipt read
    left less than one send's timeout before it is dropped and still uses an attempt.
    """
    intent, age, reason = _read_intent(intent_json, submitted_at, now_iso)
    if reason:
        return reason
    tx_hash, payload_b64 = intent["tx_identity"], intent["signed_payload_b64"]
    now = _epoch(now_iso)
    verdict = _not_due(tx_hash, age, now, float(min_age_s))
    if verdict:
        _count({"reoffers_exhausted": "exhausted"}.get(verdict, verdict))
        return verdict                    # too_young: it may simply confirm

    try:
        if receipt_fn(tx_hash) is not None:
            _count("receipt_present")
            return "receipt_present"      # the chain has an answer; the recovery owns it
    except Exception:
        logger.exception("pinned-exit receipt read failed tx=%s", tx_hash)
        return "receipt_unreadable"       # unknown chain state is never a reason to re-send

    try:
        raw = base64.b64decode(payload_b64)
    except Exception:
        _count("no_payload")
        return "undecodable_payload"

    with _LOCK:
        made = _ATTEMPTS.get(tx_hash, 0)
        if made >= MAX_REOFFERS:          # another thread took the last one since the check
            COUNTS["exhausted"] += 1
            return "reoffers_exhausted"
        COUNTS["eligible"] += 1
        _ATTEMPTS[tx_hash] = made + 1     # a copy that raises, is refused or is late still counts
        _LAST_AT[tx_hash] = now
    if send_before is not None and send_before - _monotonic() < SEND_TIMEOUT_S:
        _count("late")
        logger.warning("pinned exit re-offer dropped, its receipt read left no time for a send "
                       "before the copy's deadline: tx=%s attempt=%d/%d", tx_hash, made + 1,
                       MAX_REOFFERS)
        return "late"
    try:
        sent = send_fn(raw)
    except Exception:
        logger.exception("pinned-exit rebroadcast failed tx=%s attempt=%d/%d", tx_hash, made + 1,
                         MAX_REOFFERS)
        _count("send_failed")
        return "send_raised"
    if not sent:
        _count("send_failed")
        reason_fn = getattr(send_fn, "reason_fn", None)
        try:
            words = reason_fn() if callable(reason_fn) else None
        except Exception:
            words = None
        # ONE LINE PER REFUSED COPY, with the node's own words when the seam kept them. The node
        # says "nonce too low" or "already processed" when the first copy landed after all.
        logger.warning("pinned exit re-offer refused: tx=%s attempt=%d/%d age=%.0fs node=%s",
                       tx_hash, made + 1, MAX_REOFFERS, age, words or "no message")
        return "send_rejected"
    _count("resent")
    logger.warning("pinned exit rebroadcast: tx=%s age=%.0fs attempt=%d/%d — identical bytes, "
                   "same nonce, cannot double-sell", tx_hash, age, made + 1, MAX_REOFFERS)
    return "resent"


def _spawn(job):
    """Seam: run one detached re-offer. Tests replace it to run the job inline."""
    threading.Thread(target=job, name="memecoin-pinned-reoffer", daemon=True).start()


def copy_in_flight(tx_identity):
    """True while a detached copy of this sell may still send. The release path asks: a claim is
    not released under it. Counted, so the heartbeat shows it.

    It ends by itself: past the copy's deadline no request of it can start, and one SEND_TIMEOUT_S
    later none is in the air. A copy thread that never returns cannot hold a claim for ever."""
    wanted = None if tx_identity is None else str(tx_identity).lower()
    with _LOCK:
        busy = False
        for held, deadline in _IN_FLIGHT.items():
            if str(held).lower() == wanted:
                busy = deadline is None or _monotonic() <= deadline + SEND_TIMEOUT_S
        if busy:
            COUNTS["release_deferred"] += 1
    return busy


def rebroadcast_pinned_exit_detached(intent_json, submitted_at, *, now_iso, receipt_fn, send_fn,
                                     release_age_s=None):
    """The exit sweep's entry point: decide on the caller's thread WITHOUT I/O, then read the
    receipt and send on a daemon thread. Returns at once with `dispatched` or the reason nothing
    was started; the copy's outcome lands in the counters and the log when it finishes.

    NOTHING MAY SLOW AN EXIT SWEEP (CLAUDE.md rule 5.2). A sell is pinned when its node is in
    trouble, so a synchronous receipt read plus send here would hold every other position's exit
    check for the transport's whole timeout.

    One detached copy per submission at a time. `release_age_s`: the age at which this venue
    may release the claim with the nonce still free (EVM): the copy then has ONE absolute deadline
    (see COPY_DEADLINE_S). None (Solana: past its blockhash a copy cannot land): no deadline.
    """
    intent, age, reason = _read_intent(intent_json, submitted_at, now_iso)
    if reason:
        return reason
    tx_hash = intent["tx_identity"]
    verdict = _not_due(tx_hash, age, _epoch(now_iso), FIRST_AT_S)
    if verdict:
        _count({"reoffers_exhausted": "exhausted"}.get(verdict, verdict))
        return verdict
    # The deadline comes from the REAL clock and the submission stamp, never from `now_iso`: the
    # sweep's own reads for this and earlier positions ran between that stamp and this line.
    deadline = None
    if release_age_s is not None:
        last_send = _epoch(submitted_at) + float(release_age_s) - RELEASE_CLEARANCE_S
        budget = min(COPY_DEADLINE_S, last_send - _wall())
        if budget < MIN_COPY_BUDGET_S:
            _count("too_late")
            return "too_late"             # its claim may be released before a copy could arrive
        deadline = _monotonic() + budget
    with _LOCK:
        if tx_hash in _IN_FLIGHT:
            COUNTS["in_flight"] += 1
            return "in_flight"
        _IN_FLIGHT[tx_hash] = deadline

    def job():
        _COPY.deadline = deadline          # read by short_timeout_http on this thread
        try:
            rebroadcast_pinned_exit(intent, submitted_at, now_iso=now_iso, receipt_fn=receipt_fn,
                                    send_fn=send_fn, send_before=deadline)
        except Exception:
            logger.exception("detached pinned-exit re-offer failed tx=%s", tx_hash)
        finally:
            _COPY.deadline = None
            with _LOCK:
                _IN_FLIGHT.pop(tx_hash, None)
    try:
        _spawn(job)
    except Exception:
        with _LOCK:
            _IN_FLIGHT.pop(tx_hash, None)
        logger.exception("pinned-exit re-offer could not start tx=%s", tx_hash)
        return "spawn_failed"
    return "dispatched"


def _copy_remaining_s():
    """Seconds left of the copy running on THIS thread; infinite when it has no deadline."""
    deadline = getattr(_COPY, "deadline", None)
    return float("inf") if deadline is None else deadline - _monotonic()


def short_timeout_http(http, timeout_s=SEND_TIMEOUT_S):
    """The same transport for a copy's requests, each capped at `timeout_s`. When the copy on this
    thread has a deadline, a request that could outlast it is not started."""
    def capped(method, url, **kwargs):
        if _copy_remaining_s() < float(timeout_s):
            from sniper.execution.evm_rpc_pool import RequestNotSent
            raise RequestNotSent("pinned re-offer: the copy's deadline leaves no time for a request")
        given = kwargs.get("timeout")
        kwargs["timeout"] = float(timeout_s) if given is None else min(float(given), float(timeout_s))
        return http(method, url, **kwargs)
    capped.request_remaining_s = _copy_remaining_s       # a hedged read's wait ends at the deadline
    return capped


def stored_payload_sender(send_fn, reason_fn=None):
    """The context's re-offer seam over an executor's own send seam, or None without one.

    An intent stores the payload EXACTLY as the executor first sent it, as text: the `0x...` hex
    string on EVM, the base64 string on Solana, utf-8 encoded and then base64. `rebroadcast_pinned_exit`
    hands back those bytes, and the send seams take the string. Bytes that are not that text raise,
    which the caller counts as `send_raised`: nothing is ever re-encoded or re-signed here."""
    if not callable(send_fn):
        return None

    def send(raw):
        return send_fn(bytes(raw).decode("ascii"))
    send.reason_fn = reason_fn            # the node's words for a refused copy, on this thread
    return send


def snapshot():
    """Counters plus how many submissions are mid-retry, so a lane quietly exhausting every
    re-offer is visible rather than inferred from an absence of effect."""
    with _LOCK:
        return {"tracked": len(_ATTEMPTS), "in_flight_now": len(_IN_FLIGHT), **dict(COUNTS)}
