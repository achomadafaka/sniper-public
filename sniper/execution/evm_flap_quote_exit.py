"""The two-leg exit for a funded chain-56 flap.sh coin whose curve is quoted in a TOKEN.

Shared by the operator's one-shot tool (scripts/memecoin_flap_quote_exit.py) and, behind
MEMECOIN_EVM_FLAP_QUOTE_EXIT (default 0), the runner's exit (`runner_exit`, called from
evm_real_seams.exit_fn on chain 56 before the ladder).

WHY (operator "fix daodun asap", 2026-10-02, via Alpha). Position 68544 DAODUN 0xf98a0f65 is a
graduated flap.sh coin quoted in Cheems (native_to_quote_swap_enabled=0). The funded exit ladder
refuses it (flap_curve_non_native_quote_refused), Kyber answers "route not found", and no Pancake
or Uniswap pool holds it, so it sat stuck_bag=1. The flap Portal still swaps it: an eth_call of
swapExactInput(DAODUN -> Cheems) for the whole balance returned 120,071,791.79 Cheems at 14:59Z;
DAODUN -> native reverts. Cheems trades on Pancake V2 and V3.

WHAT IT DOES, under the per-wallet EVM exec lock (evm_exec_lock_key) for the whole run:
  leg 1  exact approve(Portal, balance), then Portal swapExactInput(coin -> quote token) for the
         whole balance; the floor comes from an eth_call made right before signing.
  leg 2  exactly leg 1's quote-token delta (from leg 1's receipt, never the wallet's whole
         balance) -> WBNB on the best of Pancake V2 and V3 by quote; exact approve(router, delta);
         eth_call / estimateGas proof right before signing.
  book   the WBNB delta from leg 2's receipt, valued at the live BNB price, through
         book_full_exit_and_finalize (is_open=0 and the fill in one transaction).
Every send goes through eth_send_raw (stamp_send advances the wallet generation the fast lanes
read). The nonce is the pending count, read under the lock, and the run refuses while any of the
wallet's transactions is pending. The journal (data/flap_quote_exit/<position>.json) is written
before every broadcast. The position's close claim is taken first and leg 1's hash is its
close_sig, so the runner's exit loop sees "already_closing" and its recovery holds a confirmed
claim it cannot price instead of re-selling.

RESUME. A rerun continues from the journal: an unresolved leg-1 or leg-2 hash is read for its
receipt first; a confirmed leg 1 goes to leg 2 with the recorded delta; a failed leg 2 is re-quoted
and retried; a confirmed leg 2 is booked. An ambiguous send (no receipt) stops the run and is never
re-signed. A failed or rejected leg 1 resets the claim, so the position is as it was.

REFUSES: a position that is not open, not funded, not on 56, already claimed; a wallet balance
that differs from the ledger's open quantity beyond dust; a coin that is not a flap.sh coin quoted
in a token; any pending transaction on the wallet.

    python3 scripts/memecoin_flap_quote_exit.py 68544              # dry run: quotes, no writes
    python3 scripts/memecoin_flap_quote_exit.py 68544 --confirm    # the operator runs this
"""
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("memecoin")

CHAIN = "56"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
V3_FEES = (2500, 500, 10000, 100)
# A QUOTE TOKEN WITH NO WBNB POOL (2026-10-10, FITCOIN 72879: quoted in LLYB, the tokenized Eli Lilly
# stock, which trades on Pancake V3 only against USDT). Leg 2 also prices quote -> USDT -> WBNB as one
# V3 exactInput path, so such a coin can leave; the best quote still wins.
USDT = "0x55d398326f99059ff775485246999027b3197955"
HOP_FEES = tuple((a, b) for a in (2500, 500, 10000) for b in (100, 500))
SEL_V3_QUOTE_PATH = "0xcdca1753"   # QuoterV2.quoteExactInput(bytes,uint256)
DUST_REL = 1e-6                   # balance vs ledger quantity
LOCK_WAIT_S = 30.0
# READ-AFTER-WRITE LAG (2026-10-02, the operator's first --confirm): leg 1's approval receipt was
# seen, then the Portal eth_call landed on a backend a block behind and read allowance 0. After a
# confirmed approval the run polls the allowance, and the simulation, on the same host until they
# show it, for at most VISIBLE_WAIT_S.
VISIBLE_WAIT_S = 6.0
DROP_CONFIRM_S = 5.0          # a send is called dropped only on two proofs at least this far apart
VISIBLE_STEP_S = 0.5
RECEIPT_WAIT_S = 90.0
DEADLINE_S = 300
APPROVE_GAS_MARGIN = 1.3
SWAP_GAS_MARGIN = 1.25
SEL_GET_AMOUNTS_OUT = "0xd06ca61f"
SEL_V2_SWAP_FOT = "0x5c11d795"      # swapExactTokensForTokensSupportingFeeOnTransferTokens
SEL_V3_QUOTE_SINGLE = "0xc6a5026a"  # QuoterV2.quoteExactInputSingle
SEL_V3_EXACT_INPUT_SINGLE = "0x414bf389"   # configured Pancake V3 SwapRouter, with deadline
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class Refused(Exception):
    """The run stops here; the message says why and what state it left."""


class PortalRefused(Refused):
    """Leg 1 cannot be proven: the Portal does not simulate the sell, its proof falls under the
    floor, or the node will not estimate it. Counted per coin; DEMOTE_AFTER in a row demotes it."""


class Pending(Refused):
    """The runner's step budget ran out while a transaction or a read was still on its way. The
    journal says where the next call resumes; nothing was refused and nothing is ambiguous yet."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _abi(types, values):
    from eth_abi import encode
    return encode(types, values).hex()


def _addr_topic(address):
    return "0x" + str(address).lower()[2:].rjust(64, "0")


def token_delta(receipt, token, owner):
    """`owner`'s net atomic change of `token` across the receipt's Transfer logs."""
    token, owner = str(token).lower(), _addr_topic(owner)
    net = 0
    for log in (receipt or {}).get("logs") or ():
        topics = [str(t).lower() for t in log.get("topics") or ()]
        if len(topics) != 3 or topics[0] != TRANSFER_TOPIC or str(log.get("address")).lower() != token:
            continue
        amount = int(log.get("data") or "0x0", 16)
        if topics[2] == owner:
            net += amount
        if topics[1] == owner:
            net -= amount
    return net


def receipt_ok(receipt):
    return str((receipt or {}).get("status")).lower() in ("0x1", "1")


class ReadBudget:
    """One outstanding read per position; a late answer is consumed on the next call.

    Only HTTP reads run on the daemon. Signing, journal/ledger mutations and writes stay
    on the caller under its wallet lock. A stuck DNS read cannot grow a worker queue.
    """
    def __init__(self, http):
        self.http, self.deadline, self.job = http, None, None
        self.answers = {}
        self.lock = threading.Lock()
        self.expired = False

    def begin(self, deadline):
        self.deadline, self.expired = deadline, False

    def check(self):
        if self.expired or (self.deadline is not None and time.monotonic() >= self.deadline):
            raise Pending("read step budget spent; retry next call")

    def __call__(self, method, url, **kwargs):
        body = kwargs.get("body")
        rpc_method = json.loads(body or "{}").get("method")
        if rpc_method not in {"eth_call", "eth_chainId", "eth_getBalance", "eth_getTransactionCount",
                              "eth_gasPrice", "eth_estimateGas", "eth_getTransactionReceipt"}:
            raise Refused("read adapter refuses %s" % rpc_method)
        return self.value((method, url, body), lambda: self.http(method, url, **kwargs))

    def value(self, key, read_fn):
        self.check()
        with self.lock:
            now = time.monotonic()
            self.answers = {k: v for k, v in self.answers.items()
                            if now - v["finished_at"] <= STEP_BUDGET_S}
            prior = self.answers.pop(key, None)
            if prior is not None:
                if "error" in prior:
                    raise prior["error"]
                return prior["answer"]
            job = self.job
            stale = (job is not None and job["done"].is_set() and
                     now - job["finished_at"] > STEP_BUDGET_S)
            if job is not None and (job["key"] != key or stale):
                if not job["done"].is_set():
                    self.expired = True
                    raise Pending("previous read still running; retry next call")
                if not stale:
                    self.answers[job["key"]] = job
                    if len(self.answers) > 16:
                        self.answers.pop(next(iter(self.answers)))
                self.job = job = None
            if job is None:
                if not _READ_WORKERS.acquire(blocking=False):
                    self.expired = True
                    raise Pending("flap read workers busy; retry next call")
                job = {"key": key, "done": threading.Event()}
                self.job = job
                def read():
                    try:
                        job["answer"] = read_fn()
                    except Exception as exc:
                        job["error"] = exc
                    finally:
                        _READ_WORKERS.release()
                        job["finished_at"] = time.monotonic()
                        job["done"].set()
                try:
                    threading.Thread(target=read, name="flap-exit-read", daemon=True).start()
                except Exception:
                    _READ_WORKERS.release()
                    self.job = None
                    raise
        remaining = max(0., self.deadline - time.monotonic()) if self.deadline else 10.
        if not job["done"].wait(remaining):
            self.expired = True
            raise Pending("read step budget spent; retry next call")
        with self.lock:
            if self.job is job:
                self.job = None
        if "error" in job:
            raise job["error"]
        return job["answer"]


_READ_WORKERS = threading.BoundedSemaphore(8)  # ponytail: eight hung reads cap thread growth.
_READ_BUDGETS = {}
_READ_BUDGETS_LOCK = threading.Lock()


def read_budget(position_id):
    from sniper.execution.evm_wallet import _http
    with _READ_BUDGETS_LOCK:
        pid = int(position_id)
        if pid not in _READ_BUDGETS:
            _READ_BUDGETS[pid] = ReadBudget(_http)
        return _READ_BUDGETS[pid]


# --- chain seams (production) -------------------------------------------------------------------

class Chain:
    """The funded wallet's reads and sends on the funded 56 RPC; one place to fake in tests."""

    def __init__(self, rpc_url, address, *, send_fn=None, approve_send_fn=None, read_budget=None,
                 http=None):
        """`send_fn` / `approve_send_fn`: the runner's own broadcast seams (submission transport and
        its audit rows); unset, eth_send_raw for both (the operator's tool)."""
        self.rpc, self.address = rpc_url, address
        self._send_fn, self._approve_send_fn = send_fn, approve_send_fn
        self.read_budget = read_budget
        self.http = http

    def call(self, to, data, *, overrides=None):
        """The eth_call result hex, or None when the call reverted or could not run."""
        from sniper.execution.evm_read_rpc import eth_call_raw
        out = eth_call_raw(to, data, self.rpc, from_addr=self.address, overrides=overrides,
                           http=self.read_budget or self.http)
        if self.read_budget:
            self.read_budget.check()
        return out if isinstance(out, str) and out.startswith("0x") else None

    def balance(self, token):
        from sniper.execution.evm_wallet import erc20_balance_raw_checked
        out = erc20_balance_raw_checked(self.address, token, self.rpc, http=self.read_budget)
        if self.read_budget:
            self.read_budget.check()
        return int(out)

    def allowance(self, token, spender):
        data = "0xdd62ed3e" + _abi(["address", "address"], [self.address, spender])
        out = self.call(token, data)
        if out is None:
            raise RuntimeError("allowance unreadable")
        return int(out, 16)

    def nonces(self):
        from sniper.execution.evm_real_seams import eth_nonce
        out = (eth_nonce(self.address, self.rpc, http=self.read_budget, block_tag="pending"),
               eth_nonce(self.address, self.rpc, http=self.read_budget, block_tag="latest"))
        if self.read_budget:
            self.read_budget.check()
        return out

    def gas_price(self):
        from sniper.execution.evm_real_seams import eth_gas_price
        out = eth_gas_price(self.rpc, http=self.read_budget)
        if self.read_budget:
            self.read_budget.check()
        return out

    def estimate_gas(self, tx):
        from sniper.execution.evm_wallet import _rpc
        call = {"from": self.address, "to": tx["to"], "data": tx["data"], "value": "0x0"}
        answer = _rpc(self.rpc, "eth_estimateGas", [call], http=self.read_budget)
        if self.read_budget:
            self.read_budget.check()
        if answer.get("error") is not None or answer.get("result") is None:
            raise RuntimeError("estimateGas refused: %s" % str(answer.get("error"))[:160])
        return int(answer["result"], 16)

    def send(self, raw, kind="swap"):
        """Hash when accepted, False when the node positively rejected it, None when ambiguous."""
        fn = self._approve_send_fn if kind == "approve" else self._send_fn
        if fn is not None:
            return fn(raw)
        from sniper.execution.evm_real_seams import eth_send_raw
        return eth_send_raw(raw, self.rpc)

    def receipt(self, tx_hash):
        from sniper.execution.evm_wallet import _rpc
        try:
            return _rpc(self.rpc, "eth_getTransactionReceipt", [tx_hash], http=self.read_budget).get("result")
        except Pending:
            raise
        except Exception:
            if self.read_budget:
                self.read_budget.check()
            return None


# --- quotes ---------------------------------------------------------------------------------------

def portal_sell_out(chain, portal, mint, quote_token, amount, *, overrides=None):
    """The Portal's real swapExactInput output for `amount`, or None when it reverts."""
    from sniper.execution.evm_flap_curve import encode_swap_exact_input
    out = chain.call(portal, encode_swap_exact_input(mint, quote_token, amount, 0), overrides=overrides)
    if not out or out == "0x":
        return None
    value = int(out[2:66] or "0", 16)
    return value or None


def _hop_route(quote_token, fees):
    return {"venue": "v3_path", "out": 0, "fee": None,
            "tokens": [quote_token, USDT, WBNB], "fees": list(fees)}


def _path_quote(chain, v3_quoter, route, amount):
    from sniper.execution.evm_dex_quote import _encode_path, _path_quote_data
    out = chain.call(v3_quoter, _path_quote_data(_encode_path(route["tokens"], route["fees"]), amount))
    return int(out[2:66], 16) if out and len(out) >= 66 else 0


def leg2_routes(chain, quote_token, amount, *, v2_router, v3_quoter):
    """Every answering route quote_token -> WBNB for `amount`, best first."""
    routes = []
    out = chain.call(v2_router, SEL_GET_AMOUNTS_OUT
                     + _abi(["uint256", "address[]"], [amount, [quote_token, WBNB]]))
    if out and len(out) >= 2 + 64 * 4:
        routes.append({"venue": "v2", "out": int(out[-64:], 16), "fee": None})
    for fee in V3_FEES:
        out = chain.call(v3_quoter, SEL_V3_QUOTE_SINGLE + _abi(
            ["(address,address,uint256,uint24,uint160)"], [(quote_token, WBNB, amount, fee, 0)]))
        if out and len(out) >= 66:
            routes.append({"venue": "v3", "out": int(out[2:66], 16), "fee": fee})
    if str(quote_token).lower() != USDT:
        for fees in HOP_FEES:
            route = _hop_route(quote_token, fees)
            route["out"] = _path_quote(chain, v3_quoter, route, amount)
            routes.append(route)
    return sorted((r for r in routes if r["out"] > 0), key=lambda r: -r["out"])


def leg2_tx(route, quote_token, amount, min_out, recipient, *, v2_router, v3_router, deadline):
    if route["venue"] == "v2":
        data = SEL_V2_SWAP_FOT + _abi(["uint256", "uint256", "address[]", "address", "uint256"],
                                      [amount, min_out, [quote_token, WBNB], recipient, deadline])
        return {"to": v2_router, "data": data}
    if route["venue"] == "v3_path":
        from sniper.execution.evm_dex_quote import _encode_pancake_exact_input, _encode_path
        data = _encode_pancake_exact_input(_encode_path(route["tokens"], route["fees"]), recipient,
                                           deadline, amount, min_out)
        return {"to": v3_router, "data": data}
    from sniper.execution.evm_dex_quote import _encode_pancake_exact_input_single
    data = _encode_pancake_exact_input_single(
        quote_token, WBNB, route["fee"], recipient, deadline, amount, min_out)
    return {"to": v3_router, "data": data}


def sell_quote(chain, mint, balance, quote_token, addresses, *, overrides_fn=None, http=None):
    """Read the same Portal and Pancake legs used by the full-close executor."""
    from sniper.execution import evm_flap_curve as F
    out = portal_sell_out(chain, addresses["portal"], mint, quote_token, balance)
    basis = "real_allowance"
    if out is None:
        overrides = (overrides_fn or F.discover_sell_overrides)(
            mint, addresses["portal"], balance, taker=chain.address, rpc_url=chain.rpc, http=http)
        out = overrides and portal_sell_out(chain, addresses["portal"], mint,
                                            quote_token, balance, overrides=overrides)
        basis = "allowance_override"
    if not out:
        raise Refused("the Portal sell quote is unavailable")
    routes = leg2_routes(chain, quote_token, out, v2_router=addresses["v2_router"],
                         v3_quoter=addresses["v3_quoter"])
    if not routes:
        raise Refused("the Pancake quote-token exit is unavailable")
    return out, routes, basis


def preview_exit(mint, amount_atomic, *, chain, addresses, price_fn, http=None):
    """Read-only full-balance value for the route runner_exit actually executes."""
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution import evm_flap_curve as F
    from sniper.execution.exit_preview import ExitPreview, PreviewResult, quote_evidence
    provider = "56:direct"
    try:
        state = F.curve_state(CHAIN, mint, chain.rpc, http=http)
        if amount_atomic <= 0 or not state or not state.get("quote_token") or F.is_native_quote(state):
            raise Refused("Flap quote-token state unavailable")
        token = state["quote_token"]
        out, routes, basis = sell_quote(chain, mint, amount_atomic, token, addresses, http=http)
        price = float(price_fn())
        expected = routes[0]["out"] / 1e18 * price
        if not math.isfinite(expected) or expected <= 0:
            raise Refused("quote-token USD value unavailable")
    except TimeoutError:
        return PreviewResult.failed(QuoteFailure.PROVIDER_TIMEOUT, provider_id=provider)
    except (Refused, TypeError, ValueError, KeyError):
        # Existing read seams can collapse reverts and provider errors to None: keep these unknown.
        return PreviewResult.failed(QuoteFailure.MALFORMED_RESPONSE, provider_id=provider)
    except Exception:
        logger.exception("Flap exit preview failed mint=%s", mint)
        return PreviewResult.failed(QuoteFailure.RPC_TRANSPORT, provider_id=provider)
    best = routes[0]
    payload = {"buyAmount": str(best["out"]), "transaction": {"to": addresses["portal"]},
               "route": {"protocol": "flap_quote_exit", "path": [mint] + best.get("tokens", [token, WBNB]),
                         "router": addresses["v2_router"] if best["venue"] == "v2" else addresses["v3_router"],
                         "fee": best.get("fee"), "quote_basis": basis},
               "_curve_leg": {"quote_mint": token, "curve": addresses["portal"], "expected_out": out}}
    evidence = quote_evidence(payload, input_atomic=amount_atomic, quote_asset=WBNB,
                              quote_asset_decimals=18, quote_asset_usd=price,
                              slippage_bps=DEFAULT_SLIPPAGE_BPS)
    return PreviewResult.succeeded(
        ExitPreview(expected, amount_atomic, "flap_quote_exit", _now(), quote_evidence=evidence),
        provider_id=provider)


def floor(amount, slippage_bps):
    return int(amount) * (10_000 - int(slippage_bps)) // 10_000


# --- journal --------------------------------------------------------------------------------------

def read_journal(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def write_journal(path, journal):
    """Atomic and durable: the file and its directory are fsynced, because the journal is the
    authority a resume replays signed bytes from (Codex EVM-05). EVERY replacement, progress
    included: each one rewrites the whole file, so a non-durable progress write could undo an
    fsynced write-ahead on power loss (Codex night review 2026-10-07). Measured 12.8 ms p50 /
    13.3 ms p90 a write on this host; exit path only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        fh.write(json.dumps(journal, indent=1, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


# --- the run ----------------------------------------------------------------------------------------

class Run:
    def __init__(self, conn, position_id, *, chain, sign_fn, journal_path, price_fn, addresses,
                 slippage_bps=300, out=print, sleep_fn=time.sleep, clock=time.monotonic,
                 overrides_fn=None, state_fn=None, step_budget_s=None, wall_fn=time.time,
                 quote_check_fn=None, exit_reason="manual"):
        """`step_budget_s`: the runner's cap on one call's waiting (None: the operator's tool, which
        waits as long as each step needs)."""
        self.conn, self.pid, self.chain = conn, int(position_id), chain
        self.step_budget_s, self._deadline = step_budget_s, None
        self.wall_fn = wall_fn                  # wall seconds: the drop proof's spacing survives calls
        self.sign_fn, self.journal_path, self.price_fn = sign_fn, journal_path, price_fn
        budget = getattr(chain, "read_budget", None)
        if budget is not None:
            self.price_fn = lambda: budget.value(("quote_usd", self.pid), price_fn)
        self.a = addresses                      # portal, v2_router, v3_router, v3_quoter
        self.slippage_bps, self.out = int(slippage_bps), out
        self.sleep_fn, self.clock = sleep_fn, clock
        self.overrides_fn, self.state_fn = overrides_fn, state_fn
        self.j = read_journal(journal_path)
        self.sent = False
        self._offered = set()                   # hashes this process has put on the wire
        self.quote_check_fn, self.exit_reason = quote_check_fn, exit_reason

    # -- shared steps --

    def _save(self, **changes):
        self.j.update(changes, updated_at=_now())
        write_journal(self.journal_path, self.j)

    def _position(self):
        from sniper.persistence import db_memecoin as m
        pos = m.get_position(self.conn, self.pid)
        if not pos:
            raise Refused("position %s does not exist" % self.pid)
        return pos

    def _curve(self, mint):
        from sniper.execution import evm_flap_curve as F
        state = self.state_fn(CHAIN, mint) if self.state_fn else F.curve_state(CHAIN, mint, self.chain.rpc, http=getattr(self.chain, "read_budget", None))
        budget = getattr(self.chain, "read_budget", None)
        if budget:
            budget.check()
        if not state or F.is_native_quote(state) or not state.get("quote_token"):
            raise Refused("not a flap.sh coin quoted in a token (state %s)" % (state and {
                k: state.get(k) for k in ("status", "quote_token", "native_to_quote_swap_enabled")}))
        return state

    def _checks(self, pos):
        from sniper.persistence import db_memecoin as m
        if (pos.get("execution_mode") != "real" or not pos.get("is_open")
                or str(pos.get("network_id")) != CHAIN):
            raise Refused("position %s is not an open funded chain-56 position" % self.pid)
        if pos.get("close_claim_id") or pos.get("close_sig") or pos.get("derisk_claim_id") \
                or pos.get("derisk_sig"):
            raise Refused("position %s already has an exit claim or signature" % self.pid)
        qty = m.open_qty(self.conn, self.pid)
        balance = self.chain.balance(pos["mint"])
        if qty <= 0 or balance <= 0 or abs(balance / 1e18 - qty) > max(qty * DUST_REL, 1e-12):
            raise Refused("wallet holds %s raw but the ledger's open quantity is %s"
                          % (balance, qty))
        return qty, balance

    def _quiet(self):
        pending, latest = self.chain.nonces()
        if pending is None or latest is None:
            raise Refused("the wallet nonce is unreadable")
        if int(pending) != int(latest):
            raise Refused("a transaction of this wallet is pending (nonce %s vs %s)" % (pending, latest))
        return int(pending)

    def _signed(self, tx, gas):
        gas_price = self.chain.gas_price()
        if not gas_price:
            raise Refused("gas price unreadable")
        nonce = self._quiet()
        signed = self.sign_fn({"to": tx["to"], "data": tx["data"], "value": 0, "gas": int(gas),
                               "gasPrice": int(gas_price), "nonce": nonce, "chainId": int(CHAIN)})
        if not signed:
            raise Refused("signing failed")
        self._nonce = nonce                     # journaled beside the hash: the drop proof needs it
        return signed

    def _drop_proof(self, nonce_key, intact):
        nonce = self.j.get(nonce_key)
        if nonce is None:
            return False
        try:
            _pending, latest = self.chain.nonces()
            return latest is not None and int(latest) > int(nonce) and bool(intact())
        except Pending:
            raise
        except Exception:
            return False

    def _dropped(self, nonce_key, intact):
        """True when a journaled send is provably dropped: its nonce is used (the latest count is
        past it), it has no receipt (the caller saw none), and its own effect is absent (`intact`:
        the coin, the quote-token balance or the allowance as before it), on TWO reads at least
        DROP_CONFIRM_S apart (Gamma: the reads are separate calls, and backend skew on one URL can
        show a fresh nonce beside a stale balance). Any doubt is False."""
        seen_key = nonce_key + "_drop_seen"
        if not self._drop_proof(nonce_key, intact):
            if self.j.get(seen_key) is not None:
                self._save(**{seen_key: None})
            return False
        seen, now = self.j.get(seen_key), self.wall_fn()
        if seen is None:
            self._save(**{seen_key: now})
            if self._deadline is not None:
                return False                    # the runner: the next call proves it again
            self.sleep_fn(DROP_CONFIRM_S)       # the tool: once more, a few seconds later
            return self._drop_proof(nonce_key, intact)
        return now - float(seen) >= DROP_CONFIRM_S

    def _handed(self, tx_hash, raw_key):
        """The send of `raw_key`'s bytes returned (any reply but a rejection): later resumes leave
        them to the receipt and the drop proof."""
        self._offered.add(tx_hash)
        self._save(**{raw_key + "_sent": True})

    def _reoffer(self, tx_hash, raw_key, kind="swap"):
        """A journaled send whose broadcast never returned (a crash between the journal and the
        send, Codex EVM-05): the receipt first, else the SAME signed bytes again. The node's reply
        decides nothing (it may already have them); only a receipt or the drop proof does."""
        raw = self.j.get(raw_key)
        if (not raw or self.j.get(raw_key + "_sent") or tx_hash in self._offered
                or self.chain.receipt(tx_hash)):
            return
        try:
            self.chain.send(raw, kind=kind)
        except Pending:
            raise
        except Exception:
            logger.warning("flap quote exit pos=%s: re-offer of %s failed", self.pid, tx_hash,
                           exc_info=True)
            return
        self._handed(tx_hash, raw_key)

    def _left(self, cap):
        """Seconds this call may still wait, at most `cap` (the runner's step budget, if any)."""
        if self._deadline is None:
            return cap
        return max(0.0, min(cap, self._deadline - self.clock()))

    def _wait(self, tx_hash):
        deadline = self.clock() + self._left(RECEIPT_WAIT_S)
        step = 1.0 if self._deadline is None else 0.25
        while True:
            receipt = self.chain.receipt(tx_hash)
            if receipt:
                return receipt
            if self.clock() >= deadline:
                return None
            self.sleep_fn(step if self._deadline is None else self._left(step))

    def _until(self, read):
        """`read()` until it answers truthy, for at most VISIBLE_WAIT_S (or the step budget)."""
        deadline = self.clock() + self._left(VISIBLE_WAIT_S)
        step = VISIBLE_STEP_S if self._deadline is None else 0.25
        while True:
            try:
                value = read()
            except Pending:
                raise
            except Exception:
                value = None
            if value or self.clock() >= deadline:
                return value
            self.sleep_fn(step if self._deadline is None else self._left(step))

    def _no_receipt(self, tx_hash, what):
        if self._deadline is not None:
            raise Pending("%s %s is not mined yet; the next call reads it first" % (what, tx_hash))
        raise Refused("%s %s has no receipt yet: AMBIGUOUS, nothing more sent. Rerun later."
                      % (what, tx_hash))

    def _not_visible(self, tx_hash, what):
        if self._deadline is not None:
            raise Pending("%s %s mined; the node does not show the allowance yet" % (what, tx_hash))
        raise Refused("the %s %s confirmed but the node does not show the allowance yet; rerun (it "
                      "will not approve again once the allowance shows)" % (what, tx_hash))

    def _allowance_ready(self, token, spender, amount, label):
        """Return once `spender` may move `amount` of `token`. An approval already journaled for
        exactly this is read first (never a second one while it may still land); else one exact
        approval is sent. Pending (runner) or Refused (tool) while it is not mined or not shown."""
        if self.chain.allowance(token, spender) >= amount:
            return
        want = [token, spender, str(amount)]
        prior = self.j.get(label) if self.j.get(label + "_for") == want else None
        if prior:
            self._reoffer(prior, label + "_raw", kind="approve")
            receipt = self._wait(prior)
            if receipt is None and not self._dropped(
                    label + "_nonce", lambda: self.chain.allowance(token, spender) < amount):
                self._no_receipt(prior, label)
            if receipt is not None and receipt_ok(receipt):
                if self._until(lambda: self.chain.allowance(token, spender) >= amount):
                    return
                self._not_visible(prior, label)
        from sniper.execution.evm_real_seams import encode_approve
        tx = {"to": token, "data": encode_approve(spender, amount)}
        gas = int(self.chain.estimate_gas(tx) * APPROVE_GAS_MARGIN)
        raw, tx_hash = self._signed(tx, gas)
        self._save(**{label: tx_hash, label + "_for": want, label + "_nonce": self._nonce,
                      label + "_raw": raw, label + "_raw_sent": None})
        self.sent = True
        self._offered.add(tx_hash)
        if self.chain.send(raw, kind="approve") is False:
            self._save(**{label: None, label + "_raw": None})
            raise Refused("the node rejected the %s" % label)
        self._handed(tx_hash, label + "_raw")
        receipt = self._wait(tx_hash)
        if receipt is None:
            self._no_receipt(tx_hash, label)
        if not receipt_ok(receipt):
            raise Refused("the %s %s reverted; nothing else was sent" % (label, tx_hash))
        if not self._until(lambda: self.chain.allowance(token, spender) >= amount):
            self._not_visible(tx_hash, label)

    def _room(self, seconds, what):
        """Pending when this call has less than `seconds` left: start `what` on the next call."""
        if self._deadline is not None and self._deadline - self.clock() < seconds:
            raise Pending("step budget spent; %s next call" % what)

    # -- dry run --

    def _routes(self, token, amount):
        """Resume the five read-only quote rungs across budgeted calls."""
        key = [str(token), str(amount)]
        scan = self.j.get("route_scan") or {}
        if scan.get("key") != key or self.wall_fn() - float(scan.get("at") or 0) > 15.:
            scan = {"key": key, "at": self.wall_fn(), "next": 0, "routes": []}
        hops = HOP_FEES if str(token).lower() != USDT else ()
        while scan["next"] < 1 + len(V3_FEES) + len(hops):
            i = scan["next"]
            if i > len(V3_FEES):
                route = _hop_route(token, hops[i - 1 - len(V3_FEES)])
                value = route["out"] = _path_quote(self.chain, self.a["v3_quoter"], route, amount)
            elif i == 0:
                out = self.chain.call(self.a["v2_router"], SEL_GET_AMOUNTS_OUT + _abi(
                    ["uint256", "address[]"], [amount, [token, WBNB]]))
                value = int(out[-64:], 16) if out and len(out) >= 258 else 0
                route = {"venue": "v2", "out": value, "fee": None}
            else:
                fee = V3_FEES[i - 1]
                out = self.chain.call(self.a["v3_quoter"], SEL_V3_QUOTE_SINGLE + _abi(
                    ["(address,address,uint256,uint24,uint160)"], [(token, WBNB, amount, fee, 0)]))
                value = int(out[2:66], 16) if out and len(out) >= 66 else 0
                route = {"venue": "v3", "out": value, "fee": fee}
            if value > 0:
                scan["routes"].append(route)
            scan["next"] += 1
            self._save(route_scan=scan)
        return sorted(scan["routes"], key=lambda r: -r["out"])

    def _route_out(self, route, token, amount):
        if route["venue"] == "v3_path":
            return _path_quote(self.chain, self.a["v3_quoter"], route, amount)
        if route["venue"] == "v2":
            out = self.chain.call(self.a["v2_router"], SEL_GET_AMOUNTS_OUT + _abi(
                ["uint256", "address[]"], [amount, [token, WBNB]]))
            return int(out[-64:], 16) if out and len(out) >= 258 else 0
        out = self.chain.call(self.a["v3_quoter"], SEL_V3_QUOTE_SINGLE + _abi(
            ["(address,address,uint256,uint24,uint160)"], [(token, WBNB, amount, route["fee"], 0)]))
        return int(out[2:66], 16) if out and len(out) >= 66 else 0

    def _confirm_route(self, pos, balance, *, out=None):
        if self.quote_check_fn is None:
            return
        if out is None:
            out = portal_sell_out(self.chain, self.a["portal"], pos["mint"], self.j["quote_token"], balance)
            if out is None:
                from sniper.execution import evm_flap_curve as F
                budget = getattr(self.chain, "read_budget", None)
                def discover():
                    return (self.overrides_fn or F.discover_sell_overrides)(
                        pos["mint"], self.a["portal"], balance, taker=self.chain.address,
                        rpc_url=self.chain.rpc)
                overrides = (budget.value(("overrides", pos["mint"], str(balance)), discover)
                             if budget else discover())
                out = overrides and portal_sell_out(self.chain, self.a["portal"], pos["mint"],
                                                     self.j["quote_token"], balance, overrides=overrides)
        if not out:
            raise Refused("actual flap quote unavailable before approval")
        routes = self._routes(self.j["quote_token"], out)
        price = self.price_fn()
        if not routes or not price or not math.isfinite(float(price)) or price <= 0:
            raise Refused("actual flap route or quote-token USD price unavailable")
        best = routes[0]
        expected_out = self._route_out(best, self.j["quote_token"], out)
        minimum_out = self._route_out(best, self.j["quote_token"], floor(out, self.slippage_bps))
        if expected_out <= 0 or minimum_out <= 0:
            raise Refused("actual flap route floor unavailable")
        expected = expected_out / 1e18 * float(price)
        minimum = floor(minimum_out, self.slippage_bps) / 1e18 * float(price)
        allowed, detail = self.quote_check_fn(pos, expected, minimum, balance / 1e18)
        if not allowed:
            raise Refused("actual flap route refused: %s" % detail)

    def estimate(self):
        """(position, ledger qty, raw balance, quote token, leg-1 out, leg-2 routes best first,
        basis). Reads only."""
        pos = self._position()
        qty, balance = self._checks(pos)
        state = self._curve(pos["mint"])
        quote_token = state["quote_token"]
        out, routes, basis = sell_quote(self.chain, pos["mint"], balance, quote_token, self.a,
                                        overrides_fn=self.overrides_fn)
        return pos, qty, balance, quote_token, out, routes, basis

    def dry_run(self):
        pos, qty, balance, quote_token, out, routes, basis = self.estimate()
        best = routes[0]
        price = self.price_fn()
        self.out("position %s %s qty %.6f (raw %d), entry $%.2f" % (
            self.pid, pos.get("ticker"), qty, balance, float(pos.get("entry_notional") or 0)))
        self.out("leg 1  Portal %s -> %s: %d (floor %d at %d bps; %s)" % (
            pos["mint"], quote_token, out, floor(out, self.slippage_bps), self.slippage_bps, basis))
        for r in routes:
            self.out("leg 2  %s%s: %.6f WBNB" % (r["venue"], "" if r["fee"] is None else "/%d" % r["fee"],
                                                r["out"] / 1e18))
        self.out("best   %s -> %.6f WBNB (floor %.6f) ~ $%s" % (
            best["venue"], best["out"] / 1e18, floor(best["out"], self.slippage_bps) / 1e18,
            "%.2f" % (best["out"] / 1e18 * price) if price else "unknown"))
        self.out("DRY RUN: nothing signed, nothing written. --confirm is the operator's.")
        return {"leg1_out": out, "routes": routes}

    # -- the live run --

    def confirm(self):
        from sniper.execution.exec_lock import evm_exec_lock_key, touch_generation, wallet_exec_lock
        key = evm_exec_lock_key(CHAIN, self.chain.address)
        deadline = self.clock() + LOCK_WAIT_S
        while True:
            with wallet_exec_lock(key, blocking=False) as held:
                if held:
                    try:
                        return self._locked()
                    finally:
                        if self.sent:
                            touch_generation(key)
            if self.clock() >= deadline:
                raise Refused("the wallet's exec lock stayed busy for %.0f s" % LOCK_WAIT_S)
            self.sleep_fn(0.5)

    def run_locked(self):
        """The live run for a caller that ALREADY holds this wallet's exec lock (the runner's exit,
        under serialize_executor, which also stamps the wallet generation)."""
        return self._locked()

    def _locked(self):
        self.exit_reason = initiating_reason(self.j, self.exit_reason)
        if self.j.get("exit_reason") and self.j["exit_reason"] != self.exit_reason:
            self._save(exit_reason=self.exit_reason)
        self._deadline = None if self.step_budget_s is None else self.clock() + self.step_budget_s
        budget = getattr(self.chain, "read_budget", None)
        if budget:
            budget.begin(None if self.step_budget_s is None else time.monotonic() + self.step_budget_s)
        steps = {"leg1_submitted": self._leg1_settle, "leg1_confirmed": self._leg2,
                 "leg2_failed": self._leg2, "leg2_rejected": self._leg2,
                 "leg2_submitted": self._leg2_settle, "leg2_confirmed": self._book}
        first = True
        while True:
            state = self.j.get("state")
            if state == "booked":
                return self.j
            if not first and self._deadline is not None and self.clock() >= self._deadline:
                # A step that ended past the runner's budget (a receipt seen on the last poll)
                # hands the next step to the next call instead of starting its reads now.
                raise Pending("step budget spent after %s; the next call continues" % state)
            first = False
            if state in (None, "leg1_failed", "leg1_rejected", "approving_leg1"):
                self._leg1()
            elif state in steps:
                steps[state]()
            else:
                raise Refused("journal state %s needs a look by hand (%s)" % (state, self.journal_path))

    def _leg1(self):
        from sniper.persistence import db_memecoin as m
        pos = self._position()
        _qty, balance = self._checks(pos)
        quote_token = self._curve(pos["mint"])["quote_token"]
        if self.j.get("state") != "approving_leg1" or self.j.get("balance_raw") != str(balance):
            kept = {k: self.j[k] for k in ("leg1_approve", "leg1_approve_for", "leg1_approve_nonce",
                                           "leg1_approve_raw", "leg1_approve_raw_sent",
                                           "exit_reason") if k in self.j}
            self.j = {"position_id": self.pid, "mint": pos["mint"], "quote_token": quote_token,
                      "balance_raw": str(balance), "state": "approving_leg1",
                      "exit_reason": self.j.get("exit_reason") or self.exit_reason, **kept}
            self._save()
        self._confirm_route(pos, balance)
        self._allowance_ready(pos["mint"], self.a["portal"], balance, "leg1_approve")
        self._room(1.5, "leg 1")
        claim = m.claim_position_close(self.conn, self.pid, _now(), self.j.get("exit_reason") or self.exit_reason)
        if not claim:
            raise Refused("could not claim the close of position %s" % self.pid)
        self._save(claim_id=claim)
        try:
            out = self._until(lambda: portal_sell_out(
                self.chain, self.a["portal"], pos["mint"], quote_token, balance))
            if not out:
                raise PortalRefused("the Portal sell does not simulate with the real allowance")
            from sniper.execution.evm_flap_curve import encode_swap_exact_input, portal_gas
            min_out = floor(out, self.slippage_bps)
            tx = {"to": self.a["portal"],
                  "data": encode_swap_exact_input(pos["mint"], quote_token, balance, min_out)}
            proven = portal_sell_out(self.chain, self.a["portal"], pos["mint"], quote_token, balance)
            if not proven or proven < min_out:
                raise PortalRefused("the leg-1 proof fell under its floor (%s < %s)" % (proven, min_out))
            try:
                estimate = self.chain.estimate_gas(tx)
            except Pending:
                raise
            except Exception as exc:
                raise PortalRefused("leg 1 will not estimate (%s)" % str(exc)[:120]) from exc
            gas = max(portal_gas(), int(estimate * SWAP_GAS_MARGIN))
            self._confirm_route(pos, balance, out=proven)
            raw, tx_hash = self._signed(tx, gas)
            # WRITE-AHEAD, journal first (Codex EVM-05): the exact bytes before the DB mark and the
            # send, so a crash anywhere after this line resumes by re-offering THESE bytes, and a
            # crash before the mark is repaired by _leg1_mark.
            self._save(state="leg1_submitted", leg1=tx_hash, leg1_nonce=self._nonce,
                       leg1_raw=raw, leg1_raw_sent=None, leg1_quote=str(out),
                       leg1_floor=str(min_out))
        except Exception:
            m.release_position_close_claim(self.conn, self.pid, claim)
            self._save(state="leg1_failed", leg1_raw=None)
            raise
        if not m.mark_position_close_submitted(self.conn, self.pid, claim, _now(), tx_hash):
            m.release_position_close_claim(self.conn, self.pid, claim)
            self._save(state="leg1_failed", leg1_raw=None)
            raise Refused("could not record leg 1's identity on the claim")
        self.sent = True
        self._offered.add(tx_hash)
        self.out("leg 1 sent %s: %d %s -> >= %d %s (quote %d)" % (
            tx_hash, balance, pos["mint"], min_out, quote_token, out))
        if self.chain.send(raw) is False:
            m.reset_position_close_submission(self.conn, self.pid, claim, tx_hash, _now())
            m.release_position_close_claim(self.conn, self.pid, claim)
            self._save(state="leg1_rejected", leg1_raw=None)
            raise Refused("the node rejected leg 1; the position is as it was")
        self._handed(tx_hash, "leg1_raw")

    def _leg1_mark(self):
        """The DB side of leg 1's write-ahead, repaired after a crash between the journal and the
        mark: our claim gets leg 1's hash. A claim or signature that is no longer ours stops the
        resume before any re-offer."""
        from sniper.persistence import db_memecoin as m
        pos = self._position()
        if pos.get("close_sig") == self.j["leg1"]:
            return
        if (pos.get("close_sig") is None and pos.get("close_claim_id") == self.j.get("claim_id")
                and m.mark_position_close_submitted(self.conn, self.pid, self.j["claim_id"], _now(),
                                                    self.j["leg1"])):
            return
        raise Refused("leg 1 %s is journaled but position %s's claim/signature is not ours "
                      "(claim %s, sig %s): look at it by hand" % (
                          self.j["leg1"], self.pid, pos.get("close_claim_id"), pos.get("close_sig")))

    def _leg1_settle(self):
        from sniper.persistence import db_memecoin as m
        self._leg1_mark()
        self._reoffer(self.j["leg1"], "leg1_raw")
        receipt = self._wait(self.j["leg1"])
        if receipt is None and self._dropped(
                "leg1_nonce", lambda: self.chain.balance(self.j["mint"]) == int(self.j["balance_raw"])):
            # DROPPED (Gamma MED on 4d255bf4a): its nonce is used and the bag is intact, so it never
            # landed. Reset and release, exactly as a reverted leg 1; the next call starts again.
            m.reset_position_close_submission(self.conn, self.pid, self.j["claim_id"], self.j["leg1"], _now())
            m.release_position_close_claim(self.conn, self.pid, self.j["claim_id"])
            self._save(state="leg1_failed", leg1_dropped=self.j["leg1"])
            raise Refused("leg 1 %s was dropped (nonce %s used, bag intact); the position is as it was"
                          % (self.j["leg1"], self.j.get("leg1_nonce")))
        if receipt is None:
            self._no_receipt(self.j["leg1"], "leg 1")
        if not receipt_ok(receipt):
            m.reset_position_close_submission(self.conn, self.pid, self.j["claim_id"], self.j["leg1"], _now())
            m.release_position_close_claim(self.conn, self.pid, self.j["claim_id"])
            self._save(state="leg1_failed")
            raise Refused("leg 1 reverted on chain; the position is as it was")
        delta = token_delta(receipt, self.j["quote_token"], self.chain.address)
        if delta <= 0:
            self._save(state="leg1_no_delta")
            raise Refused("leg 1 confirmed but paid no %s to the wallet: look at %s by hand"
                          % (self.j["quote_token"], self.j["leg1"]))
        self._save(state="leg1_confirmed", quote_delta=str(delta))
        self.out("leg 1 confirmed %s: +%d %s" % (self.j["leg1"], delta, self.j["quote_token"]))

    def _leg2(self):
        quote_token, amount = self.j["quote_token"], int(self.j["quote_delta"])
        held = self.chain.balance(quote_token)
        if held < amount:
            raise Refused("the wallet holds %d of %s, under leg 1's %d" % (held, quote_token, amount))
        routes = self._routes(quote_token, amount)
        if not routes:
            self._save(state="leg2_failed", leg2_error="no route")
            raise Refused("no Pancake route for the %s residue; leg 2 retries" % quote_token)
        refusals = []
        # A fallback only among routes within the slippage of the best: never v3/10000's 0.0002
        # WBNB beside 0.06 (the live quotes of 2026-10-02).
        near = [r for r in routes if r["out"] >= floor(routes[0]["out"], self.slippage_bps)]
        for best in near:                       # best first; a route the node refuses yields
            spender = self.a["v2_router"] if best["venue"] == "v2" else self.a["v3_router"]
            min_out = floor(best["out"], self.slippage_bps)
            self._allowance_ready(quote_token, spender, amount, "leg2_approve")
            self._room(1.0, "leg 2")
            tx = leg2_tx(best, quote_token, amount, min_out, self.chain.address,
                         v2_router=self.a["v2_router"], v3_router=self.a["v3_router"],
                         deadline=int(time.time()) + DEADLINE_S)
            try:
                gas = int(self.chain.estimate_gas(tx) * SWAP_GAS_MARGIN)   # a revert refuses here
            except Pending:
                raise
            except Exception as exc:
                refusals.append("%s: %s" % (best["venue"], str(exc)[:80]))
                continue
            if best["venue"] in ("v3", "v3_path"):
                proven = self.chain.call(tx["to"], tx["data"])
                if not proven or int(proven[2:66] or "0", 16) < min_out:
                    refusals.append("%s: proof under floor" % best["venue"])
                    continue
            break
        else:
            self._save(state="leg2_failed", leg2_error="; ".join(refusals)[:300])
            raise Refused("every leg-2 route refused (%s); leg 2 retries" % "; ".join(refusals)[:200])
        raw, tx_hash = self._signed(tx, gas)
        self._save(state="leg2_submitted", leg2=tx_hash, leg2_nonce=self._nonce, leg2_route=best,
                   leg2_raw=raw, leg2_raw_sent=None, leg2_floor=str(min_out),
                   leg2_quote_before=str(held))
        self.sent = True
        self._offered.add(tx_hash)
        self.out("leg 2 sent %s via %s: %d %s -> >= %.6f WBNB (quote %.6f)" % (
            tx_hash, best["venue"], amount, quote_token, min_out / 1e18, best["out"] / 1e18))
        if self.chain.send(raw) is False:
            self._save(state="leg2_rejected")
            raise Refused("the node rejected leg 2; leg 2 retries")
        self._handed(tx_hash, "leg2_raw")

    def _leg2_settle(self):
        self._reoffer(self.j["leg2"], "leg2_raw")
        receipt = self._wait(self.j["leg2"])
        if receipt is None and self._dropped("leg2_nonce", lambda: self.chain.balance(
                self.j["quote_token"]) == int(self.j.get("leg2_quote_before") or -1)):
            self._save(state="leg2_failed", leg2_error="dropped", leg2_dropped=self.j["leg2"])
            raise Refused("leg 2 %s was dropped (nonce %s used, the %s intact); leg 2 retries"
                          % (self.j["leg2"], self.j.get("leg2_nonce"), self.j["quote_token"]))
        if receipt is None:
            self._no_receipt(self.j["leg2"], "leg 2")
        if not receipt_ok(receipt):
            self._save(state="leg2_failed", leg2_error="reverted")
            raise Refused("leg 2 reverted; the %s stays in the wallet; leg 2 retries"
                          % self.j["quote_token"])
        wbnb = token_delta(receipt, WBNB, self.chain.address)
        if wbnb <= 0:
            self._save(state="leg2_no_delta")
            raise Refused("leg 2 confirmed but paid no WBNB: look at %s by hand" % self.j["leg2"])
        self._save(state="leg2_confirmed", wbnb_delta=str(wbnb))
        self.out("leg 2 confirmed %s: +%.6f WBNB" % (self.j["leg2"], wbnb / 1e18))

    def _book(self):
        from sniper.persistence import db_memecoin as m
        price = self.price_fn()
        if not price or not math.isfinite(float(price)) or price <= 0:
            raise Refused("the BNB price is unavailable; the booking retries (both legs are done)")
        pos = self._position()
        qty = m.open_qty(self.conn, self.pid)
        notional = int(self.j["wbnb_delta"]) / 1e18 * float(price)
        realized = m.realized_after_exit(self.conn, self.pid, notional, pos.get("entry_notional"))
        booked = m.book_full_exit_and_finalize(
            self.conn, position_id=self.pid, claim_id=self.j["claim_id"], sig=self.j["leg1"],
            closed_at=_now(), realized_pnl=realized,
            exit_reason=self.j.get("exit_reason") or self.exit_reason,
            fill_kind=m.full_exit_fill_kind(self.j.get("exit_reason") or self.exit_reason),
            price=notional / qty if qty > 0 else None, qty=qty, notional=notional, fill_source="chain")
        if not booked:
            raise Refused("the close did not book (the claim or signature changed); see the journal")
        self._save(state="booked", notional_usd=notional, realized_usd=realized, bnb_usd=price)
        with _READ_BUDGETS_LOCK:
            _READ_BUDGETS.pop(self.pid, None)
        self.out("BOOKED position %s: $%.2f received, realized %s" % (
            self.pid, notional, "unknown (a partial is unpriced)" if realized is None else "$%.2f" % realized))


# --- the runner's rung ----------------------------------------------------------------------------

DEFAULT_SLIPPAGE_BPS = 300
# ELIGIBILITY, PREWARMED (Alpha 2026-10-02: an exit must not pay the ~130 ms read). A coin's curve
# quote token and its native-swap flag are fixed at creation, so a DEFINITIVE answer is cached for
# the process: yes (_ELIGIBLE) or no (_NOT_ELIGIBLE, the Portal reverted getTokenV6: not a flap.sh
# coin; or a flap.sh coin the ladder can price). An UNKNOWN answer (transport failure, a non-revert
# RPC error, an unreadable state) is never cached: that exit runs the ladder, and the next call reads
# again. `prewarm` reads off-thread from the exit preview sweep, so the first exit reads memory.
_ELIGIBLE = set()
_NOT_ELIGIBLE = set()
_PREWARMING = set()
_ELIGIBLE_LOCK = threading.Lock()


def journal_path(position_id):
    from sniper.util.state_paths import state_path
    return state_path("data", "flap_quote_exit", "%d.json" % int(position_id))


def _portal_knows(mint, rpc_url):
    """'yes' (getTokenV6 answered), 'no' (the Portal reverted: not a flap.sh coin) or 'unknown'."""
    from sniper.execution import evm_flap_curve as F
    from sniper.execution.evm_wallet import _rpc
    try:
        answer = _rpc(rpc_url, "eth_call", [{"to": F.FLAP_PORTAL[CHAIN], "data": "0x"
                                             + F._SEL_GET_TOKEN_V6 + mint[2:].rjust(64, "0")}, "latest"])
    except Exception:
        return "unknown"
    error = (answer or {}).get("error")
    if error is None:
        return "yes" if (answer or {}).get("result") not in (None, "0x") else "unknown"
    message = str(error.get("message") if isinstance(error, dict) else error).lower()
    return "no" if "revert" in message else "unknown"


def cached_eligibility(mint):
    """True, False, or None when nothing definitive is cached. Memory only."""
    mint = str(mint or "").lower()
    with _ELIGIBLE_LOCK:
        if mint in _ELIGIBLE:
            return True
        if mint in _NOT_ELIGIBLE:
            return False
    return None


def eligible(mint, rpc_url, *, state_fn=None, knows_fn=None):
    """True when `mint` is a flap.sh coin quoted in a token whose Portal takes no native: the class
    the ladder refuses (flap_curve_non_native_quote_refused). The cache first; else one read (two
    for a flap.sh coin). Never raises; an unknown answer is False this time and is not cached."""
    from sniper.execution import evm_flap_curve as F
    mint = str(mint or "").lower()
    cached = cached_eligibility(mint)
    if cached is not None:
        return cached
    try:
        known = (knows_fn or _portal_knows)(mint, rpc_url)
        if known == "unknown":
            return False
        ok = False
        if known == "yes":
            state = (state_fn or F.curve_state)(CHAIN, mint, rpc_url)
            if not state:
                return False                                      # unknown: not cached
            ok = bool(state.get("quote_token") and not F.is_native_quote(state)
                      and not F.takes_native_payment(state))
    except Exception:
        logger.warning("flap quote exit: eligibility unreadable mint=%s", mint, exc_info=True)
        return False
    with _ELIGIBLE_LOCK:
        (_ELIGIBLE if ok else _NOT_ELIGIBLE).add(mint)
    return ok


def prewarm(mint, rpc_url, **kwargs):
    """Read `mint`'s eligibility on a daemon thread unless it is cached or already being read. For
    the exit preview sweep: never blocks, never raises."""
    mint = str(mint or "").lower()
    if not mint or cached_eligibility(mint) is not None:
        return False
    with _ELIGIBLE_LOCK:
        if mint in _PREWARMING:
            return False
        _PREWARMING.add(mint)

    def run():
        try:
            eligible(mint, rpc_url, **kwargs)
        finally:
            with _ELIGIBLE_LOCK:
                _PREWARMING.discard(mint)
    try:
        threading.Thread(target=run, name="flap-quote-eligible", daemon=True).start()
    except Exception:
        with _ELIGIBLE_LOCK:
            _PREWARMING.discard(mint)
        return False
    return True


def prewarm_after_fill(mint, rpc_url, result):
    """The chain-56 entry seam's hook, after a fill has returned: prewarm with the knob on and a
    filled result. Never raises."""
    try:
        from sniper.settings import memecoin as cfg
        if cfg.MEMECOIN_EVM_FLAP_QUOTE_EXIT and str((result or {}).get("status")) == "filled":
            prewarm(mint, rpc_url)
    except Exception:
        logger.exception("flap quote exit prewarm at entry failed mint=%s", mint)


_RESUMABLE = ("approving_leg1",) + (
    "leg1_submitted", "leg1_confirmed", "leg2_failed", "leg2_rejected", "leg2_submitted",
    "leg2_confirmed", "leg1_no_delta", "leg2_no_delta")
# Past leg 1's send the bag is sold or may be: a call ALWAYS resumes these, whatever eligibility reads.
_AFTER_LEG1 = ("leg1_submitted", "leg1_confirmed", "leg2_failed", "leg2_rejected", "leg2_submitted",
               "leg2_confirmed", "leg1_no_delta", "leg2_no_delta")
# THE SWEEP NEVER WAITS LONG ON THIS CLASS (Gamma MED on a7f43b4cf; Alpha). real_exit_sweep calls
# exit_fn inline for every chain, Solana stops included, so one runner call does what fits in
# STEP_BUDGET_S and returns `pending`; the journal says where the next call resumes. The runner's
# recovery pass (memecoin_real_runner.recover_unverified_closes) calls again for any position past
# leg 1, so a sold bag is finished even when no exit trigger re-fires.
STEP_BUDGET_S = 3.0
# THE PORTAL STOPS SELLING (Alpha LOW3): DEMOTE_AFTER PortalRefused in a row for one coin drops its
# cached yes; the ladder (and its dust guard) runs for it from then on, and Telegram is paged once.
DEMOTE_AFTER = 3
_PORTAL_REFUSALS = {}


def resume_due(position_id):
    """True when this position's journal is past leg 1's send and not booked: the runner's recovery
    pass must call its exit again. Reads one small file; never raises."""
    try:
        return read_journal(journal_path(position_id)).get("state") in _AFTER_LEG1
    except Exception:
        return False


# THE BACKSTOP (Alpha, on Gamma's MED): at most RESUME_CAP recovery resumes per position in the SAME
# journal state; past it, one Telegram page and the recovery pass stops calling (the two-leg journal keeps ownership; generic recovery
# must never interpret leg 1 as a complete cash exit). A state change starts the count over.
RESUME_CAP = 20
_RESUMES = {}


def resume_admit(position_id, page_fn=None):
    """True while the recovery pass may resume this position again. Never raises."""
    try:
        state = read_journal(journal_path(position_id)).get("state")
    except Exception:
        state = None
    pid = int(position_id)
    with _ELIGIBLE_LOCK:
        prev, n = _RESUMES.get(pid, (None, 0))
        n = n + 1 if prev == state else 1
        _RESUMES[pid] = (state, n)
    if n <= RESUME_CAP:
        return True
    if n == RESUME_CAP + 1:
        text = ("flap quote exit position %s: %d recovery resumes in state %s without progress; "
                "automatic resumes paused; close remains owned by its two-leg journal. Look at %s." % (pid, RESUME_CAP, state, journal_path(pid)))
        logger.warning("[memecoin] %s", text)
        try:
            if page_fn is not None:
                page_fn(text)
            else:
                from sniper.services.memecoin_alerts import send_alert
                send_alert("flap_quote_exit_stalled", text)
        except Exception:
            logger.exception("flap quote exit: stall page failed pos=%s", pid)
    return False


def _demote(mint, pid, reason, page_fn):
    with _ELIGIBLE_LOCK:
        _ELIGIBLE.discard(mint)
        _NOT_ELIGIBLE.add(mint)
        _PORTAL_REFUSALS.pop(mint, None)
    text = ("flap quote exit DEMOTED for %s (position %s) after %d Portal refusals in a row: %s. "
            "The ladder and its dust guard handle it now." % (mint, pid, DEMOTE_AFTER, reason))
    logger.warning("[memecoin] %s", text)
    try:
        if page_fn is not None:
            page_fn(text)
        else:
            from sniper.services.memecoin_alerts import send_alert
            send_alert("flap_quote_exit_demoted", text)
    except Exception:
        logger.exception("flap quote exit: demotion page failed mint=%s", mint)


def _grade(pid, mint, ladder_value_fn, position):
    """For grading only, off the sweep: the ladder's whole-balance answer for the bag the core is
    selling. Never decides anything."""
    try:
        value = ladder_value_fn(position)
    except Exception:
        logger.warning("flap quote exit: ladder value unreadable pos=%s", pid, exc_info=True)
        value = None
    logger.warning("[memecoin] flap quote exit grading pos=%s mint=%s ladder_wbnb=%s (the core decides; "
                   "its value is on its leg lines)", pid, mint,
                   "%.6f" % (value / 1e18) if value is not None else "none")


def initiating_reason(journal, incoming):
    # A force sell may supersede an unsigned attempt. Once leg 1 is signed, finish its own intent.
    manual = str(incoming) == "manual" or str(incoming).startswith("manual_")
    if manual and journal.get("state") not in _AFTER_LEG1:
        return incoming
    return journal.get("exit_reason") or incoming


def runner_exit(conn, position, exit_reason, now_iso, *, run_factory, eligible_fn,
                ladder_value_fn=None, page_fn=None):
    """The runner's exit for this class, or None to let the ladder run (not eligible, no journal).

    For an eligible coin this runs INSTEAD of the ladder: its aggregator answer for such a bag is
    the garbage route (Kyber quoted 0.0002 WBNB for the whole DAODUN bag, and the runner signed 9
    sends on it on b_stop, 2026-10-02). A journal past leg 1 always resumes here. One call does
    what fits in its step budget (STEP_BUDGET_S, set by the runner's run_factory). Returns the
    executor contract: closed | pending | stuck | aborted."""
    pid, mint = int(position["id"]), str(position.get("mint") or "").lower()
    path = journal_path(pid)
    state = read_journal(path).get("state")
    if state not in _AFTER_LEG1 and not eligible_fn(mint):
        return None
    run = run_factory(position, path)
    run.exit_reason = initiating_reason(run.j, exit_reason)
    if state not in _RESUMABLE and ladder_value_fn is not None:
        threading.Thread(target=_grade, args=(pid, mint, ladder_value_fn, position),
                         name="flap-quote-grade", daemon=True).start()
    try:
        journal = run.run_locked()
    except Pending as exc:
        state = run.j.get("state")
        return {"status": "pending", "reason": "flap_quote_exit: %s" % str(exc)[:160],
                "tx_hash": run.j.get("leg2" if state == "leg2_submitted" else "leg1")}
    except Refused as exc:
        state = run.j.get("state")
        reason = "flap_quote_exit: %s" % str(exc)[:160]
        logger.warning("[memecoin] flap quote exit pos=%s stopped state=%s: %s", pid, state, exc)
        if isinstance(exc, PortalRefused):
            with _ELIGIBLE_LOCK:
                n = _PORTAL_REFUSALS[mint] = _PORTAL_REFUSALS.get(mint, 0) + 1
            if n >= DEMOTE_AFTER:
                _demote(mint, pid, str(exc)[:120], page_fn)
        if state in _AFTER_LEG1:
            return {"status": "stuck", "failure_type": "flap_leg2_pending", "reason": reason}
        with _READ_BUDGETS_LOCK:
            _READ_BUDGETS.pop(pid, None)
        return {"status": "aborted", "reason": reason}
    with _ELIGIBLE_LOCK:
        _PORTAL_REFUSALS.pop(mint, None)
    return {"status": "closed", "realized": journal.get("realized_usd"),
            "received": journal.get("notional_usd"), "tx_hash": journal.get("leg1"),
            "route": "flap_quote_exit"}


def addresses(cfg):
    """Portal, Pancake V2 router, V3 SmartRouter and QuoterV2 for chain 56, from the settings."""
    from sniper.execution import evm_flap_curve as F
    v3 = cfg.memecoin_evm_v3_deployments(CHAIN)[0]
    return {"portal": F.FLAP_PORTAL[CHAIN], "v2_router": cfg.memecoin_evm_v2_exit_config(CHAIN)["router"],
            "v3_router": v3["router"], "v3_quoter": v3["quoter"]}
