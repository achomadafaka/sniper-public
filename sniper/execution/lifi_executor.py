"""Funded LI.FI network and code-authority boundary for chain 4663."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlencode

from eth_abi import encode
from eth_utils import keccak

from sniper.execution import evm_wallet
from sniper.execution.lifi_route import LifiQuoteError, decode_quote, economic_roundtrip
from sniper.execution.submission_transport import sha256_hex
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg


class LifiAuthorityError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuoteResult:
    ok: bool
    stage: str
    role: str
    failure_type: str | None
    received_at: str
    request_sha256: str
    response_sha256: str | None
    quota: dict[str, int | None]
    decoded: object | None = None


@dataclass(frozen=True)
class AuthorityProof:
    stage: str
    received_at: str
    request_sha256: str
    response_sha256: str
    block_number: int
    block_hash: str
    facet: str
    code_hashes: dict[str, str]
    proof_sha256: str


@dataclass(frozen=True)
class ExecutionProof:
    transaction: dict
    signed: object
    authority: AuthorityProof


@dataclass(frozen=True)
class ProofResult:
    ok: bool
    code: str
    proof: AuthorityProof | None = None


@dataclass(frozen=True)
class ApprovalResult:
    status: str
    tx_hash: str | None = None
    allowance_atomic: int | None = None
    cleanup_required: bool = False
    block_number: int | None = None


@dataclass(frozen=True)
class SettlementResult:
    ok: bool
    code: str
    input_atomic: int | None = None
    output_atomic: int | None = None
    source: str | None = None


@dataclass(frozen=True)
class DropProof:
    finalized_block_number: int
    finalized_block_hash: str
    replacement_tx_hash: str
    replacement_receipt_hash: str
    allowance_atomic: int
    balance_sha256: str | None


@dataclass(frozen=True)
class RecoverySnapshot:
    scanned: int
    resolved: int
    unresolved: int
    invalid: int
    scan_complete: bool
    entry_admitted: bool


@dataclass(frozen=True)
class LifiLifecycle:
    conn: object
    lifecycle_id: str
    custody: str
    chain_id: str
    nonce_fn: object
    gas_price_fn: object
    sign_fn: object
    send_fn: object
    receipt_fn: object
    allowance_fn: object
    now_fn: object
    approve_gas: int = 100_000


# li.quest sits behind Cloudflare, which answers a request with no User-Agent with
# HTTP 403 "error code: 1010" and NO ratelimit headers at all. The client marks itself
# authenticated only on 2xx plus all three quota headers, so an absent User-Agent left
# LI.FI permanently unauthenticated and every funded 4663 entry disabled. Same failure
# already handled for Jupiter (solana_quote) and GeckoTerminal (geckoterminal_quote).
# Honest client identity: verified 2026-09-02 to return 200 with limit=12000.
# Headers never enter _canonical_hash, so this changes no request/response identity.
_UA = "sniper-memecoin/1.0"


def _canonical_hash(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _quota(headers):
    values = {str(key).lower(): value for key, value in (headers or {}).items()}

    def number(name):
        for key in (name, "x-" + name):
            try:
                return int(values[key])
            except KeyError:
                continue
            except (TypeError, ValueError):
                return None
        return None

    return {
        "limit": number("ratelimit-limit"),
        "remaining": number("ratelimit-remaining"),
        "reset": number("ratelimit-reset"),
    }


def _no_route(payload):
    if not isinstance(payload, dict):
        return False
    return str(payload.get("code") or "").upper() in {"NO_ROUTE", "NO_QUOTE", "1002"}


def _query_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


class LifiClient:
    """Issue one LI.FI request and return only validated values and hashes."""

    def __init__(
            self, api_url, *, api_key, manifest, http=None, corrected_now,
            decoder=decode_quote, quota_guard=None, authentication_state=None):
        self.api_url = str(api_url)
        self.api_key = str(api_key or "")
        self.manifest = manifest
        if http is None:
            def http(method, url, **kwargs):
                try:
                    return evm_wallet._http(method, url, **kwargs)
                except HTTPError as exc:
                    try:
                        payload = json.loads(exc.read().decode())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        payload = {}
                    return exc.code, payload, dict(exc.headers or {})
        self.http = http
        self.corrected_now = corrected_now
        self.decoder = decoder
        self.quota_guard = quota_guard
        self.authentication_state = authentication_state

    def _validate_request(self, request):
        if str(request.get("fromChain")) != "4663" or str(request.get("toChain")) != "4663":
            raise ValueError("LI.FI chain is invalid")
        if request.get("allowBridges") != []:
            raise ValueError("LI.FI bridges are not disabled")
        if request.get("allowExchanges") != self.manifest["allow_exchanges"]:
            raise ValueError("LI.FI exchange authority is invalid")
        if str(request.get("fee")) != "0":
            raise ValueError("LI.FI integrator fee is invalid")
        if request.get("simulation") is not True:
            raise ValueError("LI.FI simulation is disabled")
        try:
            impact = float(request["maxPriceImpact"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("LI.FI price impact is invalid") from exc
        if impact <= 0:
            raise ValueError("LI.FI price impact is invalid")

    def quote(self, request, role, stage):
        received_at = self.corrected_now()
        try:
            self._validate_request(request)
        except ValueError:
            return QuoteResult(
                False, stage, role, "invalid_request", received_at,
                _canonical_hash(request), None, _quota(None),
            )
        request_hash = _canonical_hash(request)
        query = urlencode(
            [(key, _query_value(value)) for key, value in request.items()],
            doseq=False,
        )
        url = self.api_url + ("&" if "?" in self.api_url else "?") + query
        headers = {"user-agent": _UA}
        if self.api_key:
            headers["x-lifi-api-key"] = self.api_key
        reserve = (
            self.quota_guard.reserve_request(role)
            if self.quota_guard is not None else nullcontext()
        )
        try:
            with reserve:
                status, payload, response_headers = self.http(
                    "GET", url, headers=headers,
                )
        except TimeoutError:
            if self.quota_guard is not None:
                self.quota_guard.observe_response(None)
            return QuoteResult(
                False, stage, role, "timeout", received_at,
                request_hash, None, _quota(None),
            )
        except Exception:
            if self.quota_guard is not None:
                self.quota_guard.observe_response(None)
            return QuoteResult(
                False, stage, role, "provider_error", received_at,
                request_hash, None, _quota(None),
            )
        received_at = self.corrected_now()
        response_hash = _canonical_hash(payload)
        quota = _quota(response_headers)
        if self.quota_guard is not None:
            self.quota_guard.observe_response(quota)
        # AUTHENTICATION IS ABOUT THE KEY, NOT ABOUT ROUTE AVAILABILITY (2026-09-04).
        #
        # This required a 2xx, so a perfectly healthy "no route for this coin" -- which LI.FI
        # answers with a non-2xx -- flipped the verdict to UNAUTHENTICATED and called
        # `disable_entries()`. One ordinary market condition therefore switched the aggregator lane
        # off and reported the whole integration as unauthenticated until some later quote happened
        # to succeed. Live 2026-09-04: authenticated at 09:19, a no-route answer at 09:22 that
        # carried a perfectly good quota header (11,993 of 12,000), unauthenticated at 09:25.
        #
        # The quota header IS the authentication evidence: the provider only counts a call against
        # our key when it has accepted that key. So:
        #   401/403 or no key -> definitively NOT authenticated.
        #   complete quota headers -> definitively authenticated, whatever the routing verdict.
        #   anything else -> AMBIGUOUS, and a single ambiguous response must not clear a verdict
        #   that was already proven. Leaving it alone is what stops one bad moment disabling the
        #   lane. Route availability is decided separately below and is unchanged.
        # IDENTITY and ADMISSION are two verdicts and were computed as one.
        complete_quota = all(
            isinstance(quota.get(key), int) for key in ("limit", "remaining", "reset"))
        if not self.api_key or int(status) in (401, 403, 429):
            authed = False                 # refused or throttled: pre-existing contract, unchanged
        elif complete_quota and (200 <= int(status) < 300 or _no_route(payload)):
            # THE ONLY WIDENING: a metered NO-ROUTE answer proves the key was accepted. It was
            # previously excluded purely because it is not a 2xx, which is a statement about the
            # COIN, not about us.
            authed = True
        else:
            authed = None                  # ambiguous: keep whatever was already proven
        if authed is not None and self.authentication_state is not None:
            self.authentication_state["authenticated"] = authed
        # ADMISSION is stricter than identity: a 429 is an authenticated caller being told to slow
        # down, so entries stand down while the key stays proven.
        #
        # BUT A NO-ROUTE MUST NOT CLOSE THE LANE EITHER (2026-09-04, second pass). Splitting the two
        # verdicts fixed identity and left admission still keyed on a 2xx, so every ordinary
        # "no route for this coin" answer -- which arrives as a non-2xx -- called
        # `disable_entries()` and shut LI.FI off until some later quote happened to succeed. The
        # operator saw `LIFI-DISABLED | insufficient_quota` fire repeatedly while the heartbeat read
        # authenticated with 11,999 of 12,000 quota, no slots used and nothing in flight: the lane
        # was closed by a routing answer, not by any shortage.
        #
        # Admission tracks the same evidence identity does, minus the throttle: a metered response
        # from an accepted key keeps the lane open whatever it says about the COIN; 401, 403 and 429
        # close it; anything unmetered leaves it as it was rather than closing it on one bad moment.
        if self.quota_guard is not None:
            if not self.api_key or int(status) in (401, 403, 429):
                (self.quota_guard.disable_entries)()
            elif complete_quota and (200 <= int(status) < 300 or _no_route(payload)):
                (self.quota_guard.enable_entries)()
        if int(status) == 401:
            return QuoteResult(
                False, stage, role, "authentication", received_at,
                request_hash, response_hash, quota,
            )
        if int(status) == 429:
            return QuoteResult(
                False, stage, role, "rate_limited", received_at,
                request_hash, response_hash, quota,
            )
        if _no_route(payload):
            return QuoteResult(
                False, stage, role, "no_route", received_at,
                request_hash, response_hash, quota,
            )
        if int(status) < 200 or int(status) >= 300:
            return QuoteResult(
                False, stage, role, "provider_error", received_at,
                request_hash, response_hash, quota,
            )
        try:
            decoded = self.decoder(payload, request, self.manifest)
        except (LifiQuoteError, ValueError, TypeError, KeyError):
            return QuoteResult(
                False, stage, role, "malformed_response", received_at,
                request_hash, response_hash, quota,
            )
        if decoded.request_sha256 != request_hash or decoded.response_sha256 != response_hash:
            return QuoteResult(
                False, stage, role, "malformed_response", received_at,
                request_hash, response_hash, quota,
            )
        return QuoteResult(
            True, stage, role, None, received_at,
            request_hash, response_hash, quota, decoded,
        )


def _call(rpc, method, params, http=None):
    if callable(rpc):
        result = rpc(method, params)
    else:
        result = evm_wallet._rpc(rpc, method, params, http=http)
    if not isinstance(result, dict):
        raise LifiAuthorityError("LI.FI RPC result is invalid")
    return result


def _address(value):
    text = str(value or "").lower()
    if len(text) != 42 or not text.startswith("0x"):
        raise LifiAuthorityError("LI.FI address is invalid")
    return text


def _block(rpc, tag, *, http=None):
    result = _call(rpc, "eth_getBlockByNumber", [tag, False], http=http).get("result")
    if not isinstance(result, dict):
        raise LifiAuthorityError("LI.FI proof block is absent")
    try:
        number = int(str(result["number"]), 16)
        block_hash = str(result["hash"]).lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise LifiAuthorityError("LI.FI proof block is invalid") from exc
    if len(block_hash) != 66:
        raise LifiAuthorityError("LI.FI proof block is invalid")
    return number, block_hash


def _runtime_hash(code):
    if not isinstance(code, str) or not code.startswith("0x"):
        raise LifiAuthorityError("LI.FI runtime code is invalid")
    try:
        return "0x" + keccak(bytes.fromhex(code[2:])).hex()
    except ValueError as exc:
        raise LifiAuthorityError("LI.FI runtime code is invalid") from exc


def _authority_targets(manifest):
    top = manifest["top_level"]
    targets = {
        _address(top["target"]): str(top["code_hash"]).lower(),
        _address(top["facet"]): str(top["facet_code_hash"]).lower(),
    }
    for step in manifest["steps"]:
        targets[_address(step["target"])] = str(step["code_hash"]).lower()
        if step.get("executor"):
            targets[_address(step["executor"])] = str(step["executor_code_hash"]).lower()
    return targets


def _validate_decoded_authority(decoded, manifest):
    top = manifest["top_level"]
    if _address(decoded.target) != _address(top["target"]) \
            or _address(decoded.spender) != _address(top["spender"]) \
            or decoded.tool != top["tool"]:
        raise LifiAuthorityError("LI.FI decoded authority mismatch")
    if len(decoded.path) != 3 or decoded.path[0] != decoded.path[1]:
        raise LifiAuthorityError("LI.FI decoded authority mismatch")


def _facet_call_data(selector):
    loupe_selector = keccak(text="facetAddress(bytes4)")[:4]
    return "0x" + (loupe_selector + encode(["bytes4"], [bytes.fromhex(selector[2:])])).hex()


def _proof_at_block(
        *, decoded, request_hash, response_hash, rpc, proof_rpc, manifest, stage,
        received_at, block_tag="latest", expected_block_hash=None, http=None):
    custody_number, custody_hash = _block(rpc, block_tag, http=http)
    exact_tag = hex(custody_number)
    proof_number, proof_hash = _block(proof_rpc, exact_tag, http=http)
    if custody_number != proof_number or custody_hash != proof_hash:
        raise LifiAuthorityError("LI.FI proof block mismatch")
    if expected_block_hash is not None and custody_hash != str(expected_block_hash).lower():
        raise LifiAuthorityError("LI.FI receipt block mismatch")
    code_hashes = {}
    for address, expected_hash in _authority_targets(manifest).items():
        first = _call(rpc, "eth_getCode", [address, exact_tag], http=http).get("result")
        second = _call(proof_rpc, "eth_getCode", [address, exact_tag], http=http).get("result")
        if first != second or _runtime_hash(first) != expected_hash:
            raise LifiAuthorityError("LI.FI runtime code mismatch")
        code_hashes[address] = expected_hash
    top = manifest["top_level"]
    call = {"to": _address(top["target"]), "data": _facet_call_data(top["selector"])}
    facets = []
    for endpoint in (rpc, proof_rpc):
        raw = _call(endpoint, "eth_call", [call, exact_tag], http=http).get("result")
        if not isinstance(raw, str) or len(raw) != 66 or raw[2:26] != "0" * 24:
            raise LifiAuthorityError("LI.FI facet proof is invalid")
        facets.append("0x" + raw[-40:].lower())
    expected_facet = _address(top["facet"])
    if facets != [expected_facet, expected_facet]:
        raise LifiAuthorityError("LI.FI facet mismatch")
    body = {
        "stage": stage,
        "received_at": received_at,
        "request_sha256": request_hash,
        "response_sha256": response_hash,
        "block_number": custody_number,
        "block_hash": custody_hash,
        "facet": expected_facet,
        "code_hashes": code_hashes,
        "calldata_sha256": decoded.calldata_sha256,
        "decoded_semantics_sha256": decoded.decoded_semantics_sha256,
    }
    return AuthorityProof(
        stage=stage,
        received_at=received_at,
        request_sha256=request_hash,
        response_sha256=response_hash,
        block_number=custody_number,
        block_hash=custody_hash,
        facet=expected_facet,
        code_hashes=code_hashes,
        proof_sha256=_canonical_hash(body),
    )


def prove_quote_authority(
        decoded, request_hash, response_hash, rpc, proof_rpc, *, manifest=None,
        stage, received_at, used_quote_hashes=None, http=None, block_tag="latest"):
    manifest = manifest or cfg.load_lifi_manifest()[0]
    _validate_decoded_authority(decoded, manifest)
    if decoded.request_sha256 != request_hash:
        raise LifiAuthorityError("LI.FI request hash mismatch")
    if decoded.response_sha256 != response_hash:
        raise LifiAuthorityError("LI.FI response hash mismatch")
    quote_key = (request_hash, response_hash)
    if used_quote_hashes is not None and quote_key in used_quote_hashes:
        raise LifiAuthorityError("LI.FI quote proof was reused")
    proof = _proof_at_block(
        decoded=decoded,
        request_hash=request_hash,
        response_hash=response_hash,
        rpc=rpc,
        proof_rpc=proof_rpc,
        manifest=manifest,
        stage=stage,
        received_at=received_at,
        block_tag=block_tag,
        http=http,
    )
    if used_quote_hashes is not None:
        used_quote_hashes.add(quote_key)
    return proof


def _transaction_hash(tx):
    data = tx.get("data")
    if isinstance(data, str) and data.startswith("0x"):
        try:
            return hashlib.sha256(bytes.fromhex(data[2:])).hexdigest()
        except ValueError:
            return None
    return tx.get("data_hash")


def _validate_final_transaction(tx, decoded, custody):
    if str(tx.get("chainId")) != "4663":
        raise LifiAuthorityError("LI.FI transaction semantic mismatch")
    if _address(tx.get("from")) != _address(custody):
        raise LifiAuthorityError("LI.FI transaction semantic mismatch")
    if _address(tx.get("to")) != _address(decoded.target):
        raise LifiAuthorityError("LI.FI transaction semantic mismatch")
    value = tx.get("value") or 0
    try:
        value = int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)
    except (TypeError, ValueError) as exc:
        raise LifiAuthorityError("LI.FI transaction semantic mismatch") from exc
    if value != 0:
        raise LifiAuthorityError("LI.FI transaction semantic mismatch")
    if _transaction_hash(tx) != decoded.calldata_sha256:
        raise LifiAuthorityError("LI.FI transaction semantic mismatch")


def _spenders(manifest):
    permitted = {_address(manifest["top_level"]["spender"])}
    permitted.update(_address(step["spender"]) for step in manifest["steps"])
    rejected = {_address(address) for address in manifest["reject"]["addresses"]}
    return permitted, rejected


def prove_execution_block(
        unsigned_tx, decoded, *, rpc, proof_rpc, stage, received_at, custody,
        estimate_gas, construct_tx, allowance_reader, simulate, sign, persist,
        manifest=None, http=None, authority=None):
    manifest = manifest or cfg.load_lifi_manifest()[0]
    gas = estimate_gas(dict(unsigned_tx))
    transaction = construct_tx(dict(unsigned_tx), gas)
    _validate_final_transaction(transaction, decoded, custody)
    if authority is not None and (not isinstance(authority, AuthorityProof) or any((
            authority.request_sha256 != decoded.request_sha256,
            authority.response_sha256 != decoded.response_sha256,
            authority.stage != stage,
            authority.received_at != received_at,
    ))):
        raise LifiAuthorityError("LI.FI execution proof mismatch")
    authority = prove_quote_authority(
        decoded,
        decoded.request_sha256,
        decoded.response_sha256,
        rpc,
        proof_rpc,
        manifest=manifest,
        stage=stage,
        received_at=received_at,
        http=http,
    )
    permitted, rejected = _spenders(manifest)
    selected = _address(decoded.spender)
    if selected not in permitted:
        raise LifiAuthorityError("LI.FI selected allowance is not pinned")
    for spender in sorted(permitted | rejected):
        amount = int(allowance_reader(
            decoded.path[0], _address(custody), spender, authority.block_number,
        ))
        expected = decoded.input_atomic if spender == selected else 0
        if amount != expected:
            raise LifiAuthorityError("LI.FI allowance mismatch")
    if simulate(transaction, authority.block_number) is not True:
        raise LifiAuthorityError("LI.FI final simulation failed")
    signed = sign(transaction)
    persist(authority, signed)
    return ExecutionProof(transaction=transaction, signed=signed, authority=authority)


def audit_receipt_block(
        receipt, decoded, *, rpc, proof_rpc, stage, manifest=None, http=None):
    manifest = manifest or cfg.load_lifi_manifest()[0]
    try:
        block_number = int(str(receipt["blockNumber"]), 16)
        block_hash = str(receipt["blockHash"]).lower()
        proof = _proof_at_block(
            decoded=decoded,
            request_hash=decoded.request_sha256,
            response_hash=decoded.response_sha256,
            rpc=rpc,
            proof_rpc=proof_rpc,
            manifest=manifest,
            stage=stage,
            received_at="receipt",
            block_tag=hex(block_number),
            expected_block_hash=block_hash,
            http=http,
        )
        return ProofResult(True, "ok", proof)
    except (KeyError, TypeError, ValueError, LifiAuthorityError):
        return ProofResult(False, "receipt_block_mismatch")


def _approve_data(spender, amount):
    spender = _address(spender)
    value = int(amount)
    if value < 0:
        raise LifiAuthorityError("LI.FI approval amount is invalid")
    return "0x095ea7b3" + spender[2:].rjust(64, "0") + format(value, "064x")


def _approval_transaction(lifecycle, token, spender, amount):
    nonce = lifecycle.nonce_fn()
    gas_price = lifecycle.gas_price_fn()
    if nonce is None or gas_price is None:
        return None
    return {
        "to": _address(token),
        "data": _approve_data(spender, amount),
        "value": 0,
        "gas": int(lifecycle.approve_gas),
        "gasPrice": int(gas_price),
        "nonce": int(nonce),
        "chainId": int(lifecycle.chain_id),
    }


def _append_submission_state(
        lifecycle, expected_state, new_state, event_type, *, token, spender, amount,
        tx_hash=None, raw=None, allowance_atomic=None, block_number=None):
    return m.append_lifi_event(
        lifecycle.conn,
        lifecycle.lifecycle_id,
        expected_state=expected_state,
        new_state=new_state,
        event_type=event_type,
        stage="approval",
        token=token,
        spender=spender,
        amount_atomic=amount,
        allowance_atomic=allowance_atomic,
        block_number=block_number,
        tx_hash=tx_hash,
        tx_identity=tx_hash,
        nonce=None if raw is None else raw["nonce"],
        signed_payload_sha256=None if raw is None else raw["payload_sha256"],
        created_at=lifecycle.now_fn(),
    )


def _signed_approval(lifecycle, token, spender, amount):
    transaction = _approval_transaction(lifecycle, token, spender, amount)
    if transaction is None:
        return None
    signed = lifecycle.sign_fn(transaction)
    if not signed or len(signed) < 2:
        return None
    raw, tx_hash = signed[:2]
    return {
        "transaction": transaction,
        "raw": raw,
        "tx_hash": str(tx_hash).lower(),
        "nonce": transaction["nonce"],
        "payload_sha256": sha256_hex(raw),
    }


def _receipt_status(receipt, tx_hash):
    if not isinstance(receipt, dict):
        return "unknown"
    if str(receipt.get("transactionHash") or "").lower() != str(tx_hash).lower():
        return "unknown"
    if receipt.get("status") in ("0x1", "0x01", 1):
        return "confirmed"
    if receipt.get("status") in ("0x0", "0x00", 0):
        return "failed"
    return "unknown"


def approve_exact_and_wait(lifecycle, token, spender, amount, *, expected_state=None):
    amount = int(amount)
    if amount <= 0:
        return ApprovalResult("invalid_amount")
    latest = m.latest_lifi_lifecycle(lifecycle.conn, lifecycle.lifecycle_id)
    if latest is None:
        return ApprovalResult("missing_lifecycle")
    expected = expected_state or latest["state"]
    signed = _signed_approval(lifecycle, token, spender, amount)
    if signed is None:
        return ApprovalResult("sign_failed", cleanup_required=True)
    if not _append_submission_state(
            lifecycle, expected, "approval_signed", "approval_signed",
            token=token, spender=spender, amount=amount, tx_hash=signed["tx_hash"], raw=signed):
        return ApprovalResult("cas_conflict")
    sent = lifecycle.send_fn(signed["raw"])
    if sent is False:
        _append_submission_state(
            lifecycle, "approval_signed", "approval_rejected", "approval_rejected",
            token=token, spender=spender, amount=amount, tx_hash=signed["tx_hash"],
        )
        return ApprovalResult("rejected", signed["tx_hash"], cleanup_required=True)
    if not sent:
        _append_submission_state(
            lifecycle, "approval_signed", "approval_submitted_unknown",
            "approval_submitted_unknown", token=token, spender=spender, amount=amount,
            tx_hash=signed["tx_hash"],
        )
        return ApprovalResult("submitted_unknown", signed["tx_hash"])
    _append_submission_state(
        lifecycle, "approval_signed", "approval_submitted", "approval_submitted",
        token=token, spender=spender, amount=amount, tx_hash=signed["tx_hash"],
    )
    receipt = lifecycle.receipt_fn(signed["tx_hash"])
    status = _receipt_status(receipt, signed["tx_hash"])
    if status == "unknown":
        _append_submission_state(
            lifecycle, "approval_submitted", "approval_submitted_unknown",
            "approval_submitted_unknown", token=token, spender=spender, amount=amount,
            tx_hash=signed["tx_hash"],
        )
        return ApprovalResult("submitted_unknown", signed["tx_hash"])
    if status == "failed":
        allowance = int(lifecycle.allowance_fn(token, lifecycle.custody, spender))
        _append_submission_state(
            lifecycle, "approval_submitted", "approval_reverted", "approval_reverted",
            token=token, spender=spender, amount=amount, allowance_atomic=allowance,
            tx_hash=signed["tx_hash"],
        )
        return ApprovalResult(
            "reverted", signed["tx_hash"], allowance, cleanup_required=allowance != 0,
        )
    allowance = int(lifecycle.allowance_fn(token, lifecycle.custody, spender))
    try:
        block_number = int(str(receipt.get("blockNumber")), 16)
    except (TypeError, ValueError):
        block_number = None
    state = "approval_confirmed" if allowance == amount else "approval_allowance_mismatch"
    _append_submission_state(
        lifecycle, "approval_submitted", state, state,
        token=token, spender=spender, amount=amount, allowance_atomic=allowance,
        tx_hash=signed["tx_hash"], block_number=block_number,
    )
    if allowance != amount:
        return ApprovalResult(
            "allowance_mismatch", signed["tx_hash"], allowance, cleanup_required=allowance != 0,
        )
    return ApprovalResult(
        "confirmed", signed["tx_hash"], allowance, block_number=block_number)


_UNKNOWN_SUBMISSION_STATES = frozenset({
    "approval_signed", "approval_submitted", "approval_submitted_unknown",
    "swap_signed", "swap_submitted", "swap_submitted_unknown",
    "revoke_signed", "revoke_submitted", "revoke_submitted_unknown",
})


def revoke_and_wait(lifecycle, token, spender):
    latest = m.latest_lifi_lifecycle(lifecycle.conn, lifecycle.lifecycle_id)
    if latest is None:
        return ApprovalResult("missing_lifecycle")
    if latest["state"] in _UNKNOWN_SUBMISSION_STATES:
        return ApprovalResult("blocked_unknown_submission")
    signed = _signed_approval(lifecycle, token, spender, 0)
    if signed is None:
        return ApprovalResult("sign_failed", cleanup_required=True)
    if not _append_submission_state(
            lifecycle, latest["state"], "revoke_signed", "revoke_signed",
            token=token, spender=spender, amount=0, tx_hash=signed["tx_hash"], raw=signed):
        return ApprovalResult("cas_conflict")
    sent = lifecycle.send_fn(signed["raw"])
    if sent is False:
        _append_submission_state(
            lifecycle, "revoke_signed", "revoke_rejected", "revoke_rejected",
            token=token, spender=spender, amount=0, tx_hash=signed["tx_hash"],
        )
        return ApprovalResult("rejected", signed["tx_hash"], cleanup_required=True)
    if not sent:
        _append_submission_state(
            lifecycle, "revoke_signed", "revoke_submitted_unknown",
            "revoke_submitted_unknown", token=token, spender=spender, amount=0,
            tx_hash=signed["tx_hash"],
        )
        return ApprovalResult("submitted_unknown", signed["tx_hash"])
    _append_submission_state(
        lifecycle, "revoke_signed", "revoke_submitted", "revoke_submitted",
        token=token, spender=spender, amount=0, tx_hash=signed["tx_hash"],
    )
    status = _receipt_status(lifecycle.receipt_fn(signed["tx_hash"]), signed["tx_hash"])
    if status != "confirmed":
        state = "revoke_reverted" if status == "failed" else "revoke_submitted_unknown"
        _append_submission_state(
            lifecycle, "revoke_submitted", state, state,
            token=token, spender=spender, amount=0, tx_hash=signed["tx_hash"],
        )
        return ApprovalResult(
            "reverted" if status == "failed" else "submitted_unknown",
            signed["tx_hash"], cleanup_required=True,
        )
    allowance = int(lifecycle.allowance_fn(token, lifecycle.custody, spender))
    state = "allowance_zero" if allowance == 0 else "revoke_confirmed_nonzero"
    _append_submission_state(
        lifecycle, "revoke_submitted", state, state,
        token=token, spender=spender, amount=0, allowance_atomic=allowance,
        tx_hash=signed["tx_hash"],
    )
    return ApprovalResult(
        state, signed["tx_hash"], allowance, cleanup_required=allowance != 0,
    )


def lifi_entry_blocked(conn, chain_id, custody):
    safe_states = m.LIFI_TERMINAL_STATES | {"allowance_zero", "position_open"}
    return any(
        row["state"] not in safe_states
        for row in m.unresolved_lifi_lifecycles(conn, chain_id, custody)
    )


def finalize_lifi_failed_entry(lifecycle, pending_id, reason):
    conn = lifecycle.conn
    if conn.in_transaction:
        raise RuntimeError("LI.FI failed entry needs a clean transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        latest = m.latest_lifi_lifecycle(conn, lifecycle.lifecycle_id)
        if latest is None or latest["state"] != "allowance_zero" \
                or int(latest["pending_id"]) != int(pending_id):
            conn.rollback()
            return False
        if not m.append_lifi_event(
                conn, lifecycle.lifecycle_id, expected_state="allowance_zero",
                new_state="entry_failed", event_type="entry_failed", stage="entry",
                created_at=lifecycle.now_fn(), detail={"reason": str(reason)}, commit=False):
            conn.rollback()
            return False
        if not m.release_pending(conn, pending_id, str(reason), commit=False):
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def finalize_lifi_unmutated_entry(lifecycle, pending_id, reason):
    """Release an entry that ended before an allowance or signed swap existed."""
    conn = lifecycle.conn
    if conn.in_transaction:
        raise RuntimeError("LI.FI unmutated entry needs a clean transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        latest = m.latest_lifi_lifecycle(conn, lifecycle.lifecycle_id)
        if latest is None or latest["state"] != "reserved" \
                or int(latest["pending_id"]) != int(pending_id):
            conn.rollback()
            return False
        if not m.append_lifi_event(
                conn, lifecycle.lifecycle_id, expected_state="reserved",
                new_state="entry_failed", event_type="entry_failed", stage="entry",
                created_at=lifecycle.now_fn(), detail={"reason": str(reason)}, commit=False):
            conn.rollback()
            return False
        if not m.release_pending(conn, pending_id, str(reason), commit=False):
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _topic_address(topic):
    value = str(topic or "").lower()
    if len(value) != 66 or not value.startswith("0x") or value[2:26] != "0" * 24:
        raise ValueError("invalid transfer topic")
    return "0x" + value[-40:]


def _transfers(receipt):
    transfers = []
    for log in receipt.get("logs") or ():
        topics = log.get("topics") or ()
        data = str(log.get("data") or "").lower()
        if not topics or str(topics[0]).lower() != _TRANSFER_TOPIC:
            continue
        if len(topics) != 3:
            raise ValueError("invalid transfer topics")
        if len(data) != 66 or not data.startswith("0x"):
            raise ValueError("invalid transfer amount")
        transfers.append((
            _address(log.get("address")),
            _topic_address(topics[1]),
            _topic_address(topics[2]),
            int(data, 16),
        ))
    return transfers


def _settle(receipt, balances, decoded):
    if not isinstance(receipt, dict) or receipt.get("status") not in ("0x1", "0x01", 1):
        return SettlementResult(False, "receipt_failed")
    expected_hash = str((balances or {}).get("expected_tx_hash") or "").lower()
    if not expected_hash or str(receipt.get("transactionHash") or "").lower() != expected_hash:
        return SettlementResult(False, "receipt_hash_mismatch")
    try:
        custody = _address(decoded.custody)
        input_token = _address(decoded.path[0])
        output_token = _address(decoded.path[-1])
        recipients = {_address(value) for value in decoded.input_recipients}
        transfers = _transfers(receipt)
    except (AttributeError, TypeError, ValueError, LifiAuthorityError):
        return SettlementResult(False, "malformed_receipt")
    input_out = 0
    output_in = 0
    for token, sender, recipient, amount in transfers:
        if token == input_token and sender == custody:
            if recipient not in recipients:
                return SettlementResult(False, "unexpected_input_recipient")
            input_out += amount
        if token == input_token and recipient == custody:
            return SettlementResult(False, "unexpected_custody_movement")
        if token == output_token and sender == custody:
            return SettlementResult(False, "unexpected_custody_movement")
        if token == output_token and recipient == custody:
            output_in += amount
    if input_out != int(decoded.input_atomic):
        return SettlementResult(False, "exact_input")
    if output_in < int(decoded.output_min_atomic):
        return SettlementResult(False, "minimum_output")
    current = bool((balances or {}).get("current"))
    if current:
        input_delta = (balances or {}).get("input_delta_atomic")
        output_delta = (balances or {}).get("output_delta_atomic")
        try:
            agrees = int(input_delta) == input_out and int(output_delta) == output_in
        except (TypeError, ValueError):
            agrees = False
        if not agrees:
            return SettlementResult(False, "balance_conflict")
        source = "balance_and_receipt"
    else:
        source = "receipt"
    return SettlementResult(True, "ok", input_out, output_in, source)


def settle_entry(receipt, balances, decoded):
    return _settle(receipt, balances, decoded)


def settle_exit(receipt, balances, decoded):
    return _settle(receipt, balances, decoded)


def execute_lifi_entry(
        *, direct_miss, input_atomic, request_fn, client, proof_fn, approval_fn,
        execute_fn, cleanup_fn, quota_guard, slots_used, in_flight, gate_fn,
        roundtrip_cap_bps, trace_fn=None, canary=False):
    if not isinstance(direct_miss, dict) or direct_miss.get("code") != "no_route":
        return {"status": "not_selected", "reason": "direct_not_no_route"}
    admission = (quota_guard.admit_canary(in_flight=in_flight) if canary
                 else quota_guard.admit_entry(slots_used, in_flight=in_flight))
    if not admission.allowed and admission.code != "entry_disabled":
        return {"status": "aborted", "reason": admission.code}
    proofs = []

    def trace(stage, result=None, **extra):
        if trace_fn is None:
            return
        data = dict(extra)
        if result is not None:
            decoded = getattr(result, "decoded", None)
            data.update({
                "failure_type": getattr(result, "failure_type", None),
                "quota": getattr(result, "quota", None),
                "fee_atomic": getattr(decoded, "fee_atomic", None),
                "request_sha256": getattr(result, "request_sha256", None),
                "response_sha256": getattr(result, "response_sha256", None),
            })
        trace_fn(stage, data)

    def quote(side, amount, stage):
        request = request_fn(side, int(amount), stage)
        result = client.quote(request, role="entry", stage=stage)
        trace(stage, result)
        if not result.ok:
            # NAME THE OUTCOME, NOT THE MESSENGER (2026-09-04). LI.FI is only ever asked AFTER the
            # direct ladder has already returned no_route, so `no_route` here means every venue
            # agreed -- v3, v2, pons_v2, v4, v4_multihop AND the aggregator. Emitting that as
            # "lifi_no_route" made an ordinary market condition read as a LI.FI fault, and the
            # operator reasonably concluded LI.FI was blocking the system when it had just done its
            # job and answered. Every OTHER failure type is genuinely LI.FI's and keeps its name.
            failure = str(result.failure_type)
            reason = "no_route_all_venues" if failure == "no_route" else "lifi_" + failure
            return None, {
                "status": "aborted",
                "reason": reason,
                "lifi_failure_type": result.failure_type,
            }
        try:
            proof = proof_fn(result, stage)
        except Exception:
            return None, {
                "status": "unknown", "reason": "lifi_authority_mismatch",
                "lifi_failure_type": "authority_mismatch",
            }
        proofs.append(proof)
        return result.decoded, None

    initial_buy, failure = quote("buy", input_atomic, "initial_buy")
    if failure:
        return failure
    admission = (quota_guard.admit_canary(in_flight=quota_guard.in_flight) if canary
                 else quota_guard.admit_entry(
                     slots_used, in_flight=quota_guard.in_flight))
    if not admission.allowed:
        return {"status": "aborted", "reason": admission.code}
    initial_sell, failure = quote(
        "sell", initial_buy.output_min_atomic, "initial_sell",
    )
    if failure:
        return failure
    if initial_sell.input_atomic != initial_buy.output_min_atomic:
        return {
            "status": "refused", "reason": "initial_atomic_continuity",
            "lifi_failure_type": "authority_mismatch",
        }

    def local_gates(buy, sell, revision, cleanup):
        roundtrip = economic_roundtrip(
            int(input_atomic), buy, sell, int(roundtrip_cap_bps),
        )
        trace(
            "roundtrip", code=roundtrip.code, loss_bps=roundtrip.loss_bps,
            revision=revision,
        )
        if not roundtrip.allowed:
            if cleanup:
                cleanup_fn(roundtrip.code)
            return {"status": "refused", "reason": roundtrip.code}
        for name in (
                "token_safety", "impact", "chase", "execution_premium", "post_peak",
                "gas_sufficiency"):
            gate = gate_fn(name, buy, sell)
            allowed = gate.allowed if hasattr(gate, "allowed") else bool(gate)
            code = getattr(gate, "code", name)
            trace(
                "gate", gate=name, allowed=allowed, code=code, revision=revision,
            )
            if not allowed:
                if cleanup:
                    cleanup_fn(code)
                return {"status": "refused", "reason": code, "gate": name}
        return None

    refusal = local_gates(initial_buy, initial_sell, "initial", False)
    if refusal:
        return refusal
    approval = approval_fn(
        initial_buy.path[0], initial_buy.spender, initial_buy.input_atomic,
    )
    trace("approval", status=getattr(approval, "status", None))
    if getattr(approval, "status", None) != "confirmed":
        status = getattr(approval, "status", "failed")
        if status != "submitted_unknown" and getattr(approval, "cleanup_required", False):
            cleanup_fn("approval_" + status)
        return {
            "status": "unknown" if status == "submitted_unknown" else "aborted",
            "reason": "lifi_approval_" + status,
            "lifi_failure_type": "approval_" + status,
        }
    refreshed_buy, failure = quote("buy", input_atomic, "refreshed_buy")
    if failure:
        cleanup_fn(failure["reason"])
        return failure
    if refreshed_buy.input_atomic != initial_buy.input_atomic \
            or refreshed_buy.spender.lower() != initial_buy.spender.lower():
        cleanup_fn("refreshed_buy_authority_mismatch")
        return {
            "status": "refused", "reason": "refreshed_buy_authority_mismatch",
            "lifi_failure_type": "authority_mismatch",
        }
    final_sell, failure = quote(
        "sell", refreshed_buy.output_min_atomic, "final_sell",
    )
    if failure:
        cleanup_fn(failure["reason"])
        return failure
    if final_sell.input_atomic != refreshed_buy.output_min_atomic:
        cleanup_fn("final_atomic_continuity")
        return {
            "status": "refused", "reason": "final_atomic_continuity",
            "lifi_failure_type": "authority_mismatch",
        }
    refusal = local_gates(refreshed_buy, final_sell, "refreshed", True)
    if refusal:
        return refusal
    return execute_fn(refreshed_buy, final_sell, tuple(proofs))


def execute_lifi_exit(
        *, slippage_ladder, quote_fn, execute_fn, freshness_fn, cleanup_fn,
        trace_fn=None):
    for rung in slippage_ladder:
        quote = quote_fn(rung)
        if trace_fn is not None:
            trace_fn("quote", {
                "rung": rung,
                "failure_type": getattr(quote, "failure_type", None),
                "quota": getattr(quote, "quota", None),
            })
        if not getattr(quote, "ok", False):
            failure = getattr(quote, "failure_type", None) or "provider_error"
            return {
                "status": "deferred", "reason": "lifi_" + failure,
                "failure_type": failure,
            }
        if not freshness_fn(quote):
            cleanup_fn("stale_quote")
            return {"status": "deferred", "reason": "stale_quote"}
        result = execute_fn(quote, rung)
        failure = result.get("failure_type") if isinstance(result, dict) else None
        if failure in {"slippage", "minimum_output"}:
            continue
        return result
    return {
        "status": "deferred", "reason": "lifi_slippage_ladder_exhausted",
        "failure_type": "slippage",
    }


def select_lifi_preview(position, direct_result, direct_failure, lifi_preview_fn):
    if direct_result is not None:
        return direct_result
    if not position.get("lifi_dependent") or direct_failure != "no_route" \
            or lifi_preview_fn is None:
        return None
    return lifi_preview_fn(position)


def _quantity(value):
    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)
    return int(value)


def _rpc_value(rpc, method, params):
    try:
        return _call(rpc, method, params).get("result")
    except Exception:
        return None


def _same_receipt(left, right):
    if left is None or right is None:
        return left is None and right is None
    try:
        keys = ("transactionHash", "blockHash", "status")
        return all(str(left.get(key)).lower() == str(right.get(key)).lower() for key in keys)
    except AttributeError:
        return False


def _replacement_proof_page(lifecycle, custody_rpc, proof_rpc, page_size=64):
    try:
        signed_hash = str(lifecycle["tx_hash"]).lower()
        signed_nonce = _quantity(lifecycle["nonce"])
        custody = _address(lifecycle["custody_wallet"])
        submission_block = _quantity(lifecycle["block_number"])
        detail = json.loads(lifecycle.get("detail_json") or "{}")
        start = _quantity(detail.get("scan_next_block", submission_block))
        replacement = detail.get("replacement_tx_hash")
        replacement_block_hash = detail.get("replacement_block_hash")
    except (KeyError, TypeError, ValueError, LifiAuthorityError):
        return None, None
    final_c = _rpc_value(custody_rpc, "eth_getBlockByNumber", ["finalized", False])
    final_p = _rpc_value(proof_rpc, "eth_getBlockByNumber", ["finalized", False])
    if not isinstance(final_c, dict) or not isinstance(final_p, dict):
        return None, None
    try:
        final_number = _quantity(final_c["number"])
        if final_number != _quantity(final_p["number"]) \
                or str(final_c["hash"]).lower() != str(final_p["hash"]).lower():
            return None, None
    except (KeyError, TypeError, ValueError):
        return None, None
    receipt_c = _rpc_value(custody_rpc, "eth_getTransactionReceipt", [signed_hash])
    receipt_p = _rpc_value(proof_rpc, "eth_getTransactionReceipt", [signed_hash])
    if receipt_c is not None or receipt_p is not None:
        return None, None
    nonce_c = _rpc_value(
        custody_rpc, "eth_getTransactionCount", [custody, "finalized"])
    nonce_p = _rpc_value(proof_rpc, "eth_getTransactionCount", [custody, "finalized"])
    try:
        if _quantity(nonce_c) != _quantity(nonce_p) or _quantity(nonce_c) <= signed_nonce:
            return None, None
    except (TypeError, ValueError):
        return None, None
    if start < submission_block or start > final_number:
        return None, None
    stop = min(final_number, start + max(1, int(page_size)) - 1)
    for number in range(start, stop + 1):
        tag = hex(number)
        block_c = _rpc_value(custody_rpc, "eth_getBlockByNumber", [tag, True])
        block_p = _rpc_value(proof_rpc, "eth_getBlockByNumber", [tag, True])
        if not isinstance(block_c, dict) or not isinstance(block_p, dict):
            return None, None
        if str(block_c.get("hash")).lower() != str(block_p.get("hash")).lower():
            return None, None
        matches = []
        for block in (block_c, block_p):
            found = [
                tx for tx in (block.get("transactions") or ())
                if str(tx.get("from") or "").lower() == custody
                and _quantity(tx.get("nonce")) == signed_nonce
                and str(tx.get("hash") or "").lower() != signed_hash
            ]
            matches.append(found)
        hashes_c = [str(tx.get("hash")).lower() for tx in matches[0]]
        hashes_p = [str(tx.get("hash")).lower() for tx in matches[1]]
        if hashes_c != hashes_p or len(hashes_c) > 1:
            return None, None
        if hashes_c:
            if replacement is not None and replacement != hashes_c[0]:
                return None, None
            replacement = hashes_c[0]
            replacement_block_hash = str(block_c.get("hash")).lower()
    if stop < final_number:
        return None, {
            "scan_next_block": stop + 1,
            "replacement_tx_hash": replacement,
            "replacement_block_hash": replacement_block_hash,
        }
    if replacement is None:
        return None, None
    replacement_c = _rpc_value(
        custody_rpc, "eth_getTransactionReceipt", [replacement])
    replacement_p = _rpc_value(proof_rpc, "eth_getTransactionReceipt", [replacement])
    if not _same_receipt(replacement_c, replacement_p) or not isinstance(replacement_c, dict):
        return None, None
    if str(replacement_c.get("blockHash") or "").lower() != replacement_block_hash:
        return None, None
    reconcile = lifecycle.get("reconcile_fn")
    if not callable(reconcile):
        return None, None
    try:
        state = reconcile(
            lifecycle=lifecycle,
            finalized_block_number=final_number,
            finalized_block_hash=str(final_c["hash"]).lower(),
            replacement_receipt=replacement_c,
        )
        allowance = int(state["allowance_atomic"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not state.get("complete") or state.get("settlement_effect") or allowance < 0:
        return None, None
    return DropProof(
        final_number, str(final_c["hash"]).lower(), replacement,
        _canonical_hash(replacement_c), allowance, state.get("balance_sha256"),
    ), None


def prove_finalized_replacement(lifecycle, custody_rpc, proof_rpc):
    """Return proof only after two RPCs show the same finalized replacement."""
    proof, _progress = _replacement_proof_page(lifecycle, custody_rpc, proof_rpc)
    return proof


def _valid_recovery_row(row, context):
    return str(row.get("chain_id")) == str(context.get("chain_id")) \
        and str(row.get("custody_wallet") or "").lower() \
        == str(context.get("custody_wallet") or "").lower() \
        and str(row.get("release_sha") or "").lower() \
        == str(context.get("release_sha") or "").lower() \
        and str(row.get("manifest_hash") or "").lower() \
        == str(context.get("manifest_hash") or "").lower()


def recover_lifi_lifecycles(conn, context, now_iso):
    """Inspect one bounded lifecycle page. Recovery never signs or sends old bytes."""
    chain = str(context.get("chain_id") or "4663")
    custody = str(context.get("custody_wallet") or "").lower()
    rows = m.unresolved_lifi_lifecycles(conn, chain, custody)[:50]
    resolved = 0
    invalid = 0
    unknown_states = _UNKNOWN_SUBMISSION_STATES

    def finish_cleanup(row, expected_state, allowance):
        nonlocal resolved
        if int(allowance) != 0:
            lifecycle_factory = context.get("lifecycle_factory")
            lifecycle = lifecycle_factory(row) if callable(lifecycle_factory) else LifiLifecycle(
                conn=conn, lifecycle_id=row["lifecycle_id"], custody=custody,
                chain_id=chain, nonce_fn=context.get("nonce_fn"),
                gas_price_fn=context.get("gas_price_fn"), sign_fn=context.get("sign_fn"),
                send_fn=context.get("send_fn"), receipt_fn=context.get("receipt_fn"),
                allowance_fn=context.get("allowance_fn"), now_fn=lambda: now_iso,
                approve_gas=int(context.get("approve_gas") or 100_000),
            )
            if all(callable(value) for value in (
                    lifecycle.nonce_fn, lifecycle.gas_price_fn, lifecycle.sign_fn,
                    lifecycle.send_fn, lifecycle.receipt_fn, lifecycle.allowance_fn)):
                result = revoke_and_wait(lifecycle, row.get("token"), row.get("spender"))
                if result.status not in {
                        "cas_conflict", "submitted_unknown", "missing_lifecycle"}:
                    resolved += 1
            return
        if not m.append_lifi_event(
                conn, row["lifecycle_id"], expected_state=expected_state,
                new_state="allowance_zero", event_type="allowance_zero", stage="recovery",
                allowance_atomic=0, created_at=now_iso):
            return
        resolved += 1
        latest = m.latest_lifi_lifecycle(conn, row["lifecycle_id"])
        if latest.get("position_id") is not None:
            m.append_lifi_event(
                conn, row["lifecycle_id"], expected_state="allowance_zero",
                new_state="position_open", event_type="authority_clean", stage="recovery",
                created_at=now_iso)
            return
        lifecycle = LifiLifecycle(
            conn=conn, lifecycle_id=row["lifecycle_id"], custody=custody,
            chain_id=chain, nonce_fn=None, gas_price_fn=None, sign_fn=None,
            send_fn=None, receipt_fn=None, allowance_fn=None, now_fn=lambda: now_iso,
        )
        finalize_lifi_failed_entry(
            lifecycle, row["pending_id"], "finalized_replacement")

    for row in rows:
        if not _valid_recovery_row(row, context):
            invalid += 1
            continue
        state = row["state"]
        if state in {"entry_settled", "exit_settled", "allowance_zero"}:
            recover_confirmed = context.get("recover_confirmed_fn")
            if callable(recover_confirmed) and recover_confirmed(
                    row, None, now_iso, conn=conn):
                resolved += 1
            continue
        if state not in unknown_states:
            continue
        receipt_fn = context.get("receipt_fn")
        receipt = receipt_fn(row.get("tx_hash")) if callable(receipt_fn) else None
        status = _receipt_status(receipt, row.get("tx_hash"))
        stage = str(row.get("stage") or state.split("_", 1)[0])
        if status == "confirmed" and stage in {"approval", "revoke"}:
            allowance_fn = context.get("allowance_fn")
            if not callable(allowance_fn):
                continue
            try:
                allowance = int(allowance_fn(row.get("token"), custody, row.get("spender")))
            except Exception:
                continue
            new_state = "approval_confirmed" if stage == "approval" \
                else ("allowance_zero" if allowance == 0 else "revoke_confirmed_nonzero")
            if m.append_lifi_event(
                    conn, row["lifecycle_id"], expected_state=state,
                    new_state=new_state, event_type=new_state, stage=stage,
                    allowance_atomic=allowance, tx_hash=row.get("tx_hash"),
                    created_at=now_iso):
                resolved += 1
            continue
        if status == "confirmed":
            recover_confirmed = context.get("recover_confirmed_fn")
            if callable(recover_confirmed) and recover_confirmed(
                    row, receipt, now_iso, conn=conn):
                resolved += 1
            continue
        if status == "failed":
            new_state = stage + "_reverted"
            if m.append_lifi_event(
                    conn, row["lifecycle_id"], expected_state=state,
                    new_state=new_state, event_type=new_state, stage=stage,
                    tx_hash=row.get("tx_hash"), created_at=now_iso):
                resolved += 1
            continue
        custody_rpc = context.get("custody_rpc")
        proof_rpc = context.get("proof_rpc")
        reconcile = context.get("reconcile_drop_fn")
        if custody_rpc is None or proof_rpc is None or not callable(reconcile):
            continue
        proof, progress = _replacement_proof_page(
            {**row, "reconcile_fn": reconcile}, custody_rpc, proof_rpc)
        if proof is None:
            if progress is not None:
                m.append_lifi_event(
                    conn, row["lifecycle_id"], expected_state=state,
                    new_state=state, event_type="replacement_scan", stage=stage,
                    tx_hash=row.get("tx_hash"), detail=progress, created_at=now_iso)
            continue
        new_state = stage + "_dropped"
        if m.append_lifi_event(
                conn, row["lifecycle_id"], expected_state=state,
                new_state=new_state, event_type=new_state, stage=stage,
                allowance_atomic=proof.allowance_atomic, tx_hash=row.get("tx_hash"),
                block_number=proof.finalized_block_number,
                block_hash=proof.finalized_block_hash,
                proof_sha256=_canonical_hash(proof.__dict__), created_at=now_iso):
            resolved += 1
            finish_cleanup(row, new_state, proof.allowance_atomic)
    remaining = m.unresolved_lifi_lifecycles(conn, chain, custody)
    blocked = invalid > 0 or any(
        row["state"] in _UNKNOWN_SUBMISSION_STATES
        or row["state"].startswith("approval_")
        or row["state"].startswith("revoke_")
        for row in remaining
    )
    return RecoverySnapshot(
        scanned=len(rows), resolved=resolved, unresolved=len(remaining), invalid=invalid,
        scan_complete=len(rows) < 50, entry_admitted=len(rows) < 50 and not blocked,
    )
