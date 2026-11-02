"""Solana custody + balance reads for the real-money memecoin leg (S4-min).

Keypair generation/loading (secrets 0600, never in git/DB/logs) and JSON-RPC balance reads
(SOL + SPL token). RPC calls take an injectable `http` (method, url, headers, body, timeout) ->
(status, json, date) so the money path is unit-tested without a live endpoint. NOTHING here signs
or submits — that's the executor (S2/S3). Requires `solders` (installed; in requirements.txt).
"""
from __future__ import annotations

import json as _json
import math
import os
import urllib.request
from dataclasses import dataclass

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def _http(method, url, headers=None, body=None, timeout=10):
    h = {"user-agent": _UA, "accept": "application/json", "content-type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method=method,
                                data=body.encode() if isinstance(body, str) else body)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (_json.loads(raw) if raw else {}), None


# --- keypair (solders) ---

def generate_keypair_file(path):
    """Create a fresh keypair and write its 64-byte secret as a JSON int array (Solana CLI format),
    0600. Overwrites — caller ensures this is a deliberate new wallet."""
    from solders.keypair import Keypair
    kp = Keypair()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    # write via a private-perm temp then rename so the secret is never briefly world-readable
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(_json.dumps(list(bytes(kp))))   # 64-byte secret as int array (Solana CLI format)
    os.chmod(path, 0o600)
    return kp


def load_keypair(path):
    from solders.keypair import Keypair
    with open(path) as f:
        arr = _json.load(f)
    return Keypair.from_bytes(bytes(arr))


def pubkey_str(keypair):
    return str(keypair.pubkey())


# --- balance reads (JSON-RPC) ---


@dataclass(frozen=True)
class TokenBalanceSnapshot:
    """One exact SPL balance response for entry-delta accounting."""

    atomic: int
    decimals: int | None
    context_slot: int | None = None

    @property
    def human(self) -> float:
        if self.decimals is None:
            return 0.0
        return self.atomic / (10 ** self.decimals)


@dataclass(frozen=True)
class NativeSolBalanceRead:
    """One native-SOL read. A failed read is not a zero balance."""

    ok: bool
    balance: float | None
    failure: str | None


class TokenBalanceReadError(RuntimeError):
    """The provider did not prove a token balance; never substitute zero."""

    def __init__(self, failure):
        self.failure = failure
        super().__init__("token balance unavailable: " + failure)


def _rpc(url, method, params, http=None):
    http = http or _http
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    _s, j, _d = http("POST", url, body=body)
    if _s != 200:
        raise TokenBalanceReadError("http_" + str(_s) if type(_s) is int else "http_error")
    return j


def sol_balance_read(pubkey, rpc_url, http=None):
    """Return typed native-SOL truth from one confirmed JSON-RPC read."""
    try:
        j = _rpc(rpc_url, "getBalance", [pubkey, {"commitment": "confirmed"}], http=http)
    except Exception:
        return NativeSolBalanceRead(False, None, "rpc_error")
    if not isinstance(j, dict):
        return NativeSolBalanceRead(False, None, "malformed_response")
    if j.get("error") is not None:
        return NativeSolBalanceRead(False, None, "json_rpc_error")
    try:
        value = j["result"]["value"]
        if isinstance(value, bool):
            raise ValueError("boolean balance")
        balance = float(value) / 1e9
        if not math.isfinite(balance) or balance < 0:
            raise ValueError("invalid balance")
    except (KeyError, TypeError, ValueError, OverflowError):
        return NativeSolBalanceRead(False, None, "malformed_response")
    return NativeSolBalanceRead(True, balance, None)


def native_sol_balance_value(read):
    """Return a usable SOL value from a typed read or a legacy numeric test seam."""
    if isinstance(read, NativeSolBalanceRead):
        return read.balance if read.ok else None
    try:
        value = float(read)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def sol_balance(pubkey, rpc_url, http=None):
    """Compatibility float result for non-funded callers."""
    result = sol_balance_read(pubkey, rpc_url, http=http)
    return result.balance if result.ok else 0.0


def token_balance_snapshot(pubkey, mint, rpc_url, http=None, *, min_context_slot=None, ata_fallback=False):
    """One validated owner response; an empty index is not funded-exit proof.

    ata_fallback (EXIT reads only, never the entry path): when the owner index answers an empty list, read
    our derived associated token accounts (Token and Token-2022) directly by address in one
    getMultipleAccounts call. The owner index lags our own fresh buy by 2-10 s (2026-10-23, pos 74149: the
    trail fired at -25% but the sell could not be sized until -43.6%); an account read by its address does not."""
    try:
        options = {"encoding": "jsonParsed", "commitment": "confirmed"}
        if min_context_slot is not None:
            if type(min_context_slot) is not int or min_context_slot < 0:
                raise ValueError('invalid minimum token balance slot')
            options['minContextSlot'] = min_context_slot
        j = _rpc(rpc_url, "getTokenAccountsByOwner",
                 [pubkey, {"mint": mint}, options], http=http)
        if not isinstance(j, dict):
            raise TokenBalanceReadError("malformed_response")
        if j.get("error") is not None:
            raise TokenBalanceReadError("json_rpc_error")
        result = j.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise TokenBalanceReadError("malformed_response")
        context_slot = None
        if 'context' in result:
            context_slot = result['context']['slot']
            if type(context_slot) is not int or context_slot < 0:
                raise TokenBalanceReadError("malformed_response")
        if min_context_slot is not None and (context_slot is None or context_slot < min_context_slot):
            raise TokenBalanceReadError("stale_context")
        if ata_fallback and result["value"] == []:
            # the direct read must be at least as fresh as the strongest floor we know: an older
            # load-balanced bank must never prove the current bag gone (Codex review of 32699f336)
            floor = max(x for x in (min_context_slot, context_slot, 0) if x is not None)
            rows, context_slot = _derived_token_accounts(pubkey, mint, rpc_url, http, floor)
            result = {"value": rows}
        total_atomic = 0
        decimals = None
        seen = set()
        for acc in result["value"]:
            account = acc["account"]
            parsed = account["data"]["parsed"]
            info = parsed["info"]
            amount = info['tokenAmount']
            key, raw = acc.get('pubkey'), amount['amount']
            if (type(key) is not str or not key or key in seen or info.get('owner') != pubkey
                    or info.get('mint') != mint or type(raw) is not str or not raw.isascii()
                    or not raw.isdecimal() or int(raw) >= 2**64
                    or type(amount['decimals']) is not int or not 0 <= amount['decimals'] <= 255
                    or account.get('owner') not in (_TOKEN_PROGRAM, _TOKEN_2022_PROGRAM)
                    or parsed.get('type') != 'account'):
                raise TokenBalanceReadError("account_identity_or_amount")
            seen.add(key)
            account_decimals = amount["decimals"]
            if decimals is not None and account_decimals != decimals:
                raise TokenBalanceReadError("inconsistent_decimals")
            decimals = account_decimals
            total_atomic += int(amount["amount"])
            if total_atomic >= 2**64:
                raise TokenBalanceReadError("amount_overflow")
        return TokenBalanceSnapshot(total_atomic, decimals, context_slot)
    except TokenBalanceReadError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise TokenBalanceReadError("malformed_response") from exc
    except Exception as exc:
        failure = ("http_429" if getattr(exc, "code", None) == 429 else
                   "provider_timeout" if isinstance(exc, TimeoutError) else "rpc_transport")
        raise TokenBalanceReadError(failure) from exc


def _derived_token_accounts(pubkey, mint, rpc_url, http, min_context_slot):
    """Our ATAs for `mint` under both token programs, shaped like owner-index rows (the caller's loop
    validates owner, mint, program and amount exactly as for the index). Absent accounts are dropped;
    each present one must be owned by the program its address was derived with, and a mint lives under
    ONE program, so two present accounts are a conflict and fail closed."""
    from sniper.execution.pump_native_exit import ata, _pk
    programs = (_TOKEN_PROGRAM, _TOKEN_2022_PROGRAM)
    keys = [str(ata(_pk(pubkey), _pk(mint), _pk(program))) for program in programs]
    options = {"encoding": "jsonParsed", "commitment": "confirmed", "minContextSlot": min_context_slot}
    j = _rpc(rpc_url, "getMultipleAccounts", [keys, options], http=http)
    if not isinstance(j, dict):
        raise TokenBalanceReadError("malformed_response")
    if j.get("error") is not None:
        raise TokenBalanceReadError("json_rpc_error")
    result = j.get("result")
    values = result.get("value") if isinstance(result, dict) else None
    if not isinstance(values, list) or len(values) != len(keys):
        raise TokenBalanceReadError("malformed_response")
    context = result.get("context")
    slot = context.get("slot") if isinstance(context, dict) else None
    if type(slot) is not int or slot < 0:
        raise TokenBalanceReadError("malformed_response")
    if slot < min_context_slot:
        raise TokenBalanceReadError("stale_context")
    rows = []
    for key, program, acc in zip(keys, programs, values):
        if acc is None:
            continue
        if not isinstance(acc, dict) or acc.get("owner") != program:
            raise TokenBalanceReadError("account_identity_or_amount")
        info = ((acc.get("data") or {}).get("parsed") or {}).get("info") if isinstance(acc.get("data"), dict) else None
        if not isinstance(info, dict) or info.get("state") == "frozen":
            raise TokenBalanceReadError("account_frozen" if isinstance(info, dict) else "malformed_response")
        rows.append({"pubkey": key, "account": acc})
    if len(rows) > 1:
        raise TokenBalanceReadError("conflicting_token_program")
    return rows, slot


def token_balance_with_decimals(pubkey, mint, rpc_url, http=None):
    """Compatibility pair derived from the exact one-response balance snapshot."""
    snapshot = token_balance_snapshot(pubkey, mint, rpc_url, http=http)
    return snapshot.human, snapshot.decimals


def token_balance_checked(pubkey, mint, rpc_url, http=None):
    """Balance that RAISES when the read fails, instead of reporting a confident zero.

    A failed read must stay unknown for sizing and reconciliation: a transient RPC 429 made a fully
    held position read as a 100% ledger-vs-chain drift and pages the operator about funds that are
    exactly where the ledger says. (2026-07-29: pos#23496 held 39,178.307637 NEEGY against a ledger
    expecting 39,178.307636 — a one-atomic-unit rounding difference — while both configured RPCs were
    rate-limiting.) Only an RPC that successfully reports no token account returns 0.0 here.
    """
    accounts = token_accounts_checked(pubkey, mint, rpc_url, http=http)
    if not accounts:
        return 0.0
    return sum(a for _k, a, _d in accounts) / (10 ** accounts[0][2])


def token_accounts_checked(pubkey, mint, rpc_url, http=None):
    """[(account, atomic, decimals)] of every `mint` account `pubkey` owns (the owner scan,
    getTokenAccountsByOwner). Raises on a failed read; [] only when the RPC reports none."""
    j = _rpc(rpc_url, "getTokenAccountsByOwner",
             [pubkey, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
             http=http)
    if not isinstance(j, dict) or j.get("error") is not None:
        raise RuntimeError("solana balance read failed: %s" % ((j or {}).get("error"),))
    result = j.get("result")
    if not isinstance(result, dict) or "value" not in result:
        raise RuntimeError("solana balance read returned no result envelope")
    value = result.get("value")
    if not isinstance(value, list):
        # A null, false, "" or {} value is not a successful owner-account list (SOL-4, Codex review
        # 2026-10-07): it read as [] and so a confident 0 USDC. Only a list is an answer; [] is
        # the known zero.
        raise RuntimeError("solana balance read returned no account list: %s" % type(value).__name__)
    out, decimals = [], None
    for acc in value:
        amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
        account_decimals = int(amount["decimals"])
        if decimals is not None and account_decimals != decimals:
            raise ValueError("token accounts disagree on mint decimals")
        decimals = account_decimals
        # A reply without the account's address still counts toward the total; it just never
        # proves the ATA is the only account (UsdcBankrollRead keeps scanning).
        out.append((acc.get("pubkey"), int(amount["amount"]), account_decimals))
    return out


def token_account_checked(account, *, owner, mint, rpc_url, http=None):
    """(atomic, decimals) of ONE token account (getAccountInfo jsonParsed), or None when it is
    absent, not an initialized SPL Token account, or held by another owner or for another mint.
    Raises on a failed read."""
    j = _rpc(rpc_url, "getAccountInfo", [account, {"encoding": "jsonParsed", "commitment": "confirmed"}],
             http=http)
    if not isinstance(j, dict) or j.get("error") is not None:
        raise RuntimeError("solana account read failed: %s" % ((j or {}).get("error"),))
    result = j.get("result")
    if not isinstance(result, dict) or "value" not in result:
        raise RuntimeError("solana account read returned no result envelope")
    value = result.get("value")
    try:
        parsed = value["data"]["parsed"]
        info = parsed["info"]
        if (value.get("owner") != _TOKEN_PROGRAM or parsed.get("type") != "account"
                or info.get("owner") != owner or info.get("mint") != mint
                or info.get("state") != "initialized"):
            return None
        amount = info["tokenAmount"]
        return int(amount["amount"]), int(amount["decimals"])
    except (KeyError, TypeError, ValueError):
        return None


_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class UsdcBankrollRead:
    """The funded USDC bankroll, one light read most of the time (2026-10-07, Alpha's yes).

    The owner scan (getTokenAccountsByOwner) took 1.4-3.0 s at the prefetch loop's tail. While the
    last scan, at most SCAN_MAX_AGE_S old, found exactly one USDC account and it is the wallet's
    associated account, the bankroll is that account alone (getAccountInfo, owner and mint checked).
    Anything else is the scan, today's read: no recent scan, USDC in another account, or the ATA read
    failing, absent, closed, frozen or naming another owner or mint. A missed fast path never reads
    None or 0. USDC sent to a NEW non-ATA account is uncounted until the next scan (<= 300 s): the
    bankroll reads low, never high. `read()` raises like token_balance_checked. Thread-safe: the
    scan verdict is one tuple, replaced whole. `stats` keeps each path's milliseconds for the
    prefetch report, drained by take_stats()."""

    SCAN_MAX_AGE_S = 300.0

    def __init__(self, pubkey, rpc_url, *, http=None, clock=None):
        import collections
        import time as _time
        from sniper.execution.pump_native_exit import ata, _pk
        self.pubkey, self.rpc_url, self.http = str(pubkey), rpc_url, http
        self.clock = clock or _time.monotonic
        self.ata = str(ata(_pk(pubkey), _pk(USDC_MINT), _pk(_TOKEN_PROGRAM)))
        self._verdict = (None, False)        # (monotonic scan time, the ATA is the only account)
        self.stats = {"ata": collections.deque(maxlen=512), "scan": collections.deque(maxlen=512)}

    def read(self):
        scanned_at, ata_only = self._verdict
        if ata_only and scanned_at is not None and 0 <= self.clock() - scanned_at < self.SCAN_MAX_AGE_S:
            t0 = self.clock()
            try:
                got = token_account_checked(self.ata, owner=self.pubkey, mint=USDC_MINT,
                                            rpc_url=self.rpc_url, http=self.http)
            except Exception:
                got = None                   # a failed fast read is the scan's job, never None
            self.stats["ata"].append((self.clock() - t0) * 1000.0)
            if got is not None and got[1] == 6:
                return got[0] / 1e6
        return self._scan()

    def _scan(self):
        t0 = self.clock()
        accounts = token_accounts_checked(self.pubkey, USDC_MINT, self.rpc_url, http=self.http)
        self.stats["scan"].append((self.clock() - t0) * 1000.0)
        self._verdict = (t0, [a for a, _amt, _d in accounts] == [self.ata])
        if not accounts:
            return 0.0
        return sum(a for _k, a, _d in accounts) / (10 ** accounts[0][2])

    def take_stats(self):
        """{path: [ms...]} since the last call, then cleared (the prefetch report's window)."""
        out = {k: list(v) for k, v in self.stats.items()}
        for v in self.stats.values():
            v.clear()
        return out


def token_balance(pubkey, mint, rpc_url, http=None):
    """SPL token uiAmount for `mint` held by `pubkey` (USDC or a memecoin), summed across accounts.
    No token account -> 0.0. A failed or malformed response raises.
    Reads at commitment=confirmed — the executors confirm txs at 'confirmed', so fill deltas read
    at the default (finalized) lag behind and book good fills as anomalies/gone."""
    return token_balance_with_decimals(pubkey, mint, rpc_url, http=http)[0]


def token_balance_raw(pubkey, mint, rpc_url, http=None, *, ata_fallback=False):
    """RAW atomic SPL balance (integer, summed across accounts) — the EXACT on-chain amount, used to
    size a sell. NEVER derive atomic from uiAmount*10^dec: memecoin balances exceed 2^53 and the float
    round-trip mis-sizes the sell (revert/false-stuck or dust). Failed reads raise."""
    return token_balance_snapshot(pubkey, mint, rpc_url, http=http, ata_fallback=ata_fallback).atomic


def usdc_balance(pubkey, rpc_url, http=None):
    """Trading bankroll = live USDC balance (wallet = bankroll, per the S1 envelope)."""
    return token_balance(pubkey, USDC_MINT, rpc_url, http=http)
