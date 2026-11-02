"""Item 4 — real HTTP bindings for the DISARMED Solana executor seams.

The executor (`solana_swap_executor.execute_entry` / `solana_exit_executor.execute_exit`) is pure
orchestration over injected `quote_fn / sign_fn / send_fn / confirm_fn / usdc_fn / token_fn`. This
module supplies the REAL implementations of those seams:

  - Jupiter v6 (lite-api.jup.ag/swap/v1): quote (ExactIn) + swap-tx build. Endpoint live-verified
    2026-07-09 (a valid quote returned); the swap/sign/send/confirm chain is NOT integration-tested.
  - Solana JSON-RPC (Helius): sendTransaction (base64) + getSignatureStatuses confirm poll.
  - solders: deserialize Jupiter's unsigned VersionedTransaction, sign with our keypair, re-serialize.

`build_real_solana_executor` wires these into `execute_fn(conn, pid, mint, usd, now_iso)` and
`exit_fn(conn, position, exit_reason, now_iso)` — recovering source_user_id + strategy from the
pending row so the per-entry call contract stays small. Every function takes an injectable `http`
so the units are tested without a live endpoint. Fail-closed throughout: a bad response -> None ->
the executor aborts/widens/leaves-reserved, never a fabricated fill.

⚠ DISARMED. Requires a funded keypair + a Helius RPC key; the live smoke test (item 22) MUST precede
any arm. Nothing here runs unless the runner is armed (MEMECOIN_REAL_ENABLED) with creds present.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import re
import logging

from sniper.util.log_sample import log_sampled
import threading
import time
import json as _json
from time import monotonic as _monotonic, sleep as _sleep

from sniper.execution.solana_wallet import (
    USDC_MINT, TokenBalanceSnapshot, TokenBalanceReadError, _http, token_balance, token_balance_checked,
    token_balance_raw, token_balance_snapshot, usdc_balance,
)
from sniper.persistence import db_memecoin as m

logger = logging.getLogger(__name__)

JUP_BASE = "https://lite-api.jup.ag/swap/v1"
# Same provider, second free host. `lite-api` is the rate-limited tier and it bans by IP: on
# 2026-08-14 it answered 429 to EVERY request from this host for over an hour — including a trivial
# SOL/USDC probe — while `api.jup.ag` served complete quotes with no key. One quote host is a single
# point of failure for BOTH legs, because a funded entry and a funded exit each need an exact quote
# before they will sign: that outage cost live entries (RATIO, boden) and left position 37108 rails-
# blind for 700 sweeps at -67%. Neither host takes an API key, a tip, or a subscription, so this
# stays inside the free-submission doctrine.
JUP_FALLBACK_BASE = "https://api.jup.ag/swap/v1"
JUP_BASES = (JUP_BASE, JUP_FALLBACK_BASE)

# --- Funded Jupiter API key (2026-08-27, operator-issued portal key) ------------------------------
#
# WHY A KEY AT ALL. Measured 2026-08-27 from the heartbeat sampler: 510 of 1,933 funded Jupiter
# quotes were rate-limited, 26.4%, and every one of them landed inside a single 36-minute window
# where funded quoted 1.4-1.6/s and took ~1.0 429/s. Keyless capacity is not enough for the funded
# exit path on its own, never mind a second consumer.
#
# WHY api.jup.ag FIRST WHEN KEYED, and what that rests on. The key is issued for the portal tier,
# which is served by `api.jup.ag`; `lite-api` is the keyless rate-limited tier and honours no key,
# so sending one there buys nothing. That is the provider's own tiering, NOT a measurement of mine
# — and I cannot make one from what we record: JUPITER_LOAD counts 429s but has never recorded WHICH
# HOST answered, so today's 26.4% cannot be attributed to a host. `by_host` below closes that gap so
# the next measurement can.
#
# UNSET IS UNCHANGED. No key -> no header, no reordering, byte-identical to the keyless behaviour.
# A bad or expired key degrades to exactly today: api.jup.ag 429s, the host cools for 60s, and
# lite-api answers. Never worse than keyless.
JUP_API_KEY_VAR = "MEMECOIN_JUPITER_API_KEY"
CAPTURE_JUP_API_KEY_VAR = "MEMECOIN_CAPTURE_JUPITER_API_KEY"
PAPER_JUP_API_KEY_VAR = "MEMECOIN_PAPER_JUPITER_API_KEY"

# WHICH KEY THIS PROCESS SPENDS. A role, declared once at startup, not a per-call argument.
#
# The measurement lanes reuse `jupiter_entry_quote` / `jupiter_exit_quote`, so without this they
# would silently spend the FUNDED key — the exit brain's budget — which is the pooling the separate
# key exists to prevent. Threading a key parameter through five signatures on the funded quote path
# was the alternative; a process that quotes for research declares itself instead, and the funded
# hot path is untouched.
#
# Fails SAFE in the direction that matters: the default is the funded var, so a measurement process
# that forgets to declare gets the funded key and shows up in the guard test, rather than a funded
# process silently losing its key to a stale global.
_KEY_VAR = JUP_API_KEY_VAR

# PER-CALL ROLE, because a process-level declaration cannot work inside the funded runner.
#
# The promotion study's paper arms quote from INSIDE the runner, so `declare_measurement_process`
# is unavailable to them — declaring would hand the funded exit brain the paper key.
#
# ATTRIBUTION CORRECTED 2026-08-29. The incident that prompted this — 79 rate-limited of 199
# quotes, funded position 45003 blinded 48 times in 62 seconds — was NOT paper spending the funded
# key. `51c23d96` had already put the v6 paper lane on its own key, hours before. The mechanism was
# PER-IP CONTENTION: two consumers from one address, which is the actual content of the 08-27
# finding and is indifferent to which key each holds. The remedy worked because ARMS-off cut total
# IP volume, not because it unpooled a key.
#
# This role still earns its place — it makes the consumer nameable and is what any per-IP pacing
# has to key on — but it is not what fixed that day.
#
# A ContextVar rather than a threaded parameter: the paper quote path reaches this seam through
# five signatures on the funded hot path, and widening all of them to carry a role that is almost
# always the default is how the funded path acquires a paper-shaped bug. A ContextVar is per-call
# in effect, thread-safe by construction, and leaves the funded signatures untouched.
#
# Fails SAFE in the same direction as the process declaration: unset means FUNDED, so a paper call
# that forgets the scope spends the funded key and shows up in the guard test, rather than a funded
# call silently losing its key.
_ROLE = contextvars.ContextVar("jupiter_role", default=None)
ROLE_FUNDED, ROLE_PAPER, ROLE_CAPTURE = "funded", "paper", "capture"
_ROLE_KEY_VAR = {ROLE_PAPER: PAPER_JUP_API_KEY_VAR, ROLE_CAPTURE: CAPTURE_JUP_API_KEY_VAR}


@contextlib.contextmanager
def jupiter_role(role):
    """Scope every Jupiter call made inside this block to `role`'s key and budget.

    Wrap the CALL SITE, never a whole loop that also does funded work — the point is that a paper
    quote and a funded preview issued microseconds apart spend different budgets.
    """
    if role not in _ROLE_KEY_VAR and role != ROLE_FUNDED:
        raise ValueError("unknown jupiter role: %r" % (role,))
    token = _ROLE.set(role)
    try:
        yield
    finally:
        _ROLE.reset(token)


def active_key_var():
    """Which key variable a call issued right now would spend. For logging and for the guard."""
    role = _ROLE.get()
    if role in _ROLE_KEY_VAR:
        return _ROLE_KEY_VAR[role]
    return _KEY_VAR


def active_role():
    role = _ROLE.get()
    if role:
        return role
    return ROLE_CAPTURE if _KEY_VAR == CAPTURE_JUP_API_KEY_VAR else (
        ROLE_PAPER if _KEY_VAR == PAPER_JUP_API_KEY_VAR else ROLE_FUNDED)


def declare_measurement_process(key_var=None):
    """Call ONCE at startup in a research/measurement process. Never call this in the runner.

    `key_var` names WHICH measurement budget this process spends — the capture lane and the paper
    lanes have separate keys so one cannot starve the other. It must be a MEASUREMENT var: passing
    the funded one is refused, because the whole point of declaring is to leave the funded budget
    alone and a typo here would silently hand it back.
    """
    global _KEY_VAR
    key_var = key_var or CAPTURE_JUP_API_KEY_VAR
    if key_var not in (CAPTURE_JUP_API_KEY_VAR, PAPER_JUP_API_KEY_VAR):
        raise ValueError("%r is not a measurement key variable" % (key_var,))
    _KEY_VAR = key_var
    logger.info("jupiter: this process spends the MEASUREMENT key (%s), configured=%s",
                key_var, jup_key_configured())


def _jup_api_key(env=None):
    """The funded Jupiter key, or "" when unset. The VALUE never leaves this module."""
    import os
    try:
        return str((env if env is not None else os.environ).get(active_key_var()) or "").strip()
    except Exception:
        return ""


def jup_key_configured(env=None):
    """Whether a funded key is set. Booleans are safe to log; the key is not."""
    return bool(_jup_api_key(env))


def _jup_headers(env=None):
    """Header form of the key. A HEADER, never a query parameter — a key in the URL reaches logs,
    exception text, and any persisted request line, which is the one thing this must never do."""
    key = _jup_api_key(env)
    return {"x-api-key": key} if key else {}

# A 429'd host is skipped for this long rather than re-probed on every call — paying the ban on the
# primary before every fallback would put the outage back on the critical path.
JUP_HOST_COOLDOWN_S = 60.0
_jup_host_cooldown = {}


_HEADER_CAPABLE = {}


def _accepts_headers(http):
    """Whether this transport seam takes a `headers` kwarg.

    Injected test seams predate headers and are positional-only. Probing the signature once is
    cheaper than a TypeError retry and, unlike catching TypeError, cannot mistake an error raised
    INSIDE the seam for an unsupported signature — which on the funded path would silently drop the
    key and put us back on the keyless quota with no signal.
    """
    try:
        cached = _HEADER_CAPABLE.get(http)
    except TypeError:
        return False                       # unhashable seam: assume the old shape
    if cached is None:
        import inspect
        try:
            params = inspect.signature(http).parameters
            cached = ("headers" in params
                      or any(q.kind is inspect.Parameter.VAR_KEYWORD for q in params.values()))
        except (TypeError, ValueError):
            cached = False
        _HEADER_CAPABLE[http] = cached
    return cached


def _http_status(http, method, url, body=None, headers=None):
    """Call the http seam and ALWAYS return (status, json, _) — urllib raises HTTPError for every
    4xx/5xx, so a status-based branch (429 failover, 400 venue fallback) that only reads the tuple
    is dead code against the real transport. Live-caught 2026-08-14: Jupiter's 400 for an unindexed
    coin surfaced as an exception, was classed as transport, and the venue fallback never ran.
    Injected test seams that return tuples pass straight through; genuine transport errors
    (DNS, timeout) still raise."""
    import urllib.error
    try:
        if headers and _accepts_headers(http):
            return (http(method, url, headers=headers, body=body) if body is not None
                    else http(method, url, headers=headers))
        return http(method, url, body=body) if body is not None else http(method, url)
    except urllib.error.HTTPError as e:
        try:
            payload = _json.loads(e.read().decode())
        except Exception:
            payload = {}
        return e.code, payload, None


def _jup_hosts(clock=None, *, keyed=None, env=None):
    """Healthy hosts first, in preference order; a cooled host is still tried last, never dropped.

    With a funded key configured the keyed host leads, because the key is only honoured there. The
    keyless order is untouched, so an unset key changes nothing.
    """
    now = (clock or _monotonic)()
    if keyed is None:
        keyed = jup_key_configured(env)
    bases = (JUP_FALLBACK_BASE, JUP_BASE) if keyed else JUP_BASES
    ready = [b for b in bases if _jup_host_cooldown.get(b, 0.0) <= now]
    cooled = [b for b in bases if _jup_host_cooldown.get(b, 0.0) > now]
    return ready + cooled


# The host the CURRENT thread's last Jupiter request went to (hostname only, never the URL: a keyed
# URL is a credential). Thread-local, so concurrent entry and exit threads never read each other's.
_JUP_LAST = threading.local()


def _jup_host_name(base):
    from urllib.parse import urlsplit
    return urlsplit(str(base)).hostname or "?"


def _jup_call(method, path, *, body=None, http=None, clock=None, env=None, exclude=()):
    """Try each Jupiter host until one answers. Returns (status, json).

    Failover is for a failure about the HOST, never about the request or the network:

      - a 429 is the host refusing our volume.
      - a 401/403 FROM A HOST WE SENT A KEY TO denies keyed access, which is equally
        host-scoped. Added 2026-08-27 after review: the key shipped with failover still 429-only, so
        a revoked or expired key was a TOTAL funded quote outage rather than the degrade-to-keyless
        it was described as. Reproduced at status 401: one host tried, no fallback, quote None. Both
        funded legs need a quote before they will sign, so that is entry AND exit down at once.
        The fallback goes out WITHOUT the key header. A 403 can deny access with a valid key.
      - a transport error or a timeout PROPAGATES, so the caller's typed classifier still sees it and
        `2026-08-05-solana-quote-infra-transient-retry-design` still applies its single 200 ms retry.
        Swallowing it here would silently delete that retry.
      - any OTHER non-429 HTTP error is an answer about the REQUEST. Replaying a bad mint against
        every host just spends the healthy host's budget too. A 401 from a host we sent NO key to is
        in this class: nothing of ours was rejected, so it is not ours to retry.

    When every host 429s the last 429 is returned, so the caller still types RATE_LIMIT and the quote
    budget still takes its backoff."""
    http = http or _http
    clock = clock or _monotonic
    # FUNDED FIRST. A paper call yields here, bounded; a funded call registers itself and never
    # waits. Both branches are cheap, and the funded one takes no lock a paper call can hold.
    _role = active_role()
    if _role == ROLE_PAPER:
        _yield_to_funded()
        _inflight = contextlib.nullcontext()
    elif _role == ROLE_FUNDED:
        _inflight = _funded_inflight()
    else:
        _inflight = contextlib.nullcontext()
    with _inflight:
        return _jup_call_inner(method, path, body=body, http=http, clock=clock, env=env,
                               exclude=exclude)


def _jup_call_inner(method, path, *, body, http, clock, env, exclude=()):
    headers = _jup_headers(env)
    last = (None, None)
    _JUP_LAST.tries = 0                  # hosts this call tried (the leg-call slow line reads it)
    for base in _jup_hosts(clock, keyed=bool(headers), env=env):
        if base in exclude:
            continue
        _check_cash_jupiter_quiet()
        _JUP_LAST.tries += 1
        # ONLY THE HOST THAT HONOURS THE KEY RECEIVES IT. lite-api is the keyless tier and ignores
        # it, so sending it there buys nothing and widens the key's exposure for no gain.
        sent = headers if (headers and base in JUP_KEYED_BASES) else {}
        _JUP_LAST.base = base
        status, j, _d = _http_status(http, method, base + path, body=body, headers=sent)
        if sent and status is not None and 200 <= status < 300:
            # THE KEY WORKED, which is the only honest recovery signal: it is this same request
            # answering from the host that rejected it before. A dict read on the hot path, and an
            # enqueue only on the one call that closes a paged episode.
            _note_auth_alert(base, ok=True)
        if status == 429:
            _jup_host_cooldown[base] = clock() + JUP_HOST_COOLDOWN_S
            _note_host_429(base)
            last = (status, j)
            continue
        if status in _AUTH_STATUSES and sent:
            # Keyed access was denied. Cool this host and let the loop reach the keyless one, which
            # never saw the key and can still answer. Same 60s cooldown as a 429 — no new constant,
            # because this is the same "stop asking this host for a while" decision and I have no
            # data that would justify a different number.
            _jup_host_cooldown[base] = clock() + JUP_HOST_COOLDOWN_S
            _note_host_auth_failure(base, status)
            last = (status, j)
            continue
        return status, j
    return last


# --- Raydium venue fallback ---------------------------------------------------------------------
#
# 2026-08-14: five armed-wallet buys in one hour refused with "no quote" / "no planned-quantity
# sell route". The coins were minutes old; Jupiter's indexer had not picked their pools up yet
# (EMPLOYIM still answered HTTP 400 in BOTH directions 15 minutes later) while the traders bought
# the Raydium pool directly. Raydium's own trade API knows its pools instantly, is free, keyless,
# and venue-official — live-verified against the exact refused mints: quote both directions plus
# a one-transaction V0 build. Jupiter stays first (it routes across every venue); Raydium answers
# only when Jupiter has nothing, so an indexed coin's behaviour is byte-identical.
RAY_BASE = "https://transaction-v1.raydium.io"

_SOL_MINT = "So11111111111111111111111111111111111111112"
_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


class _LookupFailed:
    """Sentinel: the token-account lookup did not answer. Distinct from None, which is the RPC
    answering that the wallet holds no account. Falsy would be a trap here — the whole defect was
    a failed read reading as an empty one — so it is deliberately truthy and identity-compared."""

    __slots__ = ()

    def __repr__(self):
        return "LOOKUP_FAILED"


class _LookupUnavailable(Exception):
    """Internal: the RPC did not give us a usable answer on this attempt."""


def _ata(owner, mint):
    """Classic-SPL associated token account. Token-2022 mints derive from a different program and
    are NOT handled here.

    This docstring used to say their build "fails and the entry stays refused, which is the
    fail-closed side". That was wrong, and it cost money: nothing checked the mint's program, so a
    Token-2022 entry was SUBMITTED with a classic ATA and rejected on chain with
    `ConstraintMintTokenProgram` — STOCKCAT and SWEENIES, 2026-09-08, gas and a reservation slot
    each. The refusal now happens in `solana_token_safety.token_safety_ok`, before reservation."""
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(str(owner))),
         bytes(Pubkey.from_string(_TOKEN_PROGRAM)),
         bytes(Pubkey.from_string(str(mint)))],
        Pubkey.from_string(_ATA_PROGRAM))[0])


LOOKUP_FAILED = _LookupFailed()


def _owned_token_account(owner, mint, http=None, attempts=3, sleep=None, *, rpc_url=None):
    """The wallet's actual token account for `mint` (largest balance first).

    THREE states, and the caller must tell them apart:
      a pubkey       the wallet's real account
      None           the RPC ANSWERED and the wallet provably holds none — a buy of a coin never
                     held, where deriving the classic ATA is the correct and necessary answer
      LOOKUP_FAILED  we do not know, because the RPC did not answer

    Before 2026-09-08 the last two were the same value. `status` was captured and never read, so a
    429, a 5xx, a JSON-RPC error body and a transport failure all fell through
    `(((j or {}).get("result") or {}).get("value")) or []` to an empty list and returned None. The
    consumer read that as "no account" and derived the classic ATA — the exact defect recorded two
    lines above its call site as having killed the MOX exit ladder (2026-08-14,
    REQ_OWNER_ACCOUNT_ERROR on every rung, position reading "stuck" against a live bid). The fix
    for MOX was bypassed by any RPC hiccup during the exits it was written to protect.

    A 200 carrying a JSON-RPC `error` counts as a failure: checking the status code is necessary
    and not sufficient. Retries are bounded because the funded exit path waits on this.
    """
    import os as _os
    sleep = sleep or _sleep
    rpc = rpc_url if rpc_url is not None else _os.getenv("MEMECOIN_SOLANA_RPC_URL")
    if not rpc:
        # A PERMANENT FAULT, NOT A TRANSIENT ONE. This returns the same sentinel as a 429, and the
        # consumer logs the same "lookup unavailable" line for both — but a 429 self-heals in
        # seconds and an unset variable never does, silently removing Raydium from the funded exit
        # ladder for as long as it lasts. Raydium is the venue that quoted MOX.
        #
        # The branch is inert on this host only because `.env` carries the key AND
        # `load_env_files` runs AND setdefault does not lose to an empty shell value; nothing
        # asserts that chain. A startup gate is deliberately NOT the answer — one dead custody
        # provider blocking funded launch on 2026-09-03 is the cost of that design. Being audible
        # the first time it matters is enough.
        logger.error("funded Solana RPC is not configured — token account lookup cannot run and "
                     "every Raydium build will refuse; Raydium is unavailable to the exit ladder "
                     "until MEMECOIN_SOLANA_RPC_URL resolves")
        return LOOKUP_FAILED
    for attempt in range(max(1, int(attempts))):
        try:
            # At `confirmed` (2026-09-26): an exit seconds after its entry must find the account
            # that entry created; at the FINALIZED default it reads "no account" and the classic ATA
            # gets derived, the wrong account for a Token-2022 coin (the MOX shape).
            status, j, _d = _http_status(http or _http, "POST", rpc, body=_json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [str(owner), {"mint": str(mint)},
                           {"encoding": "jsonParsed", "commitment": "confirmed"}]}))
            result = (j or {}).get("result") if isinstance(j, dict) else None
            if status != 200 or not isinstance(j, dict) or j.get("error") or result is None:
                raise _LookupUnavailable("status=%s" % status)
            vals = result.get("value") or []
        except Exception:
            if attempt + 1 < max(1, int(attempts)):
                sleep(0.2)
                continue
            return LOOKUP_FAILED
        best, best_amt = None, -1
        for v in vals:
            try:
                amt = int(v["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError):
                continue
            if amt > best_amt:
                best, best_amt = v.get("pubkey"), amt
        return best
    return LOOKUP_FAILED


def _ray_quote(input_mint, output_mint, amount_atomic, slippage_bps, http):
    """Raydium compute quote, normalized to the Jupiter quote shape the executor already reads.

    priceImpactPct: Raydium reports PERCENT (1.73), Jupiter a FRACTION string ("0.0173"), and the
    executor multiplies by 100 — an unconverted Raydium value would slip a 173% impact past a 15%
    cap as 1.73%. Divide by 100 here, fail-closed to the sentinel on anything unparseable."""
    http = http or _http
    url = (f"{RAY_BASE}/compute/swap-base-in?inputMint={input_mint}&outputMint={output_mint}"
           f"&amount={int(amount_atomic)}&slippageBps={int(slippage_bps)}&txVersion=V0")
    try:
        status, j, _d = _http_status(http, "GET", url)
    except Exception:
        return None
    data = (j or {}).get("data") if isinstance(j, dict) else None
    if status != 200 or not (j or {}).get("success") or not isinstance(data, dict):
        return None
    out, thresh = data.get("outputAmount"), data.get("otherAmountThreshold")
    if not out or not thresh:
        return None
    try:
        impact = str(float(data.get("priceImpactPct")) / 100.0)
    except (TypeError, ValueError):
        impact = "1.0"                        # sentinel: unparseable impact must read as huge
    return {
        "inputMint": str(input_mint), "outputMint": str(output_mint),
        "inAmount": str(data.get("inputAmount") or int(amount_atomic)),
        "outAmount": str(out), "otherAmountThreshold": str(thresh),
        "priceImpactPct": impact, "swapMode": "ExactIn",
        "_venue": "raydium", "_raydium_response": j,
    }


def _simulate_verdict(tx_b64, http=None, *, rpc_url=None):
    """None when the unsigned transaction simulates clean; otherwise a short reason string.
    An unreadable simulation is a reason too -- it must never read as clean."""
    import os as _os
    from sniper.settings.memecoin import solana_read_rpc
    rpc = rpc_url if rpc_url is not None else (solana_read_rpc() or _os.getenv("MEMECOIN_SOLANA_RPC_URL"))
    if not rpc:
        return "unreadable: no rpc"
    try:
        status, j, _d = _http_status(http or _http, "POST", rpc, body=_json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
            "params": [tx_b64, {"encoding": "base64", "sigVerify": False,
                                "replaceRecentBlockhash": True, "commitment": "processed"}]}))
    except Exception as exc:
        return "unreadable: %s" % type(exc).__name__
    if status != 200 or not isinstance(j, dict) or j.get("error"):
        return "unreadable: status=%s error=%s" % (status, (j or {}).get("error") if isinstance(j, dict) else None)
    value = (j.get("result") or {}).get("value")
    if not isinstance(value, dict):
        return "unreadable: no value"
    err = value.get("err")
    if err is None:
        return None
    logs = [l for l in (value.get("logs") or []) if "Error" in l or "failed" in l][-2:]
    return "failed: err=%s logs=%s" % (_json.dumps(err)[:120], logs)


def _mint_is_token_2022(mint, http=None, *, rpc_url=None):
    """True / False / None for "the RPC did not answer".

    None is NOT False. A build that cannot read the mint must not proceed as though it had read
    "classic" — that is the same failed-read-reads-as-a-negative shape that killed the MOX exit
    ladder. The caller treats None as "do not use this rung".
    """
    import os as _os
    from sniper.execution.solana_token_safety import TOKEN_2022_PROGRAM
    rpc = rpc_url if rpc_url is not None else _os.getenv("MEMECOIN_SOLANA_RPC_URL")     # the same read `_owned_token_account` uses
    if not rpc:
        return None
    try:
        status, j, _d = _http_status(http or _http, "POST", rpc, body=_json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [str(mint), {"encoding": "jsonParsed"}]}))
        if status != 200 or not isinstance(j, dict) or j.get("error"):
            return None
        value = ((j.get("result") or {}) or {}).get("value")
        if not isinstance(value, dict):
            return None
        owner = value.get("owner")
        return None if owner is None else (owner == TOKEN_2022_PROGRAM)
    except Exception:
        return None


# Token-2022 extensions that make a PAYOUT asset unsafe to route a sell through (2026-09-13).
# fries' payout MCDx carried permanentDelegate, defaultAccountState, scaledUiAmountConfig,
# pausableConfig, confidentialTransferMint and a transferHook, and Jupiter failed at the ATA
# create for it. PUMP carries only a transferHook (plus metadata) and a real {CAT} holder's sell
# through it built at 1,133 bytes and simulated clean. So the hook is not the trap; these are.
_RESTRICTED_PAYOUT_EXTENSIONS = frozenset({
    "permanentDelegate", "defaultAccountState", "pausableConfig", "confidentialTransferMint",
    "scaledUiAmountConfig", "nonTransferable",
})
_PAYOUT_RESTRICTED_CACHE = {}


def _mint_extensions(mint, http=None, *, rpc_url=None):
    """frozenset of Token-2022 extension names; frozenset() for a classic SPL mint; None when the
    RPC did not answer. None is never "no extensions"."""
    import os as _os
    from sniper.execution.solana_token_safety import TOKEN_2022_PROGRAM
    rpc = rpc_url if rpc_url is not None else _os.getenv("MEMECOIN_SOLANA_RPC_URL")
    if not rpc:
        return None
    try:
        status, j, _d = _http_status(http or _http, "POST", rpc, body=_json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [str(mint), {"encoding": "jsonParsed"}]}))
        if status != 200 or not isinstance(j, dict) or j.get("error"):
            return None
        value = ((j.get("result") or {}) or {}).get("value")
        if not isinstance(value, dict) or not value.get("owner"):
            return None
        if value["owner"] != TOKEN_2022_PROGRAM:
            return frozenset()
        info = (((value.get("data") or {}).get("parsed") or {}).get("info") or {})
        return frozenset(str((e or {}).get("extension") or "") for e in info.get("extensions") or [])
    except Exception:
        return None


def _cached_payout_restricted(mint, http=None, *, rpc_url=None):
    """True / False / None: does this payout mint carry a restricting extension? Cached per mint
    (extensions do not change); None (no answer) is never cached."""
    if mint in _PAYOUT_RESTRICTED_CACHE:
        return _PAYOUT_RESTRICTED_CACHE[mint]
    exts = (_mint_extensions(mint, http, rpc_url=rpc_url) if rpc_url is not None
            else _mint_extensions(mint, http))
    if exts is None:
        return None
    answer = bool(exts & _RESTRICTED_PAYOUT_EXTENSIONS)
    _PAYOUT_RESTRICTED_CACHE[mint] = answer
    return answer


def raydium_swap_tx(quote, user_pubkey, *, http=None, priority_fee_microlamports=None,
                    return_metadata=False, read_rpc_url=None):
    """Build the unsigned V0 swap for a `_ray_quote` result. Exactly ONE transaction or None —
    a multi-transaction build cannot keep the single-signature write-ahead identity, so it is
    refused rather than partially signed."""
    http = http or _http
    read_kwargs = {"rpc_url": read_rpc_url} if read_rpc_url is not None else {}
    try:
        fee = int(priority_fee_microlamports) if priority_fee_microlamports else 25_000
        payload = {
            "computeUnitPriceMicroLamports": str(fee),
            "swapResponse": quote.get("_raydium_response"),
            "txVersion": "V0",
            "wallet": str(user_pubkey),
            "wrapSol": quote.get("inputMint") == _SOL_MINT,
            "unwrapSol": quote.get("outputMint") == _SOL_MINT,
        }
        if quote.get("inputMint") != _SOL_MINT:
            # The wallet's REAL token account, looked up — never derived. Classic-SPL derivation
            # gave the wrong address for Token-2022 mints (MOX, 2026-08-14: REQ_OWNER_ACCOUNT_ERROR
            # killed every exit-ladder rung and the position read "stuck" while Raydium quoted the
            # whole bag at $52). The lookup is one RPC call and is correct for both token programs;
            # when the wallet holds no account for the mint (a BUY of a coin never held), the
            # derived classic ATA is the account Raydium's build will create — Token-2022 buys via
            # Raydium remain unsupported and fail closed at the build, as before.
            # A FAILED LOOKUP IS NOT "NO ACCOUNT" (2026-09-08). Deriving on an unreadable lookup
            # reintroduces the MOX defect described above for every Token-2022 exit. Refusing here
            # fails this rung and it re-arms; deriving submits a doomed transaction and strands the
            # bag. `None` still derives, because there the RPC answered and the account genuinely
            # does not exist yet.
            _acct = _owned_token_account(user_pubkey, quote["inputMint"], http, **read_kwargs)
            if _acct is LOOKUP_FAILED:
                logger.warning("raydium build refused: token account lookup unavailable mint=%s "
                               "— refusing rather than deriving (MOX)", quote.get("inputMint"))
                _note_build_refusal("Raydium token account lookup unavailable")
                return None
            payload["inputAccount"] = _acct or _ata(user_pubkey, quote["inputMint"])
        # TOKEN-2022 OUTPUT: the CHAIN decides, not a rule (2026-09-11). The 2026-09-09 version of
        # this block refused every Token-2022 output on the belief that Raydium's build derives a
        # classic ATA (STOCKCAT/SWEENIES, 2026-09-08). Replayed offline on 2026-09-11, the SAME two
        # mints and the six coins refused since build with the Token-2022 program and the ATA
        # program invoked, and simulate clean. The rule cost six fills in two days, every one a
        # coin Jupiter did not know yet -- the fastest signals J7 delivers. So: build, then
        # simulate on the read endpoint; refuse only on the chain's own error, and refuse when the
        # simulation cannot be read (fail closed, MOX shape). Classic mints keep the old path.
        out_mint = quote.get("outputMint")
        _simulate = False
        if out_mint and out_mint != _SOL_MINT:
            _is_2022 = _mint_is_token_2022(out_mint, http, **read_kwargs)
            _simulate = _is_2022 is not False
        status, j, _d = _http_status(http, "POST", f"{RAY_BASE}/transaction/swap-base-in",
                                     body=_json.dumps(payload))
    except Exception:
        return None
    data = (j or {}).get("data") if isinstance(j, dict) else None
    if status != 200 or not (j or {}).get("success") or not isinstance(data, list):
        return None
    if len(data) != 1 or not (data[0] or {}).get("transaction"):
        return None
    tx = data[0]["transaction"]
    if _simulate:
        verdict = _simulate_verdict(tx, http, **read_kwargs)
        if verdict is not None:
            logger.warning("raydium build refused: output mint=%s (token-2022 or unreadable) "
                           "simulation %s", out_mint, verdict)
            _note_build_refusal(_simulation_refusal(verdict))
            return None
    # Raydium's build does not return lastValidBlockHeight; the binding column is nullable and the
    # provably-dead release simply stays on its slower not-found path for these.
    return {"swapTransaction": tx, "lastValidBlockHeight": None} if return_metadata else tx


# RAYDIUM ROUTER 0x26 IS ITS MINIMUM-OUT CHECK, NOT A TOKEN-2022 FAULT (2026-09-27, operator: "just
# relabel it, keep the slippage as is"). Every simulation refusal since 2026-09-11 (13 of them) was
# this code and was reported as "refused the Token-2022 buy", which read as a Token-2022 defect.
# It is the price moving past the quote's minimum out between the quote and the simulation: a real
# Raydium build of ATqKVV (Token-2022, 1% fee) simulates clean, and the same build with only its
# otherAmountThreshold raised fails with exactly this code. 23 Token-2022 coins filled through
# Raydium in the same period. Matched on the chain's own log line, never on the bare number, which
# other programs reuse. The "route build refused" prefix, and so the re-quote lane, is unchanged.
_RAYDIUM_ROUTER = "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS"


def _simulation_refusal(verdict):
    if f"Program {_RAYDIUM_ROUTER} failed: custom program error: 0x26" in str(verdict):
        return "Raydium simulation: price moved past min-out (router 0x26)"
    return "Raydium simulation refused the Token-2022 buy"


# WHY THE BUILD REFUSED (2026-09-11). A build that returns nothing reached the operator as "sign
# failed", yet both such aborts that day were Raydium refusing a Token-2022 coin before any key
# was used. The refusing branch notes its cause here; the entry seam hands it to the executor.
# Thread-local because entries run on worker threads; the note is read and cleared by the same
# attempt that set it.
_BUILD_REFUSAL = threading.local()


def _note_build_refusal(reason):
    _BUILD_REFUSAL.reason = reason


def take_build_refusal():
    reason = getattr(_BUILD_REFUSAL, "reason", None)
    _BUILD_REFUSAL.reason = None
    return reason


# --- Jupiter quotes -----------------------------------------------------------------------------

# VENUE EXCLUSION AFTER AN ON-CHAIN REFUSAL (2026-09-13, operator: "make sure it won't happen
# again"). BURNED (ref 8a545100): Jupiter routed the buy through Quantum, Quantum refused it on
# chain (custom 0xe), the transaction reverted and the signal was lost, although no USDC moved. A
# retry of the SAME route refuses the same way, which is why the re-quote lane leaves non-slippage
# reverts one-shot. Excluding the venue that failed makes the retry a DIFFERENT route by
# construction. Per mint, for VENUE_EXCLUSION_TTL_S; every consumer of this seam (entry quote,
# sell-route proof, exit) sees it, so the coin is not routed back into the venue that refused it.
VENUE_EXCLUSION_TTL_S = 600.0
_VENUE_EXCLUSIONS = {}              # mint -> (expires_monotonic, frozenset(labels))
_VENUE_EXCLUSIONS_LOCK = threading.Lock()

# A VENUE-WIDE REFUSAL EXCLUDES THE VENUE FOR EVERY COIN (2026-09-23, operator: "fix whatever
# caused this"). Ethena 91679b13 and hyENA 69601c34, five minutes apart: Jupiter routed the
# USDC leg of both buys through Denali (DNL1tgEj…), Denali reverted both on chain with
# VolumeCapExceeded, both fees were paid for nothing, and both coins filled only on the retry
# lane (Ethena 28 s later and 14.5% above the source's price instead of 3.9%). The per-mint
# exclusion above fired for Ethena and could not help hyENA, because a VOLUME CAP is a fact
# about the venue's pool, not about the coin we were buying through it. An Anchor error in this
# set therefore excludes the venue under the wildcard key too, for the same TTL, and
# buy quotes apply that wildcard to every mint. Exits keep only their mint-specific refusal.
VENUE_WIDE_ERRORS = frozenset({"VolumeCapExceeded"})
_ALL_MINTS = "*"


def static_excluded_venues():
    """Venues the ENTRY quote never routes through, from MEMECOIN_SOLANA_EXCLUDE_DEXES.

    Entry only: an exit takes whatever route still sells the bag. Jupiter ignores a label it does
    not know (verified 2026-09-23: `excludeDexes=NotARealDex` answers 200 with a normal route),
    so a typo here excludes nothing rather than refusing every quote. Unset means the code default;
    an explicit empty value means no static exclusion.
    """
    from sniper.settings import memecoin as cfg
    raw = getattr(cfg, "MEMECOIN_SOLANA_EXCLUDE_DEXES", "") or ""
    return frozenset(part.strip() for part in str(raw).split(",") if part.strip())
_JUP_LABELS = {"map": None, "at": 0.0}
# Programs that are plumbing, never a venue: excluding them would exclude every route.
_NEVER_EXCLUDE = frozenset({
    "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
})


def _jupiter_venue_label(program_id, *, http=None):
    """Jupiter's dex label for a program id (the name excludeDexes takes), or None."""
    now = _monotonic()
    if _JUP_LABELS["map"] is None or now - _JUP_LABELS["at"] > 3600.0:
        try:
            status, j = _jup_call("GET", "/program-id-to-label", http=http)
            if status == 200 and isinstance(j, dict) and j:
                _JUP_LABELS.update(map=j, at=now)
        except Exception:
            logger.exception("jupiter venue labels unreadable")
    return (_JUP_LABELS["map"] or {}).get(str(program_id or ""))


def exclude_failed_venue(mint, program_id, *, http=None, program_error=None):
    """Exclude the venue whose program refused a transaction for `mint`. Returns its label, or
    None when it is plumbing or has no Jupiter label (then nothing changes and nothing retries).

    `program_error` is the Anchor error name the chain logged; one in VENUE_WIDE_ERRORS excludes
    the venue for every mint as well (see the note above). Absent stays per-mint."""
    if not mint or not program_id or program_id in _NEVER_EXCLUDE:
        return None
    label = _jupiter_venue_label(program_id, http=http)
    if not label:
        return None
    venue_wide = str(program_error or "") in VENUE_WIDE_ERRORS
    with _VENUE_EXCLUSIONS_LOCK:
        for key in ((mint, _ALL_MINTS) if venue_wide else (mint,)):
            _exp, labels = _VENUE_EXCLUSIONS.get(key, (0.0, frozenset()))
            _VENUE_EXCLUSIONS[key] = (_monotonic() + VENUE_EXCLUSION_TTL_S, labels | {label})
    logger.warning("venue excluded for %s: %s (program %s refused on chain%s)", mint, label,
                   program_id, ", venue-wide: %s" % program_error if venue_wide else "")
    return label


def _live_exclusions(key):
    entry = _VENUE_EXCLUSIONS.get(key)
    if entry and entry[0] <= _monotonic():
        _VENUE_EXCLUSIONS.pop(key, None)
        return frozenset()
    return entry[1] if entry else frozenset()


def excluded_venues(mint):
    """The live exclusions for `mint` plus the venue-wide ones, expired entries dropped."""
    with _VENUE_EXCLUSIONS_LOCK:
        return _live_exclusions(mint) | _live_exclusions(_ALL_MINTS)


def _quote_exclusions(input_mint, output_mint, exclude_dexes):
    # One snapshot for Jupiter and its fallback. A buy's venue-wide failure must not suppress
    # an unrelated held coin's exit; a refusal for this exact mint still applies in both directions.
    with _VENUE_EXCLUSIONS_LOCK:
        learned = _live_exclusions(input_mint) | _live_exclusions(output_mint)
        if input_mint == USDC_MINT:
            learned |= _live_exclusions(_ALL_MINTS)
    return learned | frozenset(exclude_dexes or ())


def _jup_quote_status(input_mint, output_mint, amount_atomic, slippage_bps, http,
                      max_accounts=None, exclude_dexes=(), resolve_exclusions=True):
    """(quote_or_None, status). The status lets the venue fallback distinguish 'Jupiter does not
    KNOW this coin' (fall back) from 'Jupiter is throttled/down' (do NOT fall back — the typed
    RATE_LIMIT/transport verdict must survive so the quote budget still backs off)."""
    path = (f"/quote?inputMint={input_mint}&outputMint={output_mint}"
            f"&amount={int(amount_atomic)}&slippageBps={int(slippage_bps)}"
            "&swapMode=ExactIn")
    # Buy discovery only: exits keep the stable-intermediate default for build reliability.
    if input_mint == USDC_MINT and output_mint != USDC_MINT:
        path += "&restrictIntermediateTokens=false"
    # maxAccounts caps the route's account-key count, which is what drives the serialized packet
    # size. Only the oversize retry sets it; the normal path sends no key and keeps today's routes.
    if max_accounts:
        path += f"&maxAccounts={int(max_accounts)}"
    excluded = (_quote_exclusions(input_mint, output_mint, exclude_dexes)
                if resolve_exclusions else exclude_dexes)
    if excluded:
        from urllib.parse import quote as _urlquote
        path += "&excludeDexes=" + _urlquote(",".join(sorted(excluded)), safe="")
    status, j = _jup_call("GET", path, http=http)
    if status != 200 or not isinstance(j, dict) or j.get("error") or "outAmount" not in j:
        return None, status
    return j, status


def _jup_quote(input_mint, output_mint, amount_atomic, slippage_bps, http, max_accounts=None):
    try:
        return _jup_quote_status(input_mint, output_mint, amount_atomic, slippage_bps, http,
                                 max_accounts)[0]
    except Exception:
        return None                      # this seam's contract; the observed path types it instead


# FUNDED JUPITER LOAD, counted at the single seam every funded quote passes through.
# WHY (2026-08-26): Jupiter is keyless and rate-limits by IP, and the funded quote path shares that
# IP with anything else on this host. A proposed capture worker would be ~2.5x funded's average and
# ~7x its burst, and the 08-14 lite-api 429 storm cost live entries — so no second consumer is
# enabled until funded's own load is a measured number rather than an estimate. Counting is
# fail-soft and never gates: this must not be able to refuse a funded quote.
JUPITER_LOAD = {"quotes": 0, "rate_limited": 0, "errors": 0, "auth_failed": 0, "by_host": {},
                # BY ROLE, so the next incident names its consumer in one read. The 08-29 incident
                # took a cross-reference between the runner log, the arms config and the study's
                # position count to attribute 40% rate-limiting to the paper arms.
                "by_role": {}}

# FUNDED FIRST, AND THE ASYMMETRY IS THE DESIGN. A funded exact preview must never queue behind a
# paper quote, so the funded path only INCREMENTS a counter — it never waits, never takes a lock
# that a paper call could hold, and never sleeps. Paper calls yield to it.
_INFLIGHT = threading.Condition()
_FUNDED_INFLIGHT = {"n": 0}
# How long a paper quote will wait for the funded book to go quiet before proceeding anyway.
# Bounded rather than indefinite: a paper arm starved forever silently stops producing the
# evidence the study needs, and a silent arm reads as a result.
PAPER_YIELD_MAX_S = 2.0


@contextlib.contextmanager
def _funded_inflight():
    with _INFLIGHT:
        _FUNDED_INFLIGHT["n"] += 1
    try:
        yield
    finally:
        with _INFLIGHT:
            _FUNDED_INFLIGHT["n"] -= 1
            _INFLIGHT.notify_all()


def _yield_to_funded(max_wait_s=None):
    """Block a PAPER call while any funded quote is in flight. Returns seconds waited."""
    deadline = time.monotonic() + (PAPER_YIELD_MAX_S if max_wait_s is None else max_wait_s)
    waited = 0.0
    with _INFLIGHT:
        while _FUNDED_INFLIGHT["n"] > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _INFLIGHT.wait(timeout=min(remaining, 0.05))
            waited = time.monotonic() - (deadline - (PAPER_YIELD_MAX_S
                                                     if max_wait_s is None else max_wait_s))
    return waited


def _host_label(base):
    """`lite-api` / `api`, never the full URL — a base carries no secret today and this keeps it
    that way if one is ever added to the path."""
    try:
        return str(base).split("//", 1)[1].split(".", 1)[0]
    except Exception:
        return "unknown"


# The hosts that honour a key. lite-api is the keyless tier; a key sent there is ignored.
JUP_KEYED_BASES = (JUP_FALLBACK_BASE,)
_AUTH_STATUSES = (401, 403)


def _sink_note(field, *, role=None, host=None):
    """Mirror one counter increment into the durable per-minute sink (spec 2026-08-30).

    Fail-soft and lazily imported, for the same reason every counter here is: this must never be
    able to refuse a funded quote. Costs one dict increment plus a ~7us clock read; no I/O, no
    lock a network call can hold, no DB handle on the quote path.
    """
    try:
        from sniper.services import memecoin_jupiter_load_sink as _sink
        from sniper.util.clock import real_utc_now_iso
        _sink.note(field, role=role or active_role(), host=host,
                   now=real_utc_now_iso())
    except Exception:
        pass


def _note_auth_alert(base, *, ok, status=401):
    """Hand one keyed-host outcome to the alert aggregator. Never raises into the quote.

    The keyless fallback's limit is named in the page because that is what the operator's decision
    turns on: `lite-api` is 0.5 RPS / 30 per minute against the key's 1 RPS / 60, so a rejected key
    halves the funded lane's exit-quote budget rather than merely annoying it.
    """
    try:
        from sniper.services.memecoin_alerts import note_jupiter_auth
        note_jupiter_auth(_host_label(base), ok=ok, status=status, role=active_role(),
                          fallback_host=_host_label(JUP_BASE),
                          fallback_limit="keyless 0.5 RPS / 30 per minute")
    except Exception:
        logger.exception("jupiter auth alert failed (quotes unaffected)")


def _note_host_auth_failure(base, status):
    """Keyed access failure is distinct from rate limiting. Never logs the key.

    A 401 rejects authentication; a 403 may deny a valid key's permissions or source IP.
    """
    try:
        JUPITER_LOAD["auth_failed"] += 1
        JUPITER_LOAD["by_host"]["%s:auth" % _host_label(base)] = (
            JUPITER_LOAD["by_host"].get("%s:auth" % _host_label(base), 0) + 1)
    except Exception:
        pass
    _sink_note("auth_failed", host=_host_label(base))
    logger.warning("jupiter: host %s HTTP %s — attempting keyless fallback; %s",
                   _host_label(base), status,
                   "access denied, key validity unknown" if status == 403
                   else "authentication rejected, verify configured key")
    # AND PAGE, because the log line did not (2026-09-24). 131 of these were written while the
    # funded lane quoted keyless at 30/min instead of 60 and lost 119 exact exit quotes over
    # 2 h 28 min, and nobody was told. The alert aggregates (1/5/20 plus recovery) and enqueues;
    # it never blocks, never retries and cannot change this failover.
    _note_auth_alert(base, ok=False, status=status)


# When a FUNDED-role call last saw a 429 on any host (monotonic; None = never). The exotic warmer
# reads it to stay off the funded key while that key is being refused (Alpha (b), 2026-10-15).
_funded_last_429_at = None
FUNDED_JUPITER_429_QUIET_S = 10.0
_CASH_JUPITER_PREPARATION = contextvars.ContextVar("cash_jupiter_preparation", default=False)


class CashJupiterQuiet(RuntimeError):
    """Local background pause; never a provider response or a funded quote refusal."""


def _check_cash_jupiter_quiet():
    if _CASH_JUPITER_PREPARATION.get():
        age = funded_429_age_s()
        if age is not None and age < FUNDED_JUPITER_429_QUIET_S:
            raise CashJupiterQuiet("funded_429_quiet")


@contextlib.contextmanager
def cash_jupiter_preparation():
    """Only native cash preparation yields; keep its funded key and priority unchanged."""
    token = _CASH_JUPITER_PREPARATION.set(True)
    try:
        _check_cash_jupiter_quiet()
        yield
    finally:
        _CASH_JUPITER_PREPARATION.reset(token)


def funded_429_age_s():
    """Seconds since a funded-role call last saw a 429, or None if none has."""
    at = _funded_last_429_at
    return None if at is None else _monotonic() - at


def _note_host_429(base):
    """Which HOST 429'd. Added 2026-08-27 with the funded key.

    The 26.4% rate-limit figure measured that morning could not be attributed to a host, because
    nothing recorded one — so "did the key help" was unanswerable in the direction that matters.
    Fail-soft: a counter must never be able to refuse a funded quote."""
    try:
        JUPITER_LOAD["by_host"][_host_label(base)] = (
            JUPITER_LOAD["by_host"].get(_host_label(base), 0) + 1)
    except Exception:
        pass
    global _funded_last_429_at
    if active_role() == ROLE_FUNDED:
        _funded_last_429_at = _monotonic()
    # PRESSURE, not a lost decision: this fires per HOST ATTEMPT inside the failover loop, so a
    # quote that 429s here and succeeds on the next host records +1 pressure and +0 rate_limited.
    _sink_note("host_429", host=_host_label(base))


def note_role_quote(role=None):
    """Count ONE quote against a role. Fail-soft: a counter must never refuse a quote.

    Exported because the v6 paper lane has its OWN host ladder and request and never reaches
    `_jup_call` — so without a shared recorder the resumption gate waits on a counter that path
    structurally cannot increment, which is exactly what it was doing.
    """
    try:
        JUPITER_LOAD["quotes"] += 1
        JUPITER_LOAD["by_role"].setdefault(
            role or active_role(), {"quotes": 0, "rate_limited": 0})["quotes"] += 1
    except Exception:
        pass
    _sink_note("quotes", role=role)


def note_role_rate_limited(role=None):
    """Count ONE 429 against a role."""
    try:
        JUPITER_LOAD["by_role"].setdefault(
            role or active_role(), {"quotes": 0, "rate_limited": 0})["rate_limited"] += 1
    except Exception:
        pass


def _totals_agree():
    """Whether `by_role` partitions the totals. For the guard test, and for a reader who wants to
    know the heartbeat's parts sum to its whole before dividing one by the other."""
    br = JUPITER_LOAD.get("by_role") or {}
    return (sum(v.get("quotes", 0) for v in br.values()) == JUPITER_LOAD.get("quotes", 0)
            and sum(v.get("rate_limited", 0) for v in br.values())
            == JUPITER_LOAD.get("rate_limited", 0))


def jupiter_load_snapshot():
    """Heartbeat projection. The assembly is a WHITELIST — name this field where the heartbeat is
    built or it silently never appears, exactly as the RPC pool's `degraded` did on 2026-08-25.

    `by_host` is copied, not aliased: `dict()` is shallow, so returning the live nested dict would
    hand a reader a structure that mutates under it mid-serialisation."""
    snap = dict(JUPITER_LOAD)
    try:
        snap["by_host"] = dict(JUPITER_LOAD.get("by_host") or {})
        # Deep-copied per role for the same reason `by_host` is copied: a shallow dict would hand
        # the reader the live nested counters and they would move underneath the read.
        snap["by_role"] = {k: dict(v) for k, v in (JUPITER_LOAD.get("by_role") or {}).items()}
    except Exception:
        snap["by_host"] = {}
        snap["by_role"] = {}
    # Whether this process HAS a key, never which one and never its value. Rides inside the
    # existing whitelisted `jupiter_load` entry so the heartbeat assembly needs no new field.
    # Without it, a before/after comparison cannot tell a keyed run from a keyless one.
    snap["keyed"] = jup_key_configured()
    snap["key_role"] = _KEY_VAR
    return snap


# C2, RAYDIUM BESIDE JUPITER ON ENTRY QUOTES (spec 2026-10-01-memecoin-onchain-lane-latency-design.md,
# MEMECOIN_ENTRY_RAYDIUM_PARALLEL, default off). Two workers, process-wide and created once: when
# both are busy (a Raydium hang under a burst) the quote takes today's serial path, so orphan
# threads cannot pile up in the funded process (Gamma L2).
_RAY_PARALLEL_SLOTS = threading.BoundedSemaphore(2)
_RAY_PARALLEL_POOL = []
_RAY_PARALLEL_LOCK = threading.Lock()


def _ray_parallel_start(input_mint, output_mint, amount_atomic, slippage_bps, http, started):
    """A future for the Raydium quote begun now, or None: knob off or both workers busy. The
    result is (quote, done_ms from `started`)."""
    from sniper.settings import memecoin as cfg
    if not getattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", False):
        return None
    if not _RAY_PARALLEL_SLOTS.acquire(blocking=False):
        return None
    # THE CALLER'S QUOTE RECORDER (Gamma HIGH on 15ec9ade1). The funded entry quotes through
    # typed_jupiter_entry_quote, whose `http` (_recorded_plain/_recorded_keyed) reads the CALLER
    # thread's _ENTRY_QUOTE_RECORDING. On this pool thread it was unset: the wrapper raised, and
    # _ray_quote's except turned it into "no Raydium quote", killing the 429 rescue and every thin
    # "won". The worker runs inside the caller's recorder (same base, same calls list, so the miss
    # classifier still sees Raydium) and restores its own afterwards.
    recording = (getattr(_ENTRY_QUOTE_RECORDING, "base", None),
                 getattr(_ENTRY_QUOTE_RECORDING, "calls", None))

    def run():
        previous = (getattr(_ENTRY_QUOTE_RECORDING, "base", None),
                    getattr(_ENTRY_QUOTE_RECORDING, "calls", None))
        _ENTRY_QUOTE_RECORDING.base, _ENTRY_QUOTE_RECORDING.calls = recording
        try:
            quote = _ray_quote(input_mint, output_mint, amount_atomic, slippage_bps, http)
            return quote, (time.monotonic() - started) * 1000.0
        finally:
            _ENTRY_QUOTE_RECORDING.base, _ENTRY_QUOTE_RECORDING.calls = previous
            _RAY_PARALLEL_SLOTS.release()
    try:
        with _RAY_PARALLEL_LOCK:
            if not _RAY_PARALLEL_POOL:
                from concurrent.futures import ThreadPoolExecutor
                _RAY_PARALLEL_POOL.append(ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="entry-raydium-parallel"))
            return _RAY_PARALLEL_POOL[0].submit(run)
    except Exception:
        _RAY_PARALLEL_SLOTS.release()
        logger.warning("parallel Raydium quote did not start; serial path", exc_info=True)
        return None


def _quote_with_fallback(input_mint, output_mint, amount_atomic, slippage_bps, http,
                         max_accounts=None, exclude_dexes=(), tag_evidence=False, evidence=False,
                         entry_proof=False):
    """Jupiter first; the Raydium venue quote ONLY when Jupiter answered and does not KNOW the
    coin (a 2xx/4xx request-level refusal — a too-new unindexed token). A 429 or a transport
    error never falls back: Raydium's response would overwrite the observed failure and the typed
    RATE_LIMIT/transport verdict — the thing the quote budget's backoff and the single 200ms
    retry are built on — would silently disappear. Every quote consumer (entry, planned-quantity
    sell-route proof, round-trip cap, exit preview, rails exact quote) routes through here, so a
    coin only Raydium knows is enterable AND exitable by the same rule.

    `tag_evidence` is the ENTRY quote (it may start the parallel Raydium call); `evidence`
    tags quote diagnostics only. `entry_proof` permits a thin, valid Jupiter sell proof to
    proceed without waiting for its optional second opinion in entry nonblocking mode.
    Held-position exits never set `entry_proof`."""
    try:
        # ONE PLACE COUNTS, so the parts sum to the whole. `note_role_quote` bumps both the total
        # and the role — an earlier version incremented the total here and the role inside it, and
        # the paper lane (which bypasses this seam entirely) bumped only the role. The heartbeat
        # then read quotes=1 against by_role summing to 7, so "paper share of total" came out at
        # 600%. A partition that does not sum to its total is worse than no partition.
        note_role_quote()
    except Exception:
        pass                               # a counter must never be able to refuse a funded quote
    started = time.monotonic()
    timing = {}
    early = None
    from sniper.settings import memecoin as _mc
    # Knob off: no timing fields either, so the entry trace stays byte-identical to today's.
    parallel = bool(tag_evidence and getattr(_mc, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", False))
    timed = parallel or evidence
    tagging = tag_evidence or evidence
    try:
        excluded = _quote_exclusions(input_mint, output_mint, exclude_dexes)
        if parallel:
            # C2: ENTRY quotes only (exit previews, rails and the round-trip cap never tag). Same
            # eligibility as the second opinion below; its answer is read only where that rule
            # would have asked Raydium, and dropped unread otherwise.
            from sniper.settings.memecoin import MEMECOIN_SOLANA_VENUE_FALLBACK
            if (MEMECOIN_SOLANA_VENUE_FALLBACK and not max_accounts
                    and not any("raydium" in str(label).casefold() for label in excluded)):
                early = _ray_parallel_start(input_mint, output_mint, amount_atomic, slippage_bps,
                                            http, started)
        q, status = _jup_quote_status(input_mint, output_mint, amount_atomic, slippage_bps, http,
                                      max_accounts, excluded, False)
        # THE ONE TIE RULE (non-blocking, 2026-10-02): Raydium counts only if its future is already
        # done at the instant Jupiter's answer is in hand. Read once, here; no grace window.
        ray_ready = early.done() if early is not None else False
        if timed:
            timing = {"jupiter_done_ms": (time.monotonic() - started) * 1000.0,
                      "raydium_parallel": early is not None}
        try:
            if status == 429:
                JUPITER_LOAD["rate_limited"] += 1
                note_role_rate_limited()          # partition only; the total is bumped above
                # A LOST DECISION: every host refused. Deliberately NOT recorded inside
                # note_role_rate_limited, which the paper lane also calls per host before
                # continuing its own ladder -- recording there would inflate the persisted rate
                # by the failover fan-out.
                _sink_note("rate_limited")
        except Exception:
            pass
    except Exception:
        return None                      # transport: same contract as before, and no fallback
    def second_opinion():
        # The same Raydium call, begun beside Jupiter's when C2 started it, else begun now.
        waited = time.monotonic()
        if early is not None:
            try:
                quote, done_ms = early.result()
            except Exception:
                quote, done_ms = None, (time.monotonic() - started) * 1000.0
        else:
            quote = _ray_quote(input_mint, output_mint, amount_atomic, slippage_bps, http)
            done_ms = (time.monotonic() - started) * 1000.0
        if timed:
            timing["raydium_done_ms"] = done_ms
            # What this quote BLOCKED on Raydium: the serial call, or the wait for the parallel one.
            timing["raydium_wait_ms"] = (time.monotonic() - waited) * 1000.0
        return quote

    # The direct Raydium backend cannot enforce an account cap or select around one of its own
    # families (Raydium, CLMM, CP/CPMM, Launchlab). Unrelated Jupiter labels do not restrict it.
    if q is not None:
        # A THIN-LOOKING Jupiter route gets a second opinion (PEECOIN, 2026-08-14): Jupiter had
        # partially indexed an 8-minute-old coin — it saw a $287 Meteora dust pool but not the
        # $13.7K Raydium pool created the same second — and quoted the whole trade through dust:
        # a CORRECT 83% round-trip price for the WRONG route, refusing an entry the real pool
        # filled at ~1%. When Jupiter's own impact says the route is thin, ask Raydium directly
        # and take whichever returns more. Low-impact quotes skip the check, so the common case
        # costs zero extra requests; a Raydium error or a worse answer keeps Jupiter's quote.
        try:
            impact = abs(float(q.get("priceImpactPct") or 0.0))
        except (TypeError, ValueError):
            impact = 0.0
        from sniper.settings.memecoin import MEMECOIN_SOLANA_VENUE_FALLBACK
        eligible = (MEMECOIN_SOLANA_VENUE_FALLBACK and not max_accounts
                    and not any("raydium" in str(label).casefold() for label in excluded))
        if impact > 0.05 and eligible:
            if entry_proof and _mc.MEMECOIN_ENTRY_RAYDIUM_PARALLEL and _raydium_nonblocking():
                # TANG's entry sell proof waited 259 ms for a Raydium no-quote (2026-10-05).
                # Start only the comparison we already needed; exits and missing-route rescue
                # keep their existing path. A late result is recorded, never used by this entry.
                early = _ray_parallel_start(input_mint, output_mint, amount_atomic, slippage_bps,
                                            http, started)
                timing["raydium_parallel"] = early is not None
                if early is None or not early.done():
                    return _go_on_jupiter(q, early, impact, timing, tagging, input_mint, output_mint,
                                          amount_atomic)
            if parallel and _raydium_nonblocking() and not ray_ready:
                # The entry goes on Jupiter's quote; a late Raydium answer is a shadow only.
                return _go_on_jupiter(q, early, impact, timing, tagging, input_mint, output_mint,
                                      amount_atomic)
            second = second_opinion()
            try:
                if second is not None and int(second["outAmount"]) > int(q["outAmount"]):
                    if _raydium_entry_win_distrusted(input_mint, q, second, impact):
                        return _with_fallback_evidence(q, q, "thin", "distrusted", tagging,
                                                       raydium_quote=second, timing=timing)
                    return _with_fallback_evidence(second, q, "thin", "won", tagging,
                                                   raydium_quote=second, timing=timing)
            except (TypeError, ValueError, KeyError):
                pass
            return _with_fallback_evidence(q, q, "thin", "lost" if second is not None
                                           else "no_quote", tagging, raydium_quote=second,
                                           timing=timing)
        return _with_fallback_evidence(q, q, "thin_blocked" if impact > 0.05 else "none",
                                       "not_asked", tagging)
    from sniper.settings.memecoin import MEMECOIN_SOLANA_VENUE_FALLBACK
    if (not MEMECOIN_SOLANA_VENUE_FALLBACK or max_accounts
            or any("raydium" in str(label).casefold() for label in excluded)):
        return None
    if status is None:
        return None
    if isinstance(status, int) and status >= 500:
        return None
    # 429 DOES fall back now (reversed 2026-08-14 20:11): a Jupiter throttle burst refused three
    # routable entries in two minutes (TRENCHY, PISS, Pomni — all quotable on Raydium at that
    # moment). The bounded cost, stated: when Jupiter 429s AND Raydium also fails, the typed
    # verdict reflects Raydium's failure rather than RATE_LIMIT, so the quote budget's backoff can
    # soften for that one miss — while the host cooldown still marks the 429'd Jupiter host either
    # way. An entry that fills through the second venue beats a perfectly-typed refusal.
    rescued = second_opinion() if tagging else _ray_quote(
        input_mint, output_mint, amount_atomic, slippage_bps, http)
    return (None if rescued is None
            else _with_fallback_evidence(rescued, None, "miss", "won", tagging,
                                         raydium_quote=rescued, timing=timing))


def _raydium_nonblocking():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False))


def _go_on_jupiter(q, early, impact, timing, tagging, input_mint, output_mint, amount_atomic):
    """Jupiter's thin quote decides, without waiting for Raydium (MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING).

    raydium = `late` when the parallel call is still running: a callback hands its answer, when it
    comes, to the late-shadow writer (its own thread) with a snapshot of Jupiter's answer taken
    HERE, so nothing reads or writes the entry's quote after the entry moved on. `not_started` when
    no parallel call exists (both pool workers busy): today's serial call would block, so it is not
    made. The tag's late_shadow_id joins the trace to the shadow row."""
    shadow_id = None
    if early is not None:
        import uuid
        shadow_id = uuid.uuid4().hex
        snap = {"shadow_id": shadow_id, "input_mint": input_mint, "output_mint": output_mint,
                "amount_atomic": str(amount_atomic), "jupiter_out_atomic": _out_atomic(q),
                "jupiter_impact_pct": impact, "jupiter_route_labels": _route_labels(q),
                "jupiter_done_ms": (timing or {}).get("jupiter_done_ms")}
        try:
            early.add_done_callback(lambda fut: LATE_RAYDIUM_SHADOW.offer(snap, fut))
        except Exception:
            logger.warning("late Raydium shadow did not attach", exc_info=True)
    if timing is not None:
        timing["raydium_wait_ms"] = 0.0
    out = _with_fallback_evidence(q, q, "thin", "late" if early is not None else "not_started",
                                  tagging, timing=timing)
    if tagging and shadow_id and isinstance(out, dict) and isinstance(out.get("_sniper_fallback"), dict):
        out["_sniper_fallback"]["late_shadow_id"] = shadow_id
    return out


class LateRaydiumShadow:
    """The late Raydium answers of non-blocking entry quotes, for later grading. MEASUREMENT ONLY.

    `offer` runs as the future's done-callback (the Raydium worker, or the caller if the future
    finished in between): it reads only that future and the snapshot, and only enqueues. One daemon
    thread owns the file and is the only writer. A full queue drops and counts; nothing here raises.
    Each row says what today's blocking rule would have done with the answer: won, distrusted,
    lost, no_quote or error."""

    def __init__(self, path=None, maxsize=1000):
        import queue
        self.path, self._q = path, queue.Queue(maxsize=maxsize)
        self.stats = {"offered": 0, "written": 0, "dropped": 0, "errors": 0}
        self._thread, self._lock = None, threading.Lock()

    def offer(self, snap, fut):
        import queue
        try:
            quote, done_ms = fut.result(timeout=0)
            row = dict(snap, raydium_out_atomic=_out_atomic(quote), raydium_done_ms=done_ms)
        except Exception as exc:
            row = dict(snap, raydium_out_atomic=None, raydium_done_ms=None,
                       error=type(exc).__name__)
        self.stats["offered"] += 1
        self._start()
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self.stats["dropped"] += 1

    def _start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="raydium-late-shadow",
                                                daemon=True)
                self._thread.start()

    @staticmethod
    def would(row):
        """What today's blocking comparison would have done with this answer."""
        if row.get("error"):
            return "error"
        ray, jup = row.get("raydium_out_atomic"), row.get("jupiter_out_atomic")
        if ray is None:
            return "no_quote"
        if jup is None or int(ray) <= int(jup):
            return "lost"
        if _raydium_entry_win_distrusted(row["input_mint"], {"outAmount": jup}, {"outAmount": ray},
                                         float(row.get("jupiter_impact_pct") or 0.0)):
            return "distrusted"
        return "won"

    def _run(self):
        import json as _json
        from sniper.util.clock import real_utc_now_iso
        while True:
            row = self._q.get()
            try:
                row["would"] = self.would(row)
                done, jup = row.get("raydium_done_ms"), row.get("jupiter_done_ms")
                row["late_ms"] = (done - jup) if done is not None and jup is not None else None
                row["recorded_at"] = real_utc_now_iso()
                path = self.path
                if path is None:
                    from sniper.util.state_paths import state_path
                    path = state_path("data", "raydium_late_shadow.jsonl")
                import os as _os
                _os.makedirs(_os.path.dirname(str(path)) or ".", exist_ok=True)
                with open(path, "a") as f:
                    f.write(_json.dumps(row, sort_keys=True, default=str) + "\n")
                self.stats["written"] += 1
            except Exception:
                self.stats["errors"] += 1
                logger.warning("late Raydium shadow write failed", exc_info=True)
            finally:
                self._q.task_done()


LATE_RAYDIUM_SHADOW = LateRaydiumShadow()


def _raydium_entry_win_distrusted(input_mint, jupiter, raydium, jupiter_impact):
    """True when a Raydium ENTRY quote beats Jupiter's by more than
    MEMECOIN_SOLANA_VENUE_FALLBACK_MAX_RATIO and Jupiter's own impact does not call its route dust.

    Entries only (USDC or SOL in): an exit quote keeps the better venue, because a held coin's exit
    value read from a dust route would look like a crash and fire a stop. The sizes are atomic units
    of the same output mint for the same input, compared exactly (Fraction), never as floats. A
    Jupiter size that is zero or unparseable leaves today's contest untouched."""
    from fractions import Fraction
    from sniper.settings import memecoin as cfg
    cap = float(getattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK_MAX_RATIO", 0.0) or 0.0)
    if cap <= 0 or input_mint not in (USDC_MINT, _SOL_MINT):
        return False
    if jupiter_impact > float(getattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK_DUST_IMPACT", 0.50)):
        return False
    jup_out = int(jupiter["outAmount"])
    if jup_out <= 0:
        return False
    return Fraction(int(raydium["outAmount"])) > jup_out * Fraction(str(cap))


def _route_labels(quote):
    """The swapInfo labels of a quote's routePlan, in hop order, or None when it has none."""
    try:
        labels = [str((hop.get("swapInfo") or {}).get("label")) for hop in quote.get("routePlan") or ()]
    except (AttributeError, TypeError):
        return None
    return labels or None


def _out_atomic(quote):
    """A quote's outAmount as an exact decimal string, or None when absent or unparseable. A
    string, not a float: atomic amounts pass the 2**53 mantissa and must compare exactly."""
    try:
        raw = quote.get("outAmount") if quote is not None else None
        return str(int(raw)) if raw is not None and raw != "" else None
    except (TypeError, ValueError, AttributeError):
        return None


def _with_fallback_evidence(chosen, jupiter, branch, raydium, enabled=True, raydium_quote=None,
                            timing=None):
    """Tag the quote with which fallback branch fired, so the next review can READ it.

    WHY (2026-09-24). The Raydium fallback was switched off on every funded Solana entry for 23 h by
    accident (a861e755 restored it), and its thin-route branch could not be audited afterwards: the
    branch decides on Jupiter's own `priceImpactPct`, and 0 of 840 entry traces carried it. The
    value is already in hand here, so this records it -- no call is added.

    branch:  none | thin | thin_blocked (impact > 5% but the fallback was ineligible) | miss
    raydium: not_asked | won | lost | no_quote | distrusted (won by more than the phantom ratio on an
             entry; Jupiter's quote is returned, see `_raydium_entry_win_distrusted`)

    ABSENT STAYS ABSENT. `jupiter_impact_pct` is Jupiter's field parsed as-is, or None when Jupiter
    did not answer or sent nothing parseable -- never the 0.0 the decision above substitutes, which
    would read as a measured flat route. A total miss (Jupiter and Raydium both empty) returns None
    and carries no tag; its Jupiter failure is already typed by `EntryQuoteMissRecorder`.

    BOTH SIZES, WHICHEVER WON (2026-09-26). SPAM 4Kz2Fw: Raydium won the thin-route contest, its
    build failed simulation (router 0x26, slippage), and whether Jupiter could have filled inside
    the caps was unanswerable because the losing quote's outAmount was never kept. Both are in hand
    here. `raydium_out_atomic` is None when Raydium was not asked or returned nothing; `raydium`
    says which.
    """
    if not enabled:
        return chosen
    impact = None
    if jupiter is not None:
        try:
            raw = jupiter.get("priceImpactPct")
            impact = abs(float(raw)) if raw is not None and raw != "" else None
        except (TypeError, ValueError):
            impact = None
    try:
        chosen["_sniper_fallback"] = {"branch": branch, "raydium": raydium,
                                      "jupiter_impact_pct": impact,
                                      # JUPITER'S OWN ROUTE, before any fallback replaced it
                                      # (2026-10-02, Alpha): a Raydium win swaps the route plan,
                                      # so the losing route was unreadable afterwards.
                                      "jupiter_route_labels": _route_labels(jupiter),
                                      "jupiter_out_atomic": _out_atomic(jupiter),
                                      "raydium_out_atomic": _out_atomic(raydium_quote),
                                      # C2's instrument: each from the quote start; with
                                      # raydium_parallel the saving is (J + R) - max(J, R).
                                      **(timing if raydium != "not_asked" and timing else {})}
    except TypeError:
        pass                                  # a non-dict quote carries no tag; routing unchanged
    return chosen


def jupiter_entry_quote(mint, usd_amount, *, http=None, slippage_bps=None, max_accounts=None,
                        exclude_dexes=()):
    """USDC -> mint quote for a `usd_amount` buy. USDC is 6-decimal -> atomic = usd * 1e6."""
    if slippage_bps is None:
        from sniper.settings.memecoin import MEMECOIN_ENTRY_TX_SLIPPAGE_PCT
        slippage_bps = int(round(MEMECOIN_ENTRY_TX_SLIPPAGE_PCT * 100))
    # ENTRY ONLY: the tag is for the entry trace. An exit quote must reach the signer exactly as
    # Jupiter answered it (tests/test_memecoin_solana_stop_quote.py pins that).
    return _quote_with_fallback(USDC_MINT, mint, int(round(usd_amount * 1e6)), slippage_bps, http,
                                max_accounts, exclude_dexes, tag_evidence=True)


# TYPED ENTRY QUOTE MISS (2026-09-16). The funded Solana entry released every empty quote as a bare
# "no quote": 36 attempts in 7 days, including bopa x26 (+155% at 5 min) and SCRIBE (+785% at 15 min).
# All 36 had zero Jupiter 429s in their minute and a 349-444 ms quote stage (two round trips), so they
# were no-route answers. The miss is now labelled in the EVM vocabulary; nothing is retried, and the
# quote path itself is unchanged: the recorder only watches the calls `_quote_with_fallback` makes.
_ENTRY_QUOTE_RECORDING = threading.local()


def _record_quote_call(url, call):
    import io as _io
    import urllib.error as _urlerror
    calls = _ENTRY_QUOTE_RECORDING.calls
    try:
        result = call()
    except _urlerror.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:
            pass
        try:
            payload = _json.loads(raw.decode()) if raw else {}
        except Exception:
            payload = {}
        calls.append({"url": url, "status": exc.code, "payload": payload})
        # The body was read to classify it; hand `_http_status` an identical, unread copy.
        raise _urlerror.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, _io.BytesIO(raw)) from None
    except Exception as exc:
        calls.append({"url": url, "exception": exc})
        raise
    status = result[0] if isinstance(result, tuple) and result else None
    payload = result[1] if isinstance(result, tuple) and len(result) > 1 else None
    calls.append({"url": url, "status": status, "payload": payload})
    return result


# Two transports with stable identities, so `_accepts_headers` caches each once. The keyed one is
# chosen only when the real transport takes headers, so a funded key still reaches its host.
def _recorded_keyed(method, url, headers=None, body=None):
    base = _ENTRY_QUOTE_RECORDING.base
    kwargs = {k: v for k, v in (("headers", headers), ("body", body)) if v is not None}
    return _record_quote_call(url, lambda: base(method, url, **kwargs))


def _recorded_plain(method, url, body=None):
    base = _ENTRY_QUOTE_RECORDING.base
    return _record_quote_call(
        url, lambda: base(method, url, body=body) if body is not None else base(method, url))


def classify_entry_quote_miss(calls):
    """`{code, detail}` for an empty entry quote, from the Jupiter and Raydium calls it made."""
    jupiter = [c for c in calls if str(c.get("url", "")).startswith(JUP_BASES)]
    raydium = [c for c in calls if str(c.get("url", "")).startswith(RAY_BASE)]
    ray = ""
    if raydium:
        last = raydium[-1]
        if "exception" in last:
            ray = "; raydium %s" % type(last["exception"]).__name__
        else:
            body = last.get("payload") if isinstance(last.get("payload"), dict) else {}
            ray = "; raydium %s" % (body.get("msg") or last.get("status"))
    if not jupiter:
        return {"code": "provider_rpc_transport", "detail": "no jupiter answer" + ray}
    last = jupiter[-1]
    if "exception" in last:
        exc = last["exception"]
        timed_out = (isinstance(exc, TimeoutError)
                     or isinstance(getattr(exc, "reason", None), TimeoutError)
                     or "timed out" in str(exc).lower())
        return {"code": "provider_timeout" if timed_out else "provider_rpc_transport",
                "detail": ("jupiter %s: %s" % (type(exc).__name__, exc))[:160]}
    status, body = last.get("status"), last.get("payload")
    if status == 429:
        return {"code": "provider_rate_limit", "detail": "jupiter 429 on every host" + ray}
    if isinstance(status, int) and (status >= 500 or status in _AUTH_STATUSES):
        return {"code": "provider_http_%d" % status, "detail": "jupiter %d%s" % (status, ray)}
    if isinstance(body, dict) and (body.get("errorCode") or body.get("error")):
        label = body.get("errorCode") or body.get("error")
        return {"code": "no_route", "detail": ("jupiter %s %s%s" % (status, label, ray))[:160]}
    if isinstance(status, int) and 400 <= status < 500:
        return {"code": "no_route", "detail": "jupiter %d%s" % (status, ray)}
    return {"code": "quote_malformed",
            "detail": ("jupiter %s answered without a route or an error%s" % (status, ray))[:160]}


def _recorded_calls(base, call):
    """`(result, calls)`: run `call(http)` on a transport that records each request it makes.

    Thread-local and restored on the way out, so concurrent entries never see each other's calls.
    The recorder only watches `base`; it never adds a request."""
    previous = (getattr(_ENTRY_QUOTE_RECORDING, "base", None),
                getattr(_ENTRY_QUOTE_RECORDING, "calls", None))
    calls = []
    _ENTRY_QUOTE_RECORDING.base, _ENTRY_QUOTE_RECORDING.calls = base, calls
    try:
        return call(_recorded_keyed if _accepts_headers(base) else _recorded_plain), calls
    finally:
        _ENTRY_QUOTE_RECORDING.base, _ENTRY_QUOTE_RECORDING.calls = previous


def typed_jupiter_entry_quote(mint, usd_amount, *, http=None, slippage_bps=None, max_accounts=None,
                              exclude_dexes=()):
    """`(quote, miss)`: the exact `jupiter_entry_quote` result, and `{code, detail}` when it is empty."""
    quote, calls = _recorded_calls(http or _http, lambda recorded: jupiter_entry_quote(
        mint, usd_amount, slippage_bps=slippage_bps, max_accounts=max_accounts,
        exclude_dexes=exclude_dexes, http=recorded))
    if quote is not None:
        return quote, None
    return None, classify_entry_quote_miss(calls)


class EntryQuoteMissRecorder:
    """The funded entry's quote seam and its miss seam. The last miss is kept per thread."""

    def __init__(self, http):
        self.http = http
        self._local = threading.local()

    def quote(self, mint, usd, max_accounts=None, exclude_dexes=(), http=None):
        quote, miss = typed_jupiter_entry_quote(
            mint, usd, http=http or self.http, max_accounts=max_accounts,
            exclude_dexes=exclude_dexes)
        self._local.miss = miss
        return quote

    def miss(self):
        return getattr(self._local, "miss", None)

    # THE ENTRY'S SELL PROOF IS TYPED THE SAME WAY (2026-09-27, Codex refusal-26ebbe40 item 2).
    # "no planned-quantity sell route" read the same for a Jupiter 429, a timeout and a true
    # no-route answer, so triage could not tell infra from market. Same calls, same classifier.
    def reverse(self, mint, amount_atomic, slippage_pct, http=None):
        self._local.reverse_miss = None
        quote, calls = _recorded_calls(http or self.http or _http, lambda recorded: (
            jupiter_exit_quote(mint, amount_atomic, slippage_pct, http=recorded, evidence=True,
                               entry_proof=True)))
        if quote is None:
            self._local.reverse_miss = classify_entry_quote_miss(calls)
        return quote

    def reverse_miss(self):
        return getattr(self._local, "reverse_miss", None)


def jupiter_exit_quote(mint, amount_atomic, slippage_pct, *, http=None, max_accounts=None,
                       evidence=False, entry_proof=False):
    """mint -> USDC quote for selling `amount_atomic` (already in the token's smallest units).
    `evidence` tags it with the fallback branch (the entry's sell proof only: an exit quote must
    reach the signer exactly as Jupiter answered it)."""
    return _quote_with_fallback(mint, USDC_MINT, amount_atomic, int(round(slippage_pct * 100)), http,
                                max_accounts, evidence=evidence, entry_proof=entry_proof)


def _typed_jupiter_exit_quote(mint, amount_atomic, slippage_pct, *, http=None):
    """Return (raw quote, failure). Captures transport truth without changing entry quote callers."""
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    observed = {}
    answers = []                  # (url, status) of every answer, in order
    base_http = http or _http

    def tracked(*args, **kwargs):
        try:
            result = base_http(*args, **kwargs)
        except Exception as exc:
            observed["exception"] = exc
            raise
        observed["response"] = result
        try:
            answers.append((str(args[1] if len(args) > 1 else kwargs.get("url", "")),
                            result[0] if isinstance(result, tuple) else None))
        except Exception:
            pass
        return result

    try:
        quote = jupiter_exit_quote(mint, amount_atomic, slippage_pct, http=tracked)
    except Exception as exc:
        observed["exception"] = exc
        quote = None
    if quote is not None:
        return quote, None
    exc = observed.get("exception")
    if isinstance(exc, TimeoutError):
        return None, QuoteFailure.PROVIDER_TIMEOUT
    if exc is not None:
        # CLASSIFY BY HTTP CODE FIRST (2026-08-19). `_http` uses `urllib.request.urlopen`, which
        # RAISES HTTPError on 4xx/5xx and never returns a (429, …) tuple — so the `status == 429`
        # branch below was dead for this transport and every Jupiter rate limit typed as
        # RPC_TRANSPORT. RPC_TRANSPORT is in the `retryable` set, so a 429 was retried, which is
        # precisely what the quote-infra spec forbids: "never retry signing, submission, rate
        # limits, malformed data, or token failures". We were retrying into the throttle.
        # Live 2026-08-19: pos 40653 (RWA) went rails-blind for 21 sweeps while lite-api returned
        # 429 with a PLAIN-TEXT body; a manual read needed four backoff attempts to get a quote.
        return None, _http_status_failure(exc) or QuoteFailure.RPC_TRANSPORT
    response = observed.get("response")
    if not isinstance(response, tuple) or len(response) < 2:
        return None, QuoteFailure.MALFORMED_RESPONSE
    status, payload = response[0], response[1]
    if status == 429:
        return None, QuoteFailure.RATE_LIMIT
    if isinstance(status, int) and status >= 500:
        return None, QuoteFailure.RPC_TRANSPORT
    if status != 200 or not isinstance(payload, dict):
        _log_malformed_exit_quote(mint, status, payload)
        return None, QuoteFailure.MALFORMED_RESPONSE
    message = str(payload.get("error") or payload.get("message") or "").lower()
    if not message and payload.get("success") is False and _jupiter_refused_before_raydium(answers):
        # Raydium's refusal, {id, success: false, version, msg}: the venue fallback asked it last
        # (37 lines 10-14..10-15 typed malformed, i.e. infra). Its msg is typed by the same words,
        # only because Jupiter refused the route first; a throttled Jupiter said nothing about it.
        message = str(payload.get("msg") or "").lower()
    if "liquid" in message:
        return None, QuoteFailure.INSUFFICIENT_LIQUIDITY
    if "token" in message or "mint" in message:
        return None, QuoteFailure.TOKEN_REJECTED
    if "route" in message or payload.get("error"):
        return None, QuoteFailure.NO_ROUTE
    if (payload.get("success") is False and str(payload.get("msg") or "").upper() in _NO_ROUTE_CODES
            and answers and answers[-1][0].startswith(RAY_BASE) and not _jupiter_refused_access(answers)):
        # RAYDIUM: NO ROUTE, JUPITER SILENT ABOUT THE ROUTE (2026-10-17, ANIMA 73930). Not a malformed
        # answer and not an outage of ours: the coin is not indexed on either venue right now.
        return None, QuoteFailure.ROUTE_NOT_INDEXED
    # A 200 WITH A BODY WE COULD NOT USE. The class is right (never retried), but the page said only
    # "malformed_response" and the body was gone, so the 2026-09-17 19:0xZ episode (streak 5, one
    # position) could not be attributed. The keys and the status are enough to tell a route-less
    # answer from a shape change, and they carry no secret.
    _log_malformed_exit_quote(mint, status, payload,
                              jupiter=[s for url, s in answers if not url.startswith(RAY_BASE)])
    return None, QuoteFailure.MALFORMED_RESPONSE


# Jupiter's own answers about the route: a 200 without a quote, or a request-level 4xx. A 429 or a
# 401/403 is about our access, not the coin.
_JUPITER_ROUTE_ANSWERS = frozenset({200, 400, 404, 422})


_NO_ROUTE_CODES = frozenset({"ROUTE_NOT_FOUND", "NO_ROUTES_FOUND", "COULD_NOT_FIND_ANY_ROUTE"})
# Jupiter refusing OUR access (rate limit, key) or too slow to answer (408, Gamma LOW 2026-10-17): it
# said nothing about the coin (2026-10-15 pin), so Raydium's refusal alone stays infra.
_JUPITER_ACCESS_ANSWERS = frozenset({401, 403, 408, 429})


def _explicit_jupiter_route_refusal(calls, *, build=False):
    jupiter = [call for call in calls if str(call.get("url", "")).startswith(JUP_BASES)]
    last = jupiter[-1] if jupiter else {}
    body = last.get("payload")
    if last.get("status") not in _JUPITER_ROUTE_ANSWERS or not isinstance(body, dict):
        return False
    if any(isinstance(body.get(key), str) and body[key] in _NO_ROUTE_CODES
           for key in ("errorCode", "error")):
        return True
    error = body.get("error")
    return bool(build and isinstance(error, str) and any(
        marker in error.lower() for marker in ("instructionerror", "illegalowner")))


def _jupiter_refused_access(answers):
    jupiter = [status for url, status in answers if not url.startswith(RAY_BASE)]
    return bool(jupiter) and jupiter[-1] in _JUPITER_ACCESS_ANSWERS


def _jupiter_refused_before_raydium(answers):
    """True when Raydium answered last and the Jupiter answer before it refused the route."""
    if not answers or not answers[-1][0].startswith(RAY_BASE):
        return False
    jupiter = [status for url, status in answers if not url.startswith(RAY_BASE)]
    return bool(jupiter) and jupiter[-1] in _JUPITER_ROUTE_ANSWERS


def _log_malformed_exit_quote(mint, status, payload, jupiter=None):
    """Name what a malformed exit-quote answer actually contained. Keys only, plus Raydium's msg
    when it is a short code (ROUTE_NOT_FOUND-shaped), never other values."""
    try:
        keys = sorted(payload.keys())[:12] if isinstance(payload, dict) else type(payload).__name__
        msg = payload.get("msg") if isinstance(payload, dict) else None
        code = msg if (isinstance(msg, str) and len(msg) <= 40
                       and msg.replace("_", "").isalnum()) else None
        log_sampled(logger.warning, ("exit_quote_malformed", str(mint)[:12]),
                    "exact exit quote malformed mint=%s status=%s body=%s%s%s",
                    mint, status, keys, "" if code is None else " msg=%s" % code,
                    "" if jupiter is None else " jupiter=%s" % list(jupiter)[-4:])
    except Exception:
        pass


def _tracked_http(base_http, observed):
    def tracked(*args, **kwargs):
        try:
            result = base_http(*args, **kwargs)
        except Exception as exc:
            observed["exception"] = exc
            raise
        observed["response"] = result
        return result
    return tracked


def _http_status_failure(exc):
    """RATE_LIMIT / RPC_TRANSPORT from a RAISED HTTP error's status code, or None.

    `urlopen` signals 4xx/5xx by raising, so a status-tuple check never sees them. Only 429 is
    reclassified here — it is the one code whose correct handling (never retry) differs from the
    RPC_TRANSPORT default (retry once). 5xx keeps RPC_TRANSPORT, which is already right.
    """
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status", None)
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    return QuoteFailure.RATE_LIMIT if code == 429 else None


def _infra_balance_failures():
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    return frozenset({QuoteFailure.RPC_TRANSPORT, QuoteFailure.PROVIDER_TIMEOUT,
                      QuoteFailure.RATE_LIMIT, QuoteFailure.MALFORMED_RESPONSE})


INFRA_BALANCE_FAILURES = _infra_balance_failures()


def _fresh_desk_fill_atomic(position, now=None):
    """Raw atoms our own desk buy just received, for the exit PREVIEW only, or None.

    THE RAILS WERE BLIND FOR 2-10 s AFTER EVERY DESK BUY (2026-10-23, pos 74130..74142): the owner
    token index has not caught up with our fresh account, getTokenAccountsByOwner answers an empty
    list, and the preview had no balance to quote. The confirmed entry fill already proves the bag.
    Only inside the window after the fill and before any exit or de-risk; pump.fun mints carry 6
    decimals. The sell itself still reads the exact custody balance and refuses on an empty one."""
    import math
    from datetime import datetime
    from sniper.settings.memecoin import MEMECOIN_EXIT_PREVIEW_FILL_QTY_S as window_s
    from sniper.util.clock import real_utc_now
    try:
        if (window_s <= 0 or position.get("signal_source") != "desk"
                or position.get("entry_fill_source") != "chain"
                or not str(position.get("mint") or "").endswith("pump")
                or any(position.get(k) for k in ("derisked_at", "derisk_sig", "derisk_pending_at",
                                                  "derisk_claim_id", "close_submitted_at", "close_sig"))):
            return None
        opened = datetime.fromisoformat(str(position["opened_recorded_at"])).timestamp()
        age = (real_utc_now() if now is None else now) - opened
        notional, price = float(position["entry_notional"]), float(position["entry_price"])
        if not (0 <= age <= window_s and math.isfinite(notional) and math.isfinite(price)
                and notional > 0 and price > 0):
            return None
        atomic = int(round(notional / price * 1_000_000))
        return atomic if 0 < atomic < 2**64 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _infra_failure(observed):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    exc = observed.get("exception")
    if isinstance(exc, TimeoutError):
        return QuoteFailure.PROVIDER_TIMEOUT
    if exc is not None:
        # Same dead-branch fix as _typed_jupiter_exit_quote: a raised 429 must not read as
        # retryable transport. This classifier serves the balance/Jupiter boundary the
        # QUOTE-INFRA alert names.
        return _http_status_failure(exc) or QuoteFailure.RPC_TRANSPORT
    response = observed.get("response")
    if not isinstance(response, tuple) or len(response) < 2:
        return QuoteFailure.MALFORMED_RESPONSE
    status, payload = response[0], response[1]
    if status == 429:
        return QuoteFailure.RATE_LIMIT
    if isinstance(status, int) and status >= 500:
        return QuoteFailure.RPC_TRANSPORT
    if status != 200 or not isinstance(payload, dict):
        return QuoteFailure.MALFORMED_RESPONSE
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code") == -32429:
        return QuoteFailure.RATE_LIMIT
    if error is not None:
        return QuoteFailure.RPC_TRANSPORT
    return None


# BUILD-VERSUS-SIGN TIMING, per thread (2026-09-24). The funded runner is threaded and one seam
# serves every concurrent entry, so a plain attribute here would let one thread's build time be
# read as another's. Same shape as `_BUILD_REFUSAL` directly above, for the same reason.
_SIGN_TIMING = threading.local()


def take_sign_timing():
    """The last `_sign` split on THIS thread, consumed so it cannot be read twice.

    Popping matters more than storing: a stale split left by an earlier attempt would be attributed
    to this one, and a build that never ran would silently inherit the previous coin's number.
    """
    timing = getattr(_SIGN_TIMING, "split", None)
    _SIGN_TIMING.split = None
    return timing


def jupiter_swap_tx(quote, user_pubkey, *, http=None, priority_fee_microlamports=None,
                    return_metadata=False, read_rpc_url=None):
    """POST the quote to /swap; return the base64 UNSIGNED VersionedTransaction (or None)."""
    if isinstance(quote, dict) and quote.get("_venue") == "raydium":
        # A Raydium quote can only be built by Raydium — Jupiter's /swap does not know the coin,
        # which is the reason the fallback quoted it in the first place.
        return raydium_swap_tx(quote, user_pubkey, http=http,
                               priority_fee_microlamports=priority_fee_microlamports,
                               return_metadata=return_metadata,
                               **({"read_rpc_url": read_rpc_url} if read_rpc_url is not None else {}))
    if isinstance(quote, dict) and quote.get("_venue") in ("pump_native", "pumpswap_native"):
        # Built and SIMULATED at quote time by sniper/execution/pump_native_exit.py: pump.fun
        # sell_v2 into the curve's quote asset, then Jupiter's own route into USDC, one signed
        # identity. Jupiter cannot build this sell itself (fries 55293, 2026-09-09).
        swap = quote.get("_native_swap") or {}
        if ((quote.get("_curve_leg") or {}).get("kind") == "sol"
                and swap.get("simulated") is not True):
            return None  # Entry readiness probes are never executable exits.
        if not swap.get("swapTransaction"):
            return None
        return swap if return_metadata else swap["swapTransaction"]
    from sniper.execution.entry_attribution import public_quote
    http = http or _http
    payload = {
        "quoteResponse": public_quote(quote),
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
    }
    if priority_fee_microlamports is None:
        payload["prioritizationFeeLamports"] = "auto"  # preserve the existing entry path
    elif int(priority_fee_microlamports) > 0:
        payload["computeUnitPriceMicroLamports"] = int(priority_fee_microlamports)
    body = _json.dumps(payload)
    # The quoteResponse is self-contained, so the build may use whichever host is healthy — it does
    # not have to be the one that produced the quote.
    status, j, failure, host = _jup_build_once(body, http)
    if failure is not None:
        # SAY WHY (2026-09-25). A failed build returned a bare None, so CROWD's two refusals at
        # 18:30Z read "swap build returned nothing" with no cause while every endpoint was down on
        # a DNS stall. ONE retry, on the OTHER host, and only for a failure about the transport or
        # the server: a transport error or a 5xx. A 4xx is an answer about the request, and a 401
        # already has its own keyless path and JUPITER-KEY-REJECTED page inside _jup_call.
        retryable = status is None or (isinstance(status, int) and 500 <= status < 600)
        if retryable:
            started = _monotonic()
            status, j, failure2, host2 = _jup_build_once(body, http, exclude=(host,))
            logger.warning(
                "jupiter /swap build failed: %s on %s; retry on %s -> %s in %.0f ms",
                failure, _jup_host_name(host), _jup_host_name(host2), failure2 or "ok",
                (_monotonic() - started) * 1000.0)
            if failure2 is not None:
                return None
        else:
            logger.warning("jupiter /swap build failed: %s on %s (not retried: not a transport "
                           "error or 5xx)", failure, _jup_host_name(host))
            return None
    tx = j.get("swapTransaction") or None
    return j if tx and return_metadata else tx


def _jup_build_once(body, http, *, exclude=()):
    """(status, json, failure_or_None, host) for one /swap build. `failure` names the cause:
    an exception class, `http_<status>`, or `no_transaction`. Never the URL or the key."""
    _JUP_LAST.base = None
    try:
        status, j = _jup_call("POST", "/swap", body=body, http=http, exclude=exclude)
    except Exception as exc:
        return None, None, type(exc).__name__, getattr(_JUP_LAST, "base", None)
    host = getattr(_JUP_LAST, "base", None)
    if status is None and host is None:
        return None, None, "no_host", None          # every host excluded or cooled out
    if status != 200 or not isinstance(j, dict):
        return status, j, "http_%s" % status, host
    if not j.get("swapTransaction"):
        return status, j, "no_transaction", host
    return status, j, None, host


def _clamp_exit_fee(swap):
    """EXIT FEE CLAMP (operator yes 2026-10-23): every exit build is clamped to the exit priority cap before it
    is signed. Never refuses: an unreadable build signs unchanged and logs its leg unbounded."""
    from sniper.execution.solana_sender_ab import clamp_exit_priority
    from sniper.settings import memecoin as _cfg
    if swap is None:
        return swap, None
    swap, info = clamp_exit_priority(swap, cap=int(_cfg.MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS))
    if not info["bounded"]:
        logger.warning("exit fee unbounded: signing unchanged reason=%s", info.get("reason", "loaded_transfer"))
    elif info["clamped"]:
        logger.warning("exit fee clamped: requested=%s uL cap=%s uL cu_limit=%s ceiling=%s lamports",
                       info["requested_micro_lamports"], info["cap_micro_lamports"], info["cu_limit"],
                       info["fee_ceiling_lamports"])
    return swap, info


# --- solders signing ----------------------------------------------------------------------------

def sign_swap_tx(swap_b64, keypair):
    """Deserialize Jupiter's unsigned tx, sign with `keypair`, re-serialize. Returns
    (signed_b64, signature_str) or None on any malformed input (fail-closed)."""
    try:
        from solders.transaction import VersionedTransaction
        meta = swap_b64 if isinstance(swap_b64, dict) else None
        if meta is not None:
            swap_b64 = meta.get("swapTransaction")
        if not swap_b64:
            return None           # the build returned nothing; not a traceback (09-13, pos 56745)
        raw = base64.b64decode(swap_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        sign_started = time.perf_counter()
        signed = VersionedTransaction(unsigned.message, [keypair])
        _SIGN_TIMING.local_sign_ms = (time.perf_counter() - sign_started) * 1000.0
        result = (base64.b64encode(bytes(signed)).decode(), str(signed.signatures[0]))
        if meta is None:
            return result
        validity = {
            "blockhash": str(unsigned.message.recent_blockhash),
            "last_valid_height": meta.get("lastValidBlockHeight"),
        }
        return (*result, validity)
    except Exception:
        # LOG IT. This returned a bare None, so every distinct cause -- a Jupiter payload that is
        # not valid base64, a transaction the solders parser rejects, a keypair mismatch -- reached
        # the operator as the single word "sign failed" with nothing to act on. Five funded entries
        # have died here with no recoverable reason (2026-08-14, 08-24, 08-30 x2, 09-04). Still
        # returns None: the fail-closed behaviour is correct and unchanged, only the silence is not.
        logger.exception("solana sign_swap_tx failed (entry refused, no funds committed)")
        return None


# --- JSON-RPC send + confirm --------------------------------------------------------------------

def entry_bid_pool(quote):
    """The pool whose recent fees price an entry: the hop that OUTPUTS the bought coin (the hot account), or None.

    The old rule took a pool only when the whole route had ONE: every desk route is two hops (USDC -> SOL -> coin),
    so the desk always bid the cold fallback (2026-10-23: 6 of 6 desk sends, 5M lamports each)."""
    if not isinstance(quote, dict) or not isinstance(quote.get("routePlan"), list):
        return None
    out_mint = quote.get("outputMint")
    hops = [leg["swapInfo"] for leg in quote["routePlan"]
            if isinstance(leg, dict) and isinstance(leg.get("swapInfo"), dict)
            and isinstance(leg["swapInfo"].get("ammKey"), str)]
    coin = {h["ammKey"] for h in hops if out_mint and h.get("outputMint") == out_mint}
    if len(coin) == 1:
        return next(iter(coin))
    every = {h["ammKey"] for h in hops}
    return next(iter(every)) if len(every) == 1 else None


def bound_pumpswap_pool(mint, *, position_id=None):
    """The exact position's entry-bound AMM coin vault, or a unique mint binding for legacy callers.

    This is a vault address, not an AMM account. The native builder resolves and verifies its
    token authority before using a pool. Missing or ambiguous bindings never select another pool.
    """
    from sniper.persistence.db_ticks import connect_sol_tape_readonly
    try:
        conn = connect_sol_tape_readonly()
        try:
            conn.execute("PRAGMA busy_timeout=1000")
            rows = conn.execute(
                "SELECT DISTINCT pool FROM sol_position_pools WHERE mint=? AND venue_kind='amm' "
                + ("AND position_id=? " if position_id is not None else "") + "LIMIT 2",
                (str(mint), position_id) if position_id is not None else (str(mint),)).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("bound pumpswap pool unreadable mint=%s position=%s", mint, position_id, exc_info=True)
        return None
    return rows[0][0] if len(rows) == 1 else None


_GRADUATED_MISS = {}                 # mint -> monotonic time the canonical pool last failed to build
_GRADUATED_MISS_TTL_S = 10.0         # Gamma on c518a1f5e: a NOT-yet-graduated curve exit must not re-pay the pool
_GRADUATED_MISS_LOCK = threading.Lock()   # read on every attempt and rung


def graduated_miss_recent(mint, now=None):
    now = _monotonic() if now is None else now
    with _GRADUATED_MISS_LOCK:
        at = _GRADUATED_MISS.get(mint)
    return at is not None and now - at < _GRADUATED_MISS_TTL_S


def note_graduated_miss(mint, now=None):
    with _GRADUATED_MISS_LOCK:
        if len(_GRADUATED_MISS) > 4096:     # ponytail: crude bound; a few hundred curve exits a day
            _GRADUATED_MISS.clear()
        _GRADUATED_MISS[mint] = _monotonic() if now is None else now


def graduated_curve_pool(mint, *, position_id):
    """The CANONICAL PumpSwap pool of a position that was bound to its pump CURVE, or None (2026-10-23).

    A curve position whose coin graduates mid-hold keeps its 'pump_curve' binding, so bound_pumpswap_pool
    finds no AMM row and the native PumpSwap exit never ran: Jupiter, 2-9 s late on fresh pools, was the
    only exit at the moment the pool price moves most. The canonical migrated pool is fully derived from
    the mint (PDA ["pool", 0, PDA["pool-authority", mint], mint, wSOL] under the AMM program); only pump's
    migration can create it (pool-authority is pump's PDA). Never another pool, never a mint-wide search.
    read_pool_state then verifies AMM ownership and the base mint on chain: before graduation the account
    does not exist and the native rung refuses exactly as today. Pure local read of the binding; no RPC."""
    if position_id is None:
        return None
    from sniper.persistence.db_ticks import connect_sol_tape_readonly
    try:
        conn = connect_sol_tape_readonly()
        try:
            conn.execute("PRAGMA busy_timeout=1000")
            row = conn.execute("SELECT venue_kind FROM sol_position_pools WHERE position_id=? AND mint=?",
                               (position_id, str(mint))).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("graduated pool binding unreadable mint=%s position=%s", mint, position_id, exc_info=True)
        return None
    if row is None or row[0] != "pump_curve":
        return None
    from sniper.execution.solana_native_entry_quote import accounts_for
    return accounts_for(mint)["pool"]


def _rpc_call(rpc_url, method, params, http):
    http = http or _http
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    status, j, _d = http("POST", rpc_url, body=body)
    return status, j


def source_partial_proof_snapshot(owner, mint, rpc_url, http=None):
    """Confirmed expiry height and a bag at least as new as that bank; recovery only."""
    try:
        status, response = _rpc_call(rpc_url, 'getEpochInfo', [{'commitment': 'confirmed'}], http)
        if status != 200 or not isinstance(response, dict) or response.get('error') is not None:
            return None
        epoch = response['result']
        height, slot = epoch['blockHeight'], epoch['absoluteSlot']
        if type(height) is not int or height < 0 or type(slot) is not int or slot < 0:
            return None
        bag = token_balance_snapshot(owner, mint, rpc_url, http=http, min_context_slot=slot, ata_fallback=True)
        if (type(bag.context_slot) is not int or bag.context_slot < slot or type(bag.atomic) is not int
                or bag.atomic < 0 or type(bag.decimals) is not int or not 0 <= bag.decimals <= 255):
            return None
        return dict(block_height=height, anchor_slot=slot, balance_slot=bag.context_slot,
                    balance_atomic=bag.atomic, decimals=bag.decimals)
    except Exception:
        return None


_last_send_error = threading.local()


def take_send_error():
    """The node's words for the `rpc_send` that just failed ON THIS THREAD, consumed so they can
    describe no later send, or None. `rpc_send` answers a bare None by contract; the pinned-close
    re-offer logs what the node said about a refused copy."""
    message = getattr(_last_send_error, "message", None)
    _last_send_error.message = None
    return message


def rpc_send(signed_b64, rpc_url, *, http=None, preflight_commitment=None,
             classify_preflight=False):
    """Broadcast a base64 signed tx. Returns the tx signature the RPC echoes, or None on error.
    skipPreflight=False so an obviously-bad tx is rejected up front rather than silently dropped."""
    _last_send_error.message = None
    options = {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}
    if preflight_commitment is not None:
        if preflight_commitment not in {"processed", "confirmed", "finalized"}:
            raise ValueError("invalid preflight commitment")
        options["preflightCommitment"] = preflight_commitment
    try:
        status, j = _rpc_call(rpc_url, "sendTransaction",
                              [signed_b64, options], http)
    except Exception as exc:
        logger.warning("rpc_send failed: transport error", exc_info=True)
        _last_send_error.message = "transport error: %s" % type(exc).__name__
        return None
    if status != 200 or not isinstance(j, dict) or "result" not in j:
        # The bare None is the CONTRACT (callers treat any failure as ambiguous), but hiding WHY
        # cost two wrong diagnoses on 2026-08-14 — a BlockhashNotFound read as a transport fault.
        logger.warning("rpc_send failed: status=%s error=%s",
                       status, str((j or {}).get("error"))[:300])
        _last_send_error.message = "status=%s error=%s" % (
            status, str((j or {}).get("error") if isinstance(j, dict) else j)[:300])
        err = j.get("error") if isinstance(j, dict) else None
        # Only the opt-in maintenance caller may release its write-ahead spend from an explicit
        # simulation rejection. Transport errors and rate limits may have sent the signed bytes.
        if (classify_preflight and status == 200 and isinstance(err, dict)
                and err.get("code") == -32002 and isinstance(err.get("data"), dict)
                and err["data"].get("err") is not None):
            return "explicit_rejected"
        return None
    return j["result"]


class ConfirmState(str):
    """A confirm verdict that still carries the chain's own words.

    Subclasses `str` so every existing `status == "failed"` / `status in ("confirmed", "failed")`
    check keeps working byte-for-byte. The diagnosis rides along as attributes instead of being
    discarded, which is what made a real FAILED-TX cost six manual getTransaction calls to explain
    (sunnydog06, 2026-08-24 15:28: Meteora DBC BuyExactIn rejected the route with AnchorError
    ConstraintMintTokenProgram, and all we persisted was the word "failed").

    `deterministic` answers ONE question for the retry lane: did a program execute and reject this
    route, or did we merely fail to get it landed? It is set True only for InstructionError, where
    the chain ran the instruction and refused it. Everything else stays False on purpose — the
    consumer uses this to STOP retrying, and stopping wrongly loses a good entry, so the
    uncertain direction has to be the one that keeps trying.
    """

    __slots__ = ("err", "program_error", "deterministic", "failed_program", "pump_slippage",
                 "landing_slot", "slot_guard")

    def __new__(cls, value, *, err=None, program_error=None, deterministic=False,
                failed_program=None, pump_slippage=None, landing_slot=None, slot_guard=None):
        obj = super().__new__(cls, value)
        obj.err = err
        obj.program_error = program_error
        obj.deterministic = bool(deterministic)
        obj.failed_program = failed_program
        obj.pump_slippage = pump_slippage
        obj.landing_slot = landing_slot
        obj.slot_guard = slot_guard
        return obj

    @property
    def reason_code(self):
        """A persistable one-liner, never the bare word 'failed'."""
        if self.slot_guard:
            return self.slot_guard['reason']
        if self.program_error:
            return "chain_error:%s" % self.program_error
        if self.err is not None:
            import json as _json
            try:
                return "chain_error:%s" % _json.dumps(self.err, separators=(",", ":"))[:120]
            except Exception:
                return "chain_error:%s" % str(self.err)[:120]
        return str(self)


def _err_is_instruction_error(err):
    """True when a program executed and rejected — as opposed to the tx never landing cleanly."""
    if isinstance(err, dict):
        return "InstructionError" in err
    return isinstance(err, str) and err == "InstructionError"


def _program_error_name(logs):
    """Pull the reason a program refused out of its logs, in the program's own words.

    Three dialects, most precise first:

    1. Anchor prints `Error Code: <Name>. Error Number: <n>.` — the name is the part a human can
       act on; the number alone sends you to a table.
    2. A non-Anchor program just prints its complaint. Raydium's Router says
       `Program log: Route error: exceeds desired slippage limit` and no Error Code line at all,
       so reading only dialect 1 persisted `{"InstructionError":[0,{"Custom":38}]}` for four
       funded reverts in three days (B5vZ3spp/JUST 2026-09-08) and every one of them cost a
       manual getTransaction to explain. Take the LAST such line: a token's own chatter early in
       the transaction is not the reason the route died at the end of it.
    3. Failing both, the runtime's own `Program <id> failed: <msg>` line.

    Returns None when nothing matches, so an ordinary log stream never invents a diagnosis.
    """
    for line in logs or ():
        text = str(line)
        if "Error Code:" not in text:
            continue
        name = text.split("Error Code:", 1)[1].strip().split(".", 1)[0].strip()
        if name:
            return name[:80]

    for line in reversed(list(logs or ())):
        text = str(line)
        if not text.startswith("Program log:"):
            continue
        body = text.split("Program log:", 1)[1].strip()
        if "error" not in body.lower():
            continue
        # `Route error: exceeds desired slippage limit` -> the half that says what went wrong.
        match = re.split(r"[Ee]rror:\s*", body, maxsplit=1)
        detail = (match[1] if len(match) > 1 else body).strip()
        if detail:
            return detail[:80]

    for line in reversed(list(logs or ())):
        text = str(line)
        if text.startswith("Program ") and " failed: " in text:
            head, detail = text.split(" failed: ", 1)
            detail = detail.strip()
            # NAMED ONLY WHEN THE PROGRAM IS KNOWN (2026-09-11). Jupiter v6 fails a slippage breach
            # with the bare code and no Error Code line, so LILMET's 17:28Z revert persisted as
            # "custom program error: 0x1771" and the retry lane (which matches the word "slippage"
            # on purpose, never a number) did not re-quote it. 0x1771 = 6001 is
            # SlippageToleranceExceeded in JUP6Lkbz...; the same number in another program means
            # something else, so the program id and the code must both match.
            program = head[len("Program "):].strip()
            if program == _JUPITER_V6 and detail.endswith("custom program error: 0x1771"):
                return "slippage tolerance exceeded (Jupiter 0x1771)"
            if detail:
                return detail[:80]
    return None


_JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


def _failing_program(logs):
    """The innermost non-plumbing program that failed (the venue), or None. The runtime prints the
    failing inner program first and then every caller up the stack, so the first `failed` line
    that is not Jupiter or a token/system program is the venue that refused."""
    for line in logs or ():
        text = str(line)
        if text.startswith("Program ") and " failed: " in text:
            program = text[len("Program "):].split(" ", 1)[0].strip()
            if program not in _NEVER_EXCLUDE:
                return program
    return None


def _failed_confirm_state(err, sig, rpc_url, http, sleep=None):
    """Build the failed verdict, and spend up to three getTransaction reads to name the error.

    Only ever runs on a tx that already failed, so it costs nothing in the normal path and buys the
    difference between "failed" and "Meteora rejected this route". Best-effort throughout: any
    problem fetching logs still returns a verdict carrying the raw err.
    """
    sleep = sleep or _sleep
    program_error = None
    failed_program = None
    pump_slippage = None
    slot_guard = None
    landing_slot = None
    # ASK AT THE COMMITMENT WE ALREADY ACCEPTED (2026-09-08). `rpc_confirm` goes terminal as soon
    # as `confirmationStatus` is `confirmed`; `getTransaction` DEFAULTS TO FINALIZED, ~32 slots
    # (~13s) later. So this asked for a transaction that provably existed and got `result: null`,
    # `meta` fell back to `{}`, and the name was lost with no error raised and nothing logged.
    # ELzdKrjF 15:37:39Z read `chain_error:{"InstructionError":[0,{"Custom":38}]}` on the page
    # while its own logs, replayed minutes later, say "exceeds desired slippage limit".
    #
    # BOUNDED, because the reservation release waits behind this and the funded failure path must
    # not spin. Three attempts over ~0.5s covers ordinary index lag; past that we return the raw
    # err exactly as before rather than hold the release.
    for attempt in range(3):
        try:
            status, j = _rpc_call(
                rpc_url, "getTransaction",
                [sig, {"maxSupportedTransactionVersion": 1, "encoding": "json",
                       "commitment": "confirmed"}], http)
            if status == 200 and isinstance(j, dict):
                result = j.get("result")
                if result is None:
                    # not indexed at this commitment YET — that is a timing answer, not "no logs"
                    if attempt < 2:
                        sleep(0.25)
                    continue
                logs = (result.get("meta") or {}).get("logMessages")
                program_error = _program_error_name(logs)
                failed_program = _failing_program(logs)
                from sniper.execution.solana_slot_executor import pump_slippage_failure
                pump_slippage = pump_slippage_failure(result)
                from sniper.execution.solana_slot_guard import slot_guard_failure
                slot_guard = slot_guard_failure(result, signature=sig)
                landing_slot = result.get("slot")
                break
        except Exception:
            program_error = None
            break
    return ConfirmState("failed", err=err, program_error=program_error,
                        deterministic=_err_is_instruction_error(err),
                        failed_program=failed_program, pump_slippage=pump_slippage,
                        landing_slot=landing_slot, slot_guard=slot_guard)


def rpc_confirm(sig, rpc_url, *, http=None, sleep=None, max_attempts=40, poll_s=1.0,
                fast_poll_s=None, fast_attempts=0):
    """Poll getSignatureStatuses until terminal. Returns:
      'confirmed' — confirmationStatus in {confirmed, finalized} and err is null.
      'failed'    — err is non-null (the tx landed but reverted).
      'pending'   — the signature exists but is not terminal.
      'not_found' — a valid RPC response explicitly returned null for the signature.
      'unavailable' — no valid response was received.
    Non-terminal states never authorize a resubmit. When polls disagree, preserve the strongest
    valid evidence: pending > not_found > unavailable.

    ``fast_poll_s`` applies only to the bounded initial window; the long tail stays at ``poll_s`` so
    a signature that never lands cannot create unbounded RPC load. Solana lands in ~1-2 slots
    (~400-800ms) but the 1s cadence quantized every measured confirmation to >=1.4s — the entry
    stage sat waiting on the poll, not on the chain."""
    import time as _time
    sleep = sleep or _time.sleep
    strongest = "unavailable"
    rank = {"unavailable": 0, "not_found": 1, "pending": 2}
    for attempt in range(max(0, int(max_attempts))):
        state = "unavailable"
        try:
            status, j = _rpc_call(rpc_url, "getSignatureStatuses",
                                  [[sig], {"searchTransactionHistory": True}], http)
            result = j.get("result") if status == 200 and isinstance(j, dict) and not j.get("error") else None
            values = result.get("value") if isinstance(result, dict) else None
            if isinstance(values, list) and values:
                val = values[0]
                if val is None:
                    state = "not_found"
                elif isinstance(val, dict):
                    if val.get("err") is not None:
                        return _failed_confirm_state(val.get("err"), sig, rpc_url, http,
                                                     sleep=sleep)
                    if val.get("confirmationStatus") in ("confirmed", "finalized"):
                        return ConfirmState("confirmed", landing_slot=val.get("slot"))
                    state = "pending"
        except Exception:
            state = "unavailable"
        if rank[state] > rank[strongest]:
            strongest = state
        if attempt + 1 < max_attempts:
            sleep(fast_poll_s if fast_poll_s is not None and attempt < int(fast_attempts)
                  else poll_s)
    return strongest


def token_decimals(mint, rpc_url, *, http=None):
    """SPL token decimals via getTokenSupply. None on failure (caller treats as no-route).

    At `confirmed` (2026-09-26): at the node's FINALIZED default a mint created seconds ago does
    not exist yet (-32602 "could not find account"), so decimals read None and the chase cap
    skipped itself on the first buy of the freshest coins (entry_token_snapshot)."""
    try:
        status, j = _rpc_call(rpc_url, "getTokenSupply", [mint, {"commitment": "confirmed"}], http)
        if status != 200:
            return None
        return int(j["result"]["value"]["decimals"])
    except Exception:
        return None


_SUPPLY_CACHE = {}


def token_supply(mint, rpc_url, *, http=None):
    """(supply_human, decimals) via getTokenSupply, or (None, None). CACHED FOREVER per mint.

    A pump.fun mint has its mint authority revoked, so supply is constant -- re-reading it is pure
    latency. The cache is what makes the mcap fallback below cheap enough for the entry path after
    the first sighting of a coin."""
    hit = _SUPPLY_CACHE.get(mint)
    if hit is not None:
        return hit
    try:
        # At `confirmed`: the mcap fallback exists for mints too fresh for DexScreener, and at the
        # FINALIZED default such a mint does not exist yet, so the fallback read None and the mcap
        # ceiling did not apply (2026-09-26).
        status, j = _rpc_call(rpc_url, "getTokenSupply", [mint, {"commitment": "confirmed"}], http)
        if status != 200:
            return (None, None)
        v = j["result"]["value"]
        out = (int(v["amount"]) / (10 ** int(v["decimals"])), int(v["decimals"]))
        if out[0] and out[0] > 0:
            _SUPPLY_CACHE[mint] = out
        return out
    except Exception:
        return (None, None)


def executable_mcap(mint, rpc_url, *, usd=None, http=None, quote_fn=None):
    """Market cap from OUR OWN route price x on-chain supply. None when either half is unknown.

    THE POINT (2026-08-24): DexScreener's data plane died for ~3h and took five entry gates with
    it -- the mcap ceiling, the coin band, the revival guard, the liquidity screens and the
    execution-premium cap. Every funded fill in that window banded `assumed_nano` with mcap NULL,
    so the $250k ceiling did not apply to a single one. This path has no vendor: getTokenSupply
    from the RPC we already pay for, priced by the router we already trade on. It works on a mint
    nobody has indexed yet, which is precisely what our armed wallets buy.

    Calibrated against DexScreener while BOTH were alive, 6 solana coins from $13k to $2.1M:
    ratio 0.909-1.045, median ~1.02. So the existing $250k threshold transfers -- a coin has to sit
    within ~$12k of the ceiling for the choice of source to change the verdict. The one 0.909x
    reading is our own clip moving a thin pool, which is the term that would widen under stress;
    log both while they are both available rather than treating one sweep as settled.

    Decimals come from the SAME getTokenSupply response, so the token-amount maths cannot drift
    from the supply it is divided into."""
    from sniper.settings.memecoin import MEMECOIN_TRADE_SIZE_USD
    usd = float(usd or MEMECOIN_TRADE_SIZE_USD)
    if usd <= 0:
        return None
    supply, decimals = token_supply(mint, rpc_url, http=http)
    if not supply or decimals is None:
        return None
    try:
        quote = (quote_fn or jupiter_entry_quote)(mint, usd)
        tokens_atomic = int((quote or {}).get("outAmount") or 0)
        if tokens_atomic <= 0:
            return None
        tokens = tokens_atomic / (10 ** decimals)
        if tokens <= 0:
            return None
        return supply * (usd / tokens)
    except Exception:
        return None


def entry_token_snapshot(pubkey, mint, rpc_url, *, http=None):
    """Pre-buy token snapshot whose decimals survive a mint the wallet never held.

    `token_balance_snapshot` reads OUR token accounts, so a FIRST buy — which is nearly every
    funded entry — reports `decimals=None`. `bind_quote_metadata` then prices that quote at
    `None`, and the chase gate (`if chase_cap and ... and quote_px:`) skipped itself. Of the 131
    funded Solana attempts that reached the gate after 2026-08-14, only 4 evaluated it, and every
    one of those 4 was a RE-entry that still held dust of the mint. `skipped_chase` never fired
    once, so fills landed +76.5%, +519% and +2895% above the source price under a 30% cap.

    The getTokenSupply answer is used only when the wallet cannot answer, and it reads the mint,
    never the balance: the atomic amount stays the wallet's own so entry-delta accounting is
    unchanged.

    BOTH READS RUN AT THE SAME TIME (2026-09-27, Alpha GO; docs/audits/2026-09-27-lemon-dougherty-
    s7.md). A first buy used to pay the wallet read and then the mint read, back to back (token read
    p50 179 ms). Now the mint read runs on its own thread while this one reads the wallet, so a first
    buy pays the slower of the two. A holder pays one discarded getTokenSupply, off the critical
    path. An unreadable wallet balance raises; mint decimals cannot turn it into a zero holding.
    """
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        mint_read = pool.submit(token_decimals, mint, rpc_url, http=http)
        snapshot = token_balance_snapshot(pubkey, mint, rpc_url, http=http)
        if snapshot.decimals is not None:
            return snapshot
        return TokenBalanceSnapshot(snapshot.atomic, mint_read.result())
    finally:
        pool.shutdown(wait=False)


# --- factory: bind everything into the executor call contracts ----------------------------------

def successful_receipt_cache(read_fn):
    """Memoize `read_fn(sig)` per signature, but ONLY a usable (non-None) receipt.

    The slot lane shares one getTransaction between the fill parser and the SOL-surplus credit.
    Caching a None (receipt not yet served at `confirmed`, or a timed-out read) made every one of
    execute_prebuilt_entry's bounded retries reuse that None, so a CONFIRMED buy returned
    anomaly/receipt_unreadable after a single read and opened no position (Codex re-review RR-01,
    2026-09-26). A miss must stay a miss so the next retry reads again."""
    cache = {}

    def read(sig):
        hit = cache.get(sig)
        if hit is None:
            hit = read_fn(sig)
            if hit is not None:
                cache[sig] = hit
        return hit
    return read


_SIGNATURE_PRECOMPILE_PROGRAMS = frozenset({
    # https://solana.com/docs/core/programs/precompiles
    'Ed25519SigVerify111111111111111111111111111',
    'KeccakSecp256k11111111111111111111111111111',
    'Secp256r1SigVerify1111111111111111111111111',
})


def _slot_entry_sol_liability(message, pending, validity):
    """Compare the rewritten unsigned message with its reservation's cached SOL allowance.

    Rent is the caller's existing conservative admission bound, not an account-state probe.
    Serializing an unsigned transaction reuses the existing compute-budget parser; no signature,
    ledger read or provider call is needed.
    """
    invalid = 'slot_lane_entry_sol_budget_invalid'
    budget, rent = validity.get('entry_sol_budget'), validity.get('entry_rent_lamports')
    if (not isinstance(budget, dict) or set(budget) != {'pending_id', 'available_lamports'}
            or type(budget.get('pending_id')) is not int or budget['pending_id'] != pending['id']
            or type(budget.get('available_lamports')) is not int or budget['available_lamports'] < 0
            or type(rent) is not int or rent < 0):
        return invalid, {}
    asset = pending.get('funding_asset') or 'usdc'
    principal = 0
    if asset == 'sol':
        value = pending.get('funding_amount_atomic')
        if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
            return invalid, {}
        principal = int(value)
    elif asset != 'usdc':
        return invalid, {}
    from solders.transaction import VersionedTransaction
    from sniper.execution.solana_swap_executor import signed_priority_fee_lamports
    try:
        # Header signatures alone do not price these verification instructions. Refuse rather
        # than invent another fee parser or fetch a fee quote on the ready-entry path.
        if any(str(message.account_keys[index]) in _SIGNATURE_PRECOMPILE_PROGRAMS
               for index in {ix.program_id_index for ix in message.instructions}):
            return 'slot_lane_entry_sol_fee_unknown', {}
        unsigned = base64.b64encode(bytes(VersionedTransaction.populate(message, []))).decode('ascii')
        priority = signed_priority_fee_lamports(unsigned)
        base = 5_000 * message.header.num_required_signatures
    except (TypeError, ValueError, AttributeError, IndexError, OverflowError):
        return 'slot_lane_entry_sol_fee_unknown', {}
    if priority is None:
        return 'slot_lane_entry_sol_fee_unknown', {}
    tip = validity.get('sender_tip_lamports', 0) + validity.get('nozomi_tip_lamports', 0)
    required = principal + priority + base + tip + rent
    detail = dict(available_lamports=budget['available_lamports'], required_lamports=required,
                  principal_lamports=principal, priority_lamports=priority,
                  base_fee_lamports=base, tip_lamports=tip, rent_bound_lamports=rent)
    return ('slot_lane_entry_sol_insufficient' if required > budget['available_lamports']
            else None), detail


def build_real_solana_executor(*, keypair, rpc_url, http=None, usdc_fn=None, token_fn=None,
                               submission_transport=None, fast_tx_builder=None,
                               backend_label="rpc", now_fn=None, proof_http=None, read_rpc_url=None,
                               entry_bids=None, native_exit_hint_fn=None):
    """Return {execute_fn, exit_fn, pubkey}. execute_fn matches real_entry's
    (conn, pid, mint, usd, now_iso) contract, recovering source_user_id + strategy from the pending
    row; exit_fn matches the real exit sweep's (conn, position, exit_reason, now_iso) contract.
    `usdc_fn`/`token_fn` default to live JSON-RPC balance reads (override for tests).

    `submission_transport` (None -> baseline `rpc_send`) routes entry + full-exit + partial-exit
    broadcasts through ONE shared same-bytes fanout; `fast_tx_builder` (None -> baseline Jupiter
    /swap sign) builds the tipped priority tx via /swap-instructions. With both None every path is
    byte-identical to the current RPC behavior."""
    from sniper.execution.solana_swap_executor import (
        execute_entry, receipt_entry_fill, receipt_exit_proceeds, recover_confirmed_entry,
    )
    from sniper.execution.solana_exit_executor import execute_exit, execute_partial_exit
    from sniper.util.clock import real_utc_now_iso

    pubkey = str(keypair.pubkey())
    from sniper.settings.memecoin import solana_read_rpc
    read_url = read_rpc_url if read_rpc_url is not None else solana_read_rpc()
    build_read_kwargs = {"read_rpc_url": read_rpc_url} if read_rpc_url is not None else {}
    payout_restricted = (lambda mint: _cached_payout_restricted(mint, http, rpc_url=read_rpc_url)) \
        if read_rpc_url is not None else _cached_payout_restricted
    _now_fn = now_fn or real_utc_now_iso
    injected_usdc_fn = usdc_fn
    usdc_fn = usdc_fn or (lambda: usdc_balance(pubkey, rpc_url, http=http))
    # RAISES on a failed read instead of a confident 0.0 — the exit executors price fills by
    # wallet USDC delta, and a silent-zero pre-read books the whole balance as proceeds
    # (pos#38107, 2026-08-17). An injected test usdc_fn stays authoritative for both reads.
    usdc_checked_fn = injected_usdc_fn or (lambda: token_balance_checked(
        pubkey, USDC_MINT, rpc_url, http=http))
    injected_token_fn = token_fn
    def _exit_token_balance(mint):
        snapshot = token_balance_snapshot(pubkey, mint, rpc_url, http=http, ata_fallback=True)
        if snapshot.decimals is None:
            raise TokenBalanceReadError("account_balance_unproved")
        return snapshot.human
    token_fn = token_fn or _exit_token_balance
    entry_token_fn = injected_token_fn or (lambda mint: entry_token_snapshot(
        pubkey, mint, rpc_url, http=http))

    def _curve_state(amm_key):
        """A pump.fun bonding curve's SOL, for telling a live curve from a graduated corpse.

        LAMPORTS ONLY, DELIBERATELY. The `complete` flag lives at a byte offset in the account's
        own layout and guessing an offset is how a decoder starts returning plausible nonsense, so
        this reads the balance and nothing else. It separates the two cases cleanly: GIGACAT's
        drained curve held 0.00141732 SOL, roughly the rent-exempt minimum, while a live curve at
        a $47k market cap holds tens of SOL. Called ONLY after the impact cap has already refused,
        so a successful entry never pays for it. None on any failure -- the caller then leaves the
        ordinary refusal exactly as it was.
        """
        try:
            status, j = _rpc_call(rpc_url, "getAccountInfo",
                                  [str(amm_key), {"encoding": "base64"}], http)
            if status != 200:
                return None
            value = ((j or {}).get("result") or {}).get("value")
            if not isinstance(value, dict):
                return None
            return {"sol": float(value.get("lamports") or 0) / 1e9}
        except Exception:
            return None

    def _sign(quote, tip=None, tip_fallback=None, trace=None, before_sign=None,
              entry_min_slot=None, entry_max_slot=None, tip_guard=None):
        """`tip` = (Nozomi tip account, lamports): the Nozomi arm B on a non-slot-lane entry
        (MEMECOIN_NOZOMI_ALL_LANES). The transfer goes into the built message before the signature;
        when it cannot (`with_nozomi_tip` refuses, or the fast builder signs in one call) the
        original bytes are signed and `tip_fallback(reason)` says why. None: today's bytes."""
        if before_sign is not None:
            before_sign()  # Local policy only; a refusal leaves the swap unsigned.
        take_build_refusal()  # a stale note from an earlier attempt on this thread is not ours
        take_sign_timing()    # and neither is a stale build/sign split
        _SIGN_TIMING.local_sign_ms = None
        # Fast construction also carries last-valid-height into the durable pre-broadcast binding.
        # A Raydium venue quote always takes the default build: the fast builder speaks Jupiter
        # /swap-instructions, which does not know the coin (that is why the fallback quoted it).
        # A monolithic builder signs internally: guarded entries need the separable path below
        # so its final local check runs after building and before the custody signature.
        if fast_tx_builder is not None and before_sign is None and not (
                isinstance(quote, dict) and quote.get("_venue") in ("raydium", "pump_native", "pumpswap_native")):
            if tip is not None and tip_fallback is not None:
                tip_fallback("fast_builder")
            started = _monotonic()
            built = fast_tx_builder(quote)
            # ONE NUMBER ON THIS PATH, and it says so. The fast builder constructs AND signs behind
            # one call, so splitting it here would invent a boundary that does not exist.
            _SIGN_TIMING.split = {"builder": "fast_tx_builder",
                                  "build_ms": None, "sign_ms": None,
                                  "combined_ms": (_monotonic() - started) * 1000.0}
            return built
        # The shared default persists full blockhash validity. Requesting metadata changes no tx
        # bytes or Jupiter request fields; explicit legacy rpc mode retains the historical 2-tuple.
        # The builder named is the venue that BUILT: `jupiter_swap_tx` dispatches Raydium and
        # pump-native quotes to their own builders (SPAM 4Kz2Fw read jupiter_swap for a Raydium
        # build that Raydium's simulation refused, 2026-09-26).
        builder = {"raydium": "raydium_swap", "pump_native": "pump_native", "pumpswap_native": "pumpswap_native"}.get(
            quote.get("_venue") if isinstance(quote, dict) else None, "jupiter_swap")
        build_started = _monotonic()
        # ENTRY CU PRICE (2026-09-27): None is today's build -- Jupiter "auto", Raydium 25,000.
        # A set value reaches both builders; jupiter_swap_tx forwards it to the Raydium venue.
        from sniper.settings import memecoin as _cfg
        swap = jupiter_swap_tx(
            quote, pubkey, http=http, return_metadata=(submission_transport is not None),
            priority_fee_microlamports=_cfg.MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS, **build_read_kwargs)
        build_ms = (_monotonic() - build_started) * 1000.0
        _SIGN_TIMING.split = {"builder": builder, "build_ms": build_ms,
                              "sign_ms": None, "combined_ms": None}
        if not swap:
            # The OTHER half of "sign failed", and by far the likelier one: the swap was never
            # built, so there was nothing to sign. Naming which half failed is the difference
            # between a provider outage and a signing defect.
            logger.warning("solana swap build returned nothing for %s — entry refused before "
                           "signing, no funds committed",
                           (quote or {}).get("outputMint") if isinstance(quote, dict) else "?")
            if getattr(_BUILD_REFUSAL, "reason", None) is None:
                _note_build_refusal("swap build returned nothing")
            return None
        if tip is not None:
            swap = _with_nozomi_tip(swap, tip, tip_fallback, guard=tip_guard)
        if before_sign is not None:
            before_sign()
        if entry_min_slot is not None:
            from solders.transaction import VersionedTransaction
            from sniper.execution.solana_slot_guard import append_slot_guard
            try:
                payload = swap.get('swapTransaction') if isinstance(swap, dict) else swap
                unsigned = VersionedTransaction.from_bytes(base64.b64decode(payload))
                message = append_slot_guard(unsigned.message, entry_min_slot, entry_max_slot)
                payload = base64.b64encode(bytes(VersionedTransaction.populate(
                    message, unsigned.signatures))).decode()
                swap = {**swap, 'swapTransaction': payload} if isinstance(swap, dict) else payload
            except (ValueError, TypeError):
                _note_build_refusal('entry_slot_guard_build_failed')
                return None
        if True:   # UNCONDITIONAL entry fee clamp (operator yes 2026-10-22, option B): every entry, cache or not
            from sniper.execution.solana_sender_ab import (
                cap_unsigned_entry_priority, payload_tip, _NOZOMI_SET, nozomi_tip_cap_lamports)
            unsigned = swap.get('swapTransaction') if isinstance(swap, dict) else swap
            actual_tip = payload_tip(unsigned, _NOZOMI_SET, nozomi_tip_cap_lamports())
            if actual_tip is None:
                _note_build_refusal('entry_bid_tip_unreadable')
                return None
            pool = entry_bid_pool(quote)
            if pool is not None and entry_bids is not None:
                entry_bids.register(pool)  # Bounded background request; selection below performs no I/O.
            try:
                unsigned, _bid = cap_unsigned_entry_priority(unsigned, tip_lamports=actual_tip,
                                                              entry_bids=entry_bids, pool=pool)
            except ValueError as exc:   # a tip-guard refusal keeps its own reason
                _note_build_refusal(str(exc)[:60] if str(exc).startswith('sender_ab_tip_guard')
                                    else 'entry_bid_compute_budget_unreadable')
                return None
            swap = {**swap, 'swapTransaction': unsigned} if isinstance(swap, dict) else unsigned
        if before_sign is not None:
            before_sign()  # Building may have consumed the original source slot window.
        sign_started = _monotonic()
        signed = sign_swap_tx(swap, keypair)
        sign_ms = (_monotonic() - sign_started) * 1000.0
        _SIGN_TIMING.split = {"builder": builder, "build_ms": build_ms,
                              "sign_ms": getattr(_SIGN_TIMING, "local_sign_ms", None)
                              if getattr(_SIGN_TIMING, "local_sign_ms", None) is not None else sign_ms,
                              "combined_ms": None}
        if signed and entry_bids is not None and trace is not None:
            trace.mark_substage('entry_bid_signed', **_bid)
        return signed

    def _with_nozomi_tip(swap, tip, tip_fallback, guard=None):
        """`swap` (the base64 string, or the /swap metadata dict) with the Nozomi transfer, checked by
        the same guard the slot lane signs behind; the original `swap` when either refuses."""
        from solders.transaction import VersionedTransaction
        from sniper.execution.solana_sender_ab import guard_refusal, with_nozomi_tip
        b64 = swap.get("swapTransaction") if isinstance(swap, dict) else swap
        tipped = with_nozomi_tip(b64, pubkey, tip[0], tip[1]) if b64 else None
        reason = "insertion_refused"
        if tipped is not None:
            # `guard` = (sender lamports, nozomi lamports, dynamic): the desk fast-send arm names which
            # provider its one tip is for; None keeps the Nozomi all-lanes check exactly as before.
            sender_l, nozomi_l, dynamic = guard if guard is not None else (0, tip[1], entry_bids is not None)
            refused = guard_refusal(
                VersionedTransaction.from_bytes(base64.b64decode(tipped)).message, sender_l, nozomi_l,
                dynamic_nozomi=dynamic)
            if refused is None:
                return {**swap, "swapTransaction": tipped} if isinstance(swap, dict) else tipped
            reason = refused
        if tip_fallback is not None:
            tip_fallback(reason)
        return swap

    _sign.refusal_reason = take_build_refusal
    _sign.sign_timing = take_sign_timing

    def _exit_sign(quote, plan, before_sign=None, on_build_failure=None):
        # `before_sign` (the exit executor's verify gate, 2026-09-28): called after the build and
        # before the signature; False means the built bytes are dropped UNSIGNED and None returns.
        _SIGN_TIMING.split = None             # a stale split from an earlier call is not ours
        if fast_tx_builder is not None and not (
                isinstance(quote, dict) and quote.get("_venue") in ("raydium", "pump_native", "pumpswap_native")):
            if before_sign is not None and not before_sign():
                return None                   # one-call builder: judged before it builds
            return fast_tx_builder(quote)
        # The build/sign split, for the exit's Step 0 sub-marks (read by the executor through
        # sign_fn.sign_timing on this same thread). Same calls, same order as before.
        build_started = _monotonic()
        swap, calls = _recorded_calls(http or _http, lambda recorded: jupiter_swap_tx(
            quote, pubkey, http=recorded,
            priority_fee_microlamports=plan["priority_fee_microlamports"],
            return_metadata=True, **build_read_kwargs))
        build_ms = (_monotonic() - build_started) * 1000.0
        if before_sign is not None and not before_sign():
            return None
        if swap is None:
            # Requote on the next rung so the native bytes get their own intent and floor.
            if (on_build_failure is not None and not quote.get("_venue")
                    and _explicit_jupiter_route_refusal(calls, build=True)
                    and any(isinstance(leg, dict) and isinstance(leg.get("swapInfo"), dict)
                            and leg["swapInfo"].get("label") == "Pump.fun"
                            and leg["swapInfo"].get("outputMint") == _SOL_MINT
                            for leg in quote.get("routePlan") or ())):
                on_build_failure()
            return None
        swap, exit_fee = _clamp_exit_fee(swap)
        sign_started = _monotonic()
        signed = sign_swap_tx(swap, keypair)
        _SIGN_TIMING.split = {"builder": quote.get("_venue", "jupiter_swap"), "build_ms": build_ms,
                              "sign_ms": (_monotonic() - sign_started) * 1000.0,
                              "combined_ms": None, "exit_fee": exit_fee}
        return signed

    def _send(signed_tx):
        return rpc_send(signed_tx, rpc_url, http=http)

    def _send_fn_for(conn, owner_id, *, action, owner_kind, persist, observe_fn=None,
                     skip_preflight=False, record_async=False,
                     entry_max_slot=None, entry_head_fn=None):
        # None transport -> the exact baseline rpc_send. Otherwise ONE shared same-bytes fanout,
        # mapped back onto the executor's (identity|None|False) seam. Built per call so the audit /
        # durable binding key is the owning reservation/claim, on the caller thread.
        if submission_transport is None:
            if action == 'entry' and (entry_max_slot is not None or entry_head_fn is not None):
                return lambda *_args, **_kwargs: None  # Guarded entry cannot bypass provider gates.
            return _send
        from sniper.execution.submission_wiring import make_solana_send_fn
        claim_col = {"close_claim": "close_claim_id",
                     "derisk_claim": "derisk_claim_id"}.get(owner_kind)

        def owner_id_fn():
            row = m.get_position(conn, owner_id) if claim_col else None
            return row.get(claim_col) if row else owner_id

        return make_solana_send_fn(
            submission_transport, conn=conn, action=action, owner_kind=owner_kind,
            owner_id=owner_id, owner_id_fn=(owner_id_fn if claim_col else None),
            now_fn=_now_fn, backend_label=backend_label, persist=persist, observe_fn=observe_fn,
            skip_preflight=skip_preflight, record_async=record_async,
            entry_max_slot=entry_max_slot, entry_head_fn=entry_head_fn)

    class EntrySlotBlocked(Exception):
        pass

    def entry_window_refusal(max_slot, head_fn, min_slot=None):
        from sniper.execution.submission_transport import entry_slot_refusal
        if min_slot is not None or max_slot is not None:
            if type(min_slot) is not int or not 0 < min_slot < 2**63:
                return 'source_buy_slot_unknown'
            if type(max_slot) is not int or not min_slot <= max_slot <= min_slot + 1:
                return 'entry_slot_deadline_invalid'
        reason = entry_slot_refusal(max_slot, head_fn)
        if reason:
            return reason
        if submission_transport is None and (max_slot is not None or head_fn is not None):
            return 'entry_slot_transport_unavailable'
        return None

    def refuse_unsigned_entry(conn, pid, reason):
        released = m.release_unsigned_blocked_entry(conn, pid, reason)
        return {'status': 'aborted' if released else 'unknown', 'reason': reason}

    def _confirm(sig):
        # Solana lands in ~1-2 slots. At the flat 1s cadence every measured funded confirmation
        # floored at ~1.43s (07-15..07-18, n=16) — the stage was waiting on the poll, not the
        # chain. Observe promptly, then resume the 1s tail: 8*0.25 + 39*1 = 41s worst-case
        # non-terminal window, the same order as the previous 40s.
        return rpc_confirm(sig, rpc_url, http=http, max_attempts=47, poll_s=1.0,
                           fast_poll_s=0.25, fast_attempts=8)

    def _confirm_once(sig):
        # single-shot status check for the reservation-recovery pass — NOT the executor's fill-wait
        # poll (which blocks up to ~40s). Recovery runs inline in the loop; a blocking poll there
        # would stall the copy path + real-exit sweep for every stuck reservation, every cycle.
        # Reads through the pool: failover is SAFE here because every verdict this answer can
        # produce except not_found is POSITIVE evidence (a node reporting confirmed/failed/pending
        # SAW the transaction), and those drive orphan detection, which must keep working during a
        # custody outage — an orphan is funds already at risk. `_confirm_proof_once` below carries
        # the archival guarantee for the one verdict that needs it.
        return rpc_confirm(sig, rpc_url, http=http, max_attempts=1, poll_s=0)

    def _confirm_proof_once(sig):
        # The PINNED read, for the one recovery verdict that releases a reservation on NEGATIVE
        # evidence (peer review 2026-08-25). `http` is normally RotatingSolanaRpcHttp and
        # getSignatureStatuses is in its rotatable set, so a -32429 moves the read to another node.
        # `searchTransactionHistory=true` only searches the history the ANSWERING node kept, so two
        # nodes each answering null is not the evidence the two-spaced-reads rule collects — that
        # rule guards one lagging node re-answering, not a change of witness. Pinned to the
        # unrotated client, a rate-limited custody endpoint answers "unavailable" (fail-closed, the
        # row stays reserved) instead of a second opinion from a node that never saw the signature.
        # With no pool installed nothing rotates, so this is the same client as `_confirm_once`.
        #
        # Rejected alternative, recorded so nobody re-proposes it: gating on the response's
        # `context.slot` against `submission_last_valid_height`. Those are different units — a slot
        # counts skipped positions, a block height does not — and on 2026-08-25 the live gap was
        # 441,682,717 vs 419,731,411, about 22M slots of slack, so that comparison is true for every
        # node on the network regardless of sync state. `minContextSlot` does not help either:
        # getSignatureStatuses ignores it (verified live against api.mainnet-beta.solana.com, which
        # answered normally with minContextSlot=999999999999).
        return rpc_confirm(sig, rpc_url, http=(proof_http or http), max_attempts=1, poll_s=0)

    _entry_quotes = EntryQuoteMissRecorder(http)

    def _entry_quote(mint, usd, max_accounts=None, exclude_dexes=()):
        # Static exclusions apply to the ENTRY quote only (see static_excluded_venues).
        got = _entry_quotes.quote(
            mint, usd, max_accounts=max_accounts,
            exclude_dexes=static_excluded_venues() | frozenset(exclude_dexes or ()))
        # Register the coin's pool with the fee sampler at QUOTE time (the desk pre-quotes while JUDGE runs), so a
        # sample is usually ready by signing; a bounded deque append, no I/O on this thread.
        if entry_bids is not None:
            try:
                pool = entry_bid_pool(got)
                if pool is not None:
                    entry_bids.register(pool)
            except Exception:                     # a registration fault never touches the quote
                logger.debug("entry bid registration failed mint=%s", mint, exc_info=True)
        return got

    class _DeskNoRoute(Exception):
        """Jupiter and Raydium both answered no_route for a desk entry: the native curve rung owns it."""

    def _desk_entry_quote(desk_price_cap):
        def quote(mint, usd, max_accounts=None, exclude_dexes=()):
            from sniper.execution.desk_native_curve import desk_no_route
            got = _entry_quote(mint, usd, max_accounts=max_accounts, exclude_dexes=exclude_dexes)
            miss = _entry_quotes.miss()
            if desk_no_route(got, miss, desk_price_cap, max_accounts):
                raise _DeskNoRoute(miss)                # before execute_entry releases the reservation
            return got
        return quote

    _desk_curve_builder = []                            # one lazily built Global fee-config reader

    def _desk_fee_config():
        from sniper.execution.pump_curve_buy import SlotLaneCurveBuilder
        if not _desk_curve_builder:
            _desk_curve_builder.append(SlotLaneCurveBuilder(
                user=pubkey, rpc=lambda method, params: (
                    (_rpc_call(rpc_url, method, params, http) or (None, {}))[1] or {}).get("result")))
        builder = _desk_curve_builder[0]
        # Re-read at most every 300 s: a rotated Global fee recipient must not fail every rung entry
        # on chain until a restart (Gamma 2 on be46be528).
        if builder._fee_config is None or time.monotonic() - (builder._config_read_at or 0) > 300:
            builder._config_read_at = time.monotonic()
            builder._read_global()
        return builder._fee_config

    def _entry_reverse_quote(mint, amount_atomic):
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER
        return _entry_quotes.reverse(mint, amount_atomic, MEMECOIN_EXIT_SLIPPAGE_LADDER[-1])

    _entry_reverse_quote.miss = _entry_quotes.reverse_miss

    def _entry_receipt(sig):
        # UNREADABLE SAYS WHY (2026-09-27, Alpha GO G2). A transport error, a non-200 and a
        # JSON-RPC error all answered None with nothing logged, so an RPC outage read as "not
        # served yet". None stays the answer (the callers retry, then recovery takes over); the
        # stamped line names the cause. A plain null result (not served yet) stays silent.
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat
        try:
            status, response = _rpc_call(
                rpc_url, "getTransaction", [sig, {
                    "encoding": "jsonParsed", "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 1,
                }], http)
        except Exception as exc:
            logger.warning("%s entry receipt read failed sig=%s: %r", stamp(), sig, exc)
            return None
        if status != 200 or not isinstance(response, dict):
            logger.warning("%s entry receipt read failed sig=%s: status=%s", stamp(), sig, status)
            return None
        if response.get("error") is not None:
            logger.warning("%s entry receipt read failed sig=%s: rpc error %s", stamp(), sig,
                           str(response.get("error"))[:200])
            return None
        return response.get("result")

    def _receipt_proceeds(sig):
        return receipt_exit_proceeds(_entry_receipt(sig), signature=sig, owner=pubkey)

    def _native_exit_fits_packet(built):
        """(ok, reason). A BUILD THAT DID NOT COMPLETE IS NOT A TRANSACTION THAT IS TOO BIG.

        2026-09-18, Quant ref 7f6e294e. Both outcomes returned False, so a curve whose native sell
        assembles at 1,028 bytes was refused with the same verdict as one that assembles at 1,504
        against the 1,232-byte limit. The first is exitable and was turned away; the second is the
        trap this guard exists for. Measured minutes later, Quant built and fit on 3 of 3 attempts
        in 0.6 s each, so the entry-time failure was the build not completing — a cooled-down
        Jupiter host or a spent request budget inside the entry path — and not the coin.
        """
        from sniper.execution.solana_swap_executor import _payload_bytes
        from sniper.execution.submission_wiring import SOLANA_PACKET_LIMIT
        payload = ((built or {}).get("_native_swap") or {}).get("swapTransaction")
        size = _payload_bytes(payload)
        if size is None or size <= 0:
            return False, "incomplete"
        if size > SOLANA_PACKET_LIMIT:
            return False, "too_big:%d>%d" % (size, SOLANA_PACKET_LIMIT)
        return True, "fits:%d" % size

    def _native_exit_available(mint, amount_atomic, sell_state=None):
        """Whether the native pump.fun sell BUILDS for this curve at the planned size: the curve is
        discoverable, the curve leg and the payout->USDC tail both quote, and the transaction
        assembles. Not simulated (the wallet holds none of the coin before the buy).

        With `sell_state` (the desk rung: desk_native_curve.post_buy_sell_state, the curve AFTER our buy)
        the SOL curve is probed by Codex's native_curve_sell on that state: no curve re-read, and a coin
        Jupiter has not indexed is no longer refused for that alone (2026-10-23). Without it (other
        lanes) the probe is unchanged."""
        from sniper.execution.pump_native_exit import build_native_exit, native_curve_sell
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER

        def _once():
            rpc = lambda method, params: ((_rpc_call(rpc_url, method, params, http) or (None, {}))[1] or {}).get("result")  # noqa: E731
            jup = lambda method, path, body=None: _jup_call(method, path, body=body, http=http)  # noqa: E731
            if sell_state is not None:
                built = native_curve_sell(mint, int(amount_atomic), MEMECOIN_EXIT_SLIPPAGE_LADDER[-1], user=pubkey,
                                          rpc=rpc, jup_call=jup, simulate=False, state=sell_state)
            else:
                built = build_native_exit(mint, int(amount_atomic), MEMECOIN_EXIT_SLIPPAGE_LADDER[-1], user=pubkey,
                                          rpc=rpc, jup_call=jup, simulate=False)
            return _native_exit_fits_packet(built)

        ok, reason = _once()
        # ONE RETRY, AND ONLY FOR AN INCOMPLETE BUILD (2026-09-18). A packet that measured too big
        # will measure too big again, so retrying it spends a request to learn nothing. A build
        # that produced no payload is the transient case this guard kept mistaking for the trap,
        # and it is the same 200 ms single retry the funded Solana quote path already uses for
        # `rpc_transport` and `provider_timeout`.
        if not ok and reason == "incomplete":
            import time as _t
            _t.sleep(0.2)
            ok, retried = _once()
            # KEEP THE RETRY'S OWN MEASUREMENT (EX4, 2026-09-20). The retry can complete the build
            # and measure it real -- "fits:N" (ok) or "too_big:N>M" (still refused, but MEASURED,
            # not incomplete). Only a SECOND "incomplete" is genuinely unmeasured; overwriting a
            # real "too_big" with the generic incomplete_after_retry hid the actual reason (an
            # oversized transaction) behind the label for the transient case the retry exists to
            # cure.
            reason = retried if retried != "incomplete" else "incomplete_after_retry"
        return ok, reason

    def _exit_quote(mint, tokens_human, slip, max_accounts=None, atomic=None, position_id=None,
                    native_requested=False):
        # size the sell from the EXACT raw on-chain balance (never uiAmount*10^dec: memecoin balances
        # exceed 2^53 and the float round-trip mis-sizes the sell -> revert/false-stuck or dust).
        # `atomic` is that same raw balance when the executor's rung already read it from the same
        # getTokenAccountsByOwner response (token_snapshot_fn, 2026-09-28); then it is not re-read.
        if atomic is None:
            atomic = token_balance_raw(pubkey, mint, rpc_url, http=http, ata_fallback=True)
        if atomic <= 0:
            raise TokenBalanceReadError("account_balance_unproved")
        quote, calls = _recorded_calls(http or _http, lambda recorded: (
            jupiter_exit_quote(mint, atomic, slip, http=recorded, max_accounts=max_accounts)
            if max_accounts else jupiter_exit_quote(mint, atomic, slip, http=recorded)))
        # A CURVE QUOTED IN SOMETHING JUPITER CANNOT BUILD A SELL FOR (fries 55293, 2026-09-09):
        # Jupiter quotes it and then fails at an ATA create for the quote asset. Same predicate the
        # entry gate refuses on; here it routes the exit to the native pump.fun sell_v2 composite,
        # which is simulated before it is handed back and returns None when the simulation fails,
        # so the rung behaves exactly like a Jupiter build failure.
        #
        # EVERY NON-SETTLEMENT PAYOUT GOES NATIVE FIRST (2026-09-13). bee73cdb let unrestricted
        # payouts (PUMP) through the entry gate and, with the same predicate, sent their exits to
        # Jupiter. {CAT} (pos 56745) then failed its take-profit three times: Jupiter's routes
        # through the hooked payout built at 1,446 and 1,290 bytes against the 1,232-byte packet
        # limit and the send seam refused them. So: native sell_v2 first for any Pump.fun hop that
        # pays neither SOL nor USDC (the pre-09-12 exit routing); a restricted payout with no
        # native sell stays unsellable this rung (fries); an unrestricted one falls back to a
        # Jupiter route capped at MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS so it fits a packet.
        from sniper.execution.solana_swap_executor import unbuildable_curve_quote_asset
        payout = unbuildable_curve_quote_asset(quote)
        if payout:
            from sniper.execution.exec_tiering import exit_tier_plan
            from sniper.execution.pump_native_exit import build_native_exit
            from sniper.settings import memecoin as _cfg
            # THE NATIVE EXIT PRICES WITH THE OTHER EXITS (2026-09-27). The exit_tier_plan fee is
            # position-independent. 0 means the exit fee is off, and the native build then keeps
            # its own default rather than signing with no priority.
            _fee = exit_tier_plan({}, (), chain="solana", cfg=_cfg)["priority_fee_microlamports"]
            native = build_native_exit(
                mint, atomic, slip, user=pubkey,
                rpc=lambda method, params: ((_rpc_call(rpc_url, method, params, http) or (None, {}))[1] or {}).get("result"),
                jup_call=lambda method, path, body=None: _jup_call(method, path, body=body, http=http),
                **({"priority_fee_microlamports": _fee} if _fee > 0 else {}))
            # UNPACK, NEVER COERCE (2026-09-18). `_native_exit_fits_packet` returns (ok, reason)
            # so the entry guard can tell an incomplete build from an oversize one. A 2-tuple is
            # always truthy, so `if _native_exit_fits_packet(...)` here would return an OVERSIZE
            # native exit as the sell — the packet limit this rung exists to respect. Caught by
            # tests/test_memecoin_pump_native_exit_seam.py before it reached a release.
            _fits, _ = _native_exit_fits_packet(native)
            if _fits:
                return native
            if unbuildable_curve_quote_asset(quote, payout_restricted):
                logger.warning("exit quote: curve quoted in %s and the native pump.fun build did "
                               "not simulate clean -- no sell this rung (mint=%s)", payout[:8], mint)
                return _select_pumpswap_exit(mint, atomic, slip, None, position_id=position_id)
            from sniper.settings.memecoin import MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS
            cap = int(MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS or 0)
            if cap > 0:
                capped = _quote_with_fallback(mint, USDC_MINT, atomic, int(round(slip * 100)), http,
                                              max_accounts=cap)
                return _select_pumpswap_exit(mint, atomic, slip, capped, position_id=position_id)
        selected = _select_pumpswap_exit(mint, atomic, slip, quote, position_id=position_id)
        refused = _explicit_jupiter_route_refusal(calls)
        if ((selected is None and refused)
                or (native_requested and selected is quote and quote is not None
                    and not quote.get("_venue"))):
            native = _sol_curve_native_exit(mint, atomic, slip)
            if native is not None:
                return native
        return selected

    def _sol_curve_native_exit(mint, atomic, slip):
        from sniper.execution.exec_tiering import exit_tier_plan
        from sniper.execution.pump_native_exit import native_curve_sell
        from sniper.settings import memecoin as _cfg
        try:
            def native_rpc(method, params):
                status, body = _rpc_call(rpc_url if method == "simulateTransaction" else read_url or rpc_url,
                                         method, params, http)
                if status != 200 or not isinstance(body, dict) or body.get("error") is not None:
                    raise RuntimeError("SOL curve exit RPC unavailable")
                return body.get("result")
            fee = exit_tier_plan({}, (), chain="solana", cfg=_cfg)["priority_fee_microlamports"]
            built = native_curve_sell(
                mint, atomic, slip, user=pubkey, rpc=native_rpc,
                jup_call=lambda method, path, body=None: _jup_call(method, path, body=body, http=http),
                **({"priority_fee_microlamports": fee} if fee > 0 else {}))
            if (isinstance(built, dict) and (built.get("_native_swap") or {}).get("simulated") is True
                    and _native_exit_fits_packet(built)[0]):
                return built
        except Exception:
            logger.exception("SOL curve native exit unavailable mint=%s", mint)
        return None

    def _select_pumpswap_exit(mint, atomic, slip, quote, *, position_id=None):
        from sniper.settings import memecoin as _cfg
        if not getattr(_cfg, "MEMECOIN_SOLANA_NATIVE_EXIT", False):
            return quote
        if quote is not None:
            try:
                if native_exit_hint_fn is None or native_exit_hint_fn(mint, atomic, quote) is not True:
                    return quote
            except Exception:
                logger.exception("native exit hint unavailable mint=%s (retaining Jupiter)", mint)
                return quote
        native = _pumpswap_native_exit(mint, atomic, slip, position_id=position_id)
        if native is None:
            return quote
        # Use the executable USDC floor after the tail, not the pool's nominal USD mark.
        # A normal Jupiter route is retained unless it is severely worse than this signed floor.
        try:
            expected = int(quote["outAmount"]) if quote is not None else None
            floor = int(native["otherAmountThreshold"])
            divergence = float(_cfg.MEMECOIN_SOLANA_NATIVE_RUNG_DIVERGENCE_PCT)
            better = expected is None or (floor > 0 and 0 <= divergence < 100
                                          and expected < floor * (1 - divergence / 100))
        except (KeyError, TypeError, ValueError, OverflowError):
            return quote
        return native if better else quote

    def set_native_exit_hint_fn(callback):
        # Configured once by the runner after its mark cache exists, before exit workers start.
        nonlocal native_exit_hint_fn
        native_exit_hint_fn = callback

    def _pumpswap_native_exit(mint, atomic, slip, *, position_id=None):
        from sniper.settings import memecoin as _cfg
        pool = bound_pumpswap_pool(mint, position_id=position_id)
        graduated = False
        if pool is None:
            if graduated_miss_recent(mint):
                return None                      # tried < 10 s ago: still a live curve, no repeat pool read
            pool = graduated_curve_pool(mint, position_id=position_id)
            if pool is None:
                return None
            graduated = True
        from sniper.execution.exec_tiering import exit_tier_plan
        from sniper.execution.pumpswap_sell import build_pumpswap_native_exit
        _fee = exit_tier_plan({}, (), chain="solana", cfg=_cfg)["priority_fee_microlamports"]
        read = read_url or rpc_url
        try:
            def native_rpc(method, params):
                status, body = _rpc_call(rpc_url if method == "simulateTransaction" else read,
                                         method, params, http)
                if status != 200 or not isinstance(body, dict) or body.get("error") is not None:
                    raise RuntimeError("pumpswap native RPC unavailable")
                return body.get("result")
            built = build_pumpswap_native_exit(
                mint, atomic, slip, user=pubkey, pool=pool, rpc=native_rpc,
                jup_call=lambda method, path, body=None: _jup_call(method, path, body=body, http=http),
                fee_bps=int(_cfg.MEMECOIN_SOLANA_NATIVE_EXIT_FEE_BPS),
                **({"priority_fee_microlamports": _fee} if _fee > 0 else {}))
        except Exception:
            logger.exception("pumpswap native exit build failed mint=%s pool=%s", mint, pool)
            built = None
        if graduated:
            if built is None:
                note_graduated_miss(mint)
                logger.warning("native exit: canonical PumpSwap pool absent or unusable (not graduated?) "
                               "mint=%s position=%s pool=%s", mint, position_id, pool)
            else:
                logger.warning("native exit: GRADUATED curve position, native PumpSwap exit built on the canonical "
                               "pool mint=%s position=%s pool=%s", mint, position_id, pool)
        return built

    _preview_inflight = {}
    _preview_inflight_lock = threading.Lock()

    def preview_exit_fn(position):
        """The exact-balance preview, bounded by MEMECOIN_SOLANA_PREVIEW_DEADLINE_S (2026-09-27).

        The preview runs under the funded exit lock (all three sweep call sites), and on 66015 one
        balance read took 12.5 s while the lane held the lock ~39 s. Past the deadline this returns
        a PROVIDER_TIMEOUT (INFRA) failure: the rail holds, the ops streak moves, unsellable misses
        never do. The body keeps running on its daemon thread and its result is dropped; while it
        runs, the same position answers the same failure at once, so a hung provider costs at most
        one thread per open position. exit_fn (sign/send/confirm) is not bounded here."""
        from sniper.settings import memecoin as _cfg
        deadline = float(getattr(_cfg, "MEMECOIN_SOLANA_PREVIEW_DEADLINE_S", 0) or 0)
        if deadline <= 0:
            return _preview_exit_body(position)
        from sniper.analysis.memecoin_quote_failures import QuoteFailure
        from sniper.execution.exit_preview import PreviewResult

        def timed_out():
            return PreviewResult.failed(QuoteFailure.PROVIDER_TIMEOUT,
                                        provider_id="solana:preview_deadline",
                                        provider_group="solana:exact_quote")
        key = position.get("id")
        box = {}

        def run():
            try:
                box["result"] = _preview_exit_body(position)
            except BaseException as exc:            # re-raised on the caller's thread below
                box["error"] = exc
            finally:
                with _preview_inflight_lock:
                    if _preview_inflight.get(key) is worker:
                        del _preview_inflight[key]
        with _preview_inflight_lock:
            running = _preview_inflight.get(key)
            if running is not None and running.is_alive():
                return timed_out()
            worker = threading.Thread(target=run, daemon=True, name="exit-preview-deadline")
            _preview_inflight[key] = worker
        worker.start()
        worker.join(deadline)
        if worker.is_alive():
            from datetime import datetime, timezone
            logger.warning("%s exit preview deadline %.1fs pos=%s mint=%s: no exact quote this tick",
                           datetime.now(timezone.utc).isoformat(), deadline, key, position.get("mint"))
            return timed_out()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _preview_exit_body(position):
        """Quote the exact raw held balance without signing or broadcasting."""
        import math
        from sniper.analysis.memecoin_quote_failures import QuoteFailure
        from sniper.execution.exit_preview import ExitPreview, PreviewResult
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER
        from sniper.util.clock import real_utc_now_iso

        retryable = {QuoteFailure.RPC_TRANSPORT, QuoteFailure.PROVIDER_TIMEOUT}

        def read_balance(url):
            observed = {}
            try:
                amount = token_balance_raw(
                    pubkey, position["mint"], url,
                    http=_tracked_http(http or _http, observed), ata_fallback=True)
            except TokenBalanceReadError:
                # Keep the existing transport retry/failover; malformed data stays infrastructure.
                return 0, (_infra_failure(observed) or QuoteFailure.MALFORMED_RESPONSE), None
            quantity = None
            try:
                # The raw sizing read already carries mint decimals; never fetch them again.
                # the LAST response: the owner index, or the derived-ATA read (getMultipleAccounts rows
                # are bare accounts, null when absent) when the index answered empty
                accounts = observed["response"][1]["result"]["value"]
                if amount == 0 and all(a is None for a in accounts):
                    fill_atomic = _fresh_desk_fill_atomic(position)
                    if fill_atomic is not None:
                        return fill_atomic, None, fill_atomic / 1_000_000.0
                    return 0, QuoteFailure.MALFORMED_RESPONSE, None
                tokens = [a.get("account", a)["data"]["parsed"]["info"]["tokenAmount"] for a in accounts if a is not None]
                decimals = {token["decimals"] for token in tokens}
                dec = next(iter(decimals))
                if (len(decimals) == 1 and type(dec) is int and 0 <= dec <= 255
                        and sum(int(token["amount"]) for token in tokens) == amount):
                    quantity = amount / (10 ** dec)
            except (KeyError, TypeError, ValueError, IndexError, StopIteration, OverflowError):
                pass
            return amount, (_infra_failure(observed) or QuoteFailure.TOKEN_REJECTED), quantity

        import time as _t
        _t0 = _t.monotonic()
        balance_provider = "solana:balance_rpc"
        atomic, balance_failure, input_quantity = read_balance(rpc_url)
        if atomic <= 0 and balance_failure in retryable:
            _sleep(0.2)
            atomic, balance_failure, input_quantity = read_balance(rpc_url)
        # THE DECISION READ FAILS OVER; THE SIGNING READ DOES NOT (2026-09-09). This balance read
        # is the first step of every exact exit quote, it went to ONE custody endpoint, and a
        # failure here returns before Jupiter is asked -- no route row, no miss (the nominal mid
        # was blind too), no log. Position 55147 (BEER): quotes at ~8/s, then 57 s of nothing, then
        # the -35% stop confirmed at -52%. Zero Jupiter requests in the window is the signature.
        # A preview is a DECISION input: pricing the bag off a read endpoint is fine, and the exit
        # that follows still reads the exact balance on custody before it signs. Token failures
        # (TOKEN_REJECTED: the account really holds nothing) never fail over.
        if atomic > 0 and balance_failure is None:
            balance_provider = "solana:entry_fill_qty"
        if atomic <= 0 and balance_failure in INFRA_BALANCE_FAILURES:
            if read_url and read_url != rpc_url:
                atomic2, failure2, quantity2 = read_balance(read_url)
                if atomic2 > 0:
                    atomic, balance_failure, balance_provider = atomic2, None, "solana:balance_read_rpc"
                    input_quantity = quantity2
                else:
                    balance_failure = failure2 or balance_failure
        if atomic <= 0:
            log_sampled(logger.warning, ("exit_preview_balance", position.get("id")),
                        "exit preview: balance read FAILED pos=%s mint=%s failure=%s took=%.1fs -- "
                        "no exact quote this tick", position.get("id"), position.get("mint"),
                        getattr(balance_failure, "value", balance_failure), _t.monotonic() - _t0)
            return PreviewResult.failed(
                balance_failure, provider_id="solana:balance_rpc",
                provider_group="solana:exact_quote")
        quote, failure = _typed_jupiter_exit_quote(
            position["mint"], atomic, MEMECOIN_EXIT_SLIPPAGE_LADDER[-1], http=http,
        )
        if failure in retryable:
            _sleep(0.2)
            quote, failure = _typed_jupiter_exit_quote(
                position["mint"], atomic, MEMECOIN_EXIT_SLIPPAGE_LADDER[-1], http=http,
            )
        quote = _select_pumpswap_exit(position["mint"], atomic, MEMECOIN_EXIT_SLIPPAGE_LADDER[-1],
                                      quote, position_id=position.get("id"))
        venue = quote.get("_venue") if isinstance(quote, dict) else None
        venue = venue if venue == "pumpswap_native" else "jupiter"
        if failure is not None and venue != "pumpswap_native":
            return PreviewResult.failed(
                failure, provider_id="solana:jupiter",
                provider_group="solana:exact_quote")
        try:
            expected = int(quote["outAmount"]) / 1_000_000.0
        except (TypeError, ValueError, KeyError):
            return PreviewResult.failed(
                QuoteFailure.MALFORMED_RESPONSE, provider_id="solana:jupiter",
                provider_group="solana:exact_quote")
        if not math.isfinite(expected) or expected <= 0:
            return PreviewResult.failed(
                QuoteFailure.INSUFFICIENT_LIQUIDITY, provider_id="solana:jupiter",
                provider_group="solana:exact_quote")
        from sniper.execution.exit_preview import quote_evidence
        evidence = quote_evidence(quote, quote_asset=USDC_MINT, quote_asset_decimals=6,
                                  quote_asset_usd=1.0, input_atomic=atomic, venue=venue,
                                  slippage_bps=int(round(MEMECOIN_EXIT_SLIPPAGE_LADDER[-1] * 100)))
        return PreviewResult.succeeded(
            ExitPreview(expected, atomic, venue, real_utc_now_iso(), quote_evidence=evidence,
                        input_quantity=input_quantity),
            provider_id=("solana:exact_quote" if balance_provider == "solana:balance_rpc"
                         else "solana:exact_quote(entry_fill_qty)" if balance_provider == "solana:entry_fill_qty"
                         else "solana:exact_quote(read_balance)"),
            provider_group="solana:exact_quote")

    def _native_shadow_start(mint, usd, attempt_id=None):
        # The native quote shadow for one entry, or None (mode off, or not a pump coin). The
        # executor starts it beside its own quote; the early quote starts it beside ITS quote.
        from sniper.execution import solana_native_entry_quote as _nq
        if _nq.mode() == "off" or not _nq.qualifies(mint):
            return None
        return _nq.NativeQuoteShadow(
            mint, usd, attempt_id=attempt_id,
            rpc=lambda method, params: (_rpc_call(rpc_url, method, params, http)[1] or {}).get("result"))

    # SHADOW OF THE IMPACT CAP (2026-09-28): our own round trip at our clip after every impact
    # refusal, on the capture key and its own thread and DB. None without the capture key.
    try:
        from sniper.services.memecoin_impact_shadow import from_env as _impact_shadow_from_env
        _impact_shadow = _impact_shadow_from_env()
    except Exception:
        logger.warning("impact shadow unavailable", exc_info=True)
        _impact_shadow = None

    def _desk_native_curve_entry(conn, row, mint, usd, now_iso, miss, desk_price_cap, *, trace,
                                 presign_refusal_fn, entry_max_slot, entry_head_fn, entry_min_slot):
        """The desk's third rung (sniper/execution/desk_native_curve.py): the reservation is still held."""
        from sniper.execution.desk_native_curve import desk_curve_entry
        pid = row["id"]
        no_route = "no_route: %s" % (miss or {}).get("detail")

        def refuse(reason, status="aborted", release="quote_unavailable"):
            m.release_pending(conn, pid, release)
            if trace is not None:
                trace.mark_substage("desk_native_curve", refused=reason)
            return {"status": status, "reason": "no quote (%s; native curve: %s)" % (no_route, reason),
                    "quote_miss_code": "no_route", "desk_native_curve": reason}

        if presign_refusal_fn is not None:
            try:
                too_young = presign_refusal_fn()
            except Exception:
                logger.exception("coin age presign check failed for %s; admitting", mint)
                too_young = None
            if too_young:
                m.release_pending(conn, pid, "coin_age")
                return {"status": "rejected", "reason": too_young}
        rpc = lambda method, params: ((_rpc_call(rpc_url, method, params, http) or (None, {}))[1] or {}).get("result")  # noqa: E731
        # NOTHING BEFORE slot_execute_fn MAY LEAVE THE RESERVATION HELD (Gamma P1 on be46be528): a
        # Jupiter timeout on the leg propagates by design, and this runs inside execute_fn's except.
        try:
            # THE SAME DESK FAST SEND AS THE ORDINARY PATH (Codex on 7eacd8ad6): the Sender tip goes into the
            # native message and its validity, so slot_execute_fn's guard checks exactly that tip.
            from sniper.execution.solana_sender_ab import assign_desk_fast_send, desk_fast_send_on
            n_tip, n_arm = None, None
            n_attempt = getattr(trace, "attempt_id", None) or row.get("attempt_id")   # trace first, as ordinary
            if desk_fast_send_on() and not n_attempt and trace is not None:
                trace.mark_substage("desk_fast_send", path="native_curve", arm="untipped_no_attempt_id")
            if desk_fast_send_on() and n_attempt:
                n_arm = assign_desk_fast_send(n_attempt)
                n_arm["provider_ready"] = sender_provider is not None
                if n_arm["provider_ready"]:
                    n_tip = (n_arm["tip_account"], n_arm["sender_tip_lamports"])
                else:
                    n_arm.update(arm="untipped_provider_absent", sender_tip_lamports=0, tip_account=None)
                if trace is not None:
                    trace.mark_substage("desk_fast_send", path="native_curve", **n_arm)
            message, validity = desk_curve_entry(
                mint, usd, desk_price_cap, user=pubkey, rpc=rpc, fee_config_fn=_desk_fee_config, tip=n_tip,
                jup_call=lambda method, path, body=None: _jup_call(method, path, body=body, http=http))
            if message is None:
                return refuse(validity)
            if trace is not None:
                trace.mark_substage("desk_native_curve", **validity["native_curve"],
                                    timing=validity["native_curve_ms"])
            from sniper.execution.desk_native_curve import post_buy_sell_state
            sell_in = validity.pop("_sell_inputs")
            builder = _desk_curve_builder[0] if _desk_curve_builder else None
            configurable = getattr(builder, "_creator_fee_configurable", None)
            probe_started = _monotonic()
            if configurable is None:      # the cached Global read has not landed yet (cold start), not a curve fault
                exit_ok, exit_reason = False, "global_not_ready"
            else:
                exit_ok, exit_reason = _native_exit_available(
                    mint, validity["native_curve"]["min_tokens_out"],
                    sell_state=post_buy_sell_state(mint, sell_in["state"], sell_in["curve_account"],
                                                   validity["native_curve"], sell_in["fee_config"], configurable))
            if trace is not None:
                try:
                    trace.mark_substage("desk_exit_probe", ms=round((_monotonic() - probe_started) * 1000, 1),
                                        ok=bool(exit_ok), reason=str(exit_reason)[:80])
                except Exception:
                    logger.exception("desk exit probe trace failed pending=%s", pid)
        except Exception as exc:
            logger.exception("desk native curve rung failed pending=%s mint=%s", pid, mint)
            return refuse("native curve error: %s" % type(exc).__name__)
        if not exit_ok:
            return refuse("native exit does not build: %s" % exit_reason)
        info = validity["native_curve"]
        # The leg's own price (USDC in / SOL out) values any SOL surplus the receipt shows, as the slot
        # lane's snapshot price does (Codex: without it the surplus credit was skipped).
        sol_usd = (info["usdc_atomic"] / 1e6) / (info["leg_out_lamports"] / 1e9) if info["leg_out_lamports"] else None
        return slot_execute_fn(conn, row, message, validity=validity, now_iso=now_iso, trace=trace,
                               sol_usd=sol_usd, entry_max_slot=entry_max_slot, entry_head_fn=entry_head_fn,
                               entry_min_slot=entry_min_slot)

    def execute_fn(conn, pid, mint, usd, now_iso, impact_cap_pct=None, chase_max_pct=None,
                   trace=None, max_accounts=None, early_quote=None, chase_reference_price=None,
                   chase_reference_note=None, presign_refusal_fn=None, dip_cap=None,
                   mint_read_evidence=None, entry_max_slot=None, entry_head_fn=None,
                   entry_min_slot=None, desk_price_cap=None):
        row = m.pending_row(conn, pid)
        if not row:
            return {"status": "aborted", "reason": "no pending row"}
        slot_guarded = entry_max_slot is not None or entry_head_fn is not None
        crowd_guarded = bool(getattr(presign_refusal_fn, "require_final_check", False))
        refused = entry_window_refusal(entry_max_slot, entry_head_fn, entry_min_slot)
        if refused:
            return refuse_unsigned_entry(conn, pid, refused)

        def check_entry_window():
            # The executor admits presign callback exceptions and labels its refusals coin-age.
            # Keep that policy intact; catch this separate signer refusal at the owning seam.
            reason = entry_window_refusal(entry_max_slot, entry_head_fn, entry_min_slot)
            if reason:
                raise EntrySlotBlocked(reason)

            if crowd_guarded:
                try:reason=presign_refusal_fn()
                except Exception:reason='crowd_presign_unknown'
                if reason:raise EntrySlotBlocked(reason)

        before_sign = check_entry_window if slot_guarded or crowd_guarded else None
        def measure_roundtrip(buy, sell):
            from sniper.execution.solana_roundtrip_measurement import measure_roundtrip as measure
            from sniper.settings import memecoin as _cfg
            return measure(buy, sell, user=pubkey,
                           jup_call=lambda method, path, **kwargs: _jup_call(method, path, http=http, **kwargs),
                           rpc=lambda method, params: _rpc_call(rpc_url, method, params, http)[1],
                           max_transfer_fee_bps=int(_cfg.MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS or 0))
        from sniper.execution import solana_native_entry_quote as _nq
        native_shadow_fn = None
        if max_accounts is None and _nq.mode() != "off" and _nq.qualifies(mint):
            # ONE line per decision: the oversize retry (max_accounts) re-enters execute_entry.
            native_shadow_fn = lambda m_, usd_, pending_: _native_shadow_start(  # noqa: E731
                m_, usd_, (pending_ or {}).get("attempt_id"))
        # NOZOMI ON EVERY LANE (MEMECOIN_NOZOMI_ALL_LANES, 2026-10-09): a listed wallet's attempt
        # splits on the slot lane's rule; arm B signs one Nozomi tip into the built swap. A native-
        # funded reservation is left alone (its fill math reads the SOL it spent).
        sign_fn, nozomi_arm, nozomi_signed = _sign, None, []
        if entry_bids is not None or slot_guarded or crowd_guarded:
            def sign_fn(quote):
                return _sign(quote, trace=trace, before_sign=before_sign,
                             entry_min_slot=entry_min_slot, entry_max_slot=entry_max_slot)
            sign_fn.refusal_reason = _sign.refusal_reason
            sign_fn.sign_timing = _sign.sign_timing
        if nozomi_provider is not None and row.get("funding_asset") != "sol":
            from sniper.execution.solana_sender_ab import nozomi_entry_arm
            nozomi_arm = nozomi_entry_arm(getattr(trace, "attempt_id", None), row.get("source_user_id"),
                                         tip_lamports_fn=entry_bids.tip_lamports if entry_bids is not None else None)
        if nozomi_arm is not None:
            if trace is not None:
                trace.mark_substage("nozomi_ab", **nozomi_arm)
            tip = ((nozomi_arm["tip_account"], nozomi_arm["tip_lamports"])
                   if nozomi_arm["arm"] == "B" else None)

            def fallback(reason):
                if trace is not None:
                    trace.mark_substage("nozomi_ab_fallback", sent="untipped", reason=str(reason)[:80])

            def sign_fn(quote):
                out = _sign(quote, tip=tip, tip_fallback=fallback, trace=trace,
                            before_sign=before_sign, entry_min_slot=entry_min_slot,
                            entry_max_slot=entry_max_slot)
                if out:
                    nozomi_signed.append(out)
                return out
            sign_fn.refusal_reason = _sign.refusal_reason
            sign_fn.sign_timing = _sign.sign_timing
        desk_arm = None
        _ordinary_sign_fn = sign_fn
        try:        # the desk tip prep can only ADD a tip; any fault here signs today's untipped bytes
            if (desk_price_cap is not None and nozomi_arm is None and row.get("funding_asset") != "sol"):
                from sniper.execution.solana_sender_ab import assign_desk_fast_send, desk_fast_send_on
                if desk_fast_send_on() and getattr(trace, "attempt_id", None):
                    desk_arm = assign_desk_fast_send(trace.attempt_id)
                    ready = sender_provider is not None
                    desk_arm["provider_ready"] = ready
                    if not ready:   # no Sender provider: a tip would be paid for nothing, so none is signed
                        desk_arm.update(arm="untipped_provider_absent", sender_tip_lamports=0, tip_account=None)
                    if trace is not None:
                        try:        # a trace sink failure must never leave the reservation held
                            trace.mark_substage("desk_fast_send", **desk_arm)
                        except Exception:
                            logger.exception("desk fast send trace failed pending=%s", pid)
                    if ready:
                        d_tip = (desk_arm["tip_account"], desk_arm["sender_tip_lamports"])
                        d_guard = (desk_arm["sender_tip_lamports"], 0, True)

                        def d_fallback(reason):
                            if trace is not None:
                                trace.mark_substage("desk_fast_send_fallback", sent="untipped", reason=str(reason)[:80])

                        def sign_fn(quote):
                            return _sign(quote, tip=d_tip, tip_fallback=d_fallback, tip_guard=d_guard, trace=trace,
                                         before_sign=before_sign, entry_min_slot=entry_min_slot,
                                         entry_max_slot=entry_max_slot)
                        sign_fn.refusal_reason = _sign.refusal_reason
                        sign_fn.sign_timing = _sign.sign_timing
        except Exception:
            logger.exception("desk fast send prep failed pending=%s; signing untipped", pid)
            sign_fn = _ordinary_sign_fn
            if trace is not None:   # the trace must not keep a tipped arm for an untipped send
                try:
                    trace.mark_substage("desk_fast_send", arm="untipped_prep_fault")
                except Exception:
                    pass
        fill_receipt = None

        def receipt_fill_fn(sig, target_mint, maximum):
            nonlocal fill_receipt
            fill_receipt = _entry_receipt(sig)
            receipt_fill_fn.last_served = fill_receipt is not None    # names the miss in the executor's log
            return receipt_entry_fill(
                fill_receipt, signature=sig, owner=pubkey, mint=target_mint, maximum=maximum)

        try:
            result = execute_entry(
                conn, pending_id=pid, mint=mint, usd_amount=usd, strategy=row["strategy"],
                source_user_id=row["source_user_id"], now_iso=now_iso,
                quote_fn=(_desk_entry_quote(desk_price_cap) if desk_price_cap is not None else _entry_quote),
                quote_miss_fn=_entry_quotes.miss,
                reverse_quote_fn=_entry_reverse_quote, sign_fn=sign_fn,
                send_fn=_send_fn_for(conn, pid, action="entry", owner_kind="reservation", persist=True,
                    entry_max_slot=entry_max_slot, entry_head_fn=entry_head_fn),
                confirm_fn=_confirm,
                usdc_fn=usdc_fn, token_fn=entry_token_fn, impact_cap_pct=impact_cap_pct,
                chase_max_pct=chase_max_pct, pending=row, trace=trace,
                chase_reference_price=chase_reference_price, chase_reference_note=chase_reference_note,
                presign_refusal_fn=presign_refusal_fn, dip_cap=dip_cap, desk_price_cap=desk_price_cap,
                max_accounts=max_accounts, receipt_fill_fn=receipt_fill_fn,
                receipt_first=injected_token_fn is None,
                mint_read_evidence=mint_read_evidence,
                roundtrip_measure_fn=measure_roundtrip,
                quote_asset_restricted_fn=payout_restricted,
                curve_state_fn=_curve_state, native_exit_available_fn=_native_exit_available,
                native_shadow_fn=native_shadow_fn,
                impact_shadow_fn=(_impact_shadow.submit if _impact_shadow is not None else None),
                # The oversize retry re-quotes with max_accounts and never takes the early quote.
                early_quote=(early_quote if max_accounts is None else None))
        except EntrySlotBlocked as exc:
            return refuse_unsigned_entry(conn, pid, str(exc))
        except _DeskNoRoute as exc:
            if crowd_guarded:return refuse_unsigned_entry(conn, pid, "crowd_native_fallback_unguarded")
            return _desk_native_curve_entry(conn, row, mint, usd, now_iso, exc.args[0], desk_price_cap,
                                            trace=trace, presign_refusal_fn=presign_refusal_fn,
                                            entry_max_slot=entry_max_slot, entry_head_fn=entry_head_fn,
                                            entry_min_slot=entry_min_slot)
        except TokenBalanceReadError as exc:
            logger.warning("entry token balance unreadable pending=%s mint=%s failure=%s",
                           pid, mint, exc.failure)
            return refuse_unsigned_entry(conn, pid, "token_balance_unreadable")
        if trace is not None and isinstance(result, dict) and result.get("status") == "filled":
            # This is the receipt the existing fill read used; never read on a balance-only fill.
            try:
                from sniper.services.memecoin_execution_observer import receipt_execution_evidence
                evidence = receipt_execution_evidence(
                    fill_receipt, custody_pubkey=pubkey, mint=mint, payment_asset="usdc")
                if evidence.get("state") == "verified" and evidence.get("tx_sig") != result.get("sig"):
                    evidence = {"state": "unknown", "reason": "receipt_signature_mismatch"}
            except Exception:
                evidence = {"state": "unknown", "reason": "receipt_extraction_failed"}
                logger.exception("ordinary entry receipt evidence read failed pending=%s", pid)
            try:
                trace.mark_substage("receipt_execution_evidence", duration_ms=0.0, evidence=evidence)
            except Exception:
                logger.exception("ordinary entry receipt evidence trace failed pending=%s", pid)
        # A venue that refused this buy ON CHAIN is excluded for the coin, and the result says so:
        # the retry lane re-quotes around it (see exclude_failed_venue). A slippage revert is not
        # a venue refusal and keeps its own retry path.
        if (isinstance(result, dict) and result.get("status") == "failed"
                and "slippage" not in str(result.get("reason") or "").lower()):
            try:
                label = exclude_failed_venue(mint, result.get("failed_program"), http=http,
                                             program_error=result.get("program_error"))
            except Exception:
                logger.exception("venue exclusion failed for %s", mint)
                label = None
            if label:
                result["venue_excluded"] = label
        if nozomi_signed and trace is not None:
            # After the send, never before: the last signed bytes are the ones the transport got
            # (an oversize retry signs again through the same sign_fn).
            from sniper.execution.solana_sender_ab import (
                _NOZOMI_SET, nozomi_send_trace, payload_tip)
            signed_b64, signed_sig = nozomi_signed[-1][:2]
            frozen_tip = nozomi_arm['tip_lamports']
            if payload_tip(signed_b64, _NOZOMI_SET, frozen_tip, frozen_tip):
                try:
                    nozomi_send_trace(trace, nozomi_provider, signed_sig, signed_b64,
                                      frozen_tip)
                except Exception:
                    logger.exception("nozomi all-lanes trace failed pending=%s", pid)
        _receipt_retry.after(result, pid)
        return result

    def recover_confirmed_entry_fn(conn, pending, now_iso):
        receipt = _entry_receipt(pending.get("tx_sig"))
        return recover_confirmed_entry(conn, pending, receipt, owner=pubkey, now_iso=now_iso)

    # A SIGNED ANOMALY (confirmed, receipt unreadable) IS RE-READ AT +5/+15/+30 s on the retry's own
    # thread and ledger connection, instead of waiting >=90 s for the reconcile pass (Alpha GO G1).
    from sniper.persistence.db_core import get_connection as _retry_connection
    from sniper.services.memecoin_receipt_retry import ReceiptRetry
    _receipt_retry = ReceiptRetry(recover_fn=recover_confirmed_entry_fn,
                                  conn_factory=_retry_connection, now_fn=_now_fn)

    def _dust_hold(conn, exit_reason, now_iso, floor_usd_fn):
        """The executor's dust-route hold (memecoin_exit_dust_guard), on this exit's own floor."""
        from sniper.services import memecoin_exit_dust_guard as dust

        def hold(intent, quote_payload, pos, sell_qty):
            try:
                floor = floor_usd_fn(intent, quote_payload)
            except Exception:
                floor = None
            return dust.hold_reason(conn, pos, exit_reason=exit_reason, floor_usd=floor,
                                    sell_qty=sell_qty, now_iso=now_iso,
                                    quote_payload=quote_payload)
        return hold

    def exit_fn(conn, position, exit_reason, now_iso):
        import time
        from sniper.execution.exec_tiering import exit_tier_plan
        from sniper.settings import memecoin as cfg
        # A manual operator close wants OUT — use the wider manual ladder so it lands on the first
        # rung instead of walking 3->10->25 (each a full quote+build+sign+send+confirm) on a thin
        # pool. Automated exits keep the price-disciplined default.
        _ladder = (cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL
                   if exit_reason == "manual" and getattr(cfg, "MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL", None)
                   else cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER)
        plan = exit_tier_plan(position, _ladder, chain="solana", cfg=cfg)
        # Exit-floor sanity (UNIFROG 27097). Solana exits settle in USDC, so the signed
        # min_output_atomic IS 6-decimal USD -- no price lookup, unlike the EVM path whose quote
        # asset is WETH. The reference values the quantity ACTUALLY being sold at the newest
        # NON-quarantined tape mark: outside the quote being signed, and immune to a tick the
        # collapse guard already rejected. Both seams return None on doubt and the check then
        # allows the exit, so a missing reference can never strand a funded position.
        def _floor_usd_fn(intent, _quote_payload):
            try:
                atomic = int(getattr(intent, "min_output_atomic", 0) or 0)
                return (atomic / 1_000_000.0) if atomic > 0 else None
            except (TypeError, ValueError):
                return None

        def _reference_value_usd_fn(pos, sell_quantity):
            try:
                from sniper.services.memecoin_exits import last_accepted_mid
                mid = last_accepted_mid(conn, "solana", pos["mint"], now_iso)
                if not mid or not sell_quantity:
                    return None
                return float(sell_quantity) * float(mid)
            except Exception:
                return None

        from sniper.util.clock import real_utc_now_iso
        def _check_price_quote(fresh, payload, tokens, slip):
            from sniper.services.memecoin_real_runner import confirm_execution_stop, confirm_execution_target, _keep_route_quote
            from sniper.execution.exit_preview import quote_evidence
            try:
                expected = int(payload["outAmount"]) / 1_000_000.0
            except (KeyError, TypeError, ValueError, OverflowError):
                expected = None
            if exit_reason in {"real_tp", "mcap_target"}:
                try:
                    minimum = int(payload["otherAmountThreshold"]) / 1_000_000.0
                except (KeyError, TypeError, ValueError, OverflowError):
                    minimum = None
                return confirm_execution_target(conn, fresh, exit_reason, expected, tokens,
                                                minimum_usdc=minimum)
            _keep_route_quote(
                dict(fresh, open_qty=tokens),
                {"expected_usdc": expected,
                 "quote_evidence": quote_evidence(payload, quote_asset=USDC_MINT,
                                                   quote_asset_decimals=6, quote_asset_usd=1.0,
                                                   venue="jupiter", slippage_bps=int(round(slip * 100)))},
                "execution_quote",
            )
            return confirm_execution_stop(
                conn, fresh, expected, tokens,
                current_mcap=position.get("_exit_current_mcap"),
                bag_unknown=bool(position.get("_exit_bag_unknown")),
                confirmed_custom_stop=bool(position.get("_exit_custom_stop_confirmed")),
            )

        started = time.monotonic()
        fallback = {}
        def _request_native():
            fallback["native"] = True
        def _sign_exit(quote, before_sign=None):
            return _exit_sign(quote, plan, before_sign, on_build_failure=_request_native)
        _sign_exit.sign_timing = take_sign_timing
        def _position_quote(mint, tokens, slip, max_accounts=None, atomic=None):
            return _exit_quote(mint, tokens, slip, max_accounts=max_accounts, atomic=atomic,
                               position_id=position["id"],
                               native_requested=fallback.pop("native", False))
        result = execute_exit(
            conn, position, exit_reason, now_iso,
            quote_fn=_position_quote,
            sign_fn=_sign_exit,
            # One response for the rung's human and raw balance. An injected test token_fn keeps
            # the old reads, so a test seam stays authoritative for every balance.
            token_snapshot_fn=(None if injected_token_fn is not None else
                               lambda mint: token_balance_snapshot(pubkey, mint, rpc_url,
                                                                   http=http, ata_fallback=True)),
            send_fn=_send_fn_for(conn, position["id"], action="exit",
                                 owner_kind="close_claim", persist=False),
            confirm_fn=_confirm, usdc_fn=usdc_fn, usdc_checked_fn=usdc_checked_fn,
            receipt_proceeds_fn=_receipt_proceeds,
            receipt_first=injected_token_fn is None, receipt_fill_fn=_exit_receipt_fill,
            token_balance_fn=token_fn,
            slippage_ladder=plan["slippage_ladder"],
            decision_state_id=position.get("_engine_decision_state_id"),
            decision_state_id_fn=position.get("_engine_current_state_id_fn"),
            floor_usd_fn=_floor_usd_fn, reference_value_usd_fn=_reference_value_usd_fn,
            dust_hold_fn=_dust_hold(conn, exit_reason, now_iso, _floor_usd_fn),
            now_fn=real_utc_now_iso,
            quote_check_fn=(_check_price_quote
                            if exit_reason in {"b_stop", "real_tp", "mcap_target"} else None))
        try:
            m.record_exit_execution(
                conn, position_id=position["id"], network_id="solana",
                exit_reason=exit_reason, attempted_at=now_iso,
                status=result.get("status") or "unknown", sig=result.get("sig"),
                total_ms=(time.monotonic() - started) * 1000.0,
                priority_fee_microlamports=plan["priority_fee_microlamports"],
                depth_first_rung_pct=(plan["slippage_ladder"][0]
                                      if plan["slippage_ladder"] else None),
                tier_variant=plan["tier_variant"],
                # The CAUSE, not just the status. `reason` is the balance/route refusal,
                # `failure_type` the exhausted-ladder one -- both write `stuck` and only
                # this separates them.
                failure_type=(result.get("reason") or result.get("failure_type")))
        except Exception:
            # The write that RECORDS the cause could itself vanish silently, one layer above the
            # discard `0d0f258f` fixed. Audit-only: the sale already happened and `result` is
            # returned either way, so this must never raise -- but it must never be invisible.
            logger.exception("exit attempt audit write failed pos=%s", position.get("id"))
        return result

    def derisk_fn(conn, position, fraction, now_iso, *, source_request=None):
        fallback = {}
        def _request_native():
            fallback["native"] = True
        # partial sell: size the quote as fraction * the EXACT raw balance (same 2^53-safe rule
        # as the full exit); the executor records the fill and one-shot-marks the position.
        def _partial_quote(mint, tokens_human, slip, max_accounts=None, atomic=None):
            if source_request is not None:
                if type(atomic) is not int:
                    return None
                part = atomic
            else:
                atomic = token_balance_raw(pubkey, mint, rpc_url, http=http, ata_fallback=True)
                part = int(atomic * float(fraction))
            if part <= 0:
                return None
            return _exit_quote(mint, tokens_human, slip, max_accounts=max_accounts, atomic=part,
                               position_id=position["id"],
                               native_requested=fallback.pop("native", False))
        import time
        from sniper.execution.exec_tiering import exit_tier_plan
        from sniper.settings import memecoin as cfg
        plan = exit_tier_plan(position, cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER,
                              chain="solana", cfg=cfg)
        # Exit-floor sanity (UNIFROG 27097). Solana exits settle in USDC, so the signed
        # min_output_atomic IS 6-decimal USD -- no price lookup, unlike the EVM path whose quote
        # asset is WETH. The reference values the quantity ACTUALLY being sold at the newest
        # NON-quarantined tape mark: outside the quote being signed, and immune to a tick the
        # collapse guard already rejected. Both seams return None on doubt and the check then
        # allows the exit, so a missing reference can never strand a funded position.
        def _floor_usd_fn(intent, _quote_payload):
            try:
                atomic = int(getattr(intent, "min_output_atomic", 0) or 0)
                return (atomic / 1_000_000.0) if atomic > 0 else None
            except (TypeError, ValueError):
                return None

        def _reference_value_usd_fn(pos, sell_quantity):
            try:
                from sniper.services.memecoin_exits import last_accepted_mid
                mid = last_accepted_mid(conn, "solana", pos["mint"], now_iso)
                if not mid or not sell_quantity:
                    return None
                return float(sell_quantity) * float(mid)
            except Exception:
                return None

        started = time.monotonic()
        result = execute_partial_exit(
            conn, position, fraction, now_iso,
            quote_fn=_partial_quote,
            sign_fn=lambda quote: _exit_sign(quote, plan, on_build_failure=_request_native),
            send_fn=_send_fn_for(conn, position["id"], action="partial_exit",
                                 owner_kind="derisk_claim", persist=False),
            confirm_fn=_confirm, usdc_fn=usdc_fn, usdc_checked_fn=usdc_checked_fn,
            receipt_first=injected_token_fn is None,
            receipt_fill_fn=source_partial_receipt_fn if source_request is not None else _exit_receipt_fill,
            token_balance_fn=token_fn,
            slippage_ladder=plan["slippage_ladder"],
            decision_state_id=position.get("_engine_decision_state_id"),
            decision_state_id_fn=position.get("_engine_current_state_id_fn"),
            floor_usd_fn=_floor_usd_fn, reference_value_usd_fn=_reference_value_usd_fn,
            dust_hold_fn=_dust_hold(conn, "source_partial" if source_request else "derisk", now_iso, _floor_usd_fn),
            **({"source_request": source_request,
                "token_snapshot_fn": lambda mint: token_balance_snapshot(pubkey, mint, rpc_url, http=http, ata_fallback=True)} if source_request is not None else {}))
        try:
            m.record_exit_execution(
                conn, position_id=position["id"], network_id="solana", exit_reason="source_partial" if source_request else "derisk",
                attempted_at=now_iso, status=result.get("status") or "unknown",
                sig=result.get("sig"), total_ms=(time.monotonic() - started) * 1000.0,
                priority_fee_microlamports=plan["priority_fee_microlamports"],
                depth_first_rung_pct=(plan["slippage_ladder"][0]
                                      if plan["slippage_ladder"] else None),
                tier_variant=plan["tier_variant"],
                # The CAUSE, not just the status. `reason` is the balance/route refusal,
                # `failure_type` the exhausted-ladder one -- both write `stuck` and only
                # this separates them.
                failure_type=(result.get("reason") or result.get("failure_type")))
        except Exception:
            # Same guarantee as the full exit above: audit-only, never raises, never silent.
            logger.exception("derisk attempt audit write failed pos=%s", position.get("id"))
        return result

    def source_partial_fn(conn, position, request, now_iso):
        facts = _json.loads(request['request_json'])
        fraction = int(facts['source_sold_atomic']) / int(facts['source_pre_atomic'])
        return derisk_fn(conn, position, fraction, now_iso, source_request=request)

    def source_partial_receipt_fn(sig, mint, intent=None):
        return _exit_receipt_fill(sig, mint)

    def _slot_resender(send_fn, signed_b64, boundary, boundary_fn):
        """The slot lane's re-send of its IDENTICAL bytes through the TPU sidecar (2026-09-28), or
        None: flag off, no shared transport, or no bound request to repeat."""
        from sniper.settings import memecoin as _mc
        offsets = _mc.memecoin_slot_lane_resend_offsets_ms()
        if not offsets or submission_transport is None or not hasattr(submission_transport, "submit_one"):
            return None
        from sniper.execution.solana_slot_resend import SlotResender
        from sniper.execution.submission_transport import sha256_hex
        return SlotResender(
            lambda request: submission_transport.submit_one("solana:tpu", request),
            lambda: getattr(send_fn, "last_request", None),
            expected_sha256=sha256_hex(signed_b64), offsets_ms=offsets,
            source_slot=(boundary or {}).get("source_slot"),
            dispatch_slot=(boundary or {}).get("head_slot"),
            head_fn=((lambda: (boundary_fn() or {}).get("head_slot")) if boundary_fn else None))

    # The slot lane's Sender A/B provider (2026-10-01), present only when the knob put it in the
    # transport. None keeps slot_execute_fn byte- and time-identical to before.
    sender_provider = (submission_transport.provider("solana:sender")
                       if submission_transport is not None
                       and hasattr(submission_transport, "provider") else None)
    # The Nozomi A/B provider (2026-10-09), present only with its knob and key.
    nozomi_provider = (submission_transport.provider("solana:nozomi")
                       if submission_transport is not None
                       and hasattr(submission_transport, "provider") else None)

    def _exit_receipt_fill(sig, mint):
        from sniper.execution.solana_swap_executor import receipt_exit_fill
        return receipt_exit_fill(_entry_receipt(sig), signature=sig, owner=pubkey, mint=mint)

    def slot_execute_fn(conn, pending, message, *, validity=None, now_iso, trace=None,
                        sol_usd=None, boundary_fn=None, defer_confirmation=False,
                        entry_max_slot=None, entry_head_fn=None, entry_min_slot=None):
        """Sign one prebuilt slot-lane message with the custody key and send/confirm/book it
        through the same identity-binding send seam and receipt reader as execute_fn."""
        from solders.transaction import VersionedTransaction
        from sniper.execution.solana_swap_executor import receipt_sol_entry_fill
        from sniper.execution.solana_slot_executor import (
            execute_prebuilt_entry, receipt_owner_sol_surplus)
        from sniper.execution.submission_transport import sha256_hex
        refused = entry_window_refusal(entry_max_slot, entry_head_fn, entry_min_slot)
        if refused:
            return refuse_unsigned_entry(conn, pending['id'], refused)
        if entry_min_slot is not None:
            from sniper.execution.solana_slot_guard import append_slot_guard
            try:
                message = append_slot_guard(message, entry_min_slot, entry_max_slot)
            except (ValueError, TypeError):
                return refuse_unsigned_entry(conn, pending['id'], 'entry_slot_guard_build_failed')
        # Head checks gate new signatures/sends; signed bounds govern execution, never settlement.
        boundary = boundary_fn() if boundary_fn is not None else {}
        if boundary_fn is not None and trace is not None:
            trace.mark_substage("slot_pre_sign", **boundary)
        budgeted = isinstance(validity, dict) and 'entry_sol_budget' in validity

        def refuse_sol_budget(reason):
            # A stale unsigned caller must never release a signature now owned by settlement.
            released = m.release_unsigned_blocked_entry(conn, pending['id'], reason)
            return {**boundary, 'status': 'aborted' if released else 'unknown', 'reason': reason}

        if budgeted and any(type(validity.get(key, 0)) is not int or validity.get(key, 0) < 0
                            for key in ('sender_tip_lamports', 'nozomi_tip_lamports')):
            return refuse_sol_budget('slot_lane_entry_sol_budget_invalid')
        # SENDER A/B TIP GUARD (2026-10-01), only while the A/B is ready: the message must tip
        # exactly what its arm asked for (0 on arm A, never above the cap). Nothing is signed or sent
        # on a refusal.
        tip_lamports = int((validity or {}).get("sender_tip_lamports") or 0) \
            if isinstance(validity, dict) else 0
        nozomi_tip = int((validity or {}).get("nozomi_tip_lamports") or 0) \
            if isinstance(validity, dict) else 0
        if True:   # UNCONDITIONAL entry fee clamp (operator yes 2026-10-22, option B): every slot-lane entry
            from sniper.execution.solana_sender_ab import cap_entry_priority_fee
            try:
                message, actual_bid = cap_entry_priority_fee(
                    message, tip_lamports=nozomi_tip,
                    crowded_lamports=(validity or {}).get("crowded_priority_lamports")
                    if isinstance(validity, dict) else None)
            except ValueError as exc:
                why = (str(exc)[:60] if str(exc).startswith('sender_ab_tip_guard')
                       else 'entry_bid_compute_budget_unreadable')   # a tip-guard refusal keeps its reason
                if budgeted:
                    return refuse_sol_budget(why)
                m.release_pending(conn, pending['id'], why)
                return {**boundary, 'status': 'aborted', 'reason': why}
            if trace is not None:
                trace.mark_substage('entry_bid_signed', **actual_bid)
        if sender_provider is not None or nozomi_provider is not None or budgeted:
            from sniper.execution.solana_sender_ab import guard_refusal
            refused = guard_refusal(message, tip_lamports, nozomi_tip,
                                    dynamic_nozomi=entry_bids is not None)
            if refused:
                if budgeted:
                    return refuse_sol_budget(refused[:60])
                m.release_pending(conn, pending["id"], refused[:60])
                return {**boundary, "status": "aborted", "reason": refused}
        if budgeted:
            refused, liability = _slot_entry_sol_liability(message, pending, validity)
            if trace is not None:
                trace.mark_substage('slot_entry_sol_liability', **liability,
                                    refusal=refused)
            if refused:
                return refuse_sol_budget(refused)
        refused = entry_window_refusal(entry_max_slot, entry_head_fn, entry_min_slot)
        if refused:
            return {**boundary, **refuse_unsigned_entry(conn, pending['id'], refused)}
        sign_started = time.perf_counter()
        tx = VersionedTransaction(message, [keypair])
        local_sign_ms = (time.perf_counter() - sign_started) * 1000
        if trace is not None:
            try:
                trace.mark_substage("timing_local_sign", duration_ms=local_sign_ms, builder="solders",
                                    timing_basis="local_solders_signature")
            except Exception:
                logger.debug("local signing timing failed", exc_info=True)
            trace.mark("signing", duration_ms=local_sign_ms)
        dispatch_started = identity_started = None

        def observe_transport(phase):
            nonlocal dispatch_started, identity_started
            now = time.perf_counter()
            if phase == "identity_start":
                identity_started = now
                return
            extra = {}
            if phase == "dispatch" and boundary_fn is not None and trace is not None:
                # One existing boundary read; no extra callback on the entry path.
                extra = boundary_fn()
                trace.mark_substage("slot_pre_send", **extra)
                now = time.perf_counter()
            if phase == "dispatch":
                dispatch_started = now
            started = identity_started if phase == "identity_bound" else dispatch_started
            if trace is not None and started is not None:
                trace.mark_substage("slot_transport_" + phase,
                                    duration_ms=(now - started) * 1000,
                                    timing_basis=("durable_identity_commit" if phase == "identity_bound"
                                                  else "transport_seam_invocation_after_identity_binding"),
                                    **extra)

        receipt = successful_receipt_cache(_entry_receipt)   # shared by the fill and the credit
        native = pending.get("funding_asset") == "sol"
        signed_b64 = base64.b64encode(bytes(tx)).decode("ascii")
        # SLOT LANE ONLY (2026-09-26, operator order): the lane signs with a `confirmed`
        # blockhash, so the custody leg's finalized-bank preflight could only answer
        # "blockhash not found"; and the caller-thread audit waited
        # up to 2.3 s for the ledger lock while this lane held the wallet lock. The identity
        # bind (persist=True) still runs before any provider sees the bytes.
        send_fn = _send_fn_for(conn, pending["id"], action="entry", owner_kind="reservation",
                               persist=True, observe_fn=observe_transport, skip_preflight=True,
                               record_async=True, entry_max_slot=entry_max_slot,
                               entry_head_fn=entry_head_fn)
        result = execute_prebuilt_entry(
            conn, pending=pending, signed_b64=signed_b64,
            sig=str(tx.signatures[0]), validity=validity,
            send_fn=send_fn, resender=_slot_resender(send_fn, signed_b64, boundary, boundary_fn),
            confirm_fn=_confirm,
            receipt_fill_fn=(
                (lambda sig, mint, maximum: receipt_sol_entry_fill(
                    receipt(sig), signature=sig, owner=pubkey, mint=mint,
                    maximum_lamports=int(pending["funding_amount_atomic"]),
                    tip_lamports=tip_lamports + nozomi_tip))
                if native else
                (lambda sig, mint, maximum: receipt_entry_fill(
                    receipt(sig), signature=sig, owner=pubkey, mint=mint, maximum=maximum))),
            sol_surplus_fn=(None if native else
                            lambda sig: receipt_owner_sol_surplus(receipt(sig), pubkey)),
            sol_usd=sol_usd, now_iso=now_iso, trace=trace,
            defer_confirmation=defer_confirmation)
        def after_settlement(result):
            _receipt_retry.after(result, pending.get("id"))
            if trace is not None and result.get("status") == "filled":
                # The units the fill consumed, from its own cached receipt (no new call, after the fill):
                # the per-class CU table re-derives from these. A receipt without the field is None.
                try:
                    full_receipt = receipt(str(result.get("sig") or tx.signatures[0])) or {}
                    meta = full_receipt.get("meta") or {}
                    trace.mark_substage("receipt_cu", consumed=meta.get("computeUnitsConsumed"),
                                        fee_lamports=meta.get("fee"))      # base + priority, as paid
                    from sniper.services.memecoin_execution_observer import receipt_execution_evidence
                    native_accounting = (receipt_sol_entry_fill(
                        full_receipt, signature=str(tx.signatures[0]), owner=pubkey, mint=pending["mint"],
                        maximum_lamports=int(pending["funding_amount_atomic"]),
                        tip_lamports=tip_lamports + nozomi_tip) if native else None)
                    evidence = receipt_execution_evidence(
                        full_receipt, custody_pubkey=pubkey, mint=pending["mint"],
                        payment_asset='sol' if native else 'usdc', native_accounting=native_accounting)
                except Exception:
                    evidence = {'state': 'unknown', 'reason': 'receipt_extraction_failed'}
                    logger.exception("slot lane receipt evidence read failed pending=%s", pending.get("id"))
                try:
                    trace.mark_substage("receipt_execution_evidence", duration_ms=0.0, evidence=evidence)
                except Exception:
                    logger.exception("slot lane receipt evidence trace failed pending=%s", pending.get("id"))
                source_frame = (validity or {}).get("pump_source_frame") if isinstance(validity, dict) else None
                if source_frame is not None:
                    try:
                        from sniper.execution.entry_attribution import curve_receipt_event
                        trace.mark_substage("slot_curve_frames", source=source_frame,
                                            landing=curve_receipt_event(
                                                full_receipt, owner=pubkey, mint=pending["mint"]))
                    except Exception:
                        logger.debug("slot curve frame trace failed", exc_info=True)
            residue_mint = (validity or {}).get("residue_quote_mint") if isinstance(validity, dict) else None
            if residue_mint and result.get("status") == "filled":
                # An exotic-quote slot buy (memecoin_slot_lane exotic quotes): the quote asset its leg left
                # with the owner, from this fill's own receipt (the cached read, no new call). None =
                # the receipt does not show the owner's account for that mint.
                from sniper.services.memecoin_quote_residue import residue_from_receipt
                try:
                    result["quote_residue_atomic"] = residue_from_receipt(
                        (receipt(str(result.get("sig") or tx.signatures[0])) or {}).get("meta"),
                        pubkey, residue_mint)
                except Exception:
                    logger.exception("slot lane quote residue read failed pending=%s", pending.get("id"))
                    result["quote_residue_atomic"] = None
            if sender_provider is not None and trace is not None and tip_lamports:
                # After the send, never before: Sender's own answer for these bytes, and whether it sent
                # the bytes we signed (the ones the TPU path got).
                try:
                    outcome = sender_provider.outcome(str(tx.signatures[0])) or {"sender_state": "not_sent"}
                    sha = outcome.pop("sender_sha256", None)
                    trace.mark_substage("sender_ab_send", tip_lamports=tip_lamports, **outcome,
                                        sender_payload_sha_match=(
                                            None if sha is None else sha == sha256_hex(signed_b64)))
                except Exception:
                    logger.exception("sender A/B trace failed pending=%s", pending.get("id"))
            if nozomi_provider is not None and trace is not None and nozomi_tip:
                # After the send: Nozomi's own answer, whether it posted the bytes we signed and named our
                # signature, and when its POST started on the trace's clock (Gamma's instruments).
                try:
                    sig = str(tx.signatures[0])
                    outcome = nozomi_provider.outcome(sig) or {"nozomi_state": "not_sent"}
                    sha = outcome.pop("nozomi_sha256", None)
                    started = outcome.pop("nozomi_post_started", None)
                    finished = outcome.pop("nozomi_post_finished", None)
                    returned = outcome.get("nozomi_returned_sig")
                    trace.mark_substage(
                        "nozomi_ab_send", tip_lamports=nozomi_tip, **outcome,
                        nozomi_payload_sha_match=None if sha is None else sha == sha256_hex(signed_b64),
                        nozomi_sig_match=None if returned is None else returned == sig,
                        nozomi_post_finish_ms=(None if finished is None
                                               else round(trace.elapsed_ms_at(finished), 3)),
                        nozomi_post_start_ms=(None if started is None
                                              else round(trace.elapsed_ms_at(started), 3)))
                except Exception:
                    logger.exception("nozomi A/B trace failed pending=%s", pending.get("id"))
            return {**boundary, **result,
                    "head_minus_s": (boundary["head_slot"] - boundary["source_slot"]
                                     if boundary.get("head_slot") is not None else None)}

        if result.get("_settlement") is not None:
            deferred = result["_settlement"]
            settle = deferred.settle
            deferred.settle = lambda worker_conn: after_settlement(settle(worker_conn))
            from sniper.execution.solana_swap_executor import signed_priority_fee_lamports
            priority = signed_priority_fee_lamports(signed_b64)
            if priority is None:
                return deferred.resolve(conn)  # no proven fee ceiling: retain serial accounting
            gas = 5000 * len(tx.signatures) + priority
            if not native:
                from sniper.services.memecoin_slot_lane import NATIVE_ATA_RENT_RESERVE_LAMPORTS
                ata_program = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
                held_tip = int(pending.get('funding_overhead_atomic') or 0)
                gas += max(0, tip_lamports + nozomi_tip - held_tip) + NATIVE_ATA_RENT_RESERVE_LAMPORTS * sum(
                    str(message.account_keys[ix.program_id_index]) == ata_program
                    for ix in message.instructions)
            result['_gas_debit_lamports'] = gas
            return result
        return after_settlement(result)

    return {"execute_fn": execute_fn, "exit_fn": exit_fn, "derisk_fn": derisk_fn,
            "source_partial_fn": source_partial_fn, "source_partial_receipt_fn": source_partial_receipt_fn,
            "source_partial_snapshot_fn": lambda mint: token_balance_snapshot(pubkey, mint, rpc_url, http=http, ata_fallback=True),
            "source_partial_proof_snapshot_fn": lambda mint: source_partial_proof_snapshot(
                pubkey, mint, rpc_url, http=http),
            "slot_execute_fn": slot_execute_fn,
            "entry_slot_guard_ready": True,
            "slot_deferred_ready": injected_token_fn is None,
            # The slot lane tips only when the transport really holds the Sender provider.
            "sender_ab_ready": sender_provider is not None,
            "nozomi_ab_ready": nozomi_provider is not None,
            "preview_exit_fn": preview_exit_fn,
            "set_native_exit_hint_fn": set_native_exit_hint_fn,
            "confirm_fn": _confirm, "confirm_once_fn": _confirm_once,
            "confirm_proof_once_fn": _confirm_proof_once,
            "recover_confirmed_entry_fn": recover_confirmed_entry_fn,
            "receipt_proceeds_fn": _receipt_proceeds, "pubkey": pubkey,
            # The whole parsed receipt, for the quote-residue read on the holdings worker.
            "receipt_fn": _entry_receipt,
            "exit_quote_fn": _exit_quote,
            # The J7 early entry quote (memecoin_early_quote): the executor's own quote seam and
            # native shadow, started at decision start. Read-only; no wallet lock needed.
            "entry_quote_fn": _entry_quote, "native_shadow_start_fn": _native_shadow_start}
