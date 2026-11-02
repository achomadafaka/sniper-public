"""The EVM watcher verifies the pinned Relay/Kyber code and the chain identity BEFORE a buy arrives
(2026-09-28, Alpha GO).

Measured 2026-09-28: DegenCapitalLLC's relay buy 0x9c289070a7 reached the 4663 watcher 37 s after a
restart, with the 600 s code cache cold. tx and receipt came back in 39/45 ms, and the row was still
decoded 1.167 s after arrival; J7 decided at +0.30 s. The inline verification (eth_chainId + one
eth_getCode per pinned contract on the read host) sits on the detection path whenever the cache is
cold, and with ~20 restarts a day and a 52 min median gap between 4663 buys that is most buys.

Now: the watcher pins the chain identity once per process and verifies every pinned contract at
start, then re-verifies in the background before the TTL lapses. A refresh that reads the pinned
hash extends the entry; a WRONG hash revokes it at once; a failed read leaves it to expire, after
which the detection path verifies inline exactly as before. It never serves an unverified code. The
row's source_valuation_json carries code_basis and decode_ms {chain_id, code, decimals, price, rest}.
"""
import gzip
import json
import threading
from pathlib import Path

import pytest
from eth_utils import keccak

import scripts.research.onchain_evm_wallet_watch as w
import sniper.execution.evm_relay_quote as relay
from sniper.services import memecoin_evm_onchain_watch as M
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg

FIX = json.load(gzip.open(Path(__file__).parent / "fixtures" / "relay_purchases_2026-09-28.json.gz", "rt"))
BUY = next(b for b in FIX["buys"] if b["chain"] == "4663" and b["shape"] == "no_kyber")
CODE = "0xfeed"
PINNED = (relay.RELAY_APPROVAL_PROXY, relay.RELAY_ROUTER, M.KYBER_ROUTER_4663)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(M, "_RELAY_CODE_CACHE", {}, raising=False)
    monkeypatch.setattr(M, "_CHAIN_ID_PINNED", set(), raising=False)
    digest = "0x" + keccak(bytes.fromhex(CODE[2:])).hex()
    monkeypatch.setattr(relay, "RELAY_APPROVAL_PROXY_CODE_HASH", digest)
    monkeypatch.setattr(relay, "RELAY_ROUTER_CODE_HASH", digest)
    monkeypatch.setattr(M, "KYBER_CODE_HASH_4663", digest)
    w._configure("4663")
    yield
    w._configure("4663")


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _reader(code=CODE, chain_id=4663, fail=False):
    calls = []

    def read(method, params):
        calls.append(method)
        if fail:
            raise RuntimeError("read host down")
        if method == "eth_chainId":
            return hex(chain_id)
        if method == "eth_getCode":
            return code
        raise AssertionError(method)
    read.calls = calls
    return read


# --- the warm-up and the refresh --------------------------------------------------------------------

@pytest.mark.parametrize("chain, chain_id", [("56", 56), ("8453", 8453)])
def test_a_warm_on_another_chain_never_reads_the_4663_kyber_pin(chain, chain_id):
    # 2026-10-02: every 56 and 8453 warm logged 0x6131b5fa=mismatch, Kyber's 4663 pin read on a chain
    # where that address holds other code; a real mismatch would hide among them.
    read = _reader(chain_id=chain_id)
    seen = []

    def spy(method, params):
        seen.append(params[0] if method == "eth_getCode" else method)
        return read(method, params)
    verdicts = M.warm_relay_code(chain, spy, ttl_s=600, clock=_Clock())
    assert verdicts == {relay.RELAY_APPROVAL_PROXY: "verified", relay.RELAY_ROUTER: "verified"}
    assert M.KYBER_ROUTER_4663 not in seen
    assert M.KYBER_ROUTER_4663 in M.warm_relay_code("4663", _reader(), ttl_s=600, clock=_Clock())


def test_a_warm_verification_serves_the_detection_with_no_code_read():
    clock = _Clock()
    assert M.warm_relay_code("4663", _reader(), ttl_s=600, clock=clock) == {c: "verified" for c in PINNED}
    assert M.pin_chain_identity("4663", _reader())
    read = _reader()
    evidence = {}
    proof = M.read_relayer_purchase(BUY["receipt"], BUY["tx"], BUY["wallet"], chain="4663", read_fn=read,
                                    any_route=True, code_ttl_s=600, clock=clock, live=True, evidence=evidence)
    assert proof is not None and proof["code_basis"] == "cached"
    assert read.calls == [], "no eth_chainId and no eth_getCode on the detection path"


def test_a_wrong_hash_on_refresh_revokes_the_entry_at_once():
    clock = _Clock()
    M.warm_relay_code("4663", _reader(), ttl_s=600, clock=clock)
    clock.t += 300
    assert M.warm_relay_code("4663", _reader(code="0xbad0"), ttl_s=600, clock=clock)[relay.RELAY_ROUTER] == "mismatch"
    read = _reader()
    M.read_relayer_purchase(BUY["receipt"], BUY["tx"], BUY["wallet"], chain="4663", read_fn=read,
                            any_route=True, code_ttl_s=600, clock=clock, live=True)
    assert "eth_getCode" in read.calls, "a revoked entry is verified inline again"


def test_a_failed_refresh_keeps_the_last_verification_only_until_its_ttl():
    clock = _Clock()
    M.warm_relay_code("4663", _reader(), ttl_s=600, clock=clock)
    clock.t += 300
    assert set(M.warm_relay_code("4663", _reader(fail=True), ttl_s=600, clock=clock).values()) == {"unavailable"}
    read = _reader()
    clock.t = 1000 + 599
    assert M.read_relayer_purchase(BUY["receipt"], BUY["tx"], BUY["wallet"], chain="4663", read_fn=read,
                                   any_route=True, code_ttl_s=600, clock=clock, live=True) is not None
    assert "eth_getCode" not in read.calls, "still inside the first verification's TTL"
    clock.t = 1000 + 601
    M.read_relayer_purchase(BUY["receipt"], BUY["tx"], BUY["wallet"], chain="4663", read_fn=read,
                            any_route=True, code_ttl_s=600, clock=clock, live=True)
    assert "eth_getCode" in read.calls, "a failed refresh must not extend an old verification"


def test_a_malformed_code_answer_is_unavailable_never_a_crash():
    """Start-up must survive a provider that answers getCode with the wrong type (a dict here)."""
    read = lambda method, params: {"from": "0xab"} if method == "eth_getCode" else hex(4663)  # noqa: E731
    assert set(M.warm_relay_code("4663", read, ttl_s=600, clock=_Clock()).values()) == {"unavailable"}
    assert M._RELAY_CODE_CACHE == {}


def test_a_pinned_chain_skips_the_chain_id_read_even_when_the_code_is_cold():
    assert M.pin_chain_identity("4663", _reader())
    read = _reader()
    assert M.read_relayer_purchase(BUY["receipt"], BUY["tx"], BUY["wallet"], chain="4663", read_fn=read,
                                   any_route=True, code_ttl_s=600, clock=_Clock(), live=True) is not None
    assert "eth_getCode" in read.calls and "eth_chainId" not in read.calls


def test_a_chain_id_that_does_not_match_is_never_pinned():
    assert not M.pin_chain_identity("4663", _reader(chain_id=1))
    assert not M.pin_chain_identity("4663", _reader(fail=True))
    assert M._CHAIN_ID_PINNED == set()


def test_the_refresher_re_verifies_before_the_ttl_and_stops():
    runs = []

    class _Stop:
        def __init__(self):
            self.waits = []

        def wait(self, timeout):
            self.waits.append(timeout)
            return len(self.waits) > 2
    stop = _Stop()
    w._relay_warm_loop(lambda: runs.append(1), ttl_s=600, stop=stop)
    assert runs == [1, 1] and all(t < 600 for t in stop.waits)


@pytest.mark.parametrize('chain,ttl,interval', [('56', 600., 30.), ('4663', 600., 200.),
    ('8453', 600., 200.), ('4663', 0., 30.), ('4663', float('inf'), 30.)])
def test_relay_loop_captures_its_chain_interval_at_start(monkeypatch, chain, ttl, interval):
    monkeypatch.setattr(w, 'CHAIN', chain)
    waits, runs = [], []

    class Stop:
        def wait(self, seconds):
            waits.append(seconds)
            # Capture must survive a different test/caller's later module configuration.
            monkeypatch.setattr(w, 'CHAIN', '4663' if chain == '56' else '56')
            return len(waits) > 2

    w._relay_warm_loop(lambda: runs.append(True), ttl_s=ttl, stop=Stop())
    assert waits == [interval] * 3 and runs == [True, True]


def test_bsc_shorter_cadence_keeps_three_existing_public_checks_per_cycle(monkeypatch):
    monkeypatch.setattr(w, 'CHAIN', '56')
    read = _reader(chain_id=56)
    waits = []

    class Stop:
        def wait(self, seconds):
            waits.append(seconds)
            return len(waits) > 2

    w._relay_warm_loop(lambda: w._warm_relay(read, 600.), ttl_s=600., stop=Stop())
    assert waits == [30.] * 3
    assert read.calls == ['eth_chainId', 'eth_getCode', 'eth_getCode'] * 2


# --- the watcher, end to end --------------------------------------------------------------------------

def _run_watcher(monkeypatch, tmp_path, *, warm=True, client=None):
    inserts, path_reads, seen = [], [], []
    real_counted, real_rpc, real_decimals = w._counted_rpc, w._rpc, w.erc20_decimals

    class _Conn:
        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT"):
                inserts.append(params)
            return self

        def commit(self):
            pass

        def close(self):
            pass

        def fetchone(self):
            return None
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    wallet = BUY["wallet"].lower()
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {wallet: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {wallet: ("u1", "h1")})
    monkeypatch.setattr(w, "send_wake", lambda address: True)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda: None)
    monkeypatch.setattr(w, "erc20_decimals", lambda mint, rpc_url: 18)
    monkeypatch.setattr(w, "_payment_usd_price", lambda token, native: 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S", 600.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S", 60.0, raising=False)
    if not warm:
        monkeypatch.setattr(w, "_warm_relay", lambda read_fn, ttl_s: None)

    def counted(url, method, params):
        seen.append(method)
        if method == "eth_getTransactionByHash":
            return {"result": BUY["tx"]}
        if method == "eth_getTransactionReceipt":
            return {"result": BUY["receipt"]}
        if method == "eth_chainId":
            return {"result": hex(4663)}
        if method == "eth_getCode":
            return {"result": CODE}
        raise AssertionError(method)
    monkeypatch.setattr(w, "_counted_rpc", counted)

    def fake_stream(ws_url, **kwargs):
        start = len(seen)
        kwargs["on_transfer"]({"transactionHash": BUY["tx"]["hash"], "address": BUY["mint"]})
        path_reads.extend(seen[start:])
        return kwargs.get("state")
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    if client is None:
        w.run_push("wss://test", price=price, duration_s=0)
    else:
        # Real owned HTTP binding; the legacy offline seams remain to expose startup bypasses.
        monkeypatch.setattr(w, '_counted_rpc', real_counted)
        monkeypatch.setattr(w, '_rpc', lambda url, method, params, http=None:
                            real_rpc(url, method, params, http=http) if http else counted(url, method, params))
        monkeypatch.setattr(w, 'erc20_decimals', lambda token, url, http=None:
                            real_decimals(token, url, http=http) if http else 18)
        with w._WatcherReads(client=client) as transport:
            w.run_push('wss://test', price=price, duration_s=0, read_transport=transport)
    return inserts, path_reads


def test_a_detection_after_a_warm_start_reads_no_code_on_the_path(monkeypatch, tmp_path):
    inserts, path_reads = _run_watcher(monkeypatch, tmp_path)
    assert len(inserts) == 1, "the relay buy is detected"
    assert "eth_getCode" not in path_reads and "eth_chainId" not in path_reads, path_reads


def test_existing_startup_code_and_payment_checks_use_the_same_owned_pool(monkeypatch, tmp_path):
    calls, closed = [], []

    class Client:
        def request_with_headers(self, method, url, **kwargs):
            body = json.loads(kwargs['body'])
            calls.append(body)
            result = {'eth_chainId': '0x1237', 'eth_getCode': CODE,
                      'eth_call': '0x12', 'eth_getTransactionByHash': BUY['tx'],
                      'eth_getTransactionReceipt': BUY['receipt']}[body['method']]
            return 200, {'jsonrpc': '2.0', 'id': body['id'], 'result': result}, {}

        def close(self):
            closed.append(True)

    inserts, _ = _run_watcher(monkeypatch, tmp_path, client=Client())
    assert len(inserts) == 1 and closed == [True]
    assert [body['method'] for body in calls[:4]] == ['eth_chainId'] + ['eth_getCode'] * 3
    assert [body['params'][0]['to'] for body in calls[4:6]] == [
        '0x0bd7d308f8e1639fab988df18a8011f41eacad73', '0x5fc5360d0400a0fd4f2af552add042d716f1d168']
    assert all(body['method'] == 'eth_call' for body in calls[4:6])
    assert len(calls) == 9  # Existing startup six calls + source tx, receipt, mint decimals.
    valuation = json.loads(next(p for p in inserts[0] if isinstance(p, str) and '"basis"' in p))
    assert valuation['metadata_prefetch_rpc_attempts']['count'] == 1
    assert valuation['metadata_prefetch_rpc_attempts']['outcomes'] == ['ok']


def test_without_the_warm_up_the_path_pays_the_code_reads(monkeypatch, tmp_path):
    inserts, path_reads = _run_watcher(monkeypatch, tmp_path, warm=False)
    assert len(inserts) == 1 and path_reads.count("eth_getCode") >= 2


def test_the_row_carries_the_code_basis_and_the_decode_split(monkeypatch, tmp_path):
    inserts, _ = _run_watcher(monkeypatch, tmp_path)
    valuation = json.loads(next(p for p in inserts[0] if isinstance(p, str) and '"basis"' in p))
    assert valuation["code_basis"] == "cached"
    assert set(valuation["decode_ms"]) == {"chain_id", "code", "decimals", "price", "rest"}
    assert all(v >= 0 for v in valuation["decode_ms"].values())


def test_the_row_names_the_payment_token(monkeypatch, tmp_path):
    """A relay buy's price step reads 0.0 ms for WETH and for a warm USDG alike; the row says which
    token was priced (2026-09-28, Alpha)."""
    inserts, _ = _run_watcher(monkeypatch, tmp_path)
    valuation = json.loads(next(p for p in inserts[0] if isinstance(p, str) and '"basis"' in p))
    assert valuation["payment_token"] == "0x5fc5360d0400a0fd4f2af552add042d716f1d168"   # USDG on 4663
