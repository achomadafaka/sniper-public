"""4663 bridge fresh read (Alpha GO 2026-10-16).

Since 10-14, 10 final 4663 refusals were bridge_state_stale: the coin's pool was current, but the
native/X bridge pool (leg A) had not swapped for a median 182 blocks. When that is the only stale
state and the tape is current, the decide reads leg A's slot0 + liquidity ONCE, at the signal's
block (or the tape head if newer), from the tx_read host, bounded at 200 ms. No answer keeps
today's refusal."""
import sqlite3
import time

from sniper.execution.evm_v4_multihop import _EXTSLOAD_BATCH, _liquidity_slot
from sniper.services import memecoin_rh_fast_decide as fast
from test_memecoin_rh_fast_decide import B, CTX, SIG
from test_memecoin_rh_fast_routes import A_SQRT, APOOL, B_LIQ, B_SQRT, BPOOL, COIN, USDG, _brow, _btape

PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
SRC_IN_X, SRC_OUT = 10 ** 8, 899_000_000
BRIDGE_SIG = dict(SIG, mint=COIN, payment_token=USDG, paid=-(-SRC_IN_X * 100 // 99))
STALE_A = _brow(APOOL, B - 20, "0xa", A_SQRT * 2)              # 20 blocks old: today bridge_state_stale
SOURCE_B = _brow(BPOOL, B, "0xsrc", B_SQRT + 1000, liq=B_LIQ, a0=SRC_IN_X, a1=-SRC_OUT)


class Reader:
    def __init__(self, answer=("ok", A_SQRT, 10 ** 22)):
        self.calls, self.answer = [], answer

    def __call__(self, pool, block):
        self.calls.append((pool, block))
        outcome, sqrt, liq = self.answer
        row = None if outcome != "ok" else dict(pool=pool, block_no=block, log_index=-1, tx_sig=None,
                                                sqrt=sqrt, liquidity=liq, learned=True)
        return row, outcome, 41.0


def _decide(tape, reader, sig=None, **kw):
    return fast.decide(dict(sig or BRIDGE_SIG), tape=tape, **dict(CTX, fresh_read=reader, **kw))


def test_a_stale_bridge_leg_is_read_once_and_the_decision_sends_on_the_fresh_state():
    reader = Reader()
    rec = _decide(_btape(STALE_A, SOURCE_B), reader)
    assert reader.calls == [(APOOL, B)]                       # tape head B-1: the signal's block
    assert rec["reason"] == "would_send" and rec["route_kind"] == "v4_bridge"
    assert (rec["a_state_kind"], rec["a_state_age_blocks"], rec["a_fresh_read"]) == ("fresh_read", 0, "ok")
    assert rec["a_fresh_read_ms"] == 41.0
    clip_wei = rec["clip_wei"]
    assert rec["x_mid"] == clip_wei * A_SQRT ** 2 >> 192 and rec["a_sqrt"] == A_SQRT   # fresh, not the stale 2x


def test_the_read_block_is_the_tape_head_when_it_is_past_the_signal():
    reader = Reader()
    tape = _btape(STALE_A, SOURCE_B)
    tape.head = B + 2
    rec = _decide(tape, reader)
    assert reader.calls == [(APOOL, B + 2)] and rec["a_state_age_blocks"] == -2


def test_no_answer_keeps_todays_refusal_and_names_why():
    for outcome in ("timeout", "http_429", "malformed", "empty_state", "rpc_error", "cooldown"):
        reader = Reader((outcome, None, None))
        rec = _decide(_btape(STALE_A, SOURCE_B), reader)
        assert rec["reason"] == "bridge_state_stale" and rec["a_fresh_read"] == outcome
        assert rec["a_state_kind"] == "pre" and reader.calls == [(APOOL, B)]


def test_a_current_bridge_leg_makes_no_read():
    reader = Reader()
    rec = _decide(_btape(_brow(APOOL, B - 2, "0xa", A_SQRT), SOURCE_B), reader)
    assert rec["reason"] == "would_send" and rec["a_state_kind"] == "pre" and reader.calls == []
    assert "a_fresh_read" not in rec


def test_knob_off_is_todays_refusal():
    rec = _decide(_btape(STALE_A, SOURCE_B), None)
    assert rec["reason"] == "bridge_state_stale" and "a_fresh_read" not in rec


def test_one_read_per_signal_a_reask_does_not_read_again():
    reader, sig = Reader(("timeout", None, None)), dict(BRIDGE_SIG)
    tape = _btape(STALE_A, SOURCE_B)
    assert fast.decide(sig, tape=tape, **dict(CTX, fresh_read=reader))["reason"] == "bridge_state_stale"
    assert fast.decide(sig, tape=tape, **dict(CTX, fresh_read=reader))["reason"] == "bridge_state_stale"
    assert len(reader.calls) == 1


def test_a_stale_coin_pool_or_a_lagging_tape_makes_no_read():
    """The read buys nothing when the coin pool's own check fails, or when the tape is behind."""
    reader = Reader()
    sig = dict(BRIDGE_SIG, tx_hash="0xnot-on-tape")           # estimated path: the coin pool is "pre"
    rec = _decide(_btape(STALE_A, _brow(BPOOL, B - 20, "0xb", B_SQRT, liq=B_LIQ)), reader, sig=sig)
    assert rec["reason"] == "bridge_state_stale" and reader.calls == []
    rec = _decide(_btape(_brow(APOOL, B - 2, "0xa", A_SQRT), _brow(BPOOL, B - 20, "0xb", B_SQRT, liq=B_LIQ)),
                  reader, sig=sig)
    assert rec["reason"] == "state_stale" and reader.calls == []      # coin pool stale: unchanged, no read
    tape = _btape(STALE_A, SOURCE_B)
    tape.head = B - 30
    assert _decide(tape, reader)["reason"] == "bridge_tape_behind" and reader.calls == []
    # the same coin-pool-stale signal with a current coin pool does read
    rec = _decide(_btape(STALE_A, _brow(BPOOL, B - 2, "0xb", B_SQRT, liq=B_LIQ)), reader, sig=sig)
    assert reader.calls == [(APOOL, B)] and rec["a_state_kind"] == "fresh_read"


def test_m1_records_the_fresh_read(tmp_path):
    db = str(tmp_path / "d.db")
    fast.handle_signal(dict(BRIDGE_SIG), 0.0, tape=_btape(STALE_A, SOURCE_B),
                       live=dict(CTX, fresh_read=Reader()), db_path=db, now_fn=lambda: 1000.0)
    row = sqlite3.connect(db).execute("SELECT reason, a_state_kind, a_state_age_blocks, a_fresh_read, "
                                      "a_fresh_read_ms FROM rh_fast_decisions").fetchone()
    assert row == ("would_send", "fresh_read", 0, "ok", 41.0)


# --- the reader: one extsload eth_call at an explicit block -------------------------------------

def _answer(*words, offset=32, length=None):
    length = len(words) if length is None else length
    return "0x" + "%064x" % offset + "%064x" % length + "".join("%064x" % w for w in words)


def _slot0(sqrt, tick=-1234):
    return ((tick & 0xFFFFFF) << 160) | sqrt               # tick and fees sit above the 160-bit price


class Post:
    def __init__(self, *answers, delay=0.0):
        self.bodies, self.answers, self.delay = [], list(answers), delay

    def __call__(self, body):
        self.bodies.append(body)
        if self.delay:
            time.sleep(self.delay)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_the_reader_asks_for_this_pools_slot0_and_liquidity_at_the_explicit_block():
    post = Post((200, {"jsonrpc": "2.0", "id": 1, "result": _answer(_slot0(A_SQRT), (7 << 128) | 10 ** 22)}))
    row, outcome, ms = fast.bridge_state_reader("http://x", post=post)(APOOL, B)
    assert outcome == "ok" and row["sqrt"] == A_SQRT and row["liquidity"] == 10 ** 22
    assert (row["pool"], row["block_no"], row["learned"]) == (APOOL, B, True) and ms >= 0
    (body,) = post.bodies
    liq = _liquidity_slot(APOOL)
    base = "%064x" % (int(liq, 16) - 3)
    assert body["method"] == "eth_call" and body["params"][1] == hex(B)
    assert body["params"][0] == {"to": PM, "data": "0x" + _EXTSLOAD_BATCH + "%064x" % 32 + "%064x" % 2 + base + liq}


def test_the_reader_is_all_or_nothing():
    good = (_slot0(A_SQRT), 10 ** 22)
    for answer, want in [
            ((200, {"result": _answer(*good, length=3)}), "malformed"),
            ((200, {"result": _answer(*good, offset=64)}), "malformed"),
            ((200, {"result": _answer(good[0])}), "malformed"),
            ((200, {"result": _answer(*good) + "00"}), "malformed"),
            ((200, {"result": "0xzz"}), "malformed"),
            ((200, {"error": {"code": -32000, "message": "header not found"}}), "rpc_error"),
            ((200, {"result": None}), "rpc_error"),
            ((200, None), "http_200"),
            ((502, None), "http_502"),
            ((200, {"result": _answer(_slot0(0), 10 ** 22)}), "empty_state"),
            ((200, {"result": _answer(_slot0(A_SQRT), 0)}), "empty_state"),
            (OSError("reset"), "transport:OSError")]:
        row, outcome, _ms = fast.bridge_state_reader("http://x", post=Post(answer))(APOOL, B)
        assert (row, outcome) == (None, want), answer


def test_a_403_or_429_cools_the_reader_and_a_cooled_reader_sends_nothing():
    for status in (403, 429):
        now = [100.0]
        post = Post((status, None), (200, {"result": _answer(_slot0(A_SQRT), 10 ** 22)}))
        read = fast.bridge_state_reader("http://x", post=post, cooldown_s=60.0, clock=lambda: now[0])
        assert read(APOOL, B)[:2] == (None, "http_%d" % status)
        now[0] = 159.9
        assert read(APOOL, B)[:2] == (None, "cooldown") and len(post.bodies) == 1
        now[0] = 160.0
        assert read(APOOL, B)[1] == "ok" and len(post.bodies) == 2


def test_the_reader_is_bounded_and_drops_a_late_answer():
    post = Post((200, {"result": _answer(_slot0(A_SQRT), 10 ** 22)}), delay=0.5)
    started = time.perf_counter()
    row, outcome, ms = fast.bridge_state_reader("http://x", post=post, timeout_s=0.05)(APOOL, B)
    assert (row, outcome) == (None, "timeout") and time.perf_counter() - started < 0.3 and ms < 300


def test_the_default_post_sends_the_curl_user_agent(monkeypatch):
    """publicnode answers urllib's default agent with a bare 403 (the watcher's _http_post)."""
    import json
    import urllib.request
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"result": _answer(_slot0(A_SQRT), 10 ** 22)}).encode()

    def urlopen(req, timeout):
        seen.update(ua=req.get_header("User-agent"), url=req.full_url, timeout=timeout)
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    row, outcome, _ms = fast.bridge_state_reader("http://host.example/rpc")(APOOL, B)
    assert outcome == "ok" and seen["ua"] == "curl/8.5.0" and seen["url"] == "http://host.example/rpc"


def test_the_wiring_reads_the_knob_and_the_tx_read_url(monkeypatch):
    from sniper.settings import memecoin as cfg
    logs = []
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663", "http://host.example/rpc")
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_BRIDGE_FRESH_READ", False)
    assert fast.make_bridge_fresh_read(logs.append) is None
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_BRIDGE_FRESH_READ", True)
    assert callable(fast.make_bridge_fresh_read(logs.append))
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663", " ")
    assert fast.make_bridge_fresh_read(logs.append) is None and "TX_READ_URL_4663" in logs[-1]


def test_the_knob_parses_like_the_other_kill_knobs(monkeypatch):
    import importlib
    from sniper.settings import memecoin as cfg
    try:
        for raw, on in (("1", True), ("0", False), ("off", False), ("", True)):
            monkeypatch.setenv("MEMECOIN_RH_FAST_BRIDGE_FRESH_READ", raw)
            importlib.reload(cfg)
            assert cfg.MEMECOIN_RH_FAST_BRIDGE_FRESH_READ is on
        monkeypatch.delenv("MEMECOIN_RH_FAST_BRIDGE_FRESH_READ")
        importlib.reload(cfg)
        assert cfg.MEMECOIN_RH_FAST_BRIDGE_FRESH_READ is True
        for raw, want in (("0.2", 0.2), ("0.1", 0.1), ("0", 0.2), ("5", 0.2), ("nan", 0.2), ("x", 0.2)):
            monkeypatch.setenv("MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S", raw)
            importlib.reload(cfg)
            assert cfg.MEMECOIN_RH_FAST_BRIDGE_FRESH_READ_TIMEOUT_S == want
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_the_suite_pins_the_knob_off():
    import os
    assert os.environ.get("MEMECOIN_RH_FAST_BRIDGE_FRESH_READ") == "0"

