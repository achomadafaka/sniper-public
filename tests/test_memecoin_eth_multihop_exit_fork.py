"""Chain-1 fork rehearsal of a V4 MULTIHOP entry and its whole-balance exit (2026-10-07, Alpha).

The chain-1 safety sell probe refuses multihop-only coins because v4_multihop discards every SELL
quote in the ladder (round_trip_over_budget:None; the gate fix waits for the operator). A funded
multihop position does not exit through that ladder: it replays its stored route through
dex_v4_multihop_exit_quote with the four-field ExactInputParams (02095dcd1), which no fork or live
sell has ever run on chain 1. This module is that proof, on the step-7 harness (beta/eth-mainnet
8147497a8, docs/audits/2026-09-30-eth-step7-fork-rehearsal.md): the REAL chain-1 context buys
0x24c2c6f7 through the ladder (it must pick uniswap_v4_multihop) and sells the whole balance
through exit_fn (the stored-route branch). It lifts the chain-1 multihop skip only on PASS.

NOTHING LEAVES THIS HOST: loopback only, a throwaway key, anvil reads lazy state from
ETH_FORK_UPSTREAM (custody, the relay and every funded read provider refused as upstream).

    ETH_FORK_UPSTREAM=https://mainnet.gateway.tenderly.co \
    ETH_FORK_EVIDENCE=<dir>/multihop.jsonl \
    python3 -m pytest tests/test_memecoin_eth_multihop_exit_fork.py -q -n 0 -p no:cacheprovider

The coin file (default docs/superpowers/evidence/eth/coins_multihop_24c2.json) carries the coin's
V4 pools and its bridge tokens' pools with their PoolKeys from the ledger; they stand in for the
GeckoTerminal ranking and the stored key cache (each key hash-checked against its pool id).
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from eth_account import Account
from eth_utils import keccak

from sniper.persistence import db_memecoin as db
from sniper.settings import memecoin as cfg

UPSTREAM = os.getenv("ETH_FORK_UPSTREAM", "").strip()
COINS_FILE = os.getenv("ETH_FORK_COINS", "").strip() or str(
    Path(__file__).resolve().parents[1] / "docs/superpowers/evidence/eth/coins_multihop_24c2.json")
EVIDENCE = os.getenv("ETH_FORK_EVIDENCE", "").strip()
ANVIL = shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")
COINS = json.loads(Path(COINS_FILE).read_text()) if COINS_FILE and Path(COINS_FILE).exists() else []
ONLY = {t.strip().upper() for t in os.getenv("ETH_FORK_ONLY", "").split(",") if t.strip()}
if ONLY:
    COINS = [c for c in COINS if c["ticker"].upper() in ONLY]
ETH, ETH_USD, CLIP = "1", 2500.0, 20.0
WETH_CHECKSUM = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
ROUTERS = {"0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "v4",      # UniversalRouter
           "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "v3",      # SwapRouter02
           "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "v2"}      # Router02
SELECTORS = {"2e1a7d4d": "unwrap", "d0e30db0": "wrap", "095ea7b3": "approve",
             "87517c45": "permit2_approve"}
# Custody, the relay, and every provider behind a funded pool or a capture lane (2026-09-30).
DENIED_UPSTREAM = ("infura.io", "flashbots.net", "mevblocker.io", "publicnode.com", "alchemy.com",
                   "drpc.org", "blastapi.io", "quiknode.pro", "nodereal.io")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
LIVE_GLOBAL_KNOBS = {
    "MEMECOIN_EVM_V4_WETH_CURRENCY": True,               # default off: WETH-currency pools invisible
    "MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE": True,   # default off: the whole ladder re-runs
    "MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S": 1.5,        # default 2.0
    "MEMECOIN_EVM_V4_MULTIHOP_ENABLED": os.getenv("ETH_FORK_MULTIHOP", "1") == "1",   # default off
    "MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS": 20.0,
    "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS": 3,              # default 2
}

from sniper.execution.evm_wallet import load_keypair as _REAL_LOAD_KEYPAIR  # noqa: E402

pytestmark = pytest.mark.timeout(900)       # a held swap waits out the executor's 60 s receipt poll

fork_only = pytest.mark.skipif(
    not (UPSTREAM and COINS and Path(ANVIL).exists()),
    reason="the multihop rehearsal runs only with ETH_FORK_UPSTREAM, a coin file and anvil")
GAS_ROWS = []


def upstream_refusal(url):
    """A reason when `url` may not feed the fork, else None."""
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    if not host:
        return "no fork upstream host"
    for denied in DENIED_UPSTREAM:
        if host == denied or host.endswith("." + denied):
            return "fork upstream %s is custody, the relay, or a funded read provider" % host
    return None


def loopback_only(monkeypatch, refused):
    """Refuse, and record, every connection or name lookup that is not loopback."""
    real_connect, real_lookup = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if self.family in (socket.AF_INET, socket.AF_INET6) and str(host) not in LOOPBACK:
            refused.append("connect %s" % (host,))
            raise OSError("step 7 refused a connection to %s" % (host,))
        return real_connect(self, address)

    def lookup(host, *a, **k):
        if str(host) not in LOOPBACK:
            refused.append("lookup %s" % (host,))
            raise socket.gaierror("step 7 refused a name lookup for %s" % (host,))
        return real_lookup(host, *a, **k)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)


def tx_fields(raw_hex):
    """(to, calldata hex) of a signed legacy or typed transaction."""
    import rlp
    data = bytes.fromhex(str(raw_hex).removeprefix("0x"))
    if data[0] >= 0xc0:
        items, to_at, data_at = rlp.decode(data), 3, 5
    else:
        items = rlp.decode(data[1:])
        to_at, data_at = (4, 6) if data[0] == 1 else (5, 7)
    return "0x" + items[to_at].hex(), items[data_at].hex()


def tx_kind(raw_hex):
    to, data = tx_fields(raw_hex)
    return SELECTORS.get(data[:8]) or ("swap_" + ROUTERS.get(to.lower(), "unknown"))


def percentile(values, q):
    """Nearest-rank percentile; one sample is its own p50 and p90."""
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * q // 100) - 1)]


# ---- always on: the guards themselves ----

def test_custody_the_relay_and_funded_read_providers_are_refused_as_fork_upstream():
    for url in ("https://mainnet.infura.io/v3/x", "https://rpc.flashbots.net/fast",
                "https://ethereum-rpc.publicnode.com", "https://eth.drpc.org",
                "https://eth-mainnet.g.alchemy.com/v2/x", ""):
        assert upstream_refusal(url), url
    assert upstream_refusal("https://mainnet.gateway.tenderly.co") is None
    assert upstream_refusal("https://notinfura.io.example.org") is None


def test_the_socket_guard_refuses_and_records_every_non_loopback_target(monkeypatch):
    refused = []
    loopback_only(monkeypatch, refused)
    with pytest.raises(OSError):
        socket.create_connection(("rpc.flashbots.net", 443), timeout=1)
    with socket.socket() as s, pytest.raises(OSError):
        s.connect(("93.184.216.34", 443))
    assert refused == ["lookup rpc.flashbots.net", "connect 93.184.216.34"]
    with socket.socket() as listener:                           # loopback still works
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        socket.create_connection(listener.getsockname(), timeout=1).close()
    assert len(refused) == 2


def test_a_signed_transaction_is_named_by_its_selector_and_router():
    acct = Account.create()
    base = {"value": 0, "gas": 100_000, "nonce": 0, "chainId": 1}

    def raw(to, data, **fee):
        return acct.sign_transaction({**base, "to": to, "data": data, **fee}).raw_transaction.hex()
    legacy, typed = {"gasPrice": 10 ** 9}, {"maxFeePerGas": 10 ** 9, "maxPriorityFeePerGas": 1}
    weth = WETH_CHECKSUM
    assert tx_kind(raw(weth, "0x2e1a7d4d" + "00" * 32, **legacy)) == "unwrap"
    assert tx_kind(raw(weth, "0x095ea7b3" + "00" * 64, **typed)) == "approve"
    assert tx_kind(raw("0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af", "0x3593564c", **legacy)) == "swap_v4"
    assert tx_kind(raw("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45", "0x5ae401dc", **typed)) == "swap_v3"
    assert tx_kind(raw("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D", "0x791ac947", **legacy)) == "swap_v2"
    assert tx_kind(raw("0x" + "11" * 20, "0xdeadbeef", **legacy)) == "swap_unknown"
    assert percentile([5], 90) == 5 and percentile(list(range(1, 11)), 50) == 5
    assert percentile(list(range(1, 11)), 90) == 9


# ---- the fork ----

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def _receipt(url, tx_hash, wait_s=20.0):
    """Anvil mines a moment after it accepts (it may first fetch state upstream)."""
    end = time.monotonic() + wait_s
    while True:
        receipt = _post(url, "eth_getTransactionReceipt", [tx_hash]).get("result")
        if receipt or time.monotonic() > end:
            return receipt
        time.sleep(0.2)


@pytest.fixture(scope="module")
def fork():
    refusal = upstream_refusal(UPSTREAM)
    if refusal:
        pytest.fail(refusal)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    args = [ANVIL, "--fork-url", UPSTREAM, "--chain-id", "1", "--port", str(port),
            "--host", "127.0.0.1", "--compute-units-per-second", "100", "--silent"]
    if os.getenv("ETH_FORK_BLOCK"):
        args += ["--fork-block-number", os.environ["ETH_FORK_BLOCK"]]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if _post(url, "eth_chainId", [])["result"] == "0x1":
                    break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("anvil fork did not come up")
        head = _post(url, "eth_getBlockByNumber", ["latest", False])["result"]
        _evidence(test="fork", upstream=urllib.parse.urlsplit(UPSTREAM).hostname,
                  block=int(head["number"], 16), block_ts=int(head["timestamp"], 16),
                  base_fee_gwei=int(head.get("baseFeePerGas", "0x0"), 16) / 1e9)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


class ForkChain:
    """The executor's and the private backend's HTTP seam. Two addresses are served, both by the
    local fork: the custody URL (reads only) and the pinned private endpoint (sends only). It can
    crash around a chosen send (SystemExit, which no `except Exception` absorbs) and can play a
    relay that accepts a transaction and never lands it."""

    def __init__(self, url, private_url):
        self.url, self.private_url = url, private_url.rstrip("/")
        self.crash = None              # "<phase>:<before|after>_send_<kind>"
        self.hold = set()              # "<phase>:<kind>": accepted by the relay, never mined
        self.phase = "entry"
        self.sent = []                 # (phase, kind, tx hash, landed)
        self.held_raw = {}             # tx hash -> the bytes the relay accepted and never landed
        self.rpc_errors = []           # every JSON-RPC error a read drew (a revert names itself)
        self.violations = []

    def http(self, method, url, headers=None, body=None, timeout=10):
        target = str(url).rstrip("/")
        private = target == self.private_url
        if not private and not target.startswith(self.url):
            self.violations.append("request to %s" % url)
            raise AssertionError("step 7 refused a request to %s" % url)
        req = json.loads(body)
        batch = req if isinstance(req, list) else [req]
        sends = [r for r in batch if r.get("method") == "eth_sendRawTransaction"]
        if sends and (not private or isinstance(req, list)):
            self.violations.append("chain-1 send addressed to %s" % url)
            raise AssertionError("step 7: a send must go alone to the private endpoint, got %s" % url)
        if private and not sends:
            self.violations.append("read addressed to the private endpoint: %s" % req.get("method"))
            raise AssertionError("step 7: only sends go to the private endpoint")
        if isinstance(req, list):
            http_req = urllib.request.Request(self.url, data=json.dumps(req).encode(),
                                              headers={"content-type": "application/json"})
            with urllib.request.urlopen(http_req, timeout=120) as resp:
                return 200, json.loads(resp.read()), None
        if not sends:
            reply = _post(self.url, req["method"], req.get("params") or [])
            if isinstance(reply, dict) and reply.get("error"):
                self.rpc_errors.append({"method": req["method"], "error": reply["error"],
                                        "to": ((req.get("params") or [{}])[0] or {}).get("to")
                                        if req["method"] in ("eth_call", "eth_estimateGas") else None})
            return 200, reply, None
        raw = req["params"][0]
        kind = tx_kind(raw)
        family = "swap" if kind.startswith("swap_") else kind      # "swap" names any router
        if self.crash == f"{self.phase}:before_send_{family}":
            self.crash = None
            raise SystemExit("before_send_" + kind)
        held = f"{self.phase}:{family}" in self.hold
        if held:
            tx_hash = "0x" + keccak(bytes.fromhex(raw.removeprefix("0x"))).hex()
            reply = {"jsonrpc": "2.0", "id": req.get("id"), "result": tx_hash}
            self.held_raw[tx_hash] = raw
        else:
            reply = _post(self.url, "eth_sendRawTransaction", [raw])
            if reply.get("error"):
                self.rpc_errors.append({"method": "eth_sendRawTransaction", "kind": kind,
                                        "error": reply["error"]})
        self.sent.append((self.phase, kind, reply.get("result"), not held))
        if self.crash == f"{self.phase}:after_send_{family}":
            self.crash = None
            raise SystemExit("after_send_" + kind)
        return 200, reply, None

    def balance(self, token, owner):
        data = "0x70a08231" + "0" * 24 + owner.lower()[2:]
        return int(_post(self.url, "eth_call", [{"to": token, "data": data}, "latest"])["result"], 16)

    def gas_rows(self, **tags):
        """One row per sent transaction that landed: its kind and the fork receipt's gasUsed."""
        rows = []
        for phase, kind, tx_hash, landed in self.sent:
            receipt = _receipt(self.url, tx_hash) if landed and tx_hash else None
            if receipt:
                tx = _post(self.url, "eth_getTransactionByHash", [tx_hash])["result"]
                rows.append(dict(tags, phase=phase, kind=kind, tx=tx_hash,
                                 gas_used=int(receipt["gasUsed"], 16), gas_limit=int(tx["gas"], 16),
                                 status=int(receipt["status"], 16),
                                 gas_price_gwei=int(receipt["effectiveGasPrice"], 16) / 1e9))
        return rows

    def swaps(self, phase=None, landed=None):
        """Distinct swap transactions the endpoint ACCEPTED. The executor re-offers a swap's
        identical bytes while it waits for the receipt; a copy the node answers "nonce too low"
        (the first one landed) has no hash and is the same transaction, not a second swap."""
        return [h for p, k, h, ok in dict.fromkeys(self.sent) if h and k.startswith("swap_")
                and phase in (None, p) and landed in (None, ok)]


def _evidence(**row):
    if EVIDENCE:
        with open(EVIDENCE, "a") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _pool_id(key):
    from sniper.execution import evm_v4_quote as Q
    return "0x" + keccak(bytes.fromhex(Q._encode_pool_key(key))).hex()


@pytest.fixture(autouse=True)
def nothing_leaves_this_host(request, monkeypatch):
    """Every test in this file, fork or not: loopback only, no custody key, and a test that did
    not ask for a refusal fails when one happened."""
    refused = []
    loopback_only(monkeypatch, refused)
    from sniper.execution import evm_wallet

    def no_keypair(path):
        raise AssertionError("step 7 must never load a keypair file: %s" % path)
    monkeypatch.setattr(evm_wallet, "load_keypair", no_keypair)
    monkeypatch.setattr(evm_wallet, "eth_usd_price", lambda http=None: ETH_USD)   # no price host
    yield refused
    if "refuses" not in request.node.name:
        assert refused == [], "outbound attempts during the rehearsal: %s" % refused


@pytest.fixture
def rehearsal(fork, tmp_path, monkeypatch):
    """The PRODUCTION chain-1 context (`build_real_evm_context`: executor, private backend, exit and
    recovery seams) on the fork, with a throwaway wallet. `build()` is a restarted process."""
    from sniper.execution import evm_v4_quote as Q
    from sniper.execution import evm_wallet
    from sniper.execution.free_endpoint_registry import private_submit_endpoint
    from sniper.services import memecoin_evm_leg as leg
    snap = _post(fork, "evm_snapshot", [])["result"]
    monkeypatch.setattr(cfg, "eth_capability_reason", lambda *a, **k: None)   # rehearsal only
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_" + ETH, fork)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_" + ETH, "direct")
    # THE GAS GUARD IS OPENED HERE, and only here. It refuses while the units are unmeasured, and
    # this run is what measures them; anvil's eth_gasPrice (base fee plus its own tip estimate) is
    # also not the custody node's. The guard's own arithmetic is pinned in test_memecoin_eth_parity.
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1", "1")
    monkeypatch.setenv("MEMECOIN_EVM_MAX_GAS_GWEI_1", "0")
    monkeypatch.setenv("MEMECOIN_MAX_ROUNDTRIP_COST_PCT_1", "100")
    # The live runner's GLOBAL ladder knobs (its .env, 2026-09-30; none is chain-scoped, so chain
    # 1 inherits each). The code defaults differ, and a rehearsal on defaults is another ladder.
    for name, value in LIVE_GLOBAL_KNOBS.items():
        monkeypatch.setattr(cfg, name, value)
    ledger = tmp_path / "ledger.db"
    monkeypatch.setenv("DB_PATH", str(ledger))                  # the stored PoolKey cache reads it
    conn = sqlite3.connect(ledger)
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    # The third-party pool index is not rehearsed: the coin file's pools stand in for it, and
    # each key must hash to its pool id before it is stored.
    ranked = {}
    for coin in COINS:
        # The coin's own pools rank for the coin; each bridge token's pools rank for that token,
        # so the multihop search finds its bridge leg exactly where production finds it.
        for token, pools in [(coin["mint"], coin.get("pools") or [])] + [
                (b["token"], b.get("pools") or []) for b in coin.get("bridges") or []]:
            for depth, pool in enumerate(pools):
                assert _pool_id(pool["key"]) == pool["pool_id"].lower(), coin["ticker"]
                db.put_evm_v4_pool_key(conn, chain_id=ETH, pool_id=pool["pool_id"],
                                       pool_manager=Q.V4[ETH]["pool_manager"], key=pool["key"])
                ranked.setdefault(token.lower(), []).append(
                    (1e6 / (depth + 1), pool["pool_id"].lower()))
    monkeypatch.setattr(Q, "_gt_pools_ranked",
                        lambda chain, mint, *a, **k: (list(ranked.get(str(mint).lower(), [])), False))
    monkeypatch.setattr(Q, "_ds_pools_ranked", lambda chain, mint, *a, **k: ([], False))
    Q._pool_cache.clear()
    # A throwaway wallet, written to the test's own directory; the loader reads that file only.
    key_path = str(tmp_path / "step7_throwaway_key.json")
    acct = evm_wallet.generate_keypair_file(key_path)
    real_load = _REAL_LOAD_KEYPAIR

    def only_the_throwaway(path):
        assert os.path.realpath(path) == os.path.realpath(key_path), "step 7 refused keypair %s" % path
        return real_load(path)
    monkeypatch.setattr(evm_wallet, "load_keypair", only_the_throwaway)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", key_path)
    _post(fork, "anvil_setBalance", [acct.address, hex(10 ** 18)])
    gas_price = int(_post(fork, "eth_gasPrice", [])["result"], 16)
    signed = acct.sign_transaction({"to": WETH_CHECKSUM, "value": 2 * 10 ** 17,
                                    "data": "0xd0e30db0", "gas": 100_000,
                                    "gasPrice": gas_price * 2, "nonce": 0, "chainId": 1})
    wrapped = _post(fork, "eth_sendRawTransaction",
                    ["0x" + signed.raw_transaction.hex().removeprefix("0x")]).get("result")
    assert int(_receipt(fork, wrapped)["status"], 16) == 1
    chain = ForkChain(fork, private_submit_endpoint(ETH)[1])

    def build():
        ctx = leg.build_real_evm_context(ETH, http=chain.http, conn=conn)
        assert ctx is not None and ctx["address"] == acct.address
        return ctx

    class R:
        pass
    r = R()
    r.chain, r.conn, r.build, r.acct, r.fork = chain, conn, build, acct, fork
    r.ctx = build()
    r.held = lambda mint: chain.balance(mint, acct.address)
    r.reserve = lambda mint: db.reserve_pending(
        conn, mint=mint, source_user_id="step7-rehearsal", strategy="alpha_b", notional=CLIP,
        reserved_at=_now(), network_id=ETH)
    r.reservation = lambda pid: dict(conn.execute(
        "SELECT * FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone())
    r.positions = lambda: conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions WHERE network_id=?", (ETH,)).fetchone()[0]
    yield r
    conn.close()
    assert chain.violations == [], chain.violations
    _post(fork, "evm_revert", [snap])


def _now(offset_s=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def _advance(fork, seconds):
    """Move CHAIN time forward: the dead proof reads the block timestamp, never the host clock."""
    _post(fork, "evm_increaseTime", [hex(seconds)])
    _post(fork, "evm_mine", [])


def _settle(r):
    """After an injected crash: drop the dead executor's open transaction, let the fork mine."""
    if r.conn.in_transaction:
        r.conn.rollback()
    for _phase, _kind, tx_hash, landed in r.chain.sent:
        if landed and tx_hash:
            _receipt(r.fork, tx_hash)


def _note_gas(r, **tags):
    rows = r.chain.gas_rows(**tags)
    GAS_ROWS.extend(rows)
    for row in rows:
        _evidence(test="gas", **row)
    return rows


def _fill(r, coin, case):
    pid = r.reserve(coin["mint"])
    res = r.ctx["execute_fn"](r.conn, pid, coin["mint"], CLIP, _now())
    _evidence(test=case + "_entry", ticker=coin["ticker"], mint=coin["mint"], result=res,
              rpc_errors=r.chain.rpc_errors[-3:] if res.get("status") != "filled" else None)
    assert res["status"] == "filled", res
    assert r.held(coin["mint"]) > 0
    r.chain.phase = "exit"
    return res




@fork_only
@pytest.mark.parametrize("coin", COINS, ids=[c["ticker"] for c in COINS])
def test_a_multihop_buy_then_a_whole_balance_exit_through_the_stored_route(rehearsal, coin):
    r, mint = rehearsal, coin["mint"]
    res = _fill(r, coin, "multihop")
    position = db.get_position(r.conn, res["position_id"])
    route = json.loads(position.get("evm_route_json") or "null") or {}
    _evidence(test="multihop_entry_route", ticker=coin["ticker"], protocol=route.get("protocol"),
              legs=route.get("legs"), bridge=route.get("bridge"),
              round_trip_pct=route.get("round_trip_pct"))
    assert route.get("protocol") == coin["expect"], route
    held = r.held(mint)
    out = r.ctx["exit_fn"](r.conn, db.get_position(r.conn, res["position_id"]), "manual", _now(60))
    rows = _note_gas(r, case="multihop_round_trip", ticker=coin["ticker"])
    _evidence(test="multihop_exit", ticker=coin["ticker"], held_atoms=held, result=out,
              held_after=r.held(mint), rpc_errors=r.chain.rpc_errors[-4:],
              sent=[(p, k, bool(h)) for p, k, h, _ok in r.chain.sent])
    assert out["status"] == "closed", out
    assert r.held(mint) == 0                                    # the WHOLE balance was sold
    assert all(row["status"] == 1 for row in rows), rows
    assert len(r.chain.swaps("entry")) == 1 and len(r.chain.swaps("exit")) == 1, r.chain.sent
