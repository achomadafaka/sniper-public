"""Offline regression checks for the bounded research feed comparison."""
import asyncio
import importlib.util
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import base58
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/research/shred_shadow_test.py"
SPEC = importlib.util.spec_from_file_location("shred_shadow", SCRIPT)
shadow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shadow)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request
    import websockets
    import aiohttp
    def refused(*args, **kwargs):
        raise AssertionError("offline test attempted network access")
    original_http = urllib.request.urlopen
    def local_http_only(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError("offline test attempted remote HTTP access")
        return original_http(request, *args, **kwargs)
    monkeypatch.setattr(urllib.request, "urlopen", local_http_only)
    monkeypatch.setattr(websockets, "connect", refused)
    original = aiohttp.ClientSession._request
    async def local_only(self, method, url, *args, **kwargs):
        if urlsplit(str(url)).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError("offline test attempted remote HTTP access")
        return await original(self, method, url, *args, **kwargs)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", local_only)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def binary(slot=123, version=1):
    return bytes([version]) + slot.to_bytes(8, "little") + bytes(range(64)) + b"tx"


def test_helius_prefix_has_no_execution_or_source_proof():
    row = shadow.parse_preprocessed(binary())
    assert row["slot"] == 123
    assert row["sig"] == base58.b58encode(bytes(range(64))).decode()
    assert row["owner"] is None and row["err"] is None and row["index"] is None
    assert row["metadata"] == "unknown" and row["source_readiness"] == "unknown"


@pytest.mark.parametrize("frame", [b"", binary()[:72], binary()[:73], binary(version=2)])
def test_bad_or_new_binary_schema_is_refused(frame):
    with pytest.raises(ValueError):
        shadow.parse_preprocessed(frame)


def test_preprocessed_endpoint_uses_the_existing_isolated_research_key(monkeypatch):
    import slot_lane_send_monitor as mon
    monkeypatch.setattr(mon, "rpc_url", lambda: "https://mainnet.helius-rpc.com/?api-key=research")
    assert shadow.research_preprocessed_url() == "wss://beta.helius-rpc.com/?api-key=research"
    def refused():
        raise SystemExit("funded overlap")
    monkeypatch.setattr(mon, "rpc_url", refused)
    with pytest.raises(SystemExit, match="overlap"):
        shadow.research_preprocessed_url()


def test_recording_refuses_to_append_an_old_experiment(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("old\n")
    with pytest.raises(FileExistsError):
        shadow.Recorder(path)
    assert path.read_text() == "old\n"


@pytest.mark.parametrize("hours", [0, -1, float("nan"), float("inf")])
def test_invalid_duration_fails_before_any_owner_or_network_lookup(hours, tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "load_owners", lambda: pytest.fail("read owners before bounds"))
    with pytest.raises(ValueError, match="finite|positive"):
        shadow.run(hours, tmp_path / "new")


def test_existing_output_directory_is_refused_before_owner_lookup(tmp_path, monkeypatch):
    (tmp_path / "enriched.json").write_text("{}")
    monkeypatch.setattr(shadow, "load_owners", lambda: pytest.fail("read owners before collision"))
    with pytest.raises(FileExistsError):
        shadow.run(0.01, tmp_path)


def test_message_and_helius_credit_caps_include_duplicate_deliveries(tmp_path):
    stop = threading.Event()
    rec = shadow.Recorder(tmp_path / "events.jsonl", stop=stop, max_messages=20,
                          max_helius_credits=0.3)
    for n in range(3):
        assert rec.count("H", 80, billable=True)
    assert stop.is_set() and rec.stop_reason == "helius_credit_cap"
    assert not rec.count("H", 80, billable=True)
    assert rec.total_messages == 3
    rec.close()
    stop = threading.Event()
    rec = shadow.Recorder(tmp_path / "other.jsonl", stop=stop, max_messages=2)
    assert rec.count("A", 3) and not stop.is_set()
    assert rec.count("A2", 3) and stop.is_set()
    assert rec.stop_reason == "message_cap"
    rec.close()


def test_duplicate_keeps_the_first_signature_receipt_and_counts_replay(tmp_path):
    path = tmp_path / "events.jsonl"
    rec = shadow.Recorder(path)
    for t in (10, 20):
        rec.tx({"k": "tx", "feed": "H", "sig": "same", "t_ns": t})
    rec.close()
    txs = [r for r in rows(path) if r["k"] == "tx"]
    assert len(txs) == 1 and txs[0]["t_ns"] == 10
    assert rec.duplicates == {"H": 1}


def test_provider_exception_urls_and_tokens_never_reach_recording(tmp_path):
    path = tmp_path / "events.jsonl"
    rec = shadow.Recorder(path, secrets=("research-token",))
    rec.write({"k": "down", "why": "token research-token at wss://beta.helius-rpc.com/?api-key=other"})
    rec.close()
    assert "research-token" not in path.read_text() and "api-key" not in path.read_text()


def test_a_long_provider_token_is_redacted_before_error_text_is_truncated(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    token = "token-prefix-" + "x" * 200
    stop = threading.Event()
    rec = shadow.Recorder(path, stop=stop, secrets=(token,))
    def connect(*args, **kwargs):
        stop.set()
        raise RuntimeError("rejected x-token=" + token)
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=connect))
    asyncio.run(shadow.helius_feed("A2", "wss://x", ["owner"], rec, stop))
    rec.close()
    assert token[:30] not in path.read_text()


class Socket:
    def __init__(self, frames, on_recv=None):
        self.frames = iter(frames)
        self.sent = []
        self.closed = False
        self.on_recv = on_recv

    async def send(self, frame):
        self.sent.append(json.loads(frame))

    async def recv(self):
        frame = next(self.frames, None)
        if frame is None:
            await asyncio.Future()
        if self.on_recv:
            self.on_recv(frame)
        return frame

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


def test_processed_feeds_are_ready_only_after_all_acks_and_disconnect_clears_head(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    stop = threading.Event()
    rec = shadow.Recorder(path, stop=stop)
    head = shadow.Head()
    head.slot, head.t_ns = 50, 1
    ack = lambda n: json.dumps({"id": n, "result": n + 100})
    reject = json.dumps({"id": 2, "error": {"code": -32000, "message": "secret"}})
    observed = []
    def on_recv(frame):
        observed.append(any(r["k"] == "up" for r in rows(path)))
        if frame == reject:
            stop.set()
    ws = Socket([ack(0), ack(1), reject], on_recv)
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda *a, **k: ws))
    asyncio.run(shadow.helius_feed("A", "wss://x", ["owner1", "owner2"], rec, stop, head))
    rec.close()
    assert observed == [False, False, False]
    assert not any(r["k"] == "up" for r in rows(path))
    assert head.slot is None and head.t_ns is None and ws.closed


def test_boolean_ack_is_wrong_data_and_cannot_create_a_healthy_window(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    stop = threading.Event()
    rec = shadow.Recorder(path, stop=stop)
    ws = Socket([json.dumps({"id": 1, "result": True})], lambda _: stop.set())
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda *a, **k: ws))
    asyncio.run(shadow.preprocessed_feed("wss://x", ["owner"], rec, stop, shadow.Head()))
    rec.close()
    assert not any(r["k"] == "up" for r in rows(path))


def test_helius_early_feed_acks_then_records_binary_without_decoding(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    stop = threading.Event()
    rec = shadow.Recorder(path, stop=stop, max_helius_credits=0.1)
    head = shadow.Head()
    head.slot, head.t_ns = 123, shadow.time.monotonic_ns()
    ws = Socket([json.dumps({"id": 1, "result": 42}), binary()])
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda *a, **k: ws))
    asyncio.run(shadow.preprocessed_feed("wss://x", ["owner2", "owner1"], rec, stop, head))
    rec.close()
    assert ws.sent[0]["method"] == "preprocessedSubscribe"
    assert ws.sent[0]["params"] == {"accountInclude": ["owner1", "owner2"]}
    ev = rows(path)
    assert [r["k"] for r in ev if r["k"] in ("up", "tx", "down")] == ["up", "tx", "down"]
    assert next(r for r in ev if r["k"] == "tx")["latency_eligible"] is True
    assert ws.closed


def test_bounded_run_cancels_sockets_that_never_send_an_ack(tmp_path, monkeypatch):
    import slot_lane_send_monitor as mon
    monkeypatch.setattr(mon, "rpc_url", lambda: "https://mainnet.helius-rpc.com/?api-key=research")
    monkeypatch.setattr(shadow, "load_owners", lambda: ["owner"])
    sockets = []
    def connect(*args, **kwargs):
        ws = Socket([])
        sockets.append(ws)
        return ws
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=connect))
    started = shadow.time.monotonic()
    shadow.run(0.05 / 3600, tmp_path / "new", with_shred=False)
    assert shadow.time.monotonic() - started < 1
    assert len(sockets) == 3 and all(ws.closed for ws in sockets)
    ev = rows(tmp_path / "new/events.jsonl")
    assert ev[-1]["k"] == "end" and not any(r["k"] == "up" for r in ev)


def test_optional_shyft_arms_are_explicit_even_without_tokens_or_dependencies():
    arms = shadow.optional_arms({}, enabled=True, missing_deps=("grpc", "grpc_tools", "proto"))
    assert {a["feed"] for a in arms} == {"SHYFT_AMS", "SHYFT_FRA", "B"}
    assert all(a["status"] == "unavailable" for a in arms)
    assert all("token" in a["why"] for a in arms)
    arms = shadow.optional_arms({"MEMECOIN_RESEARCH_SHYFT_TOKEN": "research"}, enabled=True,
                               missing_deps=("grpc",))
    assert all(a["status"] == "unavailable" and "grpc" in a["why"] for a in arms)


def test_shyft_defaults_are_ready_with_a_token_but_still_opt_in():
    env = {"MEMECOIN_RESEARCH_SHYFT_TOKEN": "research-only"}
    arms = shadow.optional_arms(env, enabled=True, missing_deps=())
    assert [a["url"] for a in arms] == ["https://beta-grpc.ams.shyft.to",
        "https://beta-grpc.fra.shyft.to", "https://rabbitstream.ams.shyft.to/"]
    assert all(a["status"] == "configured" and a["from_slot"] is None for a in arms)
    assert all(a["why"] == "not_opted_in" for a in
               shadow.optional_arms(env, missing_deps=()))


@pytest.mark.parametrize("url", ["http://grpc.example", "https://user:secret@grpc.example",
    "https://grpc.example/path", "https://grpc.example?token=secret", "https://grpc.example#secret",
    "https://grpc.example:bad", "https://grpc.example:0", "https://[bad", "https://", "https://bad host"])
def test_shyft_invalid_endpoint_is_refused_without_printing_credentials(url):
    arms = shadow.optional_arms({"MEMECOIN_RESEARCH_SHYFT_TOKEN": "research-only",
        "MEMECOIN_RESEARCH_SHYFT_AMS_URL": url}, enabled=True, missing_deps=())
    assert arms[0]["status"] == "unavailable" and arms[0]["why"] == "endpoint_invalid"
    assert "secret" not in arms[0]["why"]


def test_disabled_or_uncredentialed_bad_endpoint_cannot_break_other_arms():
    for enabled in (False, True):
        arms = shadow.optional_arms({"MEMECOIN_RESEARCH_SHYFT_AMS_URL": "https://[bad"},
                                   enabled=enabled, missing_deps=())
        assert arms[0]["url"] is None
        assert arms[0]["why"] == ("token_missing" if enabled else "not_opted_in")


def test_shyft_endpoint_override_and_token_isolation_are_preserved():
    env = {"MEMECOIN_RESEARCH_SHYFT_TOKEN": "research-only",
           "MEMECOIN_RESEARCH_SHYFT_AMS_URL": "https://grpc.example:8443/"}
    assert shadow.optional_arms(env, enabled=True, missing_deps=())[0]["url"] == env[
        "MEMECOIN_RESEARCH_SHYFT_AMS_URL"]
    with pytest.raises(SystemExit, match="refusing"):
        shadow.optional_arms(dict(env, FUNDED_KEY="research-only"), enabled=True, missing_deps=())


def test_shyft_check_never_reads_owners_or_opens_network(monkeypatch):
    monkeypatch.setattr(shadow.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(shadow, "_yellowstone_modules", lambda: (object(), object()))
    monkeypatch.setattr(shadow, "load_owners", lambda: pytest.fail("check read production owners"))
    env = {"MEMECOIN_RESEARCH_SHYFT_TOKEN": "research-only-secret"}
    report = shadow.shyft_check(env)
    assert report["ready"] and report["protocol"] == "ready"
    assert report["network_checked"] is False and report["funded_enabled"] is False
    assert "research-only-secret" not in json.dumps(report)
    missing = shadow.shyft_check({})
    assert not missing["ready"] and all(a["why"] == "token_missing" for a in missing["arms"])


def test_shyft_check_reports_missing_dependencies_and_protocol_failure(monkeypatch):
    monkeypatch.setattr(shadow.importlib.util, "find_spec", lambda name: None)
    report = shadow.shyft_check({"MEMECOIN_RESEARCH_SHYFT_TOKEN": "research-only"})
    assert not report["ready"] and report["protocol"] == "dependencies_missing:grpc,grpc_tools"
    monkeypatch.setattr(shadow.importlib.util, "find_spec", lambda name: object())
    def broken():
        raise RuntimeError("secret from compiler")
    monkeypatch.setattr(shadow, "_yellowstone_modules", broken)
    report = shadow.shyft_check({"MEMECOIN_RESEARCH_SHYFT_TOKEN": "research-only"})
    assert not report["ready"] and report["protocol"] == "protocol_error:RuntimeError"
    assert "secret" not in json.dumps(report)


def test_vendored_yellowstone_compiles_real_filtered_requests_offline():
    pytest.importorskip("grpc_tools")
    pb, stub = shadow._yellowstone_modules()
    assert shadow._yellowstone_modules() == (pb, stub)
    req = pb.SubscribeRequest(transactions={"owners": pb.SubscribeRequestFilterTransactions(
        vote=False, account_include=["owner"])}, commitment=pb.CommitmentLevel.PROCESSED)
    decoded = pb.SubscribeRequest.FromString(req.SerializeToString())
    assert list(decoded.transactions["owners"].account_include) == ["owner"]
    assert not decoded.HasField("from_slot")
    assert pb.SubscribeUpdate(pong=pb.SubscribeUpdatePong(id=1)).HasField("pong")


@pytest.mark.parametrize("signature,slot,owner_bytes", [(bytes(range(64)), 10, bytes(range(32))),
    (b"bad", 10, bytes(range(32))), (bytes(range(64)), 0, bytes(range(32))),
    (bytes(range(64)), 10, b"bad")])
def test_real_shyft_wire_updates_require_valid_identity_and_never_grant_authority(
        tmp_path, monkeypatch, signature, slot, owner_bytes):
    grpc = pytest.importorskip("grpc")
    pb, pb_grpc = shadow._yellowstone_modules()
    owner = base58.b58encode(owner_bytes).decode()
    update = pb.SubscribeUpdate()
    update.transaction.slot = slot
    update.transaction.transaction.signature = signature
    update.transaction.transaction.transaction.message.account_keys.append(owner_bytes)
    path = tmp_path / "events.jsonl"
    stop = threading.Event()
    rec = shadow.Recorder(path, stop=stop)
    head = shadow.Head()
    head.slot, head.t_ns = 10, shadow.time.monotonic_ns()
    channel = types.SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(grpc, "secure_channel", lambda *a, **k: channel)
    def subscribe(requests, metadata):
        req = next(requests)
        assert list(req.transactions["owners"].account_include) == [owner]
        assert req.commitment == pb.CommitmentLevel.PROCESSED and not req.HasField("from_slot")
        assert next(requests).ping.id == 1
        assert metadata == (("x-token", "test-token"),)
        def replies():
            yield pb.SubscribeUpdate(pong=pb.SubscribeUpdatePong(id=1))
            yield pb.SubscribeUpdate.FromString(update.SerializeToString())
            stop.set()
        return types.SimpleNamespace(__iter__=replies, cancel=lambda: None)
    class Stream:
        def __init__(self, requests, metadata):
            self.replies = subscribe(requests, metadata)
        def __iter__(self):
            return self.replies.__iter__()
        def cancel(self):
            stop.set()
    monkeypatch.setattr(pb_grpc, "GeyserStub", lambda _: types.SimpleNamespace(Subscribe=Stream))
    shadow.shred_feed("https://grpc.example", "test-token", [owner], rec, stop, head=head)
    rec.close()
    txs = [r for r in rows(path) if r["k"] == "tx"]
    if len(signature) == 64 and slot and len(owner_bytes) == 32:
        assert len(txs) == 1 and txs[0]["owner"] == owner
        assert txs[0]["err"] is None and txs[0]["source_readiness"] == "unknown"
    else:
        assert txs == [] and "invalid_transaction_identity" in rows(path)[-1]["why"]


def test_latency_excludes_replay_backlog_and_stale_or_missing_head():
    head = shadow.Head()
    assert shadow.latency_exclusion(10, 100, head, 10) == "head_unknown"
    head.slot, head.t_ns = 10, 100
    assert shadow.latency_exclusion(9, 100, head, 10) == "backlog"
    assert shadow.latency_exclusion(10, 100, head, 10) is None
    assert shadow.latency_exclusion(10, 3_000_000_100, head, 10) == "head_stale"


def test_mid_session_old_slots_are_backlog_even_after_the_ack_watermark():
    head = shadow.Head()
    head.slot, head.t_ns = 50, 100
    assert shadow.latency_exclusion(20, 100, head, 10) == "backlog"
    assert shadow.latency_exclusion(49, 100, head, 10) is None


def test_receipt_guard_rechecks_the_book_after_waiting_for_the_research_pacer(monkeypatch):
    import urllib.request
    from sniper.services.memecoin_research_pacer import SharedPacer
    state = {"flat": True}
    def acquire(self):
        state["flat"] = False
    monkeypatch.setattr(SharedPacer, "acquire", acquire)
    monkeypatch.setattr(shadow, "book_is_flat", lambda: state["flat"])
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("book opened while pacing"))
    with pytest.raises(RuntimeError, match="funded_book_open"):
        shadow.read_receipt("https://research/?api-key=hidden", "sig", shadow.time.monotonic() + 1)


def test_paired_latency_and_control_use_the_same_common_healthy_window():
    s = 1_000_000_000
    events = [{"k": "up", "feed": f, "t_ns": 0} for f in ("A", "A2", "H")]
    def pair(sig, at, eligible=True):
        return [{"k": "tx", "feed": f, "sig": sig, "t_ns": int((at + delta) * s),
                 "head": 1, "latency_eligible": eligible}
                for f, delta in (("H", 0), ("A", 0.1), ("A2", 0.1))]
    events += pair("edge", 1) + pair("healthy", 20) + pair("replay", 22, False)
    events += [{"k": "down", "feed": "A2", "t_ns": 30 * s}]
    events += pair("control_outage", 40)
    events += [{"k": "down", "feed": f, "t_ns": 60 * s} for f in ("A", "H")]
    result = shadow.summarize_events(events)
    assert result["H"]["paired"] == 1
    assert result["H"]["lead_ms_p10_p50_p90"] == [100, 100, 100]
    assert result["H"]["negative_control"]["paired"] == 1
    assert result["H"]["negative_control"]["lead_ms_p10_p50_p90"] == [0, 0, 0]


@pytest.mark.parametrize("baseline,other,miss_other,miss_a", [
    ((20, True), (35, True), 1, 0),
    ((35, True), (20, True), 0, 1),
    ((20, True), (20, False), 1, 0),
    ((20, False), (20, True), 0, 1),
    ((20, True), (60, True), 1, 1),
])
def test_coverage_requires_the_same_valid_pair_as_latency(baseline, other, miss_other, miss_a):
    s = 1_000_000_000
    def receipt(sample):
        t, eligible = sample
        return {"t_ns": t * s, "latency_eligible": eligible}
    by_sig = {"source": {"A": receipt(baseline), "H": receipt(other)}}
    result = shadow._comparison(by_sig, "H", [(0, 30 * s), (50 * s, 80 * s)])
    assert result["paired"] == 0
    assert result["seen_by_A_missed_while_up"] == miss_other
    assert result["seen_missed_by_A_while_up"] == miss_a


def test_valid_pairs_cover_each_feed_once():
    s = 1_000_000_000
    result = shadow._comparison({"source": {
        "A": {"t_ns": 20 * s, "latency_eligible": True},
        "H": {"t_ns": 20 * s - 100_000_000, "latency_eligible": True}}}, "H", [(0, 30 * s)])
    assert result["paired"] == result["eligible_A"] == result["eligible_other"] == 1
    assert result["seen_by_A_missed_while_up"] == result["seen_missed_by_A_while_up"] == 0


def test_enrichment_reads_the_union_and_missing_receipt_stays_unknown(tmp_path, monkeypatch):
    import funded_exit_truth as fet
    calls = []
    def read(url, sig, deadline):
        calls.append(sig)
        return None
    monkeypatch.setattr(shadow, "read_receipt", read, raising=False)
    monkeypatch.setattr(shadow, "book_is_flat", lambda: True, raising=False)
    import slot_lane_send_monitor as mon
    monkeypatch.setattr(mon, "rpc_url", lambda: "https://mainnet.helius-rpc.com/?api-key=research")
    ev = [{"k": "start", "owners": ["owner"]},
          {"k": "tx", "feed": "H", "sig": "early_only", "owner": None},
          {"k": "tx", "feed": "A", "sig": "baseline", "owner": "owner"}]
    (tmp_path / "events.jsonl").write_text("\n".join(map(json.dumps, ev)))
    spacing = fet.SPACING_S
    shadow.enrich(tmp_path)
    enriched = json.loads((tmp_path / "enriched.json").read_text())
    assert set(enriched) == {"early_only", "baseline"}
    assert all(r["landed"] is None and r["receipt_state"] == "unknown" for r in enriched.values())
    assert set(calls) == {"early_only", "baseline"}
    assert fet.SPACING_S == spacing


def test_a_receipt_missing_metadata_does_not_imply_success():
    row = shadow.enrichment_row("sig", {"slot": 42, "transaction": {"signatures": ["sig"]}}, ["owner"])
    assert row["landed"] is True and row["err"] is None and row["lane_buy"] is None
    assert row["source_readiness"] == "unknown" and row["index"] is None


def test_missing_receipt_account_keys_are_unknown_not_no_owner_buy():
    row = shadow.enrichment_row("sig", {"slot": 42, "transaction": {"signatures": ["sig"]},
                                        "meta": {"err": None, "logMessages": []}}, ["owner"])
    assert row["lane_buy"] is None and row["source_readiness"] == "unknown"


@pytest.mark.parametrize("signatures", [None, [], "sig", ("sig",), {"sig": True}, ["other"], [None, "sig"]])
def test_receipt_without_exact_signature_identity_remains_unknown(signatures):
    row = shadow.enrichment_row("sig", {"slot": 42, "transaction": {"signatures": signatures},
                                        "meta": {"err": None, "logMessages": []}}, ["owner"])
    assert row["landed"] is None and row["receipt_state"] == "unknown"
    assert row["slot"] is None and row["err"] is None and row["lane_buy"] is None
    assert row["source_readiness"] == "unknown"


def test_receipt_without_transaction_identity_remains_unknown():
    row = shadow.enrichment_row("sig", {"slot": 42}, ["owner"])
    assert row["landed"] is None and row["receipt_state"] == "unknown" and row["slot"] is None


def test_a_secondary_signature_cannot_identify_the_benchmark_transaction():
    row = shadow.enrichment_row("sig", {"slot": 42, "transaction": {"signatures": ["primary", "sig"]}}, [])
    assert row["landed"] is None and row["receipt_state"] == "unknown" and row["slot"] is None


@pytest.mark.parametrize("count", [None, True, -1, 0, 2, 3, 1.0, "1"])
def test_malformed_signer_header_preserves_inclusion_but_not_source_proof(count, monkeypatch):
    from sniper.services import memecoin_slot_signal as signal
    monkeypatch.setattr(signal, "decode_owner_buy", lambda *a, **k: pytest.fail("invalid signer header decoded"))
    tx = {"slot": 42, "transaction": {"signatures": ["sig"], "message": {
        "header": {"numRequiredSignatures": count}, "accountKeys": ["owner", "program"]}},
        "meta": {"err": None, "logMessages": []}}
    row = shadow.enrichment_row("sig", tx, ["owner"])
    assert row["landed"] is True and row["slot"] == 42 and row["err"] is False
    assert row["receipt_state"] == "partial" and row["lane_buy"] is None
    assert row["source_readiness"] == "unknown"


@pytest.mark.parametrize("count", [1, 2])
def test_valid_signer_counts_remain_classifiable(count):
    tx = {"slot": 42, "transaction": {"signatures": ["sig"] + ["second"] * (count - 1), "message": {
        "header": {"numRequiredSignatures": count}, "accountKeys": ["other", "program"]}},
        "meta": {"err": None, "logMessages": []}}
    row = shadow.enrichment_row("sig", tx, ["owner"])
    assert row["landed"] is True and row["receipt_state"] == "confirmed" and row["lane_buy"] is False


@pytest.fixture
def local_rpc_server(monkeypatch):
    from sniper.services.memecoin_research_pacer import SharedPacer
    monkeypatch.setattr(SharedPacer, "acquire", lambda self: None)
    monkeypatch.setattr(shadow, "book_is_flat", lambda: True)
    running = []
    def serve(body=b'{"result":null}', *, status=200, delay=0, chunk_size=65536, send_length=True):
        requests = []
        disconnected = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if send_length:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for offset in range(0, len(body), chunk_size):
                        self.wfile.write(body[offset:offset + chunk_size])
                        self.wfile.flush()
                        if delay:
                            shadow.time.sleep(delay)
                except (BrokenPipeError, ConnectionResetError):
                    disconnected.set()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        running.append((server, thread))
        return "http://127.0.0.1:%d/" % server.server_port, requests, disconnected
    yield serve
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(1)


def test_trickling_receipt_cannot_outlive_the_total_deadline(local_rpc_server):
    url, requests, disconnected = local_rpc_server(
        b'{"result":null}' + b" " * 64, delay=0.02, chunk_size=1)
    started = shadow.time.monotonic()
    with pytest.raises(TimeoutError):
        shadow.read_receipt(url, "sig", started + 0.15)
    assert shadow.time.monotonic() - started < 0.5
    assert len(requests) == 1 and disconnected.wait(0.5)


@pytest.mark.parametrize("send_length", [True, False])
def test_oversized_receipt_body_is_refused(local_rpc_server, send_length):
    body = b'{"result":"' + b"x" * (4 * 1024 * 1024) + b'"}'
    url, requests, _ = local_rpc_server(body, send_length=send_length)
    with pytest.raises(ValueError, match="receipt_body_too_large"):
        shadow.read_receipt(url, "sig", shadow.time.monotonic() + 2)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_http_error_stops_enrichment_without_a_second_request(local_rpc_server, tmp_path, monkeypatch, status):
    import slot_lane_send_monitor as mon
    url, requests, _ = local_rpc_server(status=status)
    monkeypatch.setattr(mon, "rpc_url", lambda: url)
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(
        {"k": "tx", "feed": "H", "sig": sig}) for sig in ("s1", "s2")))
    shadow.enrich(tmp_path, max_rpc_calls=2, seconds=1)
    done = json.loads((tmp_path / "enriched.json").read_text())
    assert len(requests) == 1 and all(row["landed"] is None for row in done.values())
    assert all(row["read_reason"] == "http_%d" % status for row in done.values())


def test_trickling_timeout_stops_the_rest_of_enrichment(local_rpc_server, tmp_path, monkeypatch):
    import slot_lane_send_monitor as mon
    url, requests, _ = local_rpc_server(b'{"result":null}' + b" " * 64, delay=0.02, chunk_size=1)
    monkeypatch.setattr(mon, "rpc_url", lambda: url)
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(
        {"k": "tx", "feed": "H", "sig": sig}) for sig in ("s1", "s2")))
    shadow.enrich(tmp_path, max_rpc_calls=2, seconds=0.15)
    done = json.loads((tmp_path / "enriched.json").read_text())
    assert len(requests) == 1 and all(row["read_reason"] == "TimeoutError" for row in done.values())


@pytest.mark.parametrize("external_stop", [True, False])
def test_optional_grpc_stop_cancels_a_stalled_stream_and_closes_the_channel(tmp_path, monkeypatch,
                                                                        external_stop):
    path = tmp_path / "events.jsonl"
    stop = threading.Event()
    rec = shadow.Recorder(path, stop=stop)
    started = threading.Event()
    cancelled = threading.Event()
    class Stream:
        def __iter__(self):
            started.set()
            cancelled.wait(1)
            return iter(())
        def cancel(self):
            cancelled.set()
    class Channel:
        closed = False
        def close(self):
            self.closed = True
    channel = Channel()
    monkeypatch.setitem(sys.modules, "grpc", types.SimpleNamespace(
        secure_channel=lambda *a, **k: channel, ssl_channel_credentials=lambda: None))
    pb_grpc = types.SimpleNamespace(GeyserStub=lambda _: types.SimpleNamespace(Subscribe=lambda *a, **k: Stream()))
    monkeypatch.setattr(shadow, "_yellowstone_modules", lambda: (None, pb_grpc))
    monkeypatch.setattr(shadow, "ACK_TIMEOUT_S", 0.1)
    thread = threading.Thread(target=shadow.shred_feed,
                              args=("https://grpc.example", "secret", ["owner"], rec, stop))
    thread.start()
    assert started.wait(1)
    timed_out = cancelled.wait(0.4) if not external_stop else False
    stop.set()
    thread.join(0.5)
    rec.close()
    assert cancelled.is_set() and channel.closed and not thread.is_alive()
    if not external_stop:
        assert timed_out and "subscription_ack_timeout" in rows(path)[-1]["why"]


def test_enrichment_stops_on_the_first_429_and_marks_the_remaining_union_unknown(tmp_path, monkeypatch):
    import urllib.error
    import slot_lane_send_monitor as mon
    monkeypatch.setattr(mon, "rpc_url", lambda: "https://mainnet.helius-rpc.com/?api-key=research")
    monkeypatch.setattr(shadow, "book_is_flat", lambda: True, raising=False)
    calls = []
    def limited(url, sig, deadline):
        calls.append(sig)
        raise urllib.error.HTTPError(url, 429, "limited", {}, None)
    monkeypatch.setattr(shadow, "read_receipt", limited, raising=False)
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(
        {"k": "tx", "feed": "H", "sig": sig}) for sig in ("s1", "s2")))
    shadow.enrich(tmp_path, max_rpc_calls=2, seconds=1)
    done = json.loads((tmp_path / "enriched.json").read_text())
    assert calls == ["s1"] and set(done) == {"s1", "s2"}
    assert done["s1"]["read_reason"] == "http_429"
    assert done["s2"]["landed"] is None


@pytest.mark.parametrize("flat,cap,expected", [(False, 10, 0), (True, 1, 1)])
def test_enrichment_obeys_its_separate_call_cap_and_flat_book_guard(tmp_path, monkeypatch, flat, cap, expected):
    import slot_lane_send_monitor as mon
    monkeypatch.setattr(mon, "rpc_url", lambda: "https://mainnet.helius-rpc.com/?api-key=research")
    monkeypatch.setattr(shadow, "book_is_flat", lambda: flat, raising=False)
    calls = []
    monkeypatch.setattr(shadow, "read_receipt", lambda url, sig, deadline: calls.append(sig), raising=False)
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(
        {"k": "tx", "feed": "H", "sig": sig}) for sig in ("s1", "s2")))
    shadow.enrich(tmp_path, max_rpc_calls=cap, seconds=1)
    assert len(calls) == expected
    assert set(json.loads((tmp_path / "enriched.json").read_text())) == {"s1", "s2"}


def test_receipt_read_uses_confirmed_no_retry_and_deadline_aware_shared_pacing(local_rpc_server, monkeypatch):
    from sniper.services.memecoin_research_pacer import SharedPacer
    url, requests, _ = local_rpc_server()
    assert shadow.read_receipt(url, "sig", shadow.time.monotonic() + 1) is None
    assert len(requests) == 1 and requests[0]["params"][1]["commitment"] == "confirmed"
    monkeypatch.setattr(SharedPacer, "acquire", lambda self: self.sleep(20))
    with pytest.raises(TimeoutError):
        shadow.read_receipt(url, "sig", shadow.time.monotonic() + 0.1)
    assert len(requests) == 1
