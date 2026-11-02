"""BSC (56) builder A/B on the main entry path (operator order 2026-10-09 via Alpha; Lambda's report
docs/audits/2026-10-09-memecoin-evm-fast-inclusion-options.md section 2, option 2).

Behind MEMECOIN_BSC_BUILDER_AB (default off), an arm-B 56 entry (rule
bsc-builder-sha256-attempt-odd-v1 on the pending row's attempt_id) also sends its signed swap bytes
to 48 Club Puissant, and to BlockRazor when MEMECOIN_BSC_BLOCKRAZOR_AUTH is set, as an eth_sendBundle
that expires 30 s after the send. A bundle that is dropped is silent, so every bundle answer is
ambiguous for that builder only, and the expiry sits far inside the 300 s never-admitted dead proof.
Arm A is today's send. Both arms record the head right after the send, so the read can score
send-to-land blocks apart from detect and decide.
"""
import contextlib
import hashlib
import json
import sqlite3
import threading
import time

import pytest
from eth_account import Account

from sniper.execution import bsc_builder_ab as B
from sniper.execution import exec_lock
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionTransport, build_request,
)
from sniper.persistence import db_memecoin as m

TOKEN = "blockrazor-test-token-77aa"


def _arm_by_hand(attempt_id):
    return "B" if int(hashlib.sha256(attempt_id.encode()).hexdigest()[:8], 16) % 2 else "A"


def _ids():
    found, i = {}, 0
    while len(found) < 2:
        aid = "%032x" % i
        found.setdefault(_arm_by_hand(aid), aid)
        i += 1
    return found


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MEMECOIN_BSC_BUILDER_AB", raising=False)
    monkeypatch.delenv("MEMECOIN_BSC_BUILDER_ALL", raising=False)
    monkeypatch.delenv("MEMECOIN_BSC_BLOCKRAZOR_AUTH", raising=False)
    B._reset_marks()
    yield
    B._reset_marks()


class _Http:
    def __init__(self, answer=None, status=200, raises=None):
        self.calls, self.answer, self.status, self.raises = [], answer, status, raises

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        self.calls.append((method, url, dict(headers or {}), json.loads(body) if body else None))
        if self.raises is not None:
            raise self.raises
        return self.status, (self.answer if self.answer is not None else {"result": "0xbundle"}), None


def _request(raw=None, action="entry", identity="0x" + "ab" * 32):
    if raw is None:
        raw = _signed_request().signed_payload
    return build_request(chain_id="56", action=action, tx_identity=identity, signed_payload=raw)


def _signed_request():
    signed = Account.from_key("0x" + "11" * 32).sign_transaction(
        dict(chainId=56, nonce=7, gasPrice=75_000_000, gas=21_000,
             to="0x" + "22" * 20, value=1))
    return _request(raw="0x" + signed.raw_transaction.hex(), identity="0x" + signed.hash.hex())


def test_builder_does_not_send_when_the_intent_cannot_be_persisted(monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXEC_INTENT_STAMP", True)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked")
    monkeypatch.setattr(exec_lock, "_default_lock_dir", lambda: str(blocked))
    request, http = _signed_request(), _Http()
    B.mark(request.tx_identity)
    provider = B.BundleProvider(http, url=B.PUISSANT_BUNDLE_URL, provider_id=B.PROVIDER_IDS[0])
    result = provider.submit(request)
    assert result.state is AttemptState.NOT_SENT
    assert result.reason_code == "intent_unwritable" and http.calls == []
    assert exec_lock.generation_fault()


@pytest.mark.parametrize("fails", [False, True])
def test_background_builder_post_stays_fenced_after_custody_returns(monkeypatch, tmp_path, fails):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXEC_INTENT_STAMP", True)
    monkeypatch.setattr(exec_lock, "_default_lock_dir", lambda: str(tmp_path))
    entered, release = threading.Event(), threading.Event()

    def http(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        if fails:
            raise OSError("reset")
        return 200, {"result": "bundle"}, None

    class Custody:
        provider_id = "56:custody_rpc"

        def submit(self, request):
            assert entered.wait(1)
            return SubmissionAttempt(self.provider_id, AttemptState.ACCEPTED,
                                     request.tx_identity, 0.0, "200", None)

    request = _signed_request()
    B.mark(request.tx_identity)
    provider = B.BundleProvider(http, url=B.PUISSANT_BUNDLE_URL, provider_id=B.PROVIDER_IDS[0])
    transport = SubmissionTransport([Custody(), provider], deadline_s=1)
    try:
        assert transport.submit(request).state is AttemptState.ACCEPTED
        assert exec_lock.intents_outstanding("56") is True
        records = list((tmp_path / "exec_intents" / "56").iterdir())
        assert len(records) == 1
        record = json.loads(records[0].read_text())
        assert record["raw"] == request.signed_payload and record["transport"] == "bundle"
        assert record["max_block"] is None, "timestamp expiry is not a block-height proof"
    finally:
        release.set()
        deadline = time.perf_counter() + 1
        while provider.outcome(request.tx_identity) is None and time.perf_counter() < deadline:
            time.sleep(.001)
        transport.close()
    assert exec_lock.intents_outstanding("56") is False
    assert (tmp_path / "real_exec_evm-chain-56.gen").exists()
    assert provider.outcome(request.tx_identity)["answer"] == ("transport:OSError" if fails else "ok")


# --- the arm --------------------------------------------------------------------------------------

def test_the_arm_is_the_preregistered_rule():
    for aid in ["%032x" % i for i in range(64)] + ["deadbeef"]:
        got = B.assign(aid)
        assert got["arm"] == _arm_by_hand(aid) and got["rule"] == "bsc-builder-sha256-attempt-odd-v1"
        assert got["attempt_hash"] == hashlib.sha256(aid.encode()).digest()[:4].hex()
    assert B.assign(None) is None and B.assign("") is None
    assert {B.assign("%032x" % i)["arm"] for i in range(64)} == {"A", "B"}


# --- the bundle provider ----------------------------------------------------------------------------

def test_a_builder_posts_only_a_marked_entry_as_a_bundle_that_expires_in_30s():
    http = _Http()
    p = B.BundleProvider(http, url=B.PUISSANT_BUNDLE_URL, provider_id="56:puissant_bundle",
                         clock=lambda: 1_000.0)
    assert p.submit(_request()).state is AttemptState.NOT_SENT and http.calls == []
    B.mark("0x" + "ab" * 32)
    assert p.submit(_request(action="exit")).state is AttemptState.NOT_SENT and http.calls == []
    attempt = p.submit(_request())
    assert attempt.state is AttemptState.AMBIGUOUS and attempt.provider_id == "56:puissant_bundle"
    (method, url, headers, body), = http.calls
    assert (method, url) == ("POST", B.PUISSANT_BUNDLE_URL)
    assert body["method"] == "eth_sendBundle"
    assert body["params"] == [{"txs": [_request().signed_payload], "maxTimestamp": 1_030}]
    assert "Authorization" not in headers
    seen = p.outcome("0x" + "ab" * 32)
    assert seen["answer"] == "ok" and seen["http_status"] == "200" and seen["ms"] >= 0


@pytest.mark.parametrize("http", [_Http(answer={"error": {"code": -32000, "message": "bad"}}),
                                  _Http(status=429, answer={}), _Http(raises=OSError("reset"))])
def test_every_bundle_answer_is_ambiguous_for_that_builder_only(http):
    B.mark("0x" + "ab" * 32)
    p = B.BundleProvider(http, url=B.PUISSANT_BUNDLE_URL, provider_id="56:puissant_bundle")
    assert p.submit(_request()).state is AttemptState.AMBIGUOUS


def test_blockrazor_carries_the_token_in_its_header_and_never_in_its_record(caplog):
    http = _Http(answer={"error": {"message": "token %s rejected" % TOKEN}})
    B.mark("0x" + "ab" * 32)
    p = B.BundleProvider(http, url=B.BLOCKRAZOR_URL, provider_id="56:blockrazor_bundle", auth=TOKEN)
    p.submit(_request())
    assert http.calls[0][2]["Authorization"] == TOKEN
    assert TOKEN not in json.dumps(p.outcome("0x" + "ab" * 32)) and TOKEN not in repr(p)
    assert TOKEN not in caplog.text


def test_a_builder_post_starts_beside_custody_never_after_its_accept():
    posted, answered = threading.Event(), threading.Event()

    class Slow(_Http):
        def __call__(self, *a, **k):
            posted.set()
            return super().__call__(*a, **k)

    class Custody:
        provider_id = "56:custody_rpc"
        saw = None

        def submit(self, request):
            Custody.saw = posted.wait(1.0)
            answered.set()
            return SubmissionAttempt("56:custody_rpc", AttemptState.ACCEPTED, request.tx_identity,
                                     1.0, "200", None)
    B.mark("0x" + "ab" * 32)
    t = SubmissionTransport([Custody(), B.BundleProvider(Slow(), url=B.PUISSANT_BUNDLE_URL,
                                                         provider_id="56:puissant_bundle")],
                            deadline_s=2.0)
    assert t.submit(_request()).state is AttemptState.ACCEPTED
    assert answered.wait(2.0) and Custody.saw is True


# --- the backend ----------------------------------------------------------------------------------

def test_the_builders_join_only_the_56_transport_and_only_with_the_knob(monkeypatch):
    from sniper.execution import submission_backends as SB

    def ids(chain, env):
        def http(method, url, *, headers=None, body=None, timeout=None):
            if json.loads(body).get("method") == "eth_chainId":
                return 200, {"result": hex(int(chain))}, None
            return 200, {}, None
        t = SB.build_evm_backend(chain, custody_rpc_url="https://custody/rpc", env=env, http=http)
        return [p.provider_id for p in t._providers] if t is not None else None
    try:
        base = ids("56", {"MEMECOIN_EVM_SUBMIT_MODE": "auto_free"})
        on = ids("56", {"MEMECOIN_EVM_SUBMIT_MODE": "auto_free", "MEMECOIN_BSC_BUILDER_AB": "1"})
        assert on == base + ["56:puissant_bundle"]
        both = ids("56", {"MEMECOIN_EVM_SUBMIT_MODE": "auto_free", "MEMECOIN_BSC_BUILDER_AB": "1",
                          "MEMECOIN_BSC_BLOCKRAZOR_AUTH": TOKEN})
        assert both == base + ["56:puissant_bundle", "56:blockrazor_bundle"]
        assert "56:puissant_bundle" not in ids("8453", {"MEMECOIN_EVM_SUBMIT_MODE": "auto_free",
                                                       "MEMECOIN_BSC_BUILDER_AB": "1"})
    finally:
        SB.close_all()


# --- the production 56 entry path ------------------------------------------------------------------

UNWRAP = {"to": "0x" + "dd" * 20, "data": "0x2e1a7d4d" + "%064x" % 10 ** 16, "value": 0, "gas": 60_000}
SWAP = {"to": "0x" + "bb" * 20, "data": "0x" + "7ff36ab5" + "00" * 64, "value": 10 ** 16, "gas": 300_000}


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


class _Trace:
    def __init__(self):
        self.stages = {}

    def mark_substage(self, name, **kw):
        self.stages[name] = kw

    def mark(self, *_a, **_k):
        pass


def _rpc(heads):
    def http(_method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_blockNumber":
            heads.append(time.perf_counter())
            return 200, {"result": hex(64_000_000)}, None
        if meth == "eth_getTransactionCount":
            return 200, {"result": hex(5)}, None
        if meth == "eth_gasPrice":
            return 200, {"result": hex(75_000_000)}, None
        if meth == "eth_getBalance":
            return 200, {"result": hex(10 ** 18)}, None
        return 200, {"result": "0x0"}, None
    return http


def _run(conn, monkeypatch, attempt_id, *, knob=True, wait_heads=True):
    from sniper.execution import evm_real_seams as S, evm_swap_executor as executor
    from sniper.settings import memecoin as cfg
    if knob:
        monkeypatch.setenv("MEMECOIN_BSC_BUILDER_AB", "1")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    custody, bundle_http, heads, signed = [], _Http(), [], []

    class Custody:
        provider_id = "56:custody_rpc"

        def submit(self, request):
            custody.append(request.signed_payload)
            return SubmissionAttempt("56:custody_rpc", AttemptState.ACCEPTED, request.tx_identity,
                                     1.0, "200", None)
    providers = [Custody()]
    if knob:
        providers.append(B.BundleProvider(bundle_http, url=B.PUISSANT_BUNDLE_URL,
                                          provider_id="56:puissant_bundle"))
    transport = SubmissionTransport(providers, deadline_s=2.0)

    def execute(conn_, *, sign_fn, send_fn, **kw):
        pre = sign_fn(dict(UNWRAP))
        send_fn(pre[0])
        swap = sign_fn(dict(SWAP))
        signed.append(swap)
        send_fn(swap[0])
        deadline = time.monotonic() + 2.0
        while knob and B.is_marked(swap[1]) and not bundle_http.calls and time.monotonic() < deadline:
            time.sleep(0.005)
        while wait_heads and knob and len(heads) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)          # the head reads run on daemon threads; a real entry waits a receipt
        time.sleep(0.02)
        return {"status": "aborted", "reason": "fixture"}
    monkeypatch.setattr(executor, "execute_entry_evm", execute)
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="56", rpc_url="https://rpc", http=_rpc(heads),
        submission_transport=transport, price_fn=lambda: 600.0, usdc_fn=lambda: 300.0,
        token_fn=lambda _m: 0.0,
        venue_quote_fn=lambda *a, **k: {"sellAmount": "1", "buyAmount": "1",
                                        "transaction": {"to": "0x" + "cc" * 20}})
    pid = m.reserve_pending(conn, mint="0x" + "aa" * 20, source_user_id="w1", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-10-09T15:00:00+00:00", network_id="56",
                            origin_event_id="ev:1", attempt_id=attempt_id, signal_source="onchain_watch",
                            source_price=0.001)
    trace = _Trace()
    ex["execute_fn"](conn, pid, "0x" + "aa" * 20, 50.0, "2026-10-09T15:00:01+00:00", trace=trace)
    return trace, custody, bundle_http, heads, signed


def test_arm_b_sends_puissant_the_custody_swap_bytes_and_never_the_unwrap(conn, monkeypatch):
    ids = _ids()
    trace, custody, bundle_http, heads, signed = _run(conn, monkeypatch, ids["B"])
    unwrap_raw, swap_raw = custody
    (_m, url, _h, body), = bundle_http.calls
    assert url == B.PUISSANT_BUNDLE_URL and body["params"][0]["txs"] == [swap_raw]
    ab = trace.stages["bsc_builder_ab"]
    assert (ab["arm"], ab["rule"]) == ("B", "bsc-builder-sha256-attempt-odd-v1")
    send = trace.stages["bsc_builder_ab_send"]
    assert send["head_after_send"] == 64_000_000 and send["head_read_ms"] >= 0
    assert send["builders"]["56:puissant_bundle"]["answer"] == "ok"
    from sniper.execution.submission_transport import sha256_hex
    assert send["builders"]["56:puissant_bundle"]["sha256"] == sha256_hex(swap_raw)


def test_arm_a_is_todays_send_and_still_records_the_head(conn, monkeypatch):
    ids = _ids()
    trace, custody, bundle_http, heads, signed = _run(conn, monkeypatch, ids["A"])
    assert bundle_http.calls == [] and len(custody) == 2
    assert trace.stages["bsc_builder_ab"]["arm"] == "A"
    send = trace.stages["bsc_builder_ab_send"]
    assert send["head_after_send"] == 64_000_000 and send["builders"] == {}


def test_with_the_knob_off_nothing_is_marked_read_or_traced(conn, monkeypatch):
    ids = _ids()
    trace, custody, bundle_http, heads, signed = _run(conn, monkeypatch, ids["B"], knob=False)
    assert "bsc_builder_ab" not in trace.stages and "bsc_builder_ab_send" not in trace.stages
    assert heads == [] and not B.is_marked(signed[0][1])


def test_a_hung_head_read_never_delays_the_entry_return(conn, monkeypatch):
    """Alpha/Gamma LOW 2: nothing may slow filling. The head read runs on a daemon thread and the
    runner never joins it; a read still in flight is recorded as missing."""
    import sniper.execution.evm_real_seams as S
    ids = _ids()
    hang = threading.Event()
    real_rpc = S._rpc

    def rpc(url, method, params, http=None):
        if method == "eth_blockNumber":
            hang.wait(5.0)                         # a custody read that never answers in time
        return real_rpc(url, method, params, http=http)
    monkeypatch.setattr(S, "_rpc", rpc)
    started = time.monotonic()
    try:
        trace, custody, bundle_http, heads, signed = _run(conn, monkeypatch, ids["A"],
                                                          wait_heads=False)
    finally:
        hang.set()
    assert time.monotonic() - started < 1.0
    assert trace.stages["bsc_builder_ab_send"]["head_after_send"] is None
