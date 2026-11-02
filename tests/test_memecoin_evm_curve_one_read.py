"""4663 launchpad-curve token safety, cuts A+B+C (operator yes 2026-10-14 via Alpha), behind
MEMECOIN_EVM_CURVE_SAFETY_ONE_READ (default off). OUTPOST's check took 1076 ms: 10 serial eth_calls
(curve_state read 4 times, getReserves twice) plus an unmarked live Binance GET.

With the knob on: curve_state once and getReserves once per check, handed to the entry and exit
quotes; the ETH price from the executor's memo when it is <= 1 s old, else today's live read.
Every read the check makes is marked (host, method, selector, ms, outcome) on `timings`, knob on or
off, so the 615 ms curve_state on OUTPOST can be explained."""
import json

import pytest

from sniper.execution import evm_launchpad_curve as lc
from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg

MINT = "0x" + "5e" * 20
CURVE = "0x" + "c1" * 20
NATIVE = "0x" + "00" * 20
LIVE = {"coin": MINT, "curve": CURVE, "counter": NATIVE, "fee_bps": 100, "state": 0}
RESERVES = (10 ** 21, 10 ** 27)


class Reads:
    def __init__(self, state=LIVE, reserves=RESERVES):
        self.state, self.reserves = state, reserves
        self.calls = []

    def state_fn(self, chain, mint, rpc_url, *, http=None):
        self.calls.append("state")
        return self.state

    def reserves_fn(self, curve, rpc_url, *, http=None):
        self.calls.append("reserves")
        return self.reserves


def _check(monkeypatch, reads, *, on, fallback=None, price=lambda chain: 4000.0):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", on, raising=False)

    def refused(mint, **kw):
        reads.calls.append("fallback")
        return False, "fallback_refused"
    return lc.wrap_token_safety_fn(
        "4663", "https://rpc.example.test/key", taker="0x" + "ab" * 20,
        fallback_fn=fallback or refused, native_usd_price_fn=price,
        state_fn=reads.state_fn, reserves_fn=reads.reserves_fn)


def test_knob_off_keeps_todays_four_state_reads_and_two_reserve_reads(monkeypatch):
    reads = Reads()
    assert _check(monkeypatch, reads, on=False)(MINT, usd=175.0) == (
        True, "launchpad_curve_exit_quotable_at_clip")
    assert reads.calls.count("state") == 4 and reads.calls.count("reserves") == 2


def test_knob_on_reads_state_and_reserves_once_with_the_same_verdict(monkeypatch):
    reads = Reads()
    assert _check(monkeypatch, reads, on=True)(MINT, usd=175.0) == (
        True, "launchpad_curve_exit_quotable_at_clip")
    assert reads.calls == ["state", "reserves"]


@pytest.mark.parametrize("on", [False, True])
def test_an_absent_state_never_skips_the_standard_checks_reads(monkeypatch, on):
    # Alpha's runnable check: absent curve state falls through to the standard check, which does
    # its own reads; it never stands in for a pass.
    reads = Reads(state=None)
    assert _check(monkeypatch, reads, on=on)(MINT, usd=175.0) == (False, "fallback_refused")
    assert reads.calls == ["state", "fallback"]


@pytest.mark.parametrize("on", [False, True])
def test_absent_reserves_refuse_the_entry_quote(monkeypatch, on):
    reads = Reads(reserves=None)
    assert _check(monkeypatch, reads, on=on)(MINT, usd=175.0) == (
        False, "launchpad_curve_entry_unquotable")
    assert "fallback" not in reads.calls


@pytest.mark.parametrize("on", [False, True])
def test_a_graduated_coin_goes_to_the_standard_check(monkeypatch, on):
    reads = Reads(state=dict(LIVE, state=2))
    assert _check(monkeypatch, reads, on=on)(MINT, usd=175.0) == (False, "fallback_refused")


@pytest.mark.parametrize("on", [False, True])
def test_no_price_still_refuses(monkeypatch, on):
    reads = Reads()
    assert _check(monkeypatch, reads, on=on, price=lambda chain: None)(MINT, usd=175.0) == (
        False, "launchpad_curve_price_unavailable")


def test_the_bound_amount_path_reads_once_too(monkeypatch):
    reads = Reads()
    assert _check(monkeypatch, reads, on=True)(MINT, bought_amount_atomic=10 ** 24) == (
        True, "launchpad_curve_exit_quotable_at_clip")
    assert reads.calls == ["state", "reserves"]


# --- the per-read marks ------------------------------------------------------------------------

def test_every_read_is_marked_with_host_method_selector_ms_and_outcome(monkeypatch):
    sent = []

    def http(method, url, headers=None, body=None, timeout=10):
        sent.append(url)
        return 200, {"result": "0x"}, {}
    http.safety_deadline_active = lambda: True             # a seam probe the read layer makes

    def state_fn(chain, mint, rpc_url, *, http=None):
        assert http.safety_deadline_active() is True       # probes still reach the real seam
        http("POST", rpc_url, body=json.dumps({"method": "eth_call",
                                               "params": [{"to": CURVE, "data": "0x3cf28b5a00"}]}))
        http("POST", rpc_url, body=json.dumps({"method": "eth_chainId", "params": []}))
        return None
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", False, raising=False)
    check = lc.wrap_token_safety_fn(
        "4663", "https://rpc.example.test/secret-key", http=http, taker="0x" + "ab" * 20,
        fallback_fn=lambda mint, **kw: (False, "fallback"), state_fn=state_fn)
    timings = {}
    check(MINT, usd=175.0, timings=timings)
    reads = timings["curve_reads"]
    assert [(r["host"], r["rpc"], r["sel"], r["outcome"]) for r in reads] == [
        ("https://rpc.example.test", "eth_call", "3cf28b5a", "http_200"),
        ("https://rpc.example.test", "eth_chainId", None, "http_200")]
    assert all(r["ms"] >= 0 for r in reads) and "secret-key" not in json.dumps(reads)


def test_a_raised_read_is_marked_and_still_raises():
    def http(method, url, headers=None, body=None, timeout=10):
        raise TimeoutError("slow")
    reads = []
    recording = lc.recording_http(http, reads)
    with pytest.raises(TimeoutError):
        recording("POST", "https://rpc.example.test/k", body='{"method": "eth_call"}')
    assert reads[0]["outcome"] == "timeout" and reads[0]["rpc"] == "eth_call"


def test_no_timings_no_recording(monkeypatch):
    seen = []

    def state_fn(chain, mint, rpc_url, *, http=None):
        seen.append(http)
        return None
    real_http = lambda *a, **k: (200, {}, {})
    lc.wrap_token_safety_fn("4663", "https://rpc.example.test/k", http=real_http,
                            taker="0x" + "ab" * 20, fallback_fn=lambda m, **k: (False, "f"),
                            state_fn=state_fn)(MINT, usd=175.0)
    assert seen == [real_http]


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("memo", [None, {"px": 4100.0, "age_ms": 1000.1}], ids=["cold", "stale"])
def test_production_price_adapter_records_live_get_for_both_flags(monkeypatch, on, memo):
    from sniper.execution import evm_wallet
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", on)
    sent = []

    def http(method, url, **kwargs):
        sent.append((method, url))
        return 200, {"price": "4000"}, {}

    def state(chain, mint, rpc, *, http=None):
        http("POST", rpc, body='{"method":"eth_call","params":[{"data":"0x3cf28b5a"}]}')
        return LIVE

    def reserves(curve, rpc, *, http=None):
        http("POST", rpc, body='{"method":"eth_call","params":[{"data":"0x0902f1ac"}]}')
        return RESERVES

    # The actual production adapter and native-price helper, with the production live-call shape.
    price = leg.curve_native_usd_price("4663", lambda: memo,
        lambda chain, *, http=http: evm_wallet.native_usd_price(chain, http=http))
    check = lc.wrap_token_safety_fn("4663", "https://rpc.example.test/private-key", http=http,
        taker=MINT, fallback_fn=lambda *a, **k: (False, "fallback"),
        state_fn=state, reserves_fn=reserves, native_usd_price_fn=price)
    timings = {}
    assert check(MINT, usd=175., timings=timings) == (True, "launchpad_curve_exit_quotable_at_clip")
    assert [method for method, _ in sent].count("GET") == 1
    records = timings["curve_reads"]
    get, = [row for row in records if row["rpc"] == "GET"]
    assert (get["host"], get["sel"], get["outcome"]) == (
        "https://api.binance.com", None, "http_200")
    assert get["ms"] >= 0 and len(records) == len(sent)
    assert "curve_price_ms" in timings and "private-key" not in json.dumps(records)


@pytest.mark.parametrize("on", [False, True])
def test_production_price_adapter_records_timeout_and_fallback_without_fabricating_price(monkeypatch, on):
    from sniper.execution import evm_wallet
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", on)
    monkeypatch.setenv("FMP_API_KEY", "offline-private-key")
    calls = []
    def http(method, url, **kwargs):
        calls.append((method, url))
        if "binance.com" in url:
            raise TimeoutError("offline timeout")
        return 503, [], {}
    reads = Reads()
    price = leg.curve_native_usd_price("4663", lambda: None,
        lambda chain, *, http=http: evm_wallet.native_usd_price(chain, http=http))
    check = lc.wrap_token_safety_fn("4663", "https://rpc.example.test/key", http=http,
        taker=MINT, fallback_fn=lambda *a, **k: (False, "fallback"),
        state_fn=reads.state_fn, reserves_fn=reads.reserves_fn, native_usd_price_fn=price)
    timings = {}
    assert check(MINT, usd=175., timings=timings) == (False, "launchpad_curve_price_unavailable")
    assert len(calls) == 2
    assert [(row["rpc"], row["outcome"]) for row in timings["curve_reads"]] == [
        ("GET", "timeout"), ("GET", "http_503")]
    assert "offline-private-key" not in json.dumps(timings)


@pytest.mark.parametrize("on", [False, True])
def test_price_get_preserves_deadline_scope_and_expiry_overrides_its_answer(monkeypatch, on):
    from contextlib import contextmanager
    from sniper.execution import evm_wallet
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", on)
    active, expired, deadlines = [False], [False], []
    @contextmanager
    def scope(deadline):
        deadlines.append(deadline)
        active[0] = True
        try:
            yield lambda: expired[0]
        finally:
            active[0] = False
    def http(method, url, **kwargs):
        assert active[0] and method == "GET"
        expired[0] = True
        return 200, {"price": "4000"}, {}
    http.safety_deadline_active = lambda: active[0]
    def live(chain, *, http=http):
        assert http.safety_deadline_active()
        return evm_wallet.native_usd_price(chain, http=http)
    price = leg.curve_native_usd_price("4663", lambda: None, live)
    reads = Reads()
    check = lc.wrap_token_safety_fn("4663", "https://rpc.example.test/key", http=http,
        taker=MINT, fallback_fn=lambda *a, **k: (False, "fallback"),
        state_fn=reads.state_fn, reserves_fn=reads.reserves_fn, native_usd_price_fn=price,
        safety_deadline_scope=scope)
    timings = {}
    assert check(MINT, usd=175., deadline=123.5, timings=timings) == (
        False, "safety_deadline_exceeded")
    assert deadlines == [123.5] and active == [False]
    assert [row["rpc"] for row in timings["curve_reads"]] == ["GET"]


# --- A: the ETH price from the executor's memo -------------------------------------------------

@pytest.mark.parametrize("memo, on, want", [
    ({"px": 4100.0, "age_ms": 300.0}, True, 4100.0),
    ({"px": 4100.0, "age_ms": 1000.0}, True, 4100.0),
    ({"px": 4100.0, "age_ms": 1000.1}, True, "live"),         # stale
    ({"px": 4100.0, "age_ms": -1.0}, True, "live"),           # from the future
    ({"px": 0.0, "age_ms": 10.0}, True, "live"),
    ({"px": float("nan"), "age_ms": 10.0}, True, "live"),
    ({"px": True, "age_ms": 10.0}, True, "live"),
    (None, True, "live"),                                     # no price read yet
    ({"px": 4100.0, "age_ms": 10.0}, False, "live"),          # knob off: today's live read
])
def test_the_curve_price_takes_a_fresh_memo_else_the_live_read(monkeypatch, memo, on, want):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", on, raising=False)
    live = []
    price = leg.curve_native_usd_price("4663", lambda: memo,
                                       lambda chain: live.append(chain) or "live")
    assert price("4663") == want and live == ([] if want != "live" else ["4663"])


def test_a_raising_memo_falls_back_to_the_live_read(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", True, raising=False)

    def boom():
        raise RuntimeError("memo")
    assert leg.curve_native_usd_price("4663", boom, lambda chain: 3999.0)("4663") == 3999.0


def test_another_chains_price_is_never_the_memo(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", True, raising=False)
    price = leg.curve_native_usd_price("4663", lambda: {"px": 4100.0, "age_ms": 1.0},
                                       lambda chain: "live")
    assert price("56") == "live"


def test_the_4663_context_gives_the_curve_wrapper_the_executors_price_mark(monkeypatch, tmp_path):
    from sniper.execution import evm_wallet
    from test_memecoin_evm_context_keys import _production_context
    real_native_price = evm_wallet.native_usd_price
    use_real_price = [False]

    def native_price(chain, http=None):
        return real_native_price(chain, http=http) if use_real_price[0] else "live"

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", True, raising=False)
    monkeypatch.setattr(evm_wallet, "native_usd_price", native_price)
    captured = {}
    real = lc.wrap_token_safety_fn

    def capture(*a, **k):
        captured.update(k)
        return real(*a, **k)
    monkeypatch.setattr(lc, "wrap_token_safety_fn", capture)
    peeks = []
    real_price = leg.curve_native_usd_price
    monkeypatch.setattr(leg, "curve_native_usd_price",
                        lambda chain, peek, live: peeks.append((chain, peek)) or real_price(chain, peek, live))
    ctx = _production_context(monkeypatch, tmp_path, "4663")
    assert peeks == [("4663", ctx["quote_price_peek_fn"])] and callable(peeks[0][1])
    price = captured["native_usd_price_fn"]
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", False, raising=False)
    assert price("4663") == "live"                        # knob off: today's live read
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", True, raising=False)
    assert price("4663") == "live"                        # no mark read yet: the live read

    # Check the context's actual live lambda forwards the per-check transport, with either flag.
    use_real_price[0] = True
    for on in (False, True):
        monkeypatch.setattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", on)
        records = []
        http = lc.recording_http(lambda *a, **k: (200, {"price": "4000"}, {}), records)
        assert price("4663", http=http) == 4000.0
        assert [row["rpc"] for row in records] == ["GET"]
