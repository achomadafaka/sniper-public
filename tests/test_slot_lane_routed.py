"""Routed buys on the slot lane's processed socket (operator 2026-09-27: "widen the slot lane to read
routed buys"). v1 reads USDC-paid routed buys only and hands every one to the ordinary on-chain lane.

Values come from REAL frames: tests/fixtures/memecoin/slot_routed_jpegkite_probe60.json holds the
probe's own record of jpegkite's token-account notifications and logs frames (four USDC-paid buys,
one sell, one fee-in), and tests/fixtures/memecoin/slot_routed_token_frames.json holds raw captured
programNotification / logsNotification / ack frames, including a pump DistributeFeeToHolders
HANDOUT. Where a socket test needs jpegkite's values in a frame, the frame is a captured one with
only the account, mint, owner, amount, slot and signature replaced by the probe's recorded values.
"""
import asyncio
import copy
import json
import threading
import time
from pathlib import Path

from solders.pubkey import Pubkey

from sniper.services import memecoin_slot_signal as s

FIX = Path(__file__).parent / "fixtures/memecoin"
PROBE = json.loads((FIX / "slot_routed_jpegkite_probe60.json").read_text())
CAPTURED = json.loads((FIX / "slot_routed_token_frames.json").read_text())
SIGNAL_FRAMES = json.loads((FIX / "slot_signal_frames.json").read_text())
JPEG = PROBE["owner"]
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL = "So11111111111111111111111111111111111111112"
COIN_ACCT = "HHagCSqcs9C5z4LFd4ohEJczN7VA5QVfqvUjFtxCYjfm"
USDC_ACCT = "5A5XGcHtT55omjJvA6ag971eJdkd9LwKtYfWYn7rMTnM"
COIN = "2iCpCQRKaFWjEgAgwUh2Bwvz9GyygAf2Wr8RXwKrnm6s"
BASE_SLOT = 450958004   # the probe's last reading before the first buy: coin 0, USDC 107,845.212562


def _baselined(pairer):
    pairer.watch(JPEG, "uid-jpeg", "jpegkite")
    pairer.set_baseline(JPEG, "token", BASE_SLOT, {USDC_ACCT: 107845212562})
    pairer.set_baseline(JPEG, "token2022", BASE_SLOT, {COIN_ACCT: 0})


def _feed(pairer, rows):
    out = []
    for i, r in enumerate(rows):
        kw = dict(recv_monotonic=100.0 + i * 0.001, recv_utc="2026-09-27T09:36:02+00:00")
        if r["kind"] == "logs":
            out += pairer.on_logs(JPEG, r["slot"], r["signature"], lane=False, **kw)
        else:
            out += pairer.on_token(JPEG, r["kind"], r["slot"], r["account"], r["mint"],
                                   int(r["amount"]), 6, **kw)
    return out


def _after_baseline(rows=None):
    return [r for r in (rows or PROBE["rows"]) if r["slot"] > BASE_SLOT]


# --- pairing on the probe's real jpegkite record --------------------------------------------------

def test_real_jpegkite_routed_buys_pair_with_exact_usdc_sizes():
    """Four USDC-paid buys pair; the fee-in (USDC up, no logs frame) and nothing else emit."""
    pairer = s.RoutedPairer()
    _baselined(pairer)
    got = _feed(pairer, _after_baseline())
    sigs = {r["slot"]: r["signature"] for r in PROBE["rows"] if r["kind"] == "logs"}
    want = PROBE["expected_buys"]
    assert [(b.slot, b.usdc_spent_atomic, b.token_delta_atomic) for b in got] == [
        (w["slot"], w["usdc_spent_atomic"], w["token_delta_atomic"]) for w in want]
    assert all(b.mint == COIN and b.owner == JPEG and b.source_user_id == "uid-jpeg"
               and b.handle == "jpegkite" and b.token_decimals == 6 for b in got)
    assert [b.signature for b in got] == [sigs[w["slot"]] for w in want]


def test_without_a_baseline_nothing_is_a_buy():
    """An unknown prior is unknown, never 0: the probe's first reading of each account is not a buy."""
    pairer = s.RoutedPairer()
    pairer.watch(JPEG, "uid-jpeg", "jpegkite")
    assert _feed(pairer, PROBE["rows"]) == []
    pairer.set_baseline(JPEG, "token", BASE_SLOT, {USDC_ACCT: 107845212562})   # one program only
    assert _feed(pairer, _after_baseline()) == []


def test_a_coin_rise_with_no_usdc_leg_is_not_a_buy():
    rows = [r for r in _after_baseline() if r["slot"] == 450962730 and r.get("mint") != USDC]
    pairer = s.RoutedPairer()
    _baselined(pairer)
    assert _feed(pairer, rows) == []


def test_two_successful_transactions_in_the_slot_refuse_the_pair():
    rows = [r for r in _after_baseline() if r["slot"] == 450962730]
    logs = [r for r in rows if r["kind"] == "logs"]
    extra = dict(logs[0], signature="2" * 87)
    pairer = s.RoutedPairer()
    _baselined(pairer)
    assert _feed(pairer, [extra] + logs + [r for r in rows if r["kind"] != "logs"]) == []
    assert pairer.stats["ambiguous"] == 1 and pairer.stats["emitted"] == 0


def test_a_second_transaction_after_the_pair_is_counted_not_hidden():
    """The pair emits on the first complete view; a later second signature in the same slot cannot
    be bound to the balances and is counted, so shadow can measure how often it happens."""
    rows = [r for r in _after_baseline() if r["slot"] == 450962730]
    pairer = s.RoutedPairer()
    _baselined(pairer)
    assert len(_feed(pairer, rows)) == 1
    late = dict([r for r in rows if r["kind"] == "logs"][0], signature="2" * 87)
    assert _feed(pairer, [late]) == []
    assert pairer.stats["ambiguous_after"] == 1 and pairer.stats["emitted"] == 1


def test_a_lane_decoded_buy_in_the_slot_blocks_the_routed_path():
    rows = [r for r in _after_baseline() if r["slot"] == 450962730 and r["kind"] != "logs"]
    pairer = s.RoutedPairer()
    _baselined(pairer)
    pairer.on_logs(JPEG, 450962730, "3" * 87, lane=True, recv_monotonic=1.0, recv_utc="x")
    assert _feed(pairer, rows) == []


def test_a_stablecoin_or_wsol_is_never_the_coin_and_a_wsol_leg_refuses():
    rows = [r for r in _after_baseline() if r["slot"] == 450962730]
    as_usdt = [dict(r, mint=USDT) if r.get("mint") == COIN else r for r in rows]
    pairer = s.RoutedPairer()
    _baselined(pairer)
    assert _feed(pairer, as_usdt) == []
    wsol_leg = dict(rows[-1], kind="token", account="W" * 43 + "1", mint=WSOL, amount="1")
    pairer = s.RoutedPairer()
    _baselined(pairer)
    pairer.set_baseline(JPEG, "token", BASE_SLOT, {USDC_ACCT: 107845212562, "W" * 43 + "1": 5})
    assert _feed(pairer, [wsol_leg] + rows) == []


def test_a_half_pair_expires_after_the_wait_and_cannot_complete_later():
    rows = [r for r in _after_baseline() if r["slot"] == 450962730]
    tokens = [r for r in rows if r["kind"] != "logs"]
    logs = [r for r in rows if r["kind"] == "logs"]
    pairer = s.RoutedPairer()
    _baselined(pairer)
    assert _feed(pairer, tokens) == []
    assert pairer.expire(100.0 + s.ROUTED_PAIR_WAIT_S + 0.01) == 1
    assert pairer.stats["expired"] == 1
    assert _feed(pairer, logs) == []


def test_a_notification_at_or_below_the_baseline_slot_is_already_in_the_baseline():
    pairer = s.RoutedPairer()
    _baselined(pairer)
    stale = [dict(r, slot=BASE_SLOT) for r in _after_baseline() if r["slot"] == 450962730]
    assert _feed(pairer, stale) == []
    # the baseline still holds the pre-buy amounts, so the real notifications pair exactly
    got = _feed(pairer, [r for r in _after_baseline() if r["slot"] == 450962730])
    assert [(b.usdc_spent_atomic, b.token_delta_atomic) for b in got] == [(2000000000, 4693689843245)]


def test_the_captured_handout_is_not_a_buy():
    """Slot 451040320: pump DistributeFeeToHolders paid Tekkerrss 0.603064 of a Token-2022 coin in
    a successful transaction that names the owner; there is no USDC leg."""
    owner = "7UBjMaYsE3tKcanfgGrtso6aHAMhVNSVHkkX5j3t7f5y"
    pairer = s.RoutedPairer()
    pairer.watch(owner, "uid-tek", "Tekkerrss")
    pairer.set_baseline(owner, "token", 451040299, {"3KtvWij1yDU1W1XcRPqF1cimarKFK1rBjT1zpvUxQixY": 338409524767})
    pairer.set_baseline(owner, "token2022", 451040299, {})
    got = []
    for item in CAPTURED["frames"]:
        frame = item["frame"]
        if frame.get("method") == "programNotification":
            n = s.parse_token_notification(frame)
            kind = "token2022" if n["program"] == s.TOKEN_2022_PROGRAM else "token"
            got += pairer.on_token(owner, kind, n["slot"], n["pubkey"], n["mint"], n["amount"],
                                   n["decimals"], recv_monotonic=item["recv"], recv_utc="x")
        elif frame.get("method") == "logsNotification":
            v = frame["params"]["result"]
            got += pairer.on_logs(owner, v["context"]["slot"], v["value"]["signature"], lane=False,
                                  recv_monotonic=item["recv"], recv_utc="x")
    assert got == []


def test_parse_reads_the_captured_token_and_token_2022_frames():
    notes = [s.parse_token_notification(i["frame"]) for i in CAPTURED["frames"]
             if i["frame"].get("method") == "programNotification"]
    assert notes[0] == {"slot": 451040300, "pubkey": "3KtvWij1yDU1W1XcRPqF1cimarKFK1rBjT1zpvUxQixY",
                        "mint": USDC, "owner": "7UBjMaYsE3tKcanfgGrtso6aHAMhVNSVHkkX5j3t7f5y",
                        "amount": 338409524767, "decimals": 6, "program": s.TOKEN_PROGRAM}
    t22 = [n for n in notes if n["program"] == s.TOKEN_2022_PROGRAM]
    assert t22 == [{"slot": 451040320, "pubkey": "7pSJZrSyCoztaJAXYQ6Z4YuoXFsMk7tbcxWRQxCs17aa",
                    "mint": "SKHYhSjuRWHgikq8eRKbtBbpABgJSkd7ytQV14i9EQ3",
                    "owner": "7UBjMaYsE3tKcanfgGrtso6aHAMhVNSVHkkX5j3t7f5y",
                    "amount": 603064, "decimals": 6, "program": s.TOKEN_2022_PROGRAM}]
    assert s.parse_token_notification({"method": "programNotification", "params": {"result": {
        "context": {"slot": 1}, "value": {"pubkey": "x", "account": {"data": ["", "base64"]}}}}}) is None


# --- the socket loop ------------------------------------------------------------------------------

def _captured_frame(method, kind):
    for item in CAPTURED["frames"]:
        f = item["frame"]
        if f.get("method") != method:
            continue
        if method == "logsNotification":
            return copy.deepcopy(f)
        prog = f["params"]["result"]["value"]["account"]["owner"]
        if (prog == s.TOKEN_2022_PROGRAM) == (kind == "token2022"):
            return copy.deepcopy(f)
    raise AssertionError(method)


def _jpeg_frame(row):
    """A captured frame of the row's kind carrying the probe's recorded values for that row."""
    if row["kind"] == "logs":
        f = _captured_frame("logsNotification", "logs")
        f["params"]["result"]["context"]["slot"] = row["slot"]
        f["params"]["result"]["value"]["signature"] = row["signature"]
        return f
    f = _captured_frame("programNotification", row["kind"])
    f["params"]["result"]["context"]["slot"] = row["slot"]
    v = f["params"]["result"]["value"]
    v["pubkey"] = row["account"]
    info = v["account"]["data"]["parsed"]["info"]
    info.update(mint=row["mint"], owner=JPEG)
    info["tokenAmount"].update(amount=row["amount"], decimals=6)
    return f


class _RoutedWS:
    """Acks slot/logs subscriptions; acks or refuses programSubscribe; then replays `frames`
    [(owner, kind, frame)] with their subscription ids set, once every request is answered."""

    def __init__(self, stop, frames, refuse_program=False):
        self.sent, self.out, self._stop = [], asyncio.Queue(), stop
        self.frames, self.refuse, self.sids, self.done = frames, refuse_program, {}, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        method = msg["method"]
        if method == "slotSubscribe":
            await self.out.put({"jsonrpc": "2.0", "id": msg["id"], "result": 7})
        elif method == "logsSubscribe":
            sid = 5000 + msg["id"]
            self.sids[(msg["params"][0]["mentions"][0], "logs")] = sid
            await self.out.put({"jsonrpc": "2.0", "id": msg["id"], "result": sid})
        elif method == "programSubscribe":
            if self.refuse:
                await self.out.put({"jsonrpc": "2.0", "id": msg["id"],
                                    "error": {"code": -32601, "message": "Method not found"}})
                return
            prog, cfg = msg["params"]
            owner = [f["memcmp"]["bytes"] for f in cfg["filters"] if "memcmp" in f][0]
            kind = "token2022" if prog == s.TOKEN_2022_PROGRAM else "token"
            sid = 6000 + msg["id"]
            self.sids[(owner, kind)] = sid
            await self.out.put({"jsonrpc": "2.0", "id": msg["id"], "result": sid})

    async def recv(self):
        if self.out.empty() and not self.done and self.sids:
            self.done = True
            await asyncio.sleep(0.2)          # let the baseline reads land first
            for owner, kind, frame in self.frames:
                frame = copy.deepcopy(frame)
                frame["params"]["subscription"] = self.sids.get((owner, kind), -1)
                await self.out.put(frame)
        if self.out.empty():
            self._stop.set()
            await asyncio.sleep(0.05)
            raise asyncio.TimeoutError
        return json.dumps(await self.out.get())


def _run_socket(monkeypatch, frames, *, owners, baseline_fn, refuse_program=False, on_buy=None):
    monkeypatch.setattr(s, "ROSTER_REFRESH_S", 30.0)
    routed, buys, status, stop = [], [], {}, threading.Event()
    ws = _RoutedWS(stop, frames, refuse_program)
    thread = s.start_signal_thread(on_buy or buys.append, status=status, ws_url="wss://x",
                                   stop=stop, connect=lambda url: ws, owners_fn=lambda: owners,
                                   on_routed=routed.append, baseline_fn=baseline_fn)
    thread.join(timeout=15)
    assert not thread.is_alive()
    return routed, buys, status, ws


def test_socket_pairs_jpegkite_buys_end_to_end(monkeypatch):
    calls = []

    def baseline(owner):
        calls.append(owner)
        return {"token": (BASE_SLOT, {USDC_ACCT: 107845212562}), "token2022": (BASE_SLOT, {COIN_ACCT: 0})}
    frames = [(JPEG, r["kind"], _jpeg_frame(r)) for r in _after_baseline()]
    routed, buys, status, ws = _run_socket(monkeypatch, frames, owners={JPEG: ("uid-jpeg", "jpegkite")},
                                           baseline_fn=baseline)
    assert calls == [JPEG]
    assert [(b.slot, b.usdc_spent_atomic) for b in routed] == [
        (w["slot"], w["usdc_spent_atomic"]) for w in PROBE["expected_buys"]]
    assert buys == []
    r = status["routed"]
    assert (r["subs_ok"], r["subs_refused"], r["ready"], r["emitted"]) == (2, 0, 1, 4)
    subs = [m for m in ws.sent if m["method"] == "programSubscribe"]
    assert sorted(m["params"][0] for m in subs) == sorted([s.TOKEN_PROGRAM, s.TOKEN_2022_PROGRAM])
    for m in subs:
        assert m["params"][1]["commitment"] == "processed" and m["params"][1]["encoding"] == "jsonParsed"
        assert {"memcmp": {"offset": 32, "bytes": JPEG}} in m["params"][1]["filters"]


def test_a_refused_program_subscription_leaves_the_logs_lane_intact(monkeypatch):
    """Both routed subscriptions refused: the owner's lane buy still arrives on its logs subscription."""
    buyer = "2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf"
    calls = []
    routed, buys, status, ws = _run_socket(
        monkeypatch, [(buyer, "logs", copy.deepcopy(SIGNAL_FRAMES["curve_buy"]))],
        owners={buyer: ("uid-buyer", "buyer")}, baseline_fn=lambda o: calls.append(o),
        refuse_program=True)
    assert [b.owner for b in buys] == [buyer] and routed == []
    assert status["subscriptions"] == 1 and status["reconnects"] == 0
    assert status["routed"]["subs_ok"] == 0 and status["routed"]["subs_refused"] == 2
    assert status["routed"]["ready"] == 0 and calls == []
    assert not any(m["method"] in ("logsUnsubscribe", "programUnsubscribe") for m in ws.sent)


def test_a_lane_buy_frame_dispatches_first_and_never_pairs_as_routed(monkeypatch):
    """The captured curve buy, with the buyer's coin up and USDC down in its slot, stays a lane buy."""
    buyer = "2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf"
    frame = copy.deepcopy(SIGNAL_FRAMES["curve_buy"])
    slot = frame["params"]["result"]["context"]["slot"]
    usdc = _captured_frame("programNotification", "token")
    usdc["params"]["result"]["context"]["slot"] = slot
    usdc["params"]["result"]["value"]["account"]["data"]["parsed"]["info"].update(owner=buyer)
    usdc["params"]["result"]["value"]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"] = "1"
    coin = _captured_frame("programNotification", "token2022")
    coin["params"]["result"]["context"]["slot"] = slot
    coin["params"]["result"]["value"]["account"]["data"]["parsed"]["info"].update(owner=buyer)
    order = []

    def baseline(owner):
        return {"token": (slot - 1, {usdc["params"]["result"]["value"]["pubkey"]: 5}),
                "token2022": (slot - 1, {})}
    routed, buys, status, ws = _run_socket(
        monkeypatch, [(buyer, "token", usdc), (buyer, "token2022", coin), (buyer, "logs", frame)],
        owners={buyer: ("uid-buyer", "buyer")}, baseline_fn=baseline,
        on_buy=lambda b: order.append(("buy", b.signature)))
    assert order == [("buy", frame["params"]["result"]["value"]["signature"])]
    assert routed == []


# --- the lane side --------------------------------------------------------------------------------

def _rb(**kw):
    base = dict(source_user_id="uid-jpeg", handle="jpegkite", owner=JPEG, slot=450962730,
                signature="3ufQ7KDHyR72vyZoftBRfpPbjirFiLjn2TuQLXkPqPm9UtePd7gaMQKNqrov61XmEerZaDPBzPc5TFyoP1LpQKAJ",
                mint=COIN, token_delta_atomic=4693689843245, token_decimals=6,
                usdc_spent_atomic=2000000000, recv_monotonic=10.0,
                recv_utc="2026-09-27T09:36:02.532000+00:00", paired_monotonic=10.002)
    base.update(kw)
    return s.RoutedBuy(**base)


def test_routed_mode_defaults_to_shadow_and_an_unknown_value_is_off(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_SLOT_LANE_ROUTED == "shadow" or "MEMECOIN_SLOT_LANE_ROUTED" in __import__("os").environ
    for value, mode in (("shadow", "shadow"), ("LIVE", "live"), ("off", "off"), ("yes", "off"), ("", "off")):
        monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_ROUTED", value)
        assert lane.routed_mode() == mode


def test_the_shadow_line_carries_the_receipt_stamp_and_signature():
    from sniper.services import memecoin_slot_lane as lane
    line = lane.routed_log_line(_rb(), "shadow")
    assert line.startswith("[memecoin] slot lane routed shadow jpegkite 2iCpCQRK S=450962730 ")
    assert "sig=" + _rb().signature in line and "recv=2026-09-27T09:36:02.532000+00:00" in line
    assert "usdc=2000.00" in line and "pair_ms=2.0" in line


def test_unproved_live_handoff_waits_for_the_confirmed_receipt(tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_slot_lane as lane
    db = str(tmp_path / "l.db")

    def factory():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c
    conn = factory()
    m.ensure_memecoin_tables(conn)
    wake = threading.Event()
    handoff = lane.RoutedHandoff(conn_factory=factory, wake=wake)
    assert handoff.write(_rb()) == "awaiting_signer_proof" and not wake.is_set()
    assert conn.execute("SELECT COUNT(*) FROM onchain_wallet_detections").fetchone()[0] == 0
    assert handoff.write(_rb()) == "awaiting_signer_proof"


def test_the_onchain_lane_keeps_its_cadence_unless_woken():
    from sniper.services import memecoin_onchain_lane as ol
    from sniper.settings import memecoin as cfg
    wake = threading.Event()
    t0 = time.monotonic()
    assert ol.wait_for_next_sweep(wake, 0.3) is False
    assert time.monotonic() - t0 >= 0.29
    wake.set()
    t0 = time.monotonic()
    assert ol.wait_for_next_sweep(wake, 5.0) is True and time.monotonic() - t0 < 0.1
    assert not wake.is_set()
    assert ol.wait_for_next_sweep(None, 0.0) is False       # floor 0.25 s, no event: plain sleep
    assert cfg.MEMECOIN_ONCHAIN_LANE_POLL_S == 1.0 or "MEMECOIN_ONCHAIN_LANE_POLL_S" in __import__("os").environ
    runner = (Path(__file__).parents[1] / "scripts/memecoin_runner_loop.py").read_text()
    assert "wait_for_next_sweep(_onchain_wake, MEMECOIN_ONCHAIN_LANE_POLL_S)" in runner
    assert "time.sleep(max(0.25, MEMECOIN_ONCHAIN_LANE_POLL_S))" not in runner


def test_the_heartbeat_names_the_routed_counters(tmp_path):
    from sniper.services import memecoin_runner as r
    path = tmp_path / "hb.json"
    status = {"mode": "live", "signal": {"routed": {"mode": "shadow", "subs_ok": 84, "subs_refused": 0,
                                                     "ready": 42, "emitted": 3, "ambiguous": 1,
                                                     "ambiguous_after": 2, "expired": 5,
                                                     "baseline_failed": 0, "last_error": None,
                                                     "secret": "no"}}}
    r.write_heartbeat({}, True, path=str(path), slot_lane_status=status)
    got = json.loads(path.read_text())["slot_lane"]["routed"]
    assert got == {"mode": "shadow", "subs_ok": 84, "subs_refused": 0, "ready": 42, "emitted": 3,
                   "ambiguous": 1, "ambiguous_after": 2, "expired": 5, "baseline_failed": 0,
                   "last_error": None}


def _lane_with(monkeypatch, *, mode, onchain_enabled=True, baseline=lambda o: {}, wake=None):
    from sniper.services import memecoin_slot_lane as lane
    from sniper.settings import memecoin as cfg
    started, got = [], {}

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.name = name

        def start(self):
            started.append(self.name)

        def is_alive(self):
            return False
    monkeypatch.setattr("threading.Thread", Thread)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_ROUTED", mode)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_LANE_ENABLED", onchain_enabled)

    def signal_start(on_buy, **kw):
        got.update(kw)
        if "on_routed" in kw:
            kw["status"]["routed"] = {}          # start_signal_thread creates it
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    status = lane.start_slot_lane(rc, builder=object(), signal_start_fn=signal_start,
                                  sol_usd_fn=lambda: None, log=lambda _: None, roster_path="unused",
                                  conn_factory=object, readonly_conn_factory=object,
                                  routed_baseline_fn=baseline, onchain_wake=wake)
    return status, got, started


def test_the_lane_wires_routed_only_with_a_baseline_and_live_needs_the_onchain_lane(monkeypatch):
    status, got, started = _lane_with(monkeypatch, mode="shadow", baseline=None)
    assert "on_routed" not in got and "slot-lane-routed" not in started
    status, got, started = _lane_with(monkeypatch, mode="off")
    assert "on_routed" not in got and "slot-lane-routed" not in started
    status, got, started = _lane_with(monkeypatch, mode="shadow")
    assert callable(got["on_routed"]) and callable(got["baseline_fn"])
    assert started == ["slot-lane-routed", "slot-lane-prefetch-sol_usd",
                       "slot-lane-prefetch-balances", "slot-lane-prefetch-blockhash",
                       "slot-lane-prefetch", "slot-lane"]
    assert status["signal"]["routed"]["mode"] == "shadow"
    status, got, started = _lane_with(monkeypatch, mode="live", wake=None)
    assert status["signal"]["routed"]["mode"] == "shadow"          # no wake: nothing reads a row
    status, got, started = _lane_with(monkeypatch, mode="live", wake=threading.Event(),
                                      onchain_enabled=False)
    assert status["signal"]["routed"]["mode"] == "shadow"          # the on-chain lane is off
    status, got, started = _lane_with(monkeypatch, mode="live", wake=threading.Event())
    assert status["signal"]["routed"]["mode"] == "live"


def test_handle_routed_logs_shadow_and_writes_live():
    from sniper.services import memecoin_slot_lane as lane
    lines = []
    lane.handle_routed(_rb(), None, lines.append)
    assert lines == [lane.routed_log_line(_rb(), "shadow")]

    class Handoff:
        def write(self, buy):
            return "inserted"
    lane.handle_routed(_rb(), Handoff(), lines.append)
    assert lines[-1] == lane.routed_log_line(_rb(), "live") + " -> inserted"

    class Broken:
        def write(self, buy):
            raise RuntimeError("db gone")
    lane.handle_routed(_rb(), Broken(), lines.append)      # logged, never raised into the thread
    assert len(lines) == 2


def test_the_baseline_reader_parses_both_programs_and_an_error_is_never_empty(monkeypatch):
    import io
    import urllib.request
    from sniper.services import memecoin_slot_lane as lane
    notes = {}
    for item in CAPTURED["frames"]:
        f = item["frame"]
        if f.get("method") == "programNotification":
            v = f["params"]["result"]["value"]
            notes.setdefault(v["account"]["owner"], {"pubkey": v["pubkey"], "account": v["account"]})
    replies = []

    def urlopen(req, timeout):
        program = json.loads(req.data)["params"][1]["programId"]
        assert json.loads(req.data)["params"][2] == {"encoding": "jsonParsed", "commitment": "processed"}
        assert req.get_header("User-agent") == "curl/8.5.0"
        body = replies.pop(0) if replies else {"jsonrpc": "2.0", "id": 1, "result": {
            "context": {"slot": 451040300}, "value": [notes[program]]}}
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    got = lane.token_accounts_baseline("https://read.example")("7UBjMaYsE3tKcanfgGrtso6aHAMhVNSVHkkX5j3t7f5y")
    assert got == {"token": (451040300, {"3KtvWij1yDU1W1XcRPqF1cimarKFK1rBjT1zpvUxQixY": 338409524767}),
                   "token2022": (451040300, {"7pSJZrSyCoztaJAXYQ6Z4YuoXFsMk7tbcxWRQxCs17aa": 603064})}
    for bad in ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "busy"}},
                {"jsonrpc": "2.0", "id": 1, "result": None}):
        replies[:] = [bad]
        try:
            lane.token_accounts_baseline("https://read.example")("x")
        except ValueError:
            continue
        raise AssertionError("an unread baseline read as empty: %r" % (bad,))


def test_notifications_before_the_baseline_count_only_when_newer_than_the_read():
    rows = _after_baseline()
    buy1 = [r for r in rows if r["slot"] == 450962730]
    pairer = s.RoutedPairer()
    pairer.watch(JPEG, "uid-jpeg", "jpegkite")
    assert _feed(pairer, [r for r in buy1 if r["kind"] != "logs"]) == []     # held, not judged
    # A read OLDER than those notifications: they are the accounts' latest amounts.
    pairer.set_baseline(JPEG, "token", 450962000, {USDC_ACCT: 107845212562})
    pairer.set_baseline(JPEG, "token2022", 450962000, {COIN_ACCT: 0})
    assert _feed(pairer, [r for r in buy1 if r["kind"] == "logs"]) == []
    got = _feed(pairer, [r for r in rows if r["slot"] == 450962792])
    assert [(b.usdc_spent_atomic, b.token_delta_atomic) for b in got] == [(2000000000, 4287823509845)]
    # A read NEWER than the held notifications already holds them, and more (the 450962894 fee-in):
    # re-applying the older held amounts would put USDC back 0.38 below the truth.
    pairer = s.RoutedPairer()
    pairer.watch(JPEG, "uid-jpeg", "jpegkite")
    _feed(pairer, [r for r in rows if r["slot"] in (450962730, 450962792) and r["kind"] != "logs"])
    pairer.set_baseline(JPEG, "token", 450962900, {USDC_ACCT: 103845592562})
    pairer.set_baseline(JPEG, "token2022", 450962900, {COIN_ACCT: 8981513353090})
    got = _feed(pairer, [r for r in rows if r["slot"] == 450962920])
    assert [(b.usdc_spent_atomic, b.token_delta_atomic) for b in got] == [(500000000, 911539953980)]


def test_a_read_issued_before_an_overflow_is_refused_on_arrival(monkeypatch):
    """Held notifications that overflow are dropped, so a baseline read issued before the drop could
    carry a stale prior; it must not make the owner ready."""
    monkeypatch.setattr(s, "ROUTED_EARLY_CAP", 2)
    monkeypatch.setattr(s, "ROUTED_BASELINE_SPACING_S", 0.0)
    pairer = s.RoutedPairer()
    pairer.watch(JPEG, "uid-jpeg", "jpegkite")
    held = [r for r in _after_baseline() if r["kind"] != "logs"]
    routed = {"baseline_failed": 0, "last_error": None, "ready": 0}

    def baseline(owner):
        _feed(pairer, held[:3])                  # three notifications arrive during the read
        return {"token": (BASE_SLOT, {USDC_ACCT: 107845212562}), "token2022": (BASE_SLOT, {COIN_ACCT: 0})}

    async def go():
        await s._routed_baseline(JPEG, pairer, baseline, asyncio.Semaphore(1), routed,
                                 lambda: {JPEG: None}, {})
    asyncio.run(go())
    assert not pairer.ready(JPEG) and pairer.generation(JPEG) == 1 and routed["ready"] == 0
    # a read issued after the overflow is applied
    asyncio.run(s._routed_baseline(JPEG, pairer, lambda o: {
        "token": (BASE_SLOT, {USDC_ACCT: 107845212562}), "token2022": (BASE_SLOT, {COIN_ACCT: 0})},
        asyncio.Semaphore(1), routed, lambda: {JPEG: None}, {}))
    assert pairer.ready(JPEG) and routed["ready"] == 1


def test_a_failing_baseline_backs_off_doubling_to_a_cap():
    """A read that keeps failing (an owner with too many accounts, a refused method) must not
    re-read every roster pass: 30 s, then doubling, capped at 30 min."""
    failed = {}
    assert s._baseline_retry_due(failed, "o", 0.0)                 # never failed: due
    for n, now in enumerate((100.0, 200.0, 400.0), start=1):
        s._note_baseline_failure(failed, "o", now)
        wait = min(s.ROUTED_BASELINE_RETRY_S * 2 ** (n - 1), s.ROUTED_BASELINE_RETRY_MAX_S)
        assert not s._baseline_retry_due(failed, "o", now + wait - 0.001)
        assert s._baseline_retry_due(failed, "o", now + wait)
    for _ in range(20):
        s._note_baseline_failure(failed, "o", 1000.0)
    assert s._baseline_retry_due(failed, "o", 1000.0 + s.ROUTED_BASELINE_RETRY_MAX_S)
    failed.pop("o")                                                  # a success clears it
    assert s._baseline_retry_due(failed, "o", 0.0)


def test_a_failed_read_is_counted_and_a_later_success_clears_the_backoff(monkeypatch):
    monkeypatch.setattr(s, "ROUTED_BASELINE_SPACING_S", 0.0)
    pairer = s.RoutedPairer()
    pairer.watch(JPEG, "uid-jpeg", "jpegkite")
    routed, failed = {"baseline_failed": 0, "last_error": None, "ready": 0}, {}

    def refused(owner):
        raise ValueError("getTokenAccountsByOwner unread")
    asyncio.run(s._routed_baseline(JPEG, pairer, refused, asyncio.Semaphore(1), routed,
                                   lambda: {JPEG: None}, failed))
    assert failed[JPEG][1] == 1 and routed["baseline_failed"] == 1
    assert routed["last_error"] == "baseline: ValueError" and not pairer.ready(JPEG)
    asyncio.run(s._routed_baseline(JPEG, pairer, lambda o: {
        "token": (BASE_SLOT, {USDC_ACCT: 107845212562}), "token2022": (BASE_SLOT, {COIN_ACCT: 0})},
        asyncio.Semaphore(1), routed, lambda: {JPEG: None}, failed))
    assert JPEG not in failed and pairer.ready(JPEG) and routed["ready"] == 1


def test_unproved_pair_keeps_the_head_without_displacing_a_confirmed_detection(tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_slot_lane as lane
    path = tmp_path / 'routed.db'
    conn = sqlite3.connect(path)
    m.ensure_memecoin_tables(conn)
    handoff = lane.RoutedHandoff(conn_factory=lambda: sqlite3.connect(path), wake=threading.Event())
    paired=[]
    s._emit_routed([_rb()], lambda buy: paired.append(buy) or handoff.write(buy), head_slot=450962731)
    assert paired[0].paired_head_slot == 450962731
    assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0] == 0
