"""Item 5 — EVM custody + balance reads for the real-money memecoin EVM leg (S4-EVM, DISARMED).

secp256k1 keypair gen/load (secret 0600, never in git/DB/logs) and per-chain balance reads over
plain JSON-RPC: native gas (ETH-on-Base / BNB) + ERC-20 (USDC + a memecoin) via eth_call. This is a
SEPARATE wallet from the Solana leg; wallet = bankroll per wallet. RPC http is injectable so the money
path is unit-tested without a live endpoint. NOTHING here signs or submits swaps — that's the EVM
executor (S2/S3-EVM). Requires `eth_account` (installed; pinned in requirements.txt). Fail-CLOSED:
any malformed response -> 0.0 (never a phantom balance that could authorize a trade).
"""
from __future__ import annotations

import contextlib as _contextlib
import json as _json
import logging as _logging
import threading as _threading
import os
import time as _time
import urllib.parse
import urllib.error
import urllib.request

_log = _logging.getLogger("memecoin")

# canonical stablecoin per fomo chain id. Base = native Circle USDC; BSC = Binance-Peg USDC. Ethereum
# (1) is excluded from trading by config but listed for completeness. Extend when a chain is allowlisted.
# The stable LEG per chain, which is not always the same token as `CHAIN_USDC`. BSC's dominant
# stable is Binance-Peg BSC-USD (USDT) at $1.20M of census liquidity; its USDC is a minor pair.
CHAIN_STABLE = {
    "56": "0x55d398326f99059fF775485246999027B3197955",          # BSC-USD (USDT), 18 decimals
}

CHAIN_USDC = {
    "8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Base USDC
    "56": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",     # BSC (Binance-Peg) USDC
    "1": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",       # Ethereum USDC (the V3/V2 connector)
    # Arc (Circle, chain_id 5042, mainnet 2026-09-16): native gas IS the USDC settlement asset, at
    # this fixed address, 6 decimals. Listed for completeness only -- CHAIN_QUOTE["5042"] below
    # always resolves first at every live call site, so this entry is never actually read.
    "5042": "0x3600000000000000000000000000000000000000",
    "57073": "0x2D270e6886d130D724215A266106e6832161EAEd",
}

# the QUOTE token the executor spends on entries / receives on exits, per chain. kind 'usd' means
# 1 token == $1 (a stablecoin; sizing is direct); kind 'eth' means the chain's memecoin pools quote
# in WETH (Robinhood Chain 4663 has ZERO USDC pools, verified 2026-07-10) and USD sizing/accounting
# converts through a live ETH/USD price (fail-closed: unknown price -> no entry sizing, 0.0 bankroll).
# NOTE decimals are per-token truth: Binance-Peg USDC on BSC is 18-dec (the old 6-dec assumption was
# the reason BSC was held out; this map is the fix — BSC still needs its own smoke before allowlisting).
# 8453 QUOTES IN WETH, NOT USDC (2026-09-06). Base's memecoin pools are native/WETH-paired the same
# way 4663's are, so every 4663 path applies unchanged: V4 single-hop on a native pair, the multihop
# cash leg, the WETH -> ETH gas top-up, the ETH/USD sizing read, the preflight WETH entry quote.
# Keeping USDC would need a USDC -> WETH leg on every route and a USDC -> ETH top-up that does not
# exist, and would leave V4 single-hop unusable. Bridged USDC is converted once by
# scripts/memecoin_base_usdc_to_weth.py; `CHAIN_USDC["8453"]` stays, it is still the V2/multihop
# connector and the stable side of a USDC-paired coin.
# `kind: "eth"` means WRAPPED NATIVE, not "the native asset is ether". `usd_symbol` names the asset
# the quote token is actually worth. It is REQUIRED on every wrapped-native chain and there is no
# default: `quote_usd_price` priced WBNB at the live ETH mark for two days because the price read
# was hardcoded to ether while `kind` had already been generalised (2026-09-06 .. 2026-09-08). That
# overstated the BSC bankroll 3.3x and, through `usd_to_quote_atomic`, sent 3.3x too little WBNB
# into every BSC entry. Adding a wrapped-native chain without a symbol now fails closed.
CHAIN_QUOTE = {
    "8453": {"token": "0x4200000000000000000000000000000000000006",   # Base WETH
             "decimals": 18, "kind": "eth", "usd_symbol": "ETH"},
    # 56 QUOTES IN WBNB, NOT USDC (2026-09-06). Same reasoning as Base: the wrapped-native shape is
    # what every 4663 path already speaks, and the census settles the pair distribution — WBNB holds
    # $4.33M (67.9%) of the liquidity on the 20 most recent armed-wallet buys against USDT's $1.20M
    # (18.9%), with nothing else above 2.5%. NOTE the stable here is USDT, not USDC: `CHAIN_USDC`
    # keeps its Binance-Peg USDC entry because other code reads it, but the BSC stable LEG is USDT.
    "56": {"token": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",   # WBNB
           "decimals": 18, "kind": "eth", "usd_symbol": "BNB"},
    # Ethereum mainnet QUOTES IN WETH (2026-09-30, eth-mainnet spec): 73 of the 83 roster coins
    # trade on native-ETH V4 pools, and every V4/V3 native path already speaks wrapped native.
    # WETH proved on chain: SwapRouter02.WETH9() == V2Router02.WETH() == this address.
    "1": {"token": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
          "decimals": 18, "kind": "eth", "usd_symbol": "ETH"},
    "4663": {"token": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",   # Robinhood Chain WETH
             "decimals": 18, "kind": "eth", "usd_symbol": "ETH"},
    # Arc (Circle, chain_id 5042, mainnet 2026-09-16): the native gas-payment asset IS the USDC
    # quote/settlement token -- same address, same balance, unlike every other chain here where
    # gas and quote are genuinely separate holdings. kind 'usd' (1 token == $1) is correct: this
    # is exact on-chain Circle USDC, not a wrapped-native placeholder. Three 18-decimal "USDC"
    # tokens are already live on Arc (arcUSDC, dUSDC, steakUSDC) -- this address, not a symbol
    # match, is what every lookup path must key on. See native_is_quote() below.
    "5042": {"token": "0x3600000000000000000000000000000000000000", "decimals": 6, "kind": "usd"},
    "57073": {"token": "0x4200000000000000000000000000000000000006",
              "decimals": 18, "kind": "eth", "usd_symbol": "ETH"},
}

# Chains whose CHAIN_QUOTE token IS the chain's own native gas-payment asset. DATA, not a
# hardcoded chain check downstream: sniper.services.memecoin_evm_risk reads this to decide
# whether a gas reserve must be carved out of the SAME balance a trade is sized from, before the
# entry clip is computed -- on every other chain here, gas and quote are separate balances and
# the existing post-hoc native-balance check is sufficient by itself.
NATIVE_IS_QUOTE_CHAINS = frozenset({"5042"})


def native_is_quote(chain):
    return str(chain or "").strip() in NATIVE_IS_QUOTE_CHAINS


def native_quote_chains():
    """Chain ids whose quote token is WRAPPED NATIVE (kind 'eth'), so entries unwrap before a swap.

    KEYED ON THE CONFIG, NOT ON A CHAIN LITERAL. The WETH preflight and the confirmed-pre-transaction
    recovery both used `== "4663"`, which was correct while 4663 was the only wrapped-native chain
    and became a SILENTLY SKIPPED SAFETY GATE the moment Base's quote became WETH too (2026-09-06).
    """
    return frozenset(chain for chain, meta in CHAIN_QUOTE.items()
                     if str((meta or {}).get("kind") or "").lower() == "eth")


def is_native_quote_token(token):
    """True when `token` is the wrapped-native quote token of some configured chain."""
    want = str(token or "").strip().lower()
    if not want:
        return False
    return any(str((CHAIN_QUOTE.get(chain) or {}).get("token") or "").lower() == want
               for chain in native_quote_chains())


# display-only: what the bankroll on each chain is denominated in. Kept out of CHAIN_QUOTE so the
# money path's shape stays untouched. Operator surfaces label the cash number with this.
QUOTE_SYMBOL = {"8453": "WETH", "56": "WBNB", "1": "WETH", "4663": "WETH",
                "57073": "WETH"}

# well-known ERC-20 4-byte function selectors (keccak256(sig)[:4]).
SEL_BALANCE_OF = "70a08231"   # balanceOf(address)
SEL_DECIMALS = "313ce567"     # decimals()
SEL_ALLOWANCE = "dd62ed3e"    # allowance(address,address)
SEL_APPROVE = "095ea7b3"      # approve(address,uint256)
SEL_TRANSFER_FROM = "23b872dd" # transferFrom(address,address,uint256)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def _http(method, url, headers=None, body=None, timeout=10):
    h = {"user-agent": _UA, "accept": "application/json", "content-type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method=method,
                                data=body.encode() if isinstance(body, str) else body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (_json.loads(raw) if raw else {}), dict(r.headers)
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx WITH A BODY IS AN ANSWER, NOT AN OUTAGE (2026-09-03). Alchemy's free tier
        # refuses an eth_getLogs span over 10 blocks with HTTP 400 and a JSON-RPC error body
        # ("Under the Free tier plan ..."). Raising here made `_rpc` count that as a transport
        # failure: 95 penalties in one minute, the primary permanently quarantined for reads, and
        # nothing in the log, because the next endpoint served the request. The status is
        # returned so `_rpc` can classify it: 429/5xx stay provider failures, a 4xx JSON-RPC
        # error is "this endpoint refuses this shape" and rotates without a penalty.
        raw = b""
        try:
            raw = exc.read()
            body = _json.loads(raw.decode()) if raw else {}
        except Exception:
            body = {}
        if isinstance(body, dict) and "error" in body and "jsonrpc" in body:
            return exc.code, body, dict(exc.headers or {})
        # Not a JSON-RPC error (e.g. a LI.FI REST body): re-raise for the callers that parse the
        # exception themselves -- with the body RE-READABLE, since it was consumed above.
        import io as _io
        raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.headers, _io.BytesIO(raw)) from None


# --- keypair (eth_account) ---

def generate_keypair_file(path):
    """Create a fresh secp256k1 account and write its private key hex, 0600. Overwrites — the caller
    ensures this is a deliberate new wallet. Returns the LocalAccount."""
    from eth_account import Account
    acct = Account.create()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(_json.dumps({"address": acct.address, "private_key": acct.key.hex()}))
    os.chmod(path, 0o600)
    return acct


def load_keypair(path):
    from eth_account import Account
    with open(path) as f:
        d = _json.load(f)
    return Account.from_key(d["private_key"])


def address_str(acct):
    return acct.address


# --- JSON-RPC balance reads ---

# Idempotent READ methods that a latency hedge may issue twice. eth_sendRawTransaction also flows
# through _rpc (evm_real_seams.py) and must NEVER appear here — a hedged broadcast is a double-send.
# Reads are safe to duplicate: the second answer is discarded, and chain state does not change.
_HEDGEABLE_READS = frozenset({
    "eth_call", "eth_getBalance", "eth_getCode", "eth_blockNumber", "eth_chainId",
    "eth_getTransactionReceipt", "eth_getTransactionCount", "eth_gasPrice",
    "eth_getBlockByNumber", "eth_getLogs",
})
# Measured 2026-08-14 on the Robinhood 4663 primary: bimodal 180ms / 1.0-1.4s, 6/10 probes in the
# slow mode, while the pooled arrowrpc fallback held a stable ~190ms. The hedge fires the next
# verified pool endpoint only after the primary has already been slow for this long, so the fast
# path costs zero extra requests and the slow tail is capped near the fallback's latency.
def _absence_answer(method, answer):
    """True when this answer says "I have not seen it" rather than reporting a fact.

    ONLY `eth_getTransactionReceipt`, deliberately. A receipt is positive evidence and a `null` is
    the absence of it, so the two are not symmetric: a node four blocks behind answers `null`
    honestly and fast, while the node that HAS the receipt answers slower. Under first-answer-wins
    the lagging node decides, and the caller reads a mined transaction as unmined.

    Measured 2026-09-04 on chain 4663: all four `native pre-transaction ambiguous` entries this
    system has ever produced were false -- every unwrap was mined with status 1 while the poll
    reported nothing. The custody endpoint was 4 blocks behind the pool's best at the time.

    `eth_getLogs` has the identical asymmetry (an empty array from a lagging node is not proof of
    no logs) and is deliberately NOT included here: the tape's contract is gaps-never-inventions
    and changing its read semantics needs its own evidence.
    """
    return (method == "eth_getTransactionReceipt"
            and isinstance(answer, dict) and answer.get("result") is None)


# A BACKUP THIS FAR BEHIND MUST NOT RACE (2026-09-04). The hedge picks a winner by SPEED, and a
# stale endpoint is fast precisely because it is answering from old state. `choose` already prices
# staleness into the primary, but the hedge queue filtered on quarantine alone, so a node minutes
# behind could still beat the current one. For a receipt that returns a wrong `null`; for
# `eth_getTransactionCount` or `eth_getBalance` it returns a wrong VALUE, and a stale nonce signs a
# transaction the chain has already consumed. Measured on 4663 this day, over 5 samples 6s apart:
# the chain owner sat at lag 0 while BOTH Alchemy members ran 740-790 blocks (~75s) behind and
# publicnode ran 102-276 behind. Excluding a stale member costs only the hedge -- the sequential
# rotation still reaches it through `choose`, correctly scored -- so correctness wins over latency.
_HEDGE_MAX_STALE_MS = 2000.0


def _too_stale_to_race(_pool, url):
    """True when `url` is measurably behind enough that its speed would be lying."""
    lag = _pool.lag_ms(url)
    return lag is not None and lag > _HEDGE_MAX_STALE_MS


_HEDGE_AFTER_S = 0.4
# Total budget for the hedged read. `_HEDGE_AFTER_S` bounds only the wait BEFORE racing the backup;
# the collection loop below had no deadline at all, so a worker that never completed blocked the
# CALLING thread forever — silently, because nothing raised. That wedged the funded runner for 99s
# on 2026-09-02 (main loop and fast lane both stopped inside 0.4s of each other, no error logged,
# ended only by SIGTERM) and the watchdog then killed it for `sweep_stale`. A per-socket timeout
# does not bound this: it does not cover name resolution and it applies per read, not per exchange.
# Kept ABOVE the 10s socket timeout so an ordinary provider timeout resolves on its own terms and
# only a genuine hang hits this budget.
_HEDGE_TOTAL_S = float(os.getenv("MEMECOIN_EVM_HEDGE_TOTAL_S", "12"))


def _provider_http_failure(status):
    return status == 429 or (isinstance(status, int) and status >= 500)


def _http_failure(status, rpc_url=None):
    """The error for a provider HTTP failure, with the status on it (`http_status`). The message is
    unchanged. One error for a 429 and a 5xx left every caller unable to tell a rate limit from an
    outage (2026-09-30: the dip watch pauses on the first and retries on the second)."""
    error = RuntimeError("EVM RPC provider HTTP failure")
    error.http_status = status
    error.rpc_url = rpc_url
    return error


def _shape_refusal(status, answer):
    """A 4xx (not 429) carrying a JSON-RPC error body: the provider declined THIS request on
    capability or plan limits, not because it is down."""
    return (isinstance(status, int) and 400 <= status < 500 and status != 429
            and isinstance(answer, dict) and "error" in answer)


def _rpc_error_code(answer):
    try:
        return str((answer or {}).get("error", {}).get("code"))
    except Exception:
        return "?"


_SEND_PREFIXES = ("eth_send", "mev_send", "eth_cancelprivate", "eth_sign", "personal_send")


def _is_send_method(method):
    """True for a method that may leave a transaction on the network (eth_sendRawTransaction and
    its Conditional/Sync/Private variants, eth_sendTransaction, bundles, cancels, signing).

    A SEND NEVER ROTATES ON A BUSY OR UNAVAILABLE BODY (Gamma on 014b49edb, 2026-10-07): member A may
    have broadcast before answering '-32603 Internal error' or 'rate limit'; asking member B then
    returns B's 'nonce too low', which reads as a clean rejection (False) and releases a live tx.
    The body is returned as it came, so the caller keeps today's ambiguity (None)."""
    return str(method or "").lower().startswith(_SEND_PREFIXES)


def _endpoint_state_error(answer):
    error = (answer or {}).get("error") if isinstance(answer, dict) else None
    if not isinstance(error, dict):
        return False
    message = str(error.get("message") or "").lower()
    if error.get("code") == 3 or "execution reverted" in message:
        return False                       # a contract failure is not provider unavailability
    return any(word in message for word in (
        "metadata is not found", "temporarily unavailable", "service unavailable",
        "server busy", "try again later", "too many requests", "rate limit",
    ))


def _state_error_reason(answer):
    """The quarantine reason for a provider-busy body, with the provider's words (60 chars,
    whitespace collapsed). 2026-10-07: MEV Blocker sat out on 'endpoint_state_unavailable' and
    neither the log nor the trace said which body tripped it."""
    error = (answer or {}).get("error") if isinstance(answer, dict) else None
    words = " ".join(str((error or {}).get("message") or "").split())[:60]
    return "endpoint_state_unavailable: %s" % words if words else "endpoint_state_unavailable"


def _pool_eligible(method, params):
    """May a SECONDARY endpoint serve this request, or must it pin to the configured primary?

    KEYED ON THE REQUEST SHAPE, NEVER ON A CALL-SITE FLAG. A flag is a thing a call site can
    forget, and the failure it causes here is silent, so the decision has to be derivable from the
    request itself.

    The one method that is not uniform across our 4663 endpoints is `eth_getLogs`. Measured
    2026-08-30 against the same 20-block range:

        official     448 ms -> 1,092 logs
        publicnode    58 ms -> JSON-RPC error -32701 "Please specify an address in your request"

    Our 4663 tickstream scans the WHOLE CHAIN by design (memecoin_tick_stream.py:9), so it sends
    exactly the shape publicnode refuses. `eth_getLogs` is in `_HEDGEABLE_READS` where the first
    answer wins, and a valid JSON-RPC error body is by contract "the endpoint working correctly",
    so a 58 ms refusal would beat a 448 ms correct answer EVERY time and the tick would be
    skipped. The tape's guarantee is gaps, never inventions; this would thin it silently, and
    every downstream study trusts it.

    An ADDRESS-FILTERED getLogs is fine on either endpoint — that is the request publicnode is
    asking for — so the split is exactly "does the filter name an address".

    Fail-safe: an unparseable filter pins to the primary. An unrecognised shape is not a licence.
    """
    if method != "eth_getLogs":
        return True
    try:
        flt = params[0] if params else None
    except (TypeError, IndexError, KeyError):
        return False
    if not isinstance(flt, dict):
        return False
    address = flt.get("address")
    if isinstance(address, str):
        return bool(address.strip())
    if isinstance(address, (list, tuple, set)):
        return any(str(a).strip() for a in address)
    return False


# A HOST THAT IS NOT A CHAIN-WIDE LOG STORE (2026-10-07, Alpha). ethereum-rpc.publicnode.com serves
# eth_getLogs only to about head-10k blocks (head-20000..90000 all HTTP 403) while Infura and MEV
# Blocker serve deep ranges. The 403 is an error, never a false absence, but every deep key-walk page
# quarantined publicnode and knocked it out of balance and quote reads mid-ladder. Off for log reads
# on that chain only; every other method keeps the whole pool. Never empties the candidate list.
_LOG_READ_EXCLUDED_HOSTS = {"1": frozenset({"ethereum-rpc.publicnode.com"})}


def _log_read_excluded(chain, member, method):
    if method != "eth_getLogs":
        return False
    host = str(member).split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return host in _LOG_READ_EXCLUDED_HOSTS.get(str(chain), ())


_last_answer = _threading.local()


def _note_answer(target, answer):
    """Remember who answered the last read on this thread, for attribution after the fact.

    The gas top-up planner acted 13 times on 2026-09-04 on a native balance the chain never
    held, and nothing recorded which member had said it. The three-read guard now catches the
    phantom; this makes its log name the endpoint and the raw word it returned."""
    try:
        _last_answer.target = target
        _last_answer.answer = answer
        res = answer.get("result") if isinstance(answer, dict) else None
        _last_answer.result = str(res)[:80] if res is not None else None
    except Exception:
        pass


def last_answer():
    """(target, raw_result) of the most recent _rpc answer on this thread, or (None, None)."""
    return (getattr(_last_answer, "target", None), getattr(_last_answer, "result", None))


def answer_target(answer):
    """Physical endpoint for this exact answer, or None for an unrecorded/mock response."""
    if getattr(_last_answer, "answer", None) is answer:
        return getattr(_last_answer, "target", None)
    return None


def _rpc_capability(method, params):
    if method == "eth_getLogs" and isinstance(params, (list, tuple)) and params:
        flt = params[0]
        if (isinstance(flt, dict) and flt.get("fromBlock") in ("0x0", "0", 0)
                and flt.get("toBlock") == "latest"):
            return "eth_getLogs:full_range"
    return method


def _note_rpc_failure(exc, target):
    try:
        exc.rpc_url = target
    except Exception:
        pass


# PAYLOAD TAP FOR THE ROUTE LADDER (2026-09-24, operator). The 4663 ladder never recorded what it
# SENT, so "publicnode answers a proxy method in 80 ms against the primary's 205 ms" could not be
# retested with the real calls. The tap records host, method, params, RTT and outcome class so the
# exact payloads can be replayed against other endpoints later.
#
# OFF UNLESS A RUNG INSTALLED IT, and per thread because the ladder's rungs run in a
# ThreadPoolExecutor: `start_rpc_tap()` at the top of a rung, `take_rpc_tap()` at the bottom. With
# no tap installed this costs one `getattr` per RPC and records nothing, which is every other
# caller of this function -- the funded exit sweep, the balance reads, the tickstream.
#
# THE THREADS ARE REUSED across ladders, so `start_rpc_tap` REPLACES the list rather than clearing
# it; a rung that forgot to start one would otherwise inherit the previous ladder's calls.
_RPC_TAP = _threading.local()


def start_rpc_tap():
    """Begin recording this thread's JSON-RPC calls. Returns the list that will hold them."""
    calls = []
    _RPC_TAP.calls = calls
    return calls


def take_rpc_tap():
    """Stop recording on this thread and return what was recorded, or None if nothing was."""
    calls = getattr(_RPC_TAP, "calls", None)
    _RPC_TAP.calls = None
    return calls


# PROVIDER HTTP TAP FOR THE ROUTE LADDER (2026-09-27, P4). The RPC tap above sees only JSON-RPC on
# the rung's own thread, so kyber's and relay's HTTP APIs and v4_multihop's provider discovery and
# worker-thread stages were invisible (v4_multihop: 2% of its 2.85 s p50 tapped). Same contract as
# the RPC tap: off unless a rung installed it, per thread, replaced (never cleared) on start. Each
# record carries a label the CALL SITE chose, never one parsed from the URL, and the host reduced
# by `_tap_endpoint`: no path, no query, no userinfo, because keys ride in URLs.
_HTTP_TAP = _threading.local()


def start_http_tap():
    """Begin recording this thread's provider HTTP calls. Returns the list that will hold them."""
    calls = []
    _HTTP_TAP.calls = calls
    return calls


def take_http_tap():
    """Stop recording on this thread and return what was recorded, or None if nothing was."""
    calls = getattr(_HTTP_TAP, "calls", None)
    _HTTP_TAP.calls = None
    return calls


@_contextlib.contextmanager
def http_tap(label, url=None, **fields):
    """Time one provider call (or one timed stage) for the rung's `http` list.

    The block may set box["outcome"] (e.g. "http_200"); otherwise it is "ok", or the exception
    class when the block raises -- and the exception still propagates. Any other key the block puts
    in the box joins the record (2026-10-17: a GeckoTerminal read's gate_wait_ms). Recording never
    raises into the call. With no tap installed this is one getattr."""
    calls = getattr(_HTTP_TAP, "calls", None)
    box = {}
    if calls is None:
        yield box
        return
    started = _time.monotonic()
    try:
        yield box
    except BaseException as exc:
        if "outcome" not in box:
            box["outcome"] = ("timeout" if isinstance(exc, TimeoutError)
                              else "raise:%s" % type(exc).__name__)
        raise
    finally:
        try:
            calls.append({"label": str(label), "host": _tap_endpoint(url) if url else None,
                          "started_at": started, "rtt_ms": (_time.monotonic() - started) * 1000.0,
                          "outcome": box.get("outcome", "ok"), **fields,
                          **{key: value for key, value in box.items() if key != "outcome"}})
        except Exception:
            pass


def _tap_endpoint(url):
    """Which PROVIDER was asked, with nothing that could be a credential.

    ⚠️ THE TAP WROTE THE URL VERBATIM AND THE URL CARRIES KEYS. Found by Codex reviewing 7f4cbf02
    and confirmed here against `.env`: 12 of 26 configured RPC URLs hold a credential in the path or
    the query, including MEMECOIN_EVM_RPC_POOL_URLS_4663, _5042 and _8453 -- the pools the entry
    ladder actually quotes -- plus the Helius, QuickNode and Alchemy URLs. A tapped rung would have
    written an API key into `memecoin_entry_attempts.trace_json`, which is the funded ledger, backed
    up nightly and read by every trace consumer.

    Scheme, host and port only. Userinfo, path, query and fragment are dropped rather than
    sanitised, because a redactor has to be right about every provider's URL shape and this has to
    be right about none of them. Anything that does not parse as http(s) with a host is `unknown`:
    a value we cannot decompose is exactly the one not to record.

    The replay loses nothing. Gamma replays by PAYLOAD, and this field was never the endpoint that
    answered -- `_rpc` rotates and hedges across the pool internally.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host or any(c.isspace() for c in host):
            return "unknown"
        if ":" in host:                      # an IPv6 literal, which urlsplit returns unbracketed
            host = "[%s]" % host
        port = parsed.port
        return "%s://%s%s" % (parsed.scheme, host, ":%d" % port if port is not None else "")
    except (TypeError, ValueError):
        return "unknown"


def _rpc(url, method, params, http=None):
    """One JSON-RPC call, with the ladder payload tap around the real implementation.

    The tap NEVER changes the answer: the call is delegated untouched, an exception propagates
    after the record is written, and a recording failure is swallowed. A call that raised still
    spent time and still says which host and payload produced the failure, which is most of the
    question, so its record is written in a `finally`.
    """
    calls = getattr(_RPC_TAP, "calls", None)
    if calls is None:
        return _rpc_impl(url, method, params, http=http)
    started = _time.monotonic()
    outcome = "error"
    try:
        answer = _rpc_impl(url, method, params, http=http)
        outcome = "rpc_error:%s" % _rpc_error_code(answer) if isinstance(
            answer, dict) and answer.get("error") else "ok"
        return answer
    except Exception as exc:
        # THE STATUS, NOT THE CLASS (2026-10-07, HALCYON). `_http_failure` raises one RuntimeError
        # for a 429 and a 5xx alike, so the tap wrote `raise:RuntimeError` for both and a rate limit
        # could not be told from an outage after the fact.
        status = getattr(exc, "http_status", None)
        outcome = ("timeout" if isinstance(exc, TimeoutError)
                   else "http_%d" % status if isinstance(status, int) and not isinstance(status, bool)
                   else "raise:%s" % type(exc).__name__)
        raise
    finally:
        try:
            calls.append({
                # THE PROVIDER WE ASKED, reduced to scheme://host[:port] because the configured
                # URL carries credentials -- see `_tap_endpoint`. Not necessarily the member that
                # answered either: this function rotates and hedges across the chain's pool
                # internally. The PAYLOAD below is exact, which is what a replay needs.
                "url_requested": _tap_endpoint(url),
                "method": method,
                "params": params,
                "started_at": started,
                "rtt_ms": (_time.monotonic() - started) * 1000.0,
                "outcome": outcome,
            })
        except Exception:
            pass


def _rpc_impl(url, method, params, http=None):
    """One JSON-RPC call, rotating across the chain's free endpoint pool on a PROVIDER failure.

    Rotation triggers on 429/5xx/transport and provider-busy bodies. A chain-level JSON-RPC
    error is returned unchanged. A chain with no pool configured keeps the exact
    previous behaviour: one call, exceptions propagate to the caller's fail-closed handling.

    A READ (see _HEDGEABLE_READS) on a pooled chain is latency-hedged: if the chosen endpoint has
    not answered within _HEDGE_AFTER_S, the same read is issued to the next verified endpoint and
    the first answer wins. Writes are never hedged.
    """
    http = http or _http
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    from sniper.execution import evm_rpc_pool as _pool

    want = _pool.chain_of(url)

    def _verify(candidate):
        """Prove the endpoint serves the configured chain. The expectation comes from CONFIG, so
        this never queries another endpoint — that would recurse, unboundedly if the primary were
        quarantined. A funded path must not read balances or quotes from the wrong chain."""
        try:
            status, j, _d = http("POST", candidate, body=_json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}))
        except _pool.RequestNotSent:
            raise
        except Exception as exc:
            _note_rpc_failure(exc, candidate)
            raise
        if _provider_http_failure(status):
            raise _http_failure(status, candidate)
        if _shape_refusal(status, j) or not (j or {}).get("result"):
            # No chain id came back. That is NOT proof of a wrong chain -- a wrong chain answers
            # with a DIFFERENT id. Raising lets the caller treat it as a failed probe (a short
            # quarantine) instead of disproving the endpoint for _IDENTITY_TTL_S.
            raise RuntimeError("EVM RPC identity probe returned no chain id")
        seen = int(str(j["result"]), 16)
        return want is not None and seen == int(want)

    capability = _rpc_capability(method, params)
    capability_ttl = getattr(http, "read_capability_ttl_s", None)
    candidates = _pool.pool_for(url)
    if not _pool_eligible(method, params):
        candidates = [url]                 # shape this pool cannot serve uniformly -> primary only
    elif len(candidates) > 1:
        # PER-METHOD CAPABILITY (2026-09-03): a member that has structurally refused this method
        # (plan cap, archive token) is left out for THIS method only. Never empty: if every member
        # refuses, the primary is asked so the failure is honest rather than silent.
        capable = [c for c in candidates if not _pool.refuses(c, method)
                   and not _log_read_excluded(want, c, method)
                   and (capability == method or not _pool.refuses(
                       c, capability, ttl_s=capability_ttl))]
        candidates = capable or [url]
    read_candidate_allowed = getattr(http, "read_candidate_allowed", None)
    if method in _HEDGEABLE_READS and callable(read_candidate_allowed):
        candidates = [c for c in candidates if read_candidate_allowed(c)]
        if not candidates:
            raise _pool.NoEligibleReadEndpoint("no eligible read endpoint")

    def note_refusal(target, status, answer):
        refused = (_shape_refusal(status, answer)
                   or capability != method and _pool.is_full_range_refusal(answer))
        if refused and not (method == "eth_getLogs" and capability == method
                            and _pool.is_log_range_refusal(answer)):
            _pool.note_refusal(target, _rpc_error_code(answer), capability)
        return refused
    if len(candidates) == 1:
        # The one member left may not be the configured primary (per-method exclusion above).
        only = candidates[0]
        if only != url:
            identity = _pool.identity_state(only)
            if identity is None:
                identity = _verify(only)
                _pool.mark_identity(only, identity)
            if identity is not True:
                raise RuntimeError("EVM RPC backup chain identity unavailable")
        _t0 = _time.monotonic()
        try:
            status, j, _d = http("POST", only, body=body)
        except _pool.RequestNotSent:
            raise                          # never sent: no health record for this endpoint
        except Exception as exc:
            _note_rpc_failure(exc, only)
            _pool.record(only, (_time.monotonic() - _t0) * 1000.0, False)
            raise
        if _provider_http_failure(status):
            _pool.record(only, (_time.monotonic() - _t0) * 1000.0, False)
            raise _http_failure(status, only)
        note_refusal(only, status, j)
        endpoint_error = _endpoint_state_error(j)
        if endpoint_error:
            _pool.penalize(only, _state_error_reason(j))
        _pool.record(only, (_time.monotonic() - _t0) * 1000.0, not endpoint_error)
        _note_answer(only, j)
        return j
    in_safety_scope = getattr(http, "safety_deadline_active", lambda: False)
    # Chain 1's unmeasured backup is failure-only: never race a healthy custody read.
    if method in _HEDGEABLE_READS and want != "1" and not in_safety_scope():
        hedged = _rpc_hedged_read(url, body, http, _pool, _verify, candidates, method=method,
                                  note_refusal=note_refusal)
        if hedged is not None:
            return hedged
    last = last_response = None
    send_after_429 = False                 # a send that already drew a 429 from another member
    for _attempt in range(len(candidates)):
        target = _pool.choose(url, verify_fn=_verify, candidates=candidates)
        _t0 = _time.monotonic()
        try:
            # choose's singleton shortcut does not prove identity. A refusal can leave
            # exactly one backup: it still needs the same chain proof as any other backup.
            if method in _HEDGEABLE_READS and target != url:
                identity = _pool.identity_state(target)
                if identity is None:
                    identity = _verify(target)
                    _pool.mark_identity(target, identity)
                if identity is not True:
                    candidates = [candidate for candidate in candidates if candidate != target]
                    if not candidates:
                        break
                    continue
            status, j, _d = http("POST", target, body=body)
            if _provider_http_failure(status):
                raise _http_failure(status, target)
            _note_answer(target, j)
            if _endpoint_state_error(j):
                _pool.penalize(target, _state_error_reason(j))
                _pool.record(target, (_time.monotonic() - _t0) * 1000.0, False)
                if _is_send_method(method):
                    return j                   # ambiguous as it came: never ask another member
                last_response = j
                continue
            if note_refusal(target, status, j):
                # This endpoint will not serve this REQUEST; another member may. No penalty, no
                # failure recorded -- the endpoint is healthy -- but counted per method, so the
                # rotation learns the permanent no and the heartbeat shows it.
                last_response = j
                if method in _HEDGEABLE_READS:
                    candidates = [candidate for candidate in candidates if candidate != target]
                    if not candidates:
                        break
                continue
            _pool.record(target, (_time.monotonic() - _t0) * 1000.0, True)
            if send_after_429 and isinstance(j, dict) and j.get("error") is not None:
                # A 429 DOES NOT PROVE THE BYTES WERE NOT FORWARDED (review R-01, 2026-10-08): this
                # member's refusal may describe our own copy. The caller reads the flag and keeps a
                # state-dependent rejection ambiguous (evm_real_seams._eth_send_raw).
                j = dict(j, _sniper_send_after_429=True)
            return j
        except _pool.RequestNotSent:
            raise                          # never sent: stop, and do not quarantine the endpoint
        except Exception as exc:            # provider failure: quarantine and try the next endpoint
            _note_rpc_failure(exc, target)
            _pool.penalize(target, type(exc).__name__)
            _pool.record(target, (_time.monotonic() - _t0) * 1000.0, False)
            if _is_send_method(method) and getattr(exc, "http_status", None) == 429:
                send_after_429 = True
            if _is_send_method(method) and getattr(exc, "http_status", None) != 429:
                # A DISPATCHED SEND IS NOT RE-ASKED ELSEWHERE AFTER A 5xx OR A TRANSPORT FAILURE
                # (Gamma on 6c0a5e0b2): a 502/504 or a read timeout can come after the node accepted
                # the bytes, and another member's 'nonce too low' would read as a clean rejection.
                # Ambiguous; the write-ahead intent and nonce/receipt recovery decide. A 429 still
                # rotates (the pinned-close re-offer relies on that), but a later member's
                # state-dependent refusal is then ambiguous, not a rejection (R-01, flag above).
                raise
            last = exc
    if last is not None:
        raise last
    return last_response


def _rpc_hedged_read(url, body, http, _pool, _verify, candidates, method="", note_refusal=None):
    """First-answer-wins read across the pool. Returns the JSON body, or None to let the caller
    fall through to the sequential rotation (which owns quarantine + the final raise).

    THE HEDGE WALKS THE POOL (2026-09-03). It used to know a primary and ONE backup: live, Alchemy
    refused a filtered eth_getLogs in 108 ms, the chain-owner backup answered a plain 403, and the
    refusal envelope became the answer while publicnode would have served it. Now a structural
    refusal or a failure on the member in flight launches the next capable, non-quarantined
    member. A chain-level JSON-RPC error (a revert) is the chain's answer: it is held so a correct
    answer already in flight can still win, but nobody else is asked for it."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as _wait

    primary = _pool.choose(url, verify_fn=_verify, candidates=candidates)
    queue = [c for c in candidates
             if c != primary and _pool.available(c) and not _too_stale_to_race(_pool, c)]
    if not queue:
        return None
    backups = iter(queue)
    declined = set()                    # answered with a STRUCTURAL refusal (4xx + JSON-RPC body)

    def call(target):
        t0 = _time.monotonic()
        try:
            status, j, _d = http("POST", target, body=body)
            if _provider_http_failure(status):
                raise _http_failure(status, target)
        except _pool.RequestNotSent:
            raise
        except Exception as exc:
            _note_rpc_failure(exc, target)
            _pool.record(target, (_time.monotonic() - t0) * 1000.0, False)
            raise
        if note_refusal is not None:
            refused = note_refusal(target, status, j)
        else:
            refused = _shape_refusal(status, j)
            if refused:
                _pool.note_refusal(target, _rpc_error_code(j), method)
        if refused:
            declined.add(target)
        _pool.record(target, (_time.monotonic() - t0) * 1000.0,
                     not _endpoint_state_error(j))
        return j

    # NOT a context manager: `with` joins every future on exit, so the fast answer would still
    # wait out the slow endpoint's full latency — the exact tail this hedge removes. shutdown
    # below is non-blocking; the loser thread finishes in the background and its answer is dropped.
    ex = ThreadPoolExecutor(max_workers=2, thread_name_prefix="evm-read-hedge")
    futures = {}
    pending = set()
    local_refusal = None
    caller_remaining = getattr(http, "request_remaining_s", None)

    def remaining_wait(cap):
        return min(cap, caller_remaining()) if callable(caller_remaining) else cap

    def walk_on():
        """Put the next member in flight, if the pool has one left."""
        if local_refusal is not None:
            return
        for c in backups:
            # Identity may expire or be disproven after the queue was built. The sampler proves
            # backups off-path; cold failover retains the sequential path's existing verification.
            if _pool.identity_state(c) is not True or _too_stale_to_race(_pool, c):
                continue
            fut = ex.submit(call, c)
            futures[fut] = c
            pending.add(fut)
            return

    try:
        first = ex.submit(call, primary)
        futures[first] = primary
        pending.add(first)
        done, _ = _wait([first], timeout=remaining_wait(_HEDGE_AFTER_S))
        if not done:                        # primary is in its slow mode -> race the next member
            walk_on()
        refusal = None
        absent = None                   # a "no such transaction" that another member may overturn
        deadline = _time.monotonic() + _HEDGE_TOTAL_S
        while pending:
            remaining = remaining_wait(deadline - _time.monotonic())
            if remaining > 0:
                done, pending = _wait(
                    pending, return_when=FIRST_COMPLETED, timeout=remaining)
            else:
                done = ()
            if not done:                    # budget spent with nothing back -> this is a HANG, not
                if callable(caller_remaining) and caller_remaining() <= 0:
                    raise _pool.RequestNotSent("caller read deadline")
                for fut in pending:         # latency. Quarantine, or the next tick picks it again.
                    _pool.penalize(futures[fut], "hedged_read_timeout")
                break
            for fut in done:
                target = futures[fut]
                try:
                    answer = fut.result()
                except _pool.RequestNotSent as exc:
                    # Keep paid requests eligible until the caller's deadline.
                    local_refusal = exc
                    continue
                except Exception:           # that endpoint failed; the next one may answer
                    _pool.penalize(target, "hedged_read_failed")
                    if not pending:
                        walk_on()
                    continue
                if isinstance(answer, dict) and "error" in answer:
                    if _endpoint_state_error(answer):
                        _pool.penalize(target, _state_error_reason(answer))
                        if not pending:
                            walk_on()
                        continue
                    # A FAST REFUSAL MUST NOT BEAT A SLOW CORRECT ANSWER: hold it and let anything
                    # already in flight finish. A structural refusal also walks on to the next
                    # member; a chain-level error does not — every node would return the same one.
                    if refusal is None:
                        refusal = (target, answer)
                    if target in declined and not pending:
                        walk_on()
                    continue
                if _absence_answer(method, answer):
                    # A FAST "I DON'T HAVE IT" MUST NOT BEAT A SLOW RECEIPT -- same reasoning as
                    # the refusal hold above, applied to absence. Hold it, let anything already in
                    # flight finish, and ask the next member. It still wins when nobody has better.
                    if absent is None:
                        absent = (target, answer)
                    if not pending:
                        walk_on()
                    continue
                _note_answer(target, answer)
                return answer
        # Only refusals or absences came back from every member asked. That IS the answer, and
        # re-issuing it through the sequential rotation would buy the same one twice. A held
        # absence is a real, node-confirmed "no such transaction" once no member overturned it,
        # so it is returned rather than dropped. None only when nothing answered at all.
        if refusal is not None or absent is not None:
            target, answer = refusal if refusal is not None else absent
            _note_answer(target, answer)
            return answer
        if local_refusal is not None:
            raise local_refusal
        return None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


class RpcErrorAnswer(KeyError):
    """`eth_call` answered a JSON-RPC error body instead of a result.

    Still the `KeyError('result')` every caller has always caught and printed; it now carries the
    provider's code so a failed read can say which error it was (2026-10-07)."""

    def __init__(self, code):
        super().__init__("result")
        self.rpc_code = code


def _eth_call(to, data, rpc_url, http=None):
    j = _rpc(rpc_url, "eth_call", [{"to": to, "data": "0x" + data}, "latest"], http=http)
    if isinstance(j, dict) and "result" not in j and isinstance(j.get("error"), dict):
        raise RpcErrorAnswer(j["error"].get("code"))
    return j["result"]


def _pad_addr(address):
    return address[2:].lower().rjust(64, "0")


def native_balance_raw_or_none(address, rpc_url, http=None, retries=1, backoff_s=0.2):
    """Native gas balance in exact wei, or None when the read fails."""
    for attempt in range(retries + 1):
        try:
            j = _rpc(rpc_url, "eth_getBalance", [address, "latest"], http=http)
            return int(j["result"], 16)
        except Exception:
            if attempt < retries and backoff_s > 0:
                _time.sleep(backoff_s)
    return None


def native_balance_or_none(address, rpc_url, http=None, retries=1, backoff_s=0.2):
    """Native gas balance in whole units, or None when the read itself fails."""
    raw = native_balance_raw_or_none(
        address, rpc_url, http=http, retries=retries, backoff_s=backoff_s)
    return None if raw is None else raw / 1e18


def native_balance(address, rpc_url, http=None):
    """Native gas balance (ETH-on-Base / BNB), in whole units. Fail-closed -> 0.0."""
    v = native_balance_or_none(address, rpc_url, http=http, retries=0)
    return 0.0 if v is None else v


def erc20_decimals(token, rpc_url, http=None, *, retries=1):
    """Token decimals via decimals(). None on failure (caller fail-closes).

    RETRIES ONCE BY DEFAULT, because this IMMUTABLE constant sits on the funded critical path and
    a single transient loses a priced entry. Measured 2026-08-30T17:23: signal GBA on 4663 quoted
    successfully in 2483ms, then the balance read returned decimals=None, so `quote_unit_price`
    could not value our own quote and the execution-premium gate refused `unpriceable_quote` --
    correctly, by its fail-closed rule. A direct call returned 18 moments later. We asked the
    network for a constant on the money path and threw away a recoverable entry when it did not
    answer.

    NO CACHE HERE, DELIBERATELY. `memecoin_evm_leg` already wraps this in a process-lifetime
    BoundedLru keyed per (chain, token), built once at startup and threaded through as
    `decimals_fn` to the executor, token safety and both balance helpers — so the funded path is
    already cached and the incident required a COLD miss. A second module-global cache in this
    library added nothing for that path and leaked between tests, breaking three unrelated
    request-count assertions: a bounded cache with no reset seam is exactly the shared-state
    hazard this codebase has been bitten by twice today. A cold miss is precisely when a cache
    cannot help, which is why the retry is the fix and the cache is not.
    """
    from sniper.execution.evm_rpc_pool import RequestNotSent
    attempts = max(1, int(retries) + 1)
    for attempt in range(attempts):
        try:
            value = int(_eth_call(token, SEL_DECIMALS, rpc_url, http=http), 16)
        except RequestNotSent:
            # NOT SENT IS NOT A TRANSIENT (Gamma, 2026-10-14): the caller's own limit refused it before
            # dispatch (a cancelled safety job, a closed research admission), so a retry 0.15 s later
            # only spends the job's time. None, as any failure; no retry, no sleep.
            return None
        except Exception:
            value = None
        if value is not None and value >= 0:
            return value
        if attempt + 1 < attempts:
            _time.sleep(0.15)
    return None


def _valid_decimals(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _balance_word(result, token):
    """An `eth_call` balanceOf result as an integer. RAISES when it is not a readable number.

    AN EMPTY RESULT IS UNREADABLE, NOT ZERO (2026-09-06). A contract that returns no data — a
    self-destructed token, a proxy with no implementation, a node answering `"0x"` — used to reach
    `int("0x", 16)` and raise a bare ValueError. `erc20_balance_checked` exists precisely so a
    caller can tell "the read failed" from "the wallet is empty", and its contract says it raises
    RuntimeError for the former; a ValueError leaking out means a caller catching the documented
    type sees a crash instead. Live case: position 50693 (LARP, 0x43fb2502…) logged
    `invalid literal for int() with base 16: '0x'` on every reconcile pass.

    Unknown is not zero, and unknown is not a crash of a different colour.
    """
    text = str(result or "").strip()
    if not text or text in ("0x", "0X"):
        raise RuntimeError("erc20 balanceOf returned no data for %s" % token)
    try:
        return int(text, 16)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("erc20 balanceOf is unreadable for %s: %r" % (token, result)) from exc


def erc20_balance_with_decimals(owner, token, rpc_url, http=None, *, decimals=None,
                                decimals_fn=None):
    """Return human balance and the decimals read for that same balance conversion."""
    try:
        if decimals is not None:
            dec = _valid_decimals(decimals)
        elif decimals_fn is not None:
            dec = _valid_decimals(decimals_fn(token))
        else:
            dec = erc20_decimals(token, rpc_url, http=http)
        if dec is None:
            return 0.0, None
        raw = _balance_word(
            _eth_call(token, SEL_BALANCE_OF + _pad_addr(owner), rpc_url, http=http), token)
        return raw / (10 ** dec), dec
    except Exception:
        return 0.0, None


def erc20_balance(owner, token, rpc_url, http=None, *, decimals=None, decimals_fn=None):
    """ERC-20 balance of `owner` for `token`, decimal-adjusted (human units). Fail-closed -> 0.0."""
    return erc20_balance_with_decimals(
        owner, token, rpc_url, http=http, decimals=decimals, decimals_fn=decimals_fn)[0]


def erc20_balance_checked(owner, token, rpc_url, http=None, *, decimals=None, decimals_fn=None):
    """Balance that RAISES when the read fails, instead of reporting a confident zero.

    EVM twin of solana_wallet.token_balance_checked — see that docstring for why reconciliation must
    not read an RPC failure as an empty wallet. `erc20_balance` keeps its fail-closed 0.0 for the
    sell-sizing callers, which bail on a non-positive balance and retry.
    """
    if decimals is not None:
        dec = _valid_decimals(decimals)
    elif decimals_fn is not None:
        dec = _valid_decimals(decimals_fn(token))
    else:
        dec = erc20_decimals(token, rpc_url, http=http)
    if dec is None:
        raise RuntimeError("erc20 decimals unavailable for %s" % token)
    raw = _balance_word(
        _eth_call(token, SEL_BALANCE_OF + _pad_addr(owner), rpc_url, http=http), token)
    return raw / (10 ** dec)


def erc20_balance_raw_checked(owner, token, rpc_url, http=None):
    """Read the atomic balance. An unreadable response raises RuntimeError."""
    return _balance_word(
        _eth_call(token, SEL_BALANCE_OF + _pad_addr(owner), rpc_url, http=http), token)


def erc20_balance_raw(owner, token, rpc_url, http=None):
    """RAW atomic ERC-20 balance (integer, no decimal division) — the EXACT on-chain amount, used to
    size a sell. NEVER derive atomic from a human float (memecoin balances exceed 2^53). Fail-closed -> 0."""
    try:
        return _balance_word(
            _eth_call(token, SEL_BALANCE_OF + _pad_addr(owner), rpc_url, http=http), token)
    except Exception:
        return 0


def eth_get_code(address, rpc_url, http=None, block_tag="latest"):
    """Contract bytecode at `address` (hex). '0x' for an EOA / non-contract. None on RPC failure.
    `block_tag` pins the read to one block (Ink's coherent safety state); default unchanged."""
    try:
        return _rpc(rpc_url, "eth_getCode", [address, block_tag], http=http)["result"]
    except Exception:
        return None


def usdc_balance(owner, chain_id, rpc_url, http=None):
    """Trading bankroll = live USDC balance on `chain_id` (wallet = bankroll). Unknown chain -> 0.0."""
    usdc = CHAIN_USDC.get(str(chain_id))
    if not usdc:
        return 0.0
    quote = CHAIN_QUOTE.get(str(chain_id)) or {}
    known_decimals = (quote.get("decimals")
                      if (quote.get("token") or "").lower() == usdc.lower() else None)
    return erc20_balance(owner, usdc, rpc_url, http=http, decimals=known_decimals)


def asset_usd_price(symbol, http=None):
    """Return live <symbol>/USD from Binance, then FMP. Return None when both sources fail."""
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return None
    http = http or _http
    try:
        _s, j, _d = http("GET", "https://api.binance.com/api/v3/ticker/price?symbol=" + symbol + "USDT")
        p = float(j["price"])
        if p > 0:
            return p
    except Exception:
        pass
    key = os.getenv("FMP_API_KEY", "").strip()
    if not key:
        return None
    try:
        query = urllib.parse.urlencode({"symbol": symbol + "USD", "apikey": key})
        _s, j, _d = http("GET", "https://financialmodelingprep.com/stable/quote?" + query)
        p = float(j[0]["price"])
        return p if p > 0 else None
    except Exception:
        return None


def eth_usd_price(http=None):
    """Live ETH/USD. Only for callers that mean ETHER specifically — anything pricing a chain's
    quote token must use `native_usd_price(chain_id)`, which reads that chain's own asset."""
    return asset_usd_price("ETH", http=http)


def native_usd_price(chain_id, http=None):
    """Live USD price of the WRAPPED-NATIVE asset of `chain_id` (ETH on 4663/Base, BNB on 56).
    None when the chain is unknown, is stablecoin-quoted, or declares no `usd_symbol`."""
    q = CHAIN_QUOTE.get(str(chain_id)) or {}
    if str(q.get("kind") or "").lower() != "eth":
        return None
    sym = str(q.get("usd_symbol") or "").strip().upper()
    # Ether routes through `eth_usd_price` rather than straight to `asset_usd_price`: that name is
    # the seam a dozen tests and the cockpit already patch to keep the network out. Bypassing it
    # made one of them place a REAL Binance call and assert against the live mark.
    if sym == "ETH":
        return eth_usd_price(http=http)
    return asset_usd_price(sym, http=http) if sym else None


def quote_usd_price(chain_id, http=None, price_fn=None):
    """USD price of ONE quote token on `chain_id`: 1.0 for stablecoin-quoted chains, the live mark
    of that chain's own native asset for wrapped-native chains (ETH on 4663/Base, BNB on 56).
    None = unknown (fail-closed). `price_fn` injectable for tests."""
    q = CHAIN_QUOTE.get(str(chain_id))
    if not q:
        return None
    if q["kind"] == "usd":
        return 1.0
    px = (price_fn() if price_fn else native_usd_price(chain_id, http=http))
    return px if px and px > 0 else None


def quote_balance_usd(owner, chain_id, rpc_url, http=None, price_fn=None):
    """Trading bankroll in USD = quote-token balance x its USD price (WETH on 4663, USDC elsewhere).
    Unknown chain or unknown price -> 0.0 (fail-closed: blocks entries; exits size from raw token
    balances and are unaffected)."""
    q = CHAIN_QUOTE.get(str(chain_id))
    if not q:
        return 0.0
    px = quote_usd_price(chain_id, http=http, price_fn=price_fn)
    if not px:
        return 0.0
    return erc20_balance(owner, q["token"], rpc_url, http=http,
                         decimals=q["decimals"]) * px


def read_failure_class(exc):
    """What a failed read met: http_<status>, rpc_error:<code>, timeout, transport:<Class>, or
    error:<Class> for anything else (an unreadable answer, a bug)."""
    status = getattr(exc, "http_status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return "http_%d" % status
    code = getattr(exc, "rpc_code", None)
    if code is not None:
        return "rpc_error:%s" % code
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "transport:%s" % type(exc).__name__
    return "error:%s" % type(exc).__name__


# Why the last `quote_balance_usd_or_none` on THIS thread returned None, or None after a good read.
# Per thread because the gate reads run on their own threads and each reads its own answer back
# immediately (`memecoin_real_runner._gate_balances`).
_last_read_failure = _threading.local()


def last_read_failure():
    return getattr(_last_read_failure, "cls", None)


def clear_read_failure():
    _last_read_failure.cls = None


def quote_balance_usd_or_none(owner, chain_id, rpc_url, http=None, price_fn=None):
    """Return the quote balance in USD. Return None when the balance or price is unknown.

    A FAILED READ SAYS WHAT IT MET (2026-10-07, HALCYON). Six chain-1 refusals read "quote balance
    unavailable" and nothing recorded whether Infura had rate limited, failed, or answered an error:
    this swallowed the exception. The class is logged once per failure and kept for the caller on
    this thread (`last_read_failure`). Still None, never 0.0: unknown is not an empty wallet."""
    _last_read_failure.cls = None
    q = CHAIN_QUOTE.get(str(chain_id))
    if not q:
        return None
    px = quote_usd_price(chain_id, http=http, price_fn=price_fn)
    if not px:
        cls = "price_unavailable"
    else:
        try:
            return erc20_balance_checked(
                owner, q["token"], rpc_url, http=http, decimals=q["decimals"]) * px
        except Exception as exc:
            cls = read_failure_class(exc)
    _last_read_failure.cls = cls
    _log.warning("quote balance read failed chain=%s class=%s host=%s",
                 chain_id, cls, _tap_endpoint(rpc_url))
    return None


def quote_symbol(chain_id):
    """Ticker of the token the bankroll is actually held in — WETH on 4663, USDC on the stable
    chains. Display only; callers label the cash number with it instead of hardcoding 'USDC'."""
    return QUOTE_SYMBOL.get(str(chain_id))
