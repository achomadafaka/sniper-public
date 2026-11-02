"""G3 execution rehearsal on a local Ink mainnet fork (spec 2026-09-29 §5 G3; Alpha GO 2026-09-30).

The REAL executor (`build_real_evm_executor`, chain 57073, production quote rungs) against real Ink
contracts on an anvil fork: buy -> owned balance -> whole-balance sell, partial exit -> remaining
balance sell, and crash injection at the durable transitions of an entry and an exit, each followed
by the runner's own recovery pass over a fresh executor (a restarted process).

OFFLINE BY CONSTRUCTION. Every URL this process opens must be the local fork: `urlopen` refuses any
other host, and the chain wrapper refuses any RPC URL but the fork's. The wallet is anvil's own
prefunded test account, never a funded key. Nothing is ever sent to Ink mainnet.

Runs only when asked (it needs anvil and a public read host for the fork's lazy state):
    INK_FORK_UPSTREAM=https://rpc-gel.inkonchain.com INK_FORK_BLOCK=<n> INK_FORK_V3_TOKENS=0x..,0x.. \
    INK_FORK_EVIDENCE=<dir>/g3.jsonl python3 -m pytest tests/ink/test_fork_rehearsal.py -q -n 0
Never fork on the host a census or backfill is reading (thirdweb drew HTTP 429 that way,
2026-09-29). rpc-gel is an Ink free-submission endpoint: fork on it only while Ink funded is OFF.
Tokens must have a WETH pool on the certified Uniswap V3 factory 0x640887a9.. that the executor's
router and quoter use, not the Slipstream-style fork 0x04625b04...
Scope v1: uniswap_v3 (SwapRouter02) only. uniswap_v4_hookless has no Ink entry in
evm_v4_quote.V4, so it stays an explicit coverage exclusion, not a pass.
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
from eth_account import Account

from sniper.persistence import db_memecoin as db
from sniper.settings import memecoin as cfg

UPSTREAM = os.getenv("INK_FORK_UPSTREAM", "").strip()
TOKENS = [t.strip().lower() for t in os.getenv("INK_FORK_V3_TOKENS", "").split(",") if t.strip()]
EVIDENCE = os.getenv("INK_FORK_EVIDENCE", "").strip()
ANVIL = shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")
ANVIL0_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"   # anvil's test key
INK, WETH = "57073", "0x4200000000000000000000000000000000000006"
SEL_APPROVE, SEL_DEPOSIT = "095ea7b3", "d0e30db0"

pytestmark = pytest.mark.skipif(not (UPSTREAM and TOKENS and Path(ANVIL).exists()),
                                reason="G3 fork rehearsal runs only with INK_FORK_UPSTREAM, "
                                       "INK_FORK_V3_TOKENS and anvil")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


@pytest.fixture(scope="module")
def fork():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    args = [ANVIL, "--fork-url", UPSTREAM, "--chain-id", "57073", "--port", str(port),
            "--host", "127.0.0.1", "--compute-units-per-second", "50", "--silent"]
    if os.getenv("INK_FORK_BLOCK"):
        args += ["--fork-block-number", os.environ["INK_FORK_BLOCK"]]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if _post(url, "eth_chainId", [])["result"] == "0xdef1":
                    break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("anvil fork did not come up")
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture(autouse=True)
def only_the_fork(fork, monkeypatch):
    """No request leaves this host: the rehearsal cannot reach Ink mainnet or any price host."""
    real = urllib.request.urlopen

    def guarded(req, *a, **k):
        target = req.full_url if hasattr(req, "full_url") else str(req)
        if not target.startswith(fork):
            raise AssertionError("G3 rehearsal refused a non-fork request: %s" % target)
        return real(req, *a, **k)
    monkeypatch.setattr(urllib.request, "urlopen", guarded)
    monkeypatch.setattr(cfg, "ink_capability_reason", lambda *a, **k: None)   # rehearsal only
    # The quote rungs read cfg.evm_rpc_for_chain at call time; unset, every entry aborts
    # no_rpc_configured before any fork call (first run 2026-09-30, 7 of 7).
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_" + INK, fork)
    snap = _post(fork, "evm_snapshot", [])["result"]
    yield
    _post(fork, "evm_revert", [snap])


class ForkChain:
    """The executor's RPC transport: forwards to the fork, refuses any other URL, and injects one
    crash (SystemExit, which no `except Exception` absorbs) around a chosen send."""

    def __init__(self, url, owner):
        self.url, self.owner = url, owner.lower()
        self.crash = None              # "<phase>:<when>_send_<kind>", e.g. "entry:after_send_swap"
        self.phase = "entry"
        self.sent = []                 # (phase, kind, tx hash)

    def http(self, method, url, headers=None, body=None, timeout=10):
        if not str(url).startswith(self.url):
            raise AssertionError("G3 rehearsal refused RPC to %s" % url)
        req = json.loads(body)
        if isinstance(req, list):      # a JSON-RPC batch (the V3 quote reads batch their calls)
            assert all(r.get("method") != "eth_sendRawTransaction" for r in req), \
                "a batched send would bypass crash injection"
            http_req = urllib.request.Request(self.url, data=json.dumps(req).encode(),
                                              headers={"content-type": "application/json"})
            with urllib.request.urlopen(http_req, timeout=60) as resp:
                return 200, json.loads(resp.read()), None
        if req.get("method") == "eth_sendRawTransaction":
            kind = self._kind(req["params"][0])
            key = f"{self.phase}:before_send_{kind}"
            if self.crash == key:
                self.crash = None
                raise SystemExit(key)
            reply = _post(self.url, "eth_sendRawTransaction", req["params"])
            self.sent.append((self.phase, kind, reply.get("result")))
            key = f"{self.phase}:after_send_{kind}"
            if self.crash == key:
                self.crash = None
                raise SystemExit(key)
            return 200, reply, None
        return 200, _post(self.url, req["method"], req.get("params") or []), None

    @staticmethod
    def _kind(raw):
        from eth_account._utils.legacy_transactions import Transaction
        data = Transaction.from_bytes(bytes.fromhex(raw[2:])).data.hex().removeprefix("0x")
        return ("approve" if data.startswith(SEL_APPROVE) else
                "wrap" if data.startswith(SEL_DEPOSIT) else "swap")

    def balance(self, token):
        data = "0x70a08231" + "0" * 24 + self.owner[2:]
        return int(_post(self.url, "eth_call", [{"to": token, "data": data}, "latest"])["result"], 16)


@pytest.fixture
def rehearsal(fork, tmp_path):
    acct = Account.from_key(ANVIL0_KEY)
    chain = ForkChain(fork, acct.address)
    # Wrap 0.05 ETH of anvil's test balance: the entries spend WETH.
    gas_price = int(_post(fork, "eth_gasPrice", [])["result"], 16)
    nonce = int(_post(fork, "eth_getTransactionCount", [acct.address, "pending"])["result"], 16)
    signed = acct.sign_transaction({"to": WETH, "value": 5 * 10 ** 16, "data": "0x" + SEL_DEPOSIT,
                                    "gas": 100_000, "gasPrice": gas_price * 2, "nonce": nonce,
                                    "chainId": 57073})
    assert _post(fork, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")]
                 ).get("result")
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    from sniper.execution import evm_real_seams as S

    def build():
        return S.build_real_evm_executor(acct=acct, chain_id=INK, rpc_url=fork, http=chain.http,
                                         price_fn=lambda: 2500.0)

    class R:
        pass
    r = R()
    r.chain, r.conn, r.build, r.ex = chain, conn, build, build()
    r.reserve = lambda mint, usd=20.0: db.reserve_pending(
        conn, mint=mint, source_user_id="g3-rehearsal", strategy="alpha_b", notional=usd,
        reserved_at="2026-09-30T12:00:00Z", network_id=INK)
    return r


def _evidence(**row):
    if EVIDENCE:
        with open(EVIDENCE, "a") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _open_position(conn, mint):
    return conn.execute("SELECT * FROM memecoin_positions WHERE mint=? AND network_id=? "
                        "ORDER BY id DESC LIMIT 1", (mint, INK)).fetchone()


def _atoms(row_qty, decimals):
    return int(round(float(row_qty) * 10 ** decimals))


@pytest.mark.parametrize("mint", TOKENS)
def test_v3_buy_owned_balance_then_whole_balance_sell(rehearsal, mint):
    r = rehearsal
    pid = r.reserve(mint)
    res = r.ex["execute_fn"](r.conn, pid, mint, 20.0, "2026-09-30T12:00:01Z")
    _evidence(test="round_trip_entry", mint=mint, result={k: res.get(k) for k in (
        "status", "reason", "fill_price", "notional", "tx_hash")})
    assert res["status"] == "filled", res
    held = r.chain.balance(mint)
    assert held > 0
    pos = db.get_position(r.conn, res["position_id"])
    exit_res = r.ex["exit_fn"](r.conn, pos, "manual", "2026-09-30T12:01:00Z")
    _evidence(test="round_trip_exit", mint=mint, held_atoms=held,
              result={k: exit_res.get(k) for k in ("status", "reason", "realized", "tx_hash")})
    assert exit_res["status"] == "closed", exit_res
    assert r.chain.balance(mint) == 0                          # the WHOLE balance was sold
    closed = db.get_position(r.conn, res["position_id"])
    assert closed["is_open"] == 0 and closed["realized_pnl"] is not None
    assert [k for p, k, _h in r.chain.sent if k == "swap"] == ["swap", "swap"]


@pytest.mark.parametrize("mint", TOKENS)
def test_v3_partial_exit_then_remaining_balance_sell(rehearsal, mint):
    r = rehearsal
    res = r.ex["execute_fn"](r.conn, r.reserve(mint), mint, 20.0, "2026-09-30T12:00:01Z")
    assert res["status"] == "filled", res
    before = r.chain.balance(mint)
    r.chain.phase = "exit"
    part = r.ex["derisk_fn"](r.conn, db.get_position(r.conn, res["position_id"]), 0.5,
                             "2026-09-30T12:01:00Z")
    after_part = r.chain.balance(mint)
    _evidence(test="partial", mint=mint, before=before, after_part=after_part,
              result={k: part.get(k) for k in ("status", "reason", "fraction")})
    assert 0 < after_part < before, part
    rest = r.ex["exit_fn"](r.conn, db.get_position(r.conn, res["position_id"]), "manual",
                           "2026-09-30T12:02:00Z")
    assert rest["status"] == "closed", rest
    assert r.chain.balance(mint) == 0


def _receipt_proceeds(r):
    """The funded EVM context's receipt pricer, as memecoin_evm_leg.build_real_evm_context builds
    it (memecoin_evm_leg.py:333). The raw executor exposes none; the runner never reads it there."""
    from sniper.execution.evm_real_seams import exit_proceeds_atomic

    def proceeds(tx_hash, *, quote_asset_usd=None):
        atomic, _source = exit_proceeds_atomic(
            tx_hash, r.chain.url, owner=r.chain.owner, quote_token=WETH, http=r.chain.http,
            native=False)                                       # 57073: never a native block delta
        price = quote_asset_usd or 2500.0                       # the rehearsal's price_fn
        return None if atomic is None else atomic / 10 ** 18 * float(price)
    return proceeds


def _recover_entries(r, now_iso="2026-09-30T12:05:00Z"):
    from sniper.services.memecoin_real_runner import recover_stale_reservations
    ex = r.build()                                              # a restarted process
    recover_stale_reservations(
        r.conn, confirm_for=lambda _net, sig: ex["_confirm_once"](sig), now_iso=now_iso,
        max_age_s=60,
        confirmed_entry_for=lambda c, row, now: ex["recover_confirmed_entry_fn"](c, row, now),
        confirmed_nonce_for=lambda _net: ex["confirmed_nonce_fn"](),
        pending_nonce_for=lambda _net: ex["pending_nonce_fn"](),
        token_balance_for=lambda _net, mint: r.chain.balance(mint))
    return ex


@pytest.mark.parametrize("crash, landed", [
    ("entry:before_send_swap", False),
    ("entry:after_send_swap", True),
])
@pytest.mark.parametrize("mint", TOKENS[:1])
def test_an_entry_crash_then_restart_books_at_most_once(rehearsal, mint, crash, landed):
    r = rehearsal
    r.chain.crash = crash
    pid = r.reserve(mint)
    with pytest.raises(SystemExit):
        r.ex["execute_fn"](r.conn, pid, mint, 20.0, "2026-09-30T12:00:01Z")
    if r.conn.in_transaction:
        r.conn.rollback()
    _mine = _post(r.chain.url, "evm_mine", [])                  # the fork includes what was sent
    _recover_entries(r)
    swaps = [h for p, k, h in r.chain.sent if k == "swap"]
    held = r.chain.balance(mint)
    positions = r.conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE network_id=?",
                               (INK,)).fetchone()[0]
    _evidence(test="entry_crash", mint=mint, crash=crash, swaps=len(swaps), held_atoms=held,
              positions=positions)
    assert len(swaps) <= 1
    assert (held > 0) is landed
    # A swap that landed is booked exactly once; one that never left is never booked.
    assert positions == (1 if landed else 0)


@pytest.mark.parametrize("mint", TOKENS[:1])
def test_an_exit_crash_after_send_never_sells_twice(rehearsal, mint):
    from sniper.services.memecoin_real_runner import recover_unverified_closes
    r = rehearsal
    res = r.ex["execute_fn"](r.conn, r.reserve(mint), mint, 20.0, "2026-09-30T12:00:01Z")
    assert res["status"] == "filled", res
    r.chain.phase, r.chain.crash = "exit", "exit:after_send_swap"
    with pytest.raises(SystemExit):
        r.ex["exit_fn"](r.conn, db.get_position(r.conn, res["position_id"]), "manual",
                        "2026-09-30T12:01:00Z")
    if r.conn.in_transaction:
        r.conn.rollback()
    _post(r.chain.url, "evm_mine", [])
    ex = r.build()
    recover_unverified_closes(r.conn, {INK: {"confirm_once_fn": ex["_confirm_once"],
                                             "receipt_proceeds_fn": _receipt_proceeds(r)}},
                              "2026-09-30T12:05:00Z")
    exit_swaps = [h for p, k, h in r.chain.sent if p == "exit" and k == "swap"]
    pos = db.get_position(r.conn, res["position_id"])
    _evidence(test="exit_crash", mint=mint, exit_swaps=len(exit_swaps),
              held_atoms=r.chain.balance(mint), position={k: pos[k] for k in (
                  "is_open", "close_sig", "exit_reason", "realized_pnl")})
    assert len(exit_swaps) == 1                                 # never a second full-balance sell
    assert r.chain.balance(mint) == 0
    assert pos["close_sig"]                                     # the sell's identity survived
    assert pos["realized_pnl"] is not None                      # priced from the sell's receipt
