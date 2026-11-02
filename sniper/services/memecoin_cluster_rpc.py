"""Dedicated research-RPC reads for funded cluster admission."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sniper.services.memecoin_cluster_admission import ClusterQuotaError


SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"


import threading

class ResearchRpcQuotaError(ClusterQuotaError):
    """The dedicated research RPC returned HTTP 429."""


@dataclass(frozen=True)
class EligibleHolder:
    ata: str
    owner: str | None
    balance_atomic: int
    exclusion: str | None = None
    # The owner's program could not be read (null account or missing from the batch answer), so
    # it is not known whether this holder is a wallet or a program account. The funded gate keeps
    # treating it as an eligible owner, as before; the desk refuses to decide on it.
    program_unknown: bool = False


def on_curve(address):
    """True for an ed25519 key (a wallet), False for an off-curve address (a PDA), None when unparseable."""
    try:
        from solders.pubkey import Pubkey
        return bool(Pubkey.from_string(address).is_on_curve())
    except Exception:
        return None


class ResearchClusterRpc:
    # PER-THREAD, NOT PER-OBJECT. `trace_first_funder` stashes the signature it read here and the
    # caller picks it up on the next line, which is only correct while one thread owns the object.
    # `memecoin_cluster_admission` runs traces through a thread pool over a SHARED rpc, so a
    # concurrent trace could overwrite the value between the call and the read and persist another
    # owner's signature as this owner's funder evidence. The funder RESULT is returned and was
    # always safe; only this audit field was exposed. A thread-local keeps the existing attribute
    # spelling for every reader while making the value private to the trace that produced it.
    _tls = threading.local()

    @property
    def last_source_signature(self):
        return getattr(self._tls, "sig", None)

    @last_source_signature.setter
    def last_source_signature(self, value):
        self._tls.sig = value

    def __init__(self, url: str, *, http=urlopen, timeout_s: float = 8.0, on_quota=None):
        if not url or "api-key" not in url:
            raise ValueError("a dedicated research Helius URL is required")
        self.url = url
        self.http = http
        self.timeout_s = float(timeout_s)
        self.on_quota = on_quota       # called on any quota answer (e.g. the desk worker pauses the key's shared pacer)

    def _quota(self):
        if self.on_quota is not None:
            self.on_quota()

    def call(self, method: str, params: list):
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        request = Request(
            self.url, data=body, headers={"content-type": "application/json"}
        )
        try:
            response = self.http(request, timeout=self.timeout_s)
            payload = json.load(response)
        except HTTPError as exc:
            if exc.code == 429:
                self._quota()
                raise ResearchRpcQuotaError("research RPC quota") from exc
            raise
        if payload.get("error"):
            error = payload["error"]
            if error.get("code") in (429, -32429, -32005):      # -32005 "Too many requests" is Helius' JSON-RPC limit body
                self._quota()
                raise ResearchRpcQuotaError("research RPC quota")
            raise RuntimeError(f"research RPC error: {error.get('code')}")
        return payload.get("result")

    def holder_accounts(self, mint: str) -> list[tuple[str, str]]:
        result = self.call(
            "getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]
        )
        # A missing or malformed answer is NOT an empty book: only an explicit result:{value:[]} is a
        # verified zero (kill:cluster_no_holders); anything else raises, read as dependency-unknown
        # (Codex P2 on 71edac009).
        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise RuntimeError("research RPC malformed getTokenLargestAccounts result")
        rows = []
        for item in result["value"]:
            amount = item.get("amount")
            if amount is None:
                amount = int(float(item.get("uiAmount") or 0))
            if int(amount) > 0:
                rows.append((item["address"], str(amount)))
        return rows

    def token_account_owners(self, accounts: list[str]) -> dict[str, str]:
        if not accounts:
            return {}
        result = self.call(
            "getMultipleAccounts", [accounts, {"encoding": "jsonParsed"}]
        ) or {}
        owners = {}
        for ata, value in zip(accounts, result.get("value") or []):
            try:
                owners[ata] = value["data"]["parsed"]["info"]["owner"]
            except (KeyError, TypeError):
                continue
        return owners

    def owner_programs(self, owners: list[str]) -> dict[str, str | None]:
        """{owner: owning program} for each owner wallet. One batched call.

        A real wallet is owned by the System Program. A bonding curve, pool vault, or any
        other program-controlled account is owned by its program, so this separates them by
        IDENTITY rather than by balance — which the spec requires and which the data demands:
        the median top-holder share across 3,056 snapshots is 80%, so any balance heuristic
        would exclude most of the real book.
        """
        if not owners:
            return {}
        result = self.call(
            "getMultipleAccounts", [owners, {"encoding": "jsonParsed"}]
        ) or {}
        programs: dict[str, str | None] = {}
        for owner, value in zip(owners, result.get("value") or []):
            if value is None:
                programs[owner] = None  # only an explicit null proves there is no account
            elif isinstance(value, dict) and isinstance(value.get("owner"), str) and value["owner"]:
                programs[owner] = value["owner"]
        return programs

    def eligible_holders(self, mint: str) -> list[EligibleHolder]:
        """The holder book with program-controlled accounts marked ineligible.

        Without this, every pre-migration Pump coin fails closed forever: the bonding curve
        is a PDA with no funding transfer to trace, so it can never resolve, and an allow
        requires full resolution. Measured on live tape, the curve holds 99.2% of one book
        and 77.4% of all coins have a top holder past the 50% top-funder limit — so the gate
        would have blocked essentially the entire funded universe and looked like it worked.

        An owner whose account cannot be read stays ELIGIBLE and unresolved. That lowers
        coverage and blocks, which is the fail-closed behaviour the spec requires: an
        unidentified large holder is never waved through.
        """
        accounts = self.holder_accounts(mint)
        owners = self.token_account_owners([ata for ata, _balance in accounts])
        distinct = sorted({owner for owner in owners.values() if owner})
        # A failed program lookup RAISES (Codex, d87abea8c): swallowed, it read as "no program
        # owners", and a thin desk book traced clean and passed on a dependency failure. The caller
        # turns it into an unknown decision, never a smaller or unidentified owner set. An account
        # that reads back with no owner (value None) still stays eligible and unresolved, as before.
        programs = self.owner_programs(distinct)
        holders = []
        for ata, balance in accounts:
            owner = owners.get(ata)
            exclusion, program_unknown = None, False
            if owner is not None:
                program = programs.get(owner)
                if owner not in programs:
                    program_unknown = True  # an omitted or malformed provider row proves no absence
                elif program is None:
                    # No account at the owner address (Alpha/Beta 2026-10-29: 11 of 11 sampled were on-curve 0-SOL
                    # wallets). An on-curve key is a wallet: eligible and funder-traced as usual. Anything else (an
                    # off-curve PDA authority, an unparseable address) stays program_unknown and is NOT excluded:
                    # excluding drops its balance from the concentration math (memecoin_cluster_service.decide,
                    # owners_by_supply), so a large unfunded PDA could make a coin look less concentrated.
                    program_unknown = on_curve(owner) is not True
                elif program != SYSTEM_PROGRAM_ID:
                    exclusion = f"program_account:{program}"
            holders.append(EligibleHolder(ata, owner, int(balance), exclusion, program_unknown))
        return holders

    def trace_first_funder(
        self, owner: str, max_pages: int = 20, should_stop=None
    ) -> tuple[str | None, str]:
        """Funder of the owner's FIRST transaction. Records the exact signature the
        decision was read from on `last_source_signature`, so a later parser-version
        change can identify and invalidate the rows it affected.

        `should_stop` is an optional zero-argument predicate checked between pages. A trace can run
        for 20 paged RPC reads with network timeouts, and NOTHING outside it can stop that: a
        caller's `future.cancel()` only drops futures that never started, and
        `ThreadPoolExecutor.shutdown(wait=False)` deliberately does not wait. Measured before this
        existed: five back-to-back deadlined `decide_cluster` calls at workers=3 left 15 traces
        still running and 16 threads alive, each holding an RPC connection and spending the
        research quota. Cooperative cancellation is the only thing that actually stops the work.
        Returns `(None, "aborted")` when asked to stop; None means the old unstoppable behaviour.
        """
        self.last_source_signature = None
        before = None
        earliest = None
        candidate = None
        for _page in range(max_pages):
            if should_stop is not None and should_stop():
                return None, "aborted"
            options = {"limit": 1000}
            if before:
                options["before"] = before
            rows = self.call("getSignaturesForAddress", [owner, options]) or []
            if not rows:
                earliest = candidate
                break
            candidate = rows[-1].get("signature")
            if len(rows) < 1000:
                earliest = candidate
                break
            before = candidate
        else:
            return None, "deep"
        if not earliest:
            return None, "unresolved"
        if should_stop is not None and should_stop():
            return None, "aborted"
        tx = self.call(
            "getTransaction",
            [earliest, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
        )
        if not tx:
            return None, "unresolved"
        instructions = list(tx["transaction"]["message"].get("instructions") or [])
        for group in (tx.get("meta") or {}).get("innerInstructions") or []:
            instructions.extend(group.get("instructions") or [])
        for instruction in instructions:
            parsed = instruction.get("parsed")
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info") or {}
            destination = (
                info.get("destination")
                if parsed.get("type") == "transfer"
                else info.get("newAccount")
            )
            if parsed.get("type") in ("transfer", "createAccount") and destination == owner:
                self.last_source_signature = earliest
                return info.get("source"), "resolved"
        return None, "unresolved"


# ---- desk-only funder trace (Alpha/Gamma 2026-10-22; funded gate untouched) -------------------------------------------------
# The desk cluster check is a JUDGE fact; its trace differs from the funded gate's in exactly two frozen ways, both part of the
# cohort identity (cluster_parser_version, max_pages): (1) at most DESK_MAX_PAGES signature pages, so an owner with more than
# 1000 * DESK_MAX_PAGES signatures ends 'deep' after DESK_MAX_PAGES calls instead of 20; (2) a funder that credits the owner
# through a wrapped-SOL close is read: the first transaction's closeAccount(destination = owner) names the closed temp account,
# and the funder is the source of the largest system credit into that account (createAccount, createAccountWithSeed or transfer).
# Replay of 70 unresolved owners: 36% fit this shape (11 seeded, 14 createAccount), 0 extra calls. ResearchClusterRpc itself is
# byte-for-byte unchanged, so the funded gate and its parser_version 1 cache cannot move.
DESK_PARSER_VERSION = 3            # v2 (wrap rule + page cap) plus the funded-by fallback: its results never share a cache key with v2
DESK_PARSER_VERSION_NO_FALLBACK = 2   # the worker's cache key with the fallback switched off (kill knob)
DESK_MAX_PAGES = 2
DESK_WRAP_RULE = "largest-system-credit-source-v1"   # the wrap rule's name, frozen in the cohort policy
DESK_FUNDED_BY = "funded-by-v1"    # the fallback's name, frozen in the cohort policy
FUNDED_BY_HOST = "https://api.helius.xyz/v1/wallet/%s/funded-by"
KNOWN_FUNDERS_PATH = Path(__file__).with_name("memecoin_cluster_known_funders.json")


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_address(value) -> bool:
    """A real Solana address: base58 alphabet only, 32-44 characters, decoding to exactly 32 bytes (a length check alone accepts
    32 spaces). Anything else is never an identity."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44 or any(ch not in _B58 for ch in value):
        return False
    n = 0
    for ch in value:
        n = n * 58 + _B58.index(ch)
    return len((b"\x00" * (len(value) - len(value.lstrip("1")))) + n.to_bytes((n.bit_length() + 7) // 8, "big")) == 32


def desk_definition(funded_by=True) -> dict:
    """The WHOLE desk cluster definition, in the cohort policy's own shape: the one dict the provision script freezes, preflight
    compares, the worker stamps on every row and the consumer compares for equality. funded_by False (the kill knob) is the v2
    definition: no fallback, no list."""
    if not funded_by:
        return {"parser_version": DESK_PARSER_VERSION_NO_FALLBACK, "max_pages": DESK_MAX_PAGES, "wrap_close_rule": DESK_WRAP_RULE,
                "funded_by_fallback": None, "known_funders": None}
    known = known_funders()
    return {"parser_version": DESK_PARSER_VERSION, "max_pages": DESK_MAX_PAGES, "wrap_close_rule": DESK_WRAP_RULE,
            "funded_by_fallback": DESK_FUNDED_BY, "known_funders": {"version": known["version"], "sha256": known["sha256"]}}


def known_funders():
    """{version, sha256, addresses: frozenset} of the frozen non-clusterable funder list (shared infrastructure: exchange hot wallets,
    the FOMO co-signer). The policy freezes version and sha256; this reads the committed file, never a live provider label."""
    raw = KNOWN_FUNDERS_PATH.read_bytes()
    doc = json.loads(raw)
    return {"version": doc["version"], "sha256": hashlib.sha256(raw).hexdigest(), "addresses": frozenset(doc["addresses"])}


def wrap_close_funder(instructions, owner):
    """The payer that funded `owner` through a closed wrapped-SOL account in these instructions, or None. Pure."""
    def info(ix):
        parsed = ix.get("parsed")
        return (parsed.get("type"), parsed.get("info") or {}) if isinstance(parsed, dict) else (None, {})
    closed = {i.get("account") for ix in instructions for kind, i in [info(ix)]
              if kind == "closeAccount" and i.get("destination") == owner}
    credits = []
    for ix in instructions:
        kind, i = info(ix)
        dest = i.get("newAccount") if kind in ("createAccount", "createAccountWithSeed") else (
            i.get("destination") if kind == "transfer" else None)
        lamports = i.get("lamports")
        if dest in closed and i.get("source") and type(lamports) is int and not isinstance(lamports, bool) and lamports > 0:
            credits.append((lamports, i["source"]))
    if not credits:
        return None
    best = max(credits)[1]
    return best if is_address(best) and best != owner else None


def _funder_type_slug(value):
    """The provider's funder category as a short slug (never free text into a stored fact), or None."""
    import re
    text = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")[:32] if isinstance(value, str) else ""
    return text or None


class DeskClusterRpc(ResearchClusterRpc):
    """ResearchClusterRpc with the desk-only trace above. One instance per check.
    Order per owner: direct transfer / createAccount to the owner (source direct_tx), then the wrapped-SOL close rule (wrap_rule),
    then, ONLY for an owner left unresolved or deep and only when funded_by is on, ONE Helius funded-by call (funded_by). A
    parser-resolved owner is never overridden; optionally every `audit_every`-th such owner (stable hash) is probed for comparison
    only (the answer is counted, never used) while the check has calls to spare.
    funded-by outcomes, each distinct and none valued as 'no funder': ok (a funder), absent (404, or no/self funder: the provider
    knows there is no funding), dependency (429, 5xx, timeout, malformed: the provider could not answer; one paced retry for a
    transient only; a 429 pauses the shared pacer), fatal (401/403: the pass stops). The owner stays unresolved / deep in every
    case but ok."""

    def __init__(self, *args, max_pages=DESK_MAX_PAGES, funded_by=False, audit_every=0, calls_left=None,
                 parser_version=DESK_PARSER_VERSION, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_pages = int(max_pages)
        self.parser_version = int(parser_version)
        self.deep = 0
        self.funded_by = bool(funded_by)
        self.audit_every = int(audit_every)
        self.calls_left = calls_left            # () -> calls the per-coin budget still has; an audit call needs room
        self.stats = {"funded_by_calls": 0, "ok": 0, "absent": 0, "dependency": 0, "audit_n": 0, "audit_agree": 0}
        self.source_counts = {}                 # funder_source -> owners traced in this check
        self.fatal = None
        self._lock = threading.Lock()
        from urllib.parse import parse_qs, urlsplit
        self._api_key = (parse_qs(urlsplit(self.url).query).get("api-key") or [""])[0]
        self._info = threading.local()          # per trace thread: the source of the funder last returned

    @property
    def last_source_info(self):
        return getattr(self._info, "value", None)

    def _tally(self, key, n=1):
        with self._lock:
            self.stats[key] += n

    def _funded_by_call(self, owner):
        """('ok', funder, type, detail) | ('absent', ...) | ('dependency', ...) | ('fatal', ...). Never raises for a provider answer.
        ONE call: a transient failure (5xx, timeout) was already retried once, paced and counted, by the metered opener that is
        self.http (a second retry here would stack to four calls on a flaky provider)."""
        request = Request(FUNDED_BY_HOST % owner, headers={"accept": "application/json", "x-api-key": self._api_key})
        self._tally("funded_by_calls")
        try:
            response = self.http(request, timeout=self.timeout_s)
            body = json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                return "absent", None, None, None
            if exc.code in (401, 403):
                self.fatal = "funded_by_auth"           # set HERE so the fallback and the audit probe both stop the pass
                return "fatal", None, None, None
            if exc.code == 429:
                self._quota()                       # pauses the shared pacer
            return "dependency", None, None, None
        except (OSError, ValueError):
            return "dependency", None, None, None
        funder = body.get("funder") if isinstance(body, dict) else None
        if funder is None and isinstance(body, dict):
            return "absent", None, None, None
        if not is_address(funder):
            return "dependency", None, None, None          # malformed: not a base58 address of 32 bytes
        if funder == owner:
            return "absent", None, None, None
        detail = {k: body.get(k) for k in ("slot", "date", "signature") if body.get(k) is not None}
        return "ok", funder, _funder_type_slug(body.get("funderType")), detail

    def _fallback(self, owner, status, should_stop):
        """The parser left `owner` as `status` ('unresolved' | 'deep')."""
        if not self.funded_by or self.fatal or (should_stop is not None and should_stop()):
            return None, status
        outcome, funder, funder_type, detail = self._funded_by_call(owner)
        self._tally("ok" if outcome == "ok" else outcome if outcome in ("absent", "dependency") else "dependency")
        if outcome != "ok":
            return None, status
        self._info.value = {"funder_source": "funded_by", "funder_type": funder_type, "detail": detail}
        self._count("funded_by")
        return funder, "resolved"

    def _count(self, source):
        with self._lock:
            self.source_counts[source] = self.source_counts.get(source, 0) + 1

    def _resolved(self, owner, funder, source, signature):
        """A parser-resolved owner: record its source and, for a stable sample, compare with funded-by (never used)."""
        self.last_source_signature = signature
        self._info.value = {"funder_source": source, "funder_type": None, "detail": None}
        self._count(source)
        if (self.funded_by and self.audit_every > 0 and not self.fatal
                and int(hashlib.sha256(owner.encode()).hexdigest(), 16) % self.audit_every == 0
                and (self.calls_left is None or self.calls_left() > 8)):
            outcome, other, _t, _d = self._funded_by_call(owner)
            if outcome == "ok":
                self._tally("audit_n")
                self._tally("audit_agree", int(other == funder))
        return funder, "resolved"

    def extras(self):
        """The row fields: frozen definition pieces, per-source owner counts and the funded-by accounting."""
        with self._lock:
            return {"cluster_definition": desk_definition(self.funded_by),
                    "cluster_parser_version": self.parser_version, "max_pages": self.max_pages, "deep_owners": self.deep,
                    "funded_by_enabled": self.funded_by, "funder_sources": dict(self.source_counts),
                    "funded_by": dict(self.stats), **({"fatal": self.fatal} if self.fatal else {})}

    def trace_first_funder(self, owner, max_pages=None, should_stop=None):
        pages = self.max_pages if max_pages is None else max_pages
        self.last_source_signature = None
        self._info.value = None
        before, earliest, candidate, candidate_err = None, None, None, None
        for _page in range(pages):
            if should_stop is not None and should_stop():
                return None, "aborted"
            options = {"limit": 1000}
            if before:
                options["before"] = before
            rows = self.call("getSignaturesForAddress", [owner, options]) or []
            if not rows:
                earliest = candidate
                break
            candidate = rows[-1].get("signature")
            candidate_err = rows[-1].get("err")
            if len(rows) < 1000:
                earliest = candidate
                break
            before = candidate
        else:
            with self._lock:
                self.deep += 1
            return self._fallback(owner, "deep", should_stop)
        if not earliest:
            return self._fallback(owner, "unresolved", should_stop)
        if candidate_err is not None:
            # A FAILED first transaction moves no lamports: its instruction list names a transfer that never happened (Codex P1, 3821d27b3).
            # The parser leaves it unresolved (no funder, no source signature); the funded-by fallback, which knows the first
            # SUCCESSFUL incoming transfer, may still answer for it.
            return self._fallback(owner, "unresolved", should_stop)
        if should_stop is not None and should_stop():
            return None, "aborted"
        tx = self.call("getTransaction", [earliest, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if not tx:
            return self._fallback(owner, "unresolved", should_stop)
        meta = tx.get("meta")
        if not isinstance(meta, dict) or meta.get("err") is not None:    # cannot verify success, or it failed: fail closed
            return self._fallback(owner, "unresolved", should_stop)
        instructions = list(tx["transaction"]["message"].get("instructions") or [])
        for group in (tx.get("meta") or {}).get("innerInstructions") or []:
            instructions.extend(group.get("instructions") or [])
        for instruction in instructions:
            parsed = instruction.get("parsed")
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info") or {}
            destination = (info.get("destination") if parsed.get("type") == "transfer" else info.get("newAccount"))
            if parsed.get("type") in ("transfer", "createAccount") and destination == owner:
                if not is_address(info.get("source")) or info.get("source") == owner:
                    break                                   # not a funder identity: fall through to the wrap rule and the fallback
                return self._resolved(owner, info.get("source"), "direct_tx", earliest)
        wrapped = wrap_close_funder(instructions, owner)
        if wrapped:
            return self._resolved(owner, wrapped, "wrap_rule", earliest)
        return self._fallback(owner, "unresolved", should_stop)
