"""Helius Preconfirmations as the slot lane's third feed (operator 2026-10-17 "turn preconf on"; Alpha
GO with conditions). Pins, in Alpha's order:
- the SIMD-0385 v1 parser on 16 recorded real v1 owner transactions (9 owners), plus malformed and
  truncated bytes, which fail closed (Malformed, never another exception, never a buy);
- the ed25519_local owner proof, which a tampered message or signature fails;
- a v0 frame with lookup tables is frame-log only; status Failed / Unknown is refused;
- ONE shared buyer: rabbit and preconf never both fire one signature;
- the credit meter: max(300, 10 x messages) per covered slot, a hard daily cap that closes the socket
  until 00:00Z and pages once;
- the preconf knob and breaker are its own; the pre-execution proof gate wants the preconf knob and
  a local signature for a preconf proof;
- a recorded direct curve buy, delivered as a preconf frame, fires exactly one buy from the preconf
  feed with an ed25519_local proof.
"""
import asyncio
import base64
import dataclasses
import json
import random
import struct
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import base58
import pytest

from sniper.services import memecoin_preconf_signal as P
from sniper.services import memecoin_rabbit_signal as R
from sniper.services import memecoin_shyft_prepare as prep
from sniper.services.memecoin_feed_arrivals import StampedArrivals
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from sniper.settings import memecoin as cfg
from test_memecoin_rabbit_signal_stage2 import (_ev, _offer, _rabbit_buy, backstop, fire_rpc, FIRE_MINT,
                                                FIRE_OWNER, FIRE_EVENT)

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/preconf_v1_owner_txs_20261017.json").read_text())
V1 = sorted(FX["v1"].items())
OTHER = sorted(FX["other"].items())
FEED = P.FEED
RABBIT = "rabbit:rabbitstream.fra.shyft.to"


def _raw(rec):
    return base64.b64decode(rec["raw_b64"])


def _frame(slot, tx, status=P.STATUS_SUCCESS, index=7):
    return bytes([P.FRAME_VERSION]) + struct.pack("<QQ", slot, index) + bytes([status]) + tx


@pytest.fixture
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_BUY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF", True)


# --- the v1 parser ------------------------------------------------------------------------------

def test_sixteen_real_v1_owner_transactions_parse_exactly_and_prove_the_owner(knobs):
    assert len(V1) >= 10 and len({rec["owner"] for _s, rec in V1}) >= 5
    decoded = 0
    for signature, rec in V1:
        raw = _raw(rec)
        tx = P.parse_v1(raw)
        assert base58.b58encode(tx.signatures[0]).decode() == signature
        assert tx.message_end + 64 * tx.num_required == len(raw)
        update, verify = P.frame_update(rec["slot"], raw)
        assert verify(rec["owner"]) == "ed25519_local", signature
        msg = update.transaction.transaction.transaction.message
        assert msg.versioned and msg.HasField("config") and len(msg.account_keys) == len(tx.addresses)
        assert prep.owner_signer(update, {rec["owner"]: ("u", "h")}) == (signature, rec["owner"])
        candidate = prep.preparation_candidate(update, {rec["owner"]: ("u", "h")}, recv_monotonic=1.0,
                                               generation=1, on_owner_miss=lambda *a: None)
        if candidate is not None:                               # the seam fields for Beta's decoder
            assert candidate["version"] == "v1" and candidate["signers"][1] == rec["owner"]
            assert len(candidate["writable"]) == len(candidate["account_keys"])
            assert "config" not in candidate                    # no execution input on a candidate
            decoded += 1
    assert decoded >= 2                                         # the DFlow -> DAMM v2 frames decode


def test_a_tampered_message_or_signature_fails_the_owner_proof():
    signature, rec = V1[0]
    raw = bytearray(_raw(rec))
    tx = P.parse_v1(bytes(raw))
    for at in (tx.message_end - 1, 41 + 32 + 5):                # instruction data; an address byte
        tampered = bytearray(raw)
        tampered[at] ^= 0x01
        try:
            _update, verify = P.frame_update(rec["slot"], bytes(tampered))
        except P.Malformed:
            continue                                            # a structural byte: refused earlier
        assert verify(rec["owner"]) is None
    owner_index = next(i for i, key in enumerate(tx.addresses[:tx.num_required])
                       if base58.b58encode(key).decode() == rec["owner"])
    forged = bytearray(raw)
    forged[tx.message_end + 64 * owner_index] ^= 0x01
    _update, verify = P.frame_update(rec["slot"], bytes(forged))
    assert verify(rec["owner"]) is None
    _update, verify = P.frame_update(rec["slot"], bytes(raw))
    assert verify("11111111111111111111111111111111") is None    # not a required signer


def test_malformed_and_truncated_bytes_fail_closed():
    _signature, rec = V1[0]
    raw = _raw(rec)
    for cut in sorted({1, 4, 41, 42, 43, len(raw) // 2, len(raw) - 65, len(raw) - 1}):
        with pytest.raises(P.Malformed):
            P.parse_v1(raw[:cut])
    with pytest.raises(P.Malformed):
        P.parse_v1(raw + b"\x00")                             # trailing data after the signatures
    with pytest.raises(P.Malformed):
        P.parse_v1(b"\x80" + raw[1:])                         # not the v1 prefix
    for at, value in ((4, raw[4] | 0x20), (4, (raw[4] & ~0x03) | 0x01), (1, 0), (2, raw[1]), (41, 65)):
        mutated = bytearray(raw)
        mutated[at] = value
        with pytest.raises(P.Malformed):
            P.parse_v1(bytes(mutated))
    dup = bytearray(raw)
    dup[42 + 32:42 + 64] = raw[42:42 + 32]                    # second address = first
    with pytest.raises(P.Malformed):
        P.parse_v1(bytes(dup))
    rng = random.Random(20261017)
    for _ in range(400):                                      # fuzz: Malformed or a clean parse, nothing else
        blob = bytearray(raw)
        for _flip in range(rng.randint(1, 4)):
            blob[rng.randrange(len(blob))] = rng.randrange(256)
        blob = bytes(blob[:rng.randint(1, len(blob))])
        try:
            P.frame_update(rec["slot"], blob)
        except P.Malformed:
            pass
    for bad in (b"", bytes([P.FRAME_VERSION]) + bytes(17), bytes([2]) + bytes(40),
                bytes([P.FRAME_VERSION]) + struct.pack("<QQ", 1, 0) + b"\x03" + raw):
        with pytest.raises(P.Malformed):
            P.parse_frame(bad)
    assert P.parse_frame(_frame(9, raw)) == (9, 7, P.STATUS_SUCCESS, raw)


def test_legacy_and_v0_frames_verify_and_a_v0_with_lookup_tables_is_frame_log_only():
    seen = {"v0": 0, "legacy": 0, "lookups": 0}
    for signature, rec in OTHER:
        update, verify = P.frame_update(rec["slot"], _raw(rec))
        assert verify(rec["owner"]) == "ed25519_local", signature
        misses = []
        candidate = prep.preparation_candidate(update, {rec["owner"]: ("u", "h")}, recv_monotonic=1.0,
                                               generation=1, on_owner_miss=lambda *a: misses.append(a[2]))
        if rec["lookups"]:
            seen["lookups"] += 1
            assert candidate is None and misses == ["alt"]
        seen["legacy" if rec["version"] == "legacy" else "v0"] += 1
    assert seen["v0"] >= 1 and seen["legacy"] >= 1 and seen["lookups"] >= 1


# --- the sink: status, the shared buyer -----------------------------------------------------------

class _Frames:
    def __init__(self):
        self.notes, self.frames = [], []

    def frame(self, *a):
        self.frames.append(a)

    def note(self, signature, owner, verdict=None, *, venue=None, route=None):
        self.notes.append((signature, verdict, venue))


def _sink(buyer=None, owners=None):
    status, frames = {}, _Frames()
    arrivals = StampedArrivals(("shyft:grpc.fra.shyft.to", RABBIT, FEED))
    sink = P.FrameSink(feed=FEED, owners_fn=lambda: owners or {FIRE_OWNER: ("uid", "h")}, arrivals=arrivals,
                       frames=frames, status=status, buyer=buyer)
    return sink, status, frames, arrivals


@pytest.mark.parametrize("state, reason", [(P.STATUS_FAILED, "source_failed"),
                                           (P.STATUS_UNKNOWN, "status_unknown")])
def test_a_failed_or_unknown_status_is_refused_before_any_decode(state, reason):
    class Buyer:
        def offer(self, *a, **k):
            raise AssertionError("never offered")
    sink, status, frames, arrivals = _sink(Buyer())
    rec = FX["fire_direct"]
    assert sink(rec["slot"], state, _raw(rec), 5.0, 1) is None
    assert status["refused"] == {reason: 1} and status["owner_frames"] == 1 and status["offered"] == 0
    assert frames.notes == [(rec["signature"], reason, None)]
    assert arrivals.raw.arrivals(rec["signature"], FIRE_OWNER) == ((FEED, 5.0),)   # still stamped


def test_a_frame_without_a_tracked_signer_or_with_bad_bytes_is_counted():
    sink, status, _f, _a = _sink(owners={"11111111111111111111111111111111": ("u", "h")})
    rec = FX["fire_direct"]
    sink(rec["slot"], P.STATUS_SUCCESS, _raw(rec), 1.0, 1)
    sink(rec["slot"], P.STATUS_SUCCESS, b"\x81\x00", 1.0, 1)
    assert status["refused"] == {"not_owner_signed": 1, "malformed:v1_prefix": 1}


def _shared_buyer(read):
    stats, fired = {}, []
    rabbit = backstop(read)
    landing = R.LandingWatch(backstop=rabbit, head_fn=lambda: (None, None), status={})
    buyer = R.RabbitBuyer(seen={}, owners_fn=lambda: {FIRE_OWNER: ("uid", "h")}, on_buy=fired.append,
                          backstop=rabbit, feed=RABBIT, status=stats, landing=landing,
                          head_fn=lambda: (FX["fire_direct"]["slot"] - 1, 1.0), epoch_fn=lambda: 1760000000.0)
    return buyer, stats, fired, rabbit


async def _drain(buyer):
    while buyer._tasks:
        await asyncio.sleep(0)
        await asyncio.gather(*list(buyer._tasks), return_exceptions=True)


def test_a_recorded_curve_buy_as_a_preconf_frame_fires_once_with_a_local_proof(knobs):
    buyer, stats, fired, _rabbit = _shared_buyer(fire_rpc())
    sink, status, _frames, _arr = _sink(buyer)
    rec = FX["fire_direct"]

    async def main():
        sink(rec["slot"], P.STATUS_SUCCESS, _raw(rec), time.monotonic(), 3)
        await _drain(buyer)
    asyncio.run(main())
    assert status["offered"] == 1 and stats["fired"] == 1 and len(fired) == 1
    buy = fired[0]
    assert R.is_rabbit(buy) and buy.signal_feed == FEED and buy.mint == FIRE_MINT
    assert buy.token_amount == FIRE_EVENT["token_amount"]
    proof = buy.source_initiation
    assert proof["signature_basis"] == "ed25519_local" and proof["feed"] == FEED
    assert stats["by_feed"] == {FEED: {"candidates": 1, "fired": 1}}


@pytest.mark.parametrize("first", [RABBIT, FEED])
def test_one_shared_buyer_never_fires_a_signature_twice_across_rabbit_and_preconf(knobs, first):
    buyer, stats, fired, _rabbit = _shared_buyer(fire_rpc())
    rec = FX["fire_direct"]
    update, verify = P.frame_update(rec["slot"], _raw(rec))

    def candidate(feed):
        c = prep.preparation_candidate(update, {FIRE_OWNER: ("uid", "h")}, recv_monotonic=time.monotonic(),
                                       generation=1)
        c["feed"] = feed
        return c

    async def main():
        for feed in (first, RABBIT if first == FEED else FEED):
            buyer.offer(candidate(feed), update, basis=verify(FIRE_OWNER) if feed == FEED else R._UNSET)
            await _drain(buyer)
    asyncio.run(main())
    assert len(fired) == 1 and fired[0].signal_feed == first and stats["duplicate"] == 1


# --- the knob, the breaker, the proof gate --------------------------------------------------------

def test_the_preconf_knob_and_breaker_are_its_own(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_BUY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF", False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_BREAKER_N", 2)
    pages, exits = [], []
    b = R.RabbitBackstop(read=None, exit_offer=exits.append, alert_fn=lambda *a: pages.append(a))
    assert b.allowed(RABBIT) is True and b.allowed(FEED) is False and b.allowed() is True
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF", True)
    for i in range(2):
        b.verdict(dict(signature="s%d" % i, reason=R.SOURCE_NOT_LANDED, feed=FEED))
    assert b.allowed(FEED) is False and b.allowed(RABBIT) is True            # preconf tripped alone
    assert b.status["preconf"]["trips"] == 1 and b.status["trips"] == 0
    assert pages and pages[0][0] == "SLOT-LANE PRECONF BREAKER" and "MEMECOIN_SLOT_LANE_PRECONF" in pages[0][1]
    assert len(exits) == 2                                                    # every verdict still exits
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF", False)
    b.allowed(FEED)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF", True)
    assert b.allowed(FEED) is True                                            # off then on: the resume
    b.note_result({"status": "filled", "sig": "x", "landing_slot": 11}, 10, feed=FEED)
    assert b.status["preconf"]["fills"] == 1 and b.status["fills"] == 0
    assert b.status["preconf"]["land_minus_s"]["p50"] == 1


def test_the_proof_gate_wants_the_preconf_knob_and_a_local_signature(knobs, monkeypatch):
    buy = _rabbit_buy()
    proof = dict(buy.source_initiation, feed=FEED, signature_basis="ed25519_local")
    ev = _ev(dataclasses.replace(buy, source_initiation=proof))
    assert source_initiation_refusal(ev, "solana") is None
    leader = _ev(dataclasses.replace(buy, source_initiation=dict(proof, signature_basis="leader_sigverify_v1")))
    assert source_initiation_refusal(leader, "solana") == "wallet_initiation_unproved"
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF", False)
    assert source_initiation_refusal(ev, "solana") == "wallet_initiation_unproved"
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_BUY", False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF", True)
    assert source_initiation_refusal(ev, "solana") is None                    # rabbit off: preconf stands
    assert source_initiation_refusal(_ev(buy), "solana") == "wallet_initiation_unproved"


# --- credits ------------------------------------------------------------------------------------

class _Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def test_the_meter_bills_each_covered_slot_and_caps_until_utc_midnight():
    clock = _Clock(datetime(2026, 10, 17, 23, 0, tzinfo=timezone.utc))
    meter = P.CreditMeter(1000, now_fn=clock)
    assert not meter.note(5) and meter.credits == 300                         # one message: the minimum
    for _ in range(30):
        meter.note(5)                                                        # 31 in one slot: 310
    assert meter.credits == 310 and meter.status["covered_slots"] == 1
    meter.note(6)
    meter.note(7)
    assert meter.credits == 910 and not meter.capped()
    assert meter.note(8) and meter.capped()
    assert meter.status["paused_until"] == "2026-10-18T00:00:00+00:00"
    assert meter.pause_s() == pytest.approx(3600.0)
    assert meter.page_once() and meter.page_once() is None                     # once per day
    clock.at = datetime(2026, 10, 18, 0, 0, 1, tzinfo=timezone.utc)
    assert not meter.capped() and meter.credits == 0 and meter.pause_s() == 0.0


class _WS:
    def __init__(self, script, sent):
        self.script, self.sent = list(script), sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if self.script:
            return self.script.pop(0)
        await asyncio.sleep(0.05)
        raise asyncio.TimeoutError


def test_the_feed_closes_at_the_cap_pages_once_and_resubscribes_on_a_roster_change():
    rec = FX["fire_direct"]
    raw = _raw(rec)
    sent, connects, pages, frames = [], [], [], []
    roster = {"A": ("u1", "a")}
    stop = threading.Event()
    clock = _Clock(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc))
    meter = P.CreditMeter(600, now_fn=clock)

    def connect(url):
        connects.append(url)
        if len(connects) == 1:          # ack, one frame, then the roster changes
            return _WS([json.dumps({"jsonrpc": "2.0", "id": 1, "result": 77}), _frame(100, raw)], sent)
        return _WS([json.dumps({"jsonrpc": "2.0", "id": 1, "result": 78}), _frame(101, raw),
                    _frame(101, raw), _frame(102, raw)], sent)

    def on_frame(slot, state, tx, mono, generation):
        frames.append((slot, generation))
        if len(frames) == 1:
            roster["B"] = ("u2", "b")

    def page(*a):
        pages.append(a)
        stop.set()
    status = {}

    async def main():
        await P.run_preconf_feed("wss://x/?api-key=k", lambda: roster, status, stop, on_frame=on_frame,
                                 meter=meter, connect=connect, alert_fn=page, reroster_s=0.0)
    asyncio.run(asyncio.wait_for(main(), 10))
    assert [r["params"][0]["signerInclude"] for r in sent] == [["A"], ["A", "B"]]
    assert all("accountInclude" not in r["params"][0] for r in sent)      # a mention is not a signer
    assert all(r["method"] == "preconfSubscribe" and r["params"][0]["includeSource"] == ["helius"]
               and r["params"][0]["failed"] is False for r in sent)
    # slot 100 (300) on the first socket; slot 101 (300) on the second reaches the cap and closes it:
    # the rest of that socket's frames are never read, and the page goes out once
    assert frames == [(100, 1), (101, 2)] and meter.credits == 600 and meter.capped()
    assert len(connects) == 2 and len(pages) == 1 and pages[0][0] == "SLOT-LANE PRECONF CREDIT CAP"
    assert status["acknowledged"] is False and status["connected"] is False


def test_the_url_comes_from_its_knob_or_the_research_key_and_never_another():
    assert P.preconf_url({"MEMECOIN_SLOT_LANE_PRECONF_URL": "wss://own/?api-key=1"}) == "wss://own/?api-key=1"
    assert P.preconf_url({"MEMECOIN_SLOT_LANE_PRECONF_URL": "https://own/?api-key=1"}) is None
    assert P.preconf_url({"MEMECOIN_RESEARCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=K"}) == \
        "wss://beta.helius-rpc.com/?api-key=K"
    assert P.preconf_url({}) is None


# --- the wiring: the signal loop runs the feed beside rabbit and the processed feeds --------------

def test_the_signal_loop_runs_preconf_through_the_shared_buyer_and_reports_it(knobs, monkeypatch, tmp_path):
    from sniper.services import memecoin_runner as runner
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services import memecoin_slot_signal as signal
    rec = FX["fire_direct"]
    stop, status, fired, sent = threading.Event(), {}, [], []
    rabbit = backstop(fire_rpc())

    async def idle(*a, **k):
        while not stop.is_set():
            await asyncio.sleep(0.01)
    monkeypatch.setattr(prep, "run_prepare_feed", idle)
    monkeypatch.setattr(full, "run_feed", idle)

    def on_buy(buy):
        fired.append(buy)
        stop.set()

    def connect(url):
        assert url == "wss://beta.helius-rpc.com/?api-key=K"
        return _WS([json.dumps({"jsonrpc": "2.0", "id": 1, "result": 5}),
                    _frame(rec["slot"], _raw(rec))], sent)
    asyncio.run(asyncio.wait_for(signal._run_signals(
        on_buy, lambda: {FIRE_OWNER: ("uid", "h")}, None, status, stop, None, None, None, None,
        ("token", ("https://grpc.ams.shyft.to",)), infra_provider="shyft",
        prepare_config=("token", "https://rabbitstream.fra.shyft.to"), on_prepare=lambda c: None,
        rabbit=rabbit, preconf={"url": "wss://beta.helius-rpc.com/?api-key=K", "connect": connect,
                                "alert_fn": lambda *a: None, "credit_path": tmp_path / "credits.json",
                                "sources": ("helius",)}), 10))
    assert len(fired) == 1 and fired[0].signal_feed == FEED
    assert sent[0]["params"][0]["signerInclude"] == [FIRE_OWNER]
    assert status["rabbit_buy"]["handlers"] == ["pump_curve", "dflow_pump_amm"]   # Beta's, after the curve
    assert status["preconf"]["offered"] == 1 and status["preconf"]["credits"]["credits"] == 300
    assert status["rabbit_buy"]["by_feed"][FEED]["fired"] == 1
    assert FEED in status["arrival_metrics"].feeds and RABBIT in status["arrival_metrics"].feeds
    # the heartbeat exports the whitelisted keys, and never the socket's key
    path = tmp_path / "heartbeat.json"
    runner.write_heartbeat({}, path=path, auth_ok=True,
                           slot_lane_status={"mode": "live", "signal": status, "rabbit_breaker": rabbit.status})
    text = path.read_text()
    lane = json.loads(text)["slot_lane"]
    assert lane["preconf"]["feed"]["acknowledged"] in (True, False) and lane["preconf"]["offered"] == 1
    assert lane["preconf"]["credits"]["cap"] == cfg.MEMECOIN_SLOT_LANE_PRECONF_DAILY_CREDITS
    assert lane["rabbit_buy"]["by_feed"][FEED]["fired"] == 1
    assert lane["rabbit_breaker"]["preconf"]["knob"] is True
    assert lane["rabbit_buy"]["handlers"] == ["pump_curve", "dflow_pump_amm"]
    assert lane["rabbit_buy"]["handler_status"]["dflow_pump_amm"]["drift_base_bps"] == {"n": 0, "p50": None, "p90": None}
    assert lane["rabbit_buy"]["amm_unwired"] == {"okx_pump_amm": "no sol_usd reader",   # no reader here
                                                 "jupiter_pump_amm": "no sol_usd reader"}
    assert "api-key" not in text
    pair = lane["feed_arrivals_raw"]["pairs"]
    assert any(FEED in key and RABBIT in key for key in pair) and any(FEED in key and "shyft:" in key for key in pair)


# --- signerInclude, one socket per source (Alpha GO 2026-10-17) --------------------------------------
# accountInclude matched any transaction MENTIONING an owner: 21 of the day's first 24 billed slots
# carried no owner frame. A frame has no source or subscription field, so each includeSource value
# gets its own socket and feed; the shared buyer fires once, and each subscription bills its slot.

@pytest.mark.parametrize("source", ["helius", "bam"])
def test_the_subscription_asks_for_owner_signed_transactions_from_one_source(source):
    request = json.loads(P.subscribe_request({"B", "A"}, source=source))
    assert request["method"] == "preconfSubscribe"
    assert request["params"] == [{"failed": False, "signerInclude": ["A", "B"], "includeSource": [source]}]
    with pytest.raises(ValueError):
        P.subscribe_request({"A"}, source="all")


def test_two_sources_in_one_slot_bill_twice_and_one_cap_covers_both():
    meter, status, seen = P.CreditMeter(50000), {"malformed": 0, "frames": 0}, []
    frame = _frame(100, _raw(FX["fire_direct"]))
    for source in ("helius", "bam", "bam"):
        P._handle_binary(frame, 1.0, 1, status, meter, lambda *a: seen.append(a), source=source)
    assert meter.credits == 600 and meter.messages == 3 and len(meter.slots) == 2 and len(seen) == 3


@pytest.mark.parametrize("raw,want", [("helius,bam", ("helius", "bam")), ("bam", ("bam",)),
                                      (" BAM , x, bam", ("bam",)), ("", ("helius",)), ("x", ("helius",))])
def test_the_sources_knob(raw, want):
    assert cfg._preconf_sources(raw) == want


class _SourceWS(_WS):
    """A socket whose frames depend on the source it is asked for."""

    def __init__(self, scripts, sent):
        super().__init__([], sent)
        self.scripts = scripts

    async def send(self, text):
        await super().send(text)
        source = json.loads(text)["params"][0]["includeSource"][0]
        self.script = list(self.scripts[source])


def test_each_source_is_its_own_feed_and_the_buy_fires_once(knobs, monkeypatch, tmp_path):
    from sniper.services import memecoin_runner as runner
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services import memecoin_slot_signal as signal
    rec = FX["fire_direct"]
    stop, status, fired, sent, lines = threading.Event(), {}, [], [], []
    rabbit = backstop(fire_rpc())

    async def idle(*a, **k):
        while not stop.is_set():
            await asyncio.sleep(0.01)
    monkeypatch.setattr(prep, "run_prepare_feed", idle)
    monkeypatch.setattr(full, "run_feed", idle)
    ack = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 5})
    frame = _frame(rec["slot"], _raw(rec))

    def connect(url):
        return _SourceWS({"helius": [ack, frame], "bam": [ack, frame]}, sent)

    async def main():
        task = asyncio.ensure_future(signal._run_signals(
            fired.append, lambda: {FIRE_OWNER: ("uid", "h")}, None, status, stop, None, None, None, None,
            ("token", ("https://grpc.ams.shyft.to",)), infra_provider="shyft",
            prepare_config=("token", "https://rabbitstream.fra.shyft.to"), on_prepare=lambda c: None,
            rabbit=rabbit, preconf={"url": "wss://beta.helius-rpc.com/?api-key=K", "connect": connect,
                                    "alert_fn": lambda *a: None, "credit_path": tmp_path / "c.json",
                                    "sources": ("helius", "bam")}))
        for _ in range(200):
            by = status.get("preconf_sources") or {}
            if fired and all(by.get(s, {}).get("offered") for s in ("helius", "bam")):
                break
            await asyncio.sleep(0.02)
        stop.set()
        await task
    monkeypatch.setattr("sniper.services.memecoin_rabbit_signal.logger.warning",
                        lambda msg, *a: lines.append(msg % a))
    from sniper.services import memecoin_rabbit_signal as R
    made, real_log = {}, R.FrameLog

    def frame_log(**kw):
        made[kw["feed"]] = kw
        return real_log(**kw)
    monkeypatch.setattr(R, "FrameLog", frame_log)
    asyncio.run(asyncio.wait_for(main(), 15))
    # rabbit's line never reads a preconf frame as its processed arrival, nor one preconf source another's
    assert set(made[RABBIT]["exclude"]) == {"preconf:helius", "preconf:bam"}
    assert made["preconf:bam"]["exclude"] == ("preconf:helius",) and made["preconf:bam"]["source"] == "bam"
    assert sorted(r["params"][0]["includeSource"][0] for r in sent) == ["bam", "helius"]
    assert all(r["params"][0]["signerInclude"] == [FIRE_OWNER] for r in sent)
    assert len(fired) == 1                                   # one signature, one buy
    by = status["preconf_sources"]
    assert by["helius"]["offered"] == 1 and by["bam"]["offered"] == 1
    assert by["helius"]["credits"]["credits"] == 600          # one meter: each subscription billed its slot
    assert status["preconf"] is by["helius"]
    feeds = status["arrival_metrics"].feeds
    assert "preconf:helius" in feeds and "preconf:bam" in feeds
    path = tmp_path / "heartbeat.json"
    runner.write_heartbeat({}, path=path, auth_ok=True,
                           slot_lane_status={"mode": "live", "signal": status, "rabbit_breaker": rabbit.status})
    lane = json.loads(path.read_text())["slot_lane"]
    assert set(lane["preconf_sources"]) == {"helius", "bam"}
    assert lane["preconf_sources"]["bam"]["offered"] == 1 and lane["preconf"]["offered"] == 1


def test_a_frame_line_names_its_source_and_never_counts_another_preconf_feed_as_processed():
    from sniper.services.memecoin_rabbit_signal import FrameLog
    out = []
    arrivals = {("S", "O"): (("preconf:helius", 1.000), ("preconf:bam", 1.002), ("shyft:x", 1.050))}
    log = FrameLog(feed="preconf:bam", arrivals_fn=lambda sig, owner: arrivals[(sig, owner)],
                   log=lambda msg, *a: out.append(msg % a), label="preconf", source="bam",
                   exclude=("preconf:helius",), peer="rabbit:r")
    log.frame("S", "O", 100, 1.002)
    log.write([(("S", "O"), log._recs.pop(("S", "O")), (1.050, "shyft:x"))])
    assert out[0].startswith("[memecoin] slot lane preconf frame sig=S")
    assert out[0].endswith(" source=bam") and "processed=shyft:x" in out[0]
    assert log.due() == []


def test_a_second_handler_with_the_agreed_seam_plugs_in_after_the_curve(knobs):
    """Beta's DflowAmmHandler shapes: match -> (plan, None) | (None, reason) | None, read(plan),
    verdict -> (fill, None) | (None, reason), build(..., no label). A frame the curve handler does not
    claim reaches it; its read runs on a worker thread; its buy fires once, attributed to the feed."""
    signature, rec = next((s, r) for s, r in V1 if r["rabbit_venue"].startswith("meteora_damm"))
    update, verify = P.frame_update(rec["slot"], _raw(rec))
    calls = []

    class Handler:
        route = "test_route"

        def match(self, candidate, update=None):
            calls.append("match")
            return ({"mint": "M"}, None) if candidate["signature"] == signature else None

        def read(self, plan):
            calls.append("read")
            return {"ok": True}

        def verdict(self, plan, state, candidate):
            return ({"amount": 1}, None) if state["ok"] else (None, "bad")

        def build(self, candidate, plan, state, fill, *, source_user_id, handle, signature_basis, feed, read_ms,
                  recv_epoch, head, head_mono, now_mono):
            calls.append(("build", signature_basis, feed))
            return types.SimpleNamespace(mint=plan["mint"], signal_feed=feed, source_initiation={"route": "x"})
    buyer, stats, fired, _rabbit = _shared_buyer(fire_rpc())
    buyer.owners_fn = lambda: {rec["owner"]: ("uid", "h")}
    buyer.handlers.append(Handler())
    candidate = prep.preparation_candidate(update, {rec["owner"]: ("uid", "h")}, recv_monotonic=time.monotonic(),
                                           generation=1)
    candidate["feed"] = FEED

    async def main():
        buyer.offer(candidate, update, basis=verify(rec["owner"]))
        await _drain(buyer)
    asyncio.run(main())
    assert calls == ["match", "read", ("build", "ed25519_local", FEED)] and len(fired) == 1
    assert stats["by_route"]["test_route"]["read_ms_p50"] is not None
    assert fired[0].source_initiation["feed"] == FEED          # stamped: the proof gate reads its knob from it
    joins = []
    Handler.observe_processed = lambda self, sig, line: joins.append((sig, line))
    buyer.observe_processed(types.SimpleNamespace(venue="pump_amm", signature="S", trade_event_log="Program data: x"))
    buyer.observe_processed(types.SimpleNamespace(venue="pump_curve", signature="S", virtual_sol_reserves=None))
    assert joins == [("S", "Program data: x")]                  # a PumpSwap processed buy joins the handler


# --- Gamma MED-1 / LOW-1 on 9874cd074 -------------------------------------------------------------

def test_a_frame_whose_owner_signature_does_not_verify_never_fires_through_the_sink(knobs):
    """The sink hands the buyer the feed's OWN basis (verify(owner)), never a constant: a frame whose
    owner signature fails is unsigned and fires nothing (Gamma MED-1: basis='ed25519_local' was green)."""
    buyer, stats, fired, _rabbit = _shared_buyer(fire_rpc())
    sink, status, _frames, _arr = _sink(buyer)
    rec = FX["fire_direct"]
    raw = bytearray(_raw(rec))
    update, _verify = P.frame_update(rec["slot"], bytes(raw))
    keys = [base58.b58encode(k).decode() for k in update.transaction.transaction.transaction.message.account_keys]
    raw[1 + 64 * keys.index(FIRE_OWNER)] ^= 0x01            # a v0 tx: one shortvec byte, then the signatures
    assert P.frame_update(rec["slot"], bytes(raw))[1](FIRE_OWNER) is None

    async def main():
        sink(rec["slot"], P.STATUS_SUCCESS, bytes(raw), time.monotonic(), 3)
        await _drain(buyer)
    asyncio.run(main())
    assert fired == [] and stats["unsigned"] == 1 and status["offered"] == 1


def test_an_unparsed_binary_frame_is_billed_as_its_own_slot_and_counted():
    """Gamma LOW-1: Helius bills whatever it delivered; a frame we cannot parse still costs a slot's 300."""
    meter, status, called = P.CreditMeter(50000), {"malformed": 0, "frames": 0}, []
    P._handle_binary(b"\x02not a preconf frame", 1.0, 1, status, meter, lambda *a: called.append(a))
    P._handle_binary(b"\x02not a preconf frame", 1.0, 1, status, meter, lambda *a: called.append(a))
    assert meter.credits == 600 and status == {"malformed": 2, "frames": 0} and called == []


def test_a_restart_inside_the_utc_day_keeps_the_spent_credits_and_the_page_mark(tmp_path):
    """Alpha 2026-10-17 (Gamma LOW-2): the 50000 cap is one cap per UTC day, not per process lifetime."""
    path = tmp_path / "data" / "slot_lane_preconf_credits.json"
    clock = _Clock(datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc))
    first = P.CreditMeter(1000, now_fn=clock, path=path)
    first.note(1)
    first.note(2)
    first.save(first.snapshot())
    second = P.CreditMeter(1000, now_fn=clock, path=path)          # the restarted feed
    assert second.credits == 600 and second.messages == 2 and second.status["restored"] is True
    assert second.status["covered_slots"] == 2
    second.note(3)
    assert not second.capped() and second.note(4) and second.credits == 1200
    assert second.page_once() is not None
    second.save(second.snapshot())
    third = P.CreditMeter(1000, now_fn=clock, path=path)
    assert third.capped() and third.page_once() is None              # paged once for the whole day
    clock.at = datetime(2026, 10, 18, 0, 0, 5, tzinfo=timezone.utc)
    assert P.CreditMeter(1000, now_fn=clock, path=path).credits == 0  # a new UTC day starts at 0
    path.write_text("{not json")
    clock.at = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
    assert P.CreditMeter(1000, now_fn=clock, path=path).credits == 0  # unreadable: logged, 0


def test_the_feed_loop_writes_the_days_spend_off_the_loop(tmp_path):
    rec = FX["fire_direct"]
    raw = _raw(rec)
    path = tmp_path / "credits.json"
    clock = _Clock(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc))
    meter = P.CreditMeter(600, now_fn=clock, path=path)
    stop, pages = threading.Event(), []

    def connect(url):
        return _WS([json.dumps({"jsonrpc": "2.0", "id": 1, "result": 1}), _frame(100, raw), _frame(101, raw)], [])

    def page(*a):
        pages.append(a)
        stop.set()

    async def main():
        await P.run_preconf_feed("wss://x", lambda: {"A": ("u", "a")}, {}, stop, on_frame=lambda *a: None,
                                 meter=meter, connect=connect, alert_fn=page)
    asyncio.run(asyncio.wait_for(main(), 10))
    saved = json.loads(path.read_text())
    assert saved["day"] == "2026-10-17" and saved["credits"] == 600 and saved["paged_day"] == "2026-10-17"
    assert len(pages) == 1


def test_a_real_dflow_pump_amm_v1_frame_fires_through_the_sink_and_the_registered_amm_handler(knobs, monkeypatch):
    """Alpha 2026-10-17: reachability pinned from BYTES, not assumed. A real armed-wallet DFlow -> pump_amm
    v1 transaction (Beta's fixture, its wire bytes) goes through FrameSink -> preparation_target ->
    preparation_candidate -> the shared buyer, where the curve handler passes it on and the
    DflowAmmHandler matches, reads (one getMultipleAccounts), binds and fires."""
    from sniper.services import memecoin_rabbit_dflow_amm as dfa
    from test_memecoin_rabbit_dflow_amm import FIX as AMM_FIX, state_for
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", True)
    name, rec = sorted(FX["dflow_pump_amm_v1"].items())[0]
    tx = AMM_FIX["pure"][name]
    raw = _raw(rec)
    owner = tx["transaction"]["message"]["accountKeys"][1]
    reads = []
    holder = {}

    def read_fn(method, params):
        reads.append(method)
        keys = params[0]
        accounts = state_for(tx, holder["plan"], read_slot=rec["slot"] - 1)["accounts"]
        return {"context": {"slot": rec["slot"] - 1}, "value": [accounts.get(key) for key in keys]}
    buyer, stats, fired, _rabbit = _shared_buyer(fire_rpc())
    buyer.owners_fn = lambda: {owner: ("uid", "h")}
    amm = dfa.DflowAmmHandler(read_fn=read_fn, alert_fn=lambda *a: None)
    original_match = amm.match

    def match(candidate, update=None):
        answer = original_match(candidate, update)
        if answer and answer[0] is not None:
            holder["plan"] = answer[0]
        return answer
    amm.match = match
    buyer.handlers.append(amm)
    sink, status, frames, _arr = _sink(buyer, owners={owner: ("uid", "h")})

    async def main():
        sink(rec["slot"], P.STATUS_SUCCESS, raw, time.monotonic(), 2)
        await _drain(buyer)
    asyncio.run(main())
    assert status["candidates"] == 1 and status["offered"] == 1, (status, frames.notes)
    assert reads == ["getMultipleAccounts"] and len(fired) == 1, stats
    buy = fired[0]
    assert buy.venue == "pump_amm" and buy.signal_feed == FEED and buy.rabbit["route"] == dfa.ROUTE
    proof = buy.source_initiation
    assert proof["feed"] == FEED and proof["signature_basis"] == "ed25519_local" and proof["route"] == dfa.ROUTE
    assert stats["by_feed"][FEED]["fired"] == 1


def test_the_next_frame_is_read_while_a_write_is_in_flight_and_the_latest_snapshot_lands(tmp_path):
    """Gamma MED-2: the receive loop never awaits the (fsynced) credit write. A write is held open; the
    next frame must still be read, and after it the latest snapshot lands (an awaited write in the
    receive loop would hold frame 101 behind the gate and time out)."""
    rec = FX["fire_direct"]
    raw = _raw(rec)
    path = tmp_path / "credits.json"
    meter = P.CreditMeter(50000, now_fn=_Clock(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)), path=path)
    gate, saved, stop, order = threading.Event(), [], threading.Event(), []
    real_save = meter.save

    def slow_save(snap):
        if not saved:
            assert gate.wait(5.0), "the first write was never released"
        saved.append(snap["credits"])
        real_save(snap)
    meter.save = slow_save

    def on_frame(slot, state, tx, mono, generation):
        order.append((slot, gate.is_set(), list(saved)))
        if slot == 101:
            gate.set()                         # frame 101 was read while frame 100's write was held open
            stop.set()

    def connect(url):
        return _WS([json.dumps({"jsonrpc": "2.0", "id": 1, "result": 1}), _frame(100, raw), _frame(101, raw)], [])

    async def main():
        await P.run_preconf_feed("wss://x", lambda: {"A": ("u", "a")}, {}, stop, on_frame=on_frame, meter=meter,
                                 connect=connect, alert_fn=lambda *a: None)
    asyncio.run(asyncio.wait_for(main(), 10))
    assert order == [(100, False, []), (101, False, [])]      # 101 read with the first write still open
    assert saved == [300, 600] and json.loads(path.read_text())["credits"] == 600


def test_only_one_credit_write_is_ever_in_flight(tmp_path):
    """Gamma (88fd58ff4, not blocking): a kick while a write is in flight starts nothing; the bill stays
    dirty and the next kick writes the latest snapshot. Two concurrent writes could land out of order."""
    meter = P.CreditMeter(50000, now_fn=_Clock(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)),
                          path=tmp_path / "c.json")
    gate, lock, live, saved = threading.Event(), threading.Lock(), {"now": 0, "max": 0}, []

    def slow_save(snap):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        gate.wait(5.0)
        saved.append(snap["credits"])
        with lock:
            live["now"] -= 1
    meter.save = slow_save
    writer = {}

    async def main():
        meter.note(1)
        P._kick(meter, writer)
        first = writer["task"]
        await asyncio.sleep(0.05)               # the first write is running, held on the gate
        meter.note(2)
        P._kick(meter, writer)                  # in flight: starts nothing
        assert writer["task"] is first and meter.dirty
        gate.set()
        await first
        P._kick(meter, writer)                  # now the latest snapshot goes
        await writer["task"]
    asyncio.run(asyncio.wait_for(main(), 10))
    assert live["max"] == 1 and saved == [300, 600]


def test_the_breaker_knobs_resolve_per_feed_and_fall_back_to_rabbits_by_name(monkeypatch):
    """Gamma LOW on bf10344c5: with the preconf breaker knobs unset each one falls back to ITS rabbit
    counterpart (N, WINDOW_MIN, PAUSE_MIN), never another; set, they are preconf's and rabbit's stay."""
    for name, value in (("MEMECOIN_SLOT_LANE_RABBIT_BREAKER_N", 3), ("MEMECOIN_SLOT_LANE_RABBIT_BREAKER_WINDOW_MIN", 15.0),
                        ("MEMECOIN_SLOT_LANE_RABBIT_BREAKER_PAUSE_MIN", 60.0), ("MEMECOIN_SLOT_LANE_PRECONF_BREAKER_N", None),
                        ("MEMECOIN_SLOT_LANE_PRECONF_BREAKER_WINDOW_MIN", None),
                        ("MEMECOIN_SLOT_LANE_PRECONF_BREAKER_PAUSE_MIN", None)):
        monkeypatch.setattr(cfg, name, value)
    assert R.RabbitBackstop._knobs("rabbit") == (3, 900.0, 3600.0)
    assert R.RabbitBackstop._knobs("preconf") == (3, 900.0, 3600.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF_BREAKER_N", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF_BREAKER_WINDOW_MIN", 7.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF_BREAKER_PAUSE_MIN", 11.0)
    assert R.RabbitBackstop._knobs("preconf") == (5, 420.0, 660.0)
    assert R.RabbitBackstop._knobs("rabbit") == (3, 900.0, 3600.0)


def test_both_sources_share_one_credit_file_writer(knobs, monkeypatch, tmp_path):
    """Gamma LOW on 156c80023: with a writer per socket the two sockets' credit writes overlap and
    can land out of order (the file up to ~2 bills stale). One meter, ONE writer: never two saves
    in flight. The first save is held until both sockets have billed."""
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services import memecoin_slot_signal as signal
    rec = FX["fire_direct"]
    stop, status, fired, sent = threading.Event(), {}, [], []
    rabbit = backstop(fire_rpc())
    in_flight, peak, release = [0], [0], threading.Event()
    lock = threading.Lock()

    def save(self, snap):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        release.wait(2.0)
        with lock:
            in_flight[0] -= 1
    monkeypatch.setattr(P.CreditMeter, "save", save)

    async def idle(*a, **k):
        while not stop.is_set():
            await asyncio.sleep(0.01)
    monkeypatch.setattr(prep, "run_prepare_feed", idle)
    monkeypatch.setattr(full, "run_feed", idle)
    ack = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 5})

    def connect(url):
        return _SourceWS({"helius": [ack, _frame(rec["slot"], _raw(rec))],
                          "bam": [ack, _frame(rec["slot"] + 1, _raw(rec))]}, sent)

    async def main():
        task = asyncio.ensure_future(signal._run_signals(
            fired.append, lambda: {FIRE_OWNER: ("uid", "h")}, None, status, stop, None, None, None, None,
            ("token", ("https://grpc.ams.shyft.to",)), infra_provider="shyft",
            prepare_config=("token", "https://rabbitstream.fra.shyft.to"), on_prepare=lambda c: None,
            rabbit=rabbit, preconf={"url": "wss://beta.helius-rpc.com/?api-key=K", "connect": connect,
                                    "alert_fn": lambda *a: None, "credit_path": tmp_path / "c.json",
                                    "sources": ("helius", "bam")}))
        for _ in range(200):
            credits = ((status.get("preconf_sources") or {}).get("helius") or {}).get("credits") or {}
            if credits.get("messages") == 2:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)                 # both sockets had their chance to start a write
        release.set()
        stop.set()
        await task
    asyncio.run(asyncio.wait_for(main(), 15))
    assert status["preconf_sources"]["helius"]["credits"]["messages"] == 2
    assert peak[0] == 1, "two credit-file writes were in flight at once"
