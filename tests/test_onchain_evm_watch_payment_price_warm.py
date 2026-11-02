"""A relay buy's payment-token price is kept warm off the detection path (2026-09-28, Alpha GO).

Measured 2026-09-28 on the first warm-cache relay detection (row 2931, DopamineFeenFr1): decode_ms
price 87.5 of a 89.5 ms decode. The payment token was USDG, priced by a Kraken GET cached for 30 s,
so on sparse 4663 buys it is fetched on the path almost every time. A background refresher now
re-reads this chain's payment tickers every 10 s; a detection uses a price younger
than 30 s. A failed refresh never overwrites a good price with None: the old entry ages out and the
detection path then fetches inline exactly as before.
"""
import scripts.research.onchain_evm_wallet_watch as w

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


class _Http:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self, method, url, timeout=None):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return 500, None, None
        return 200, {"error": [], "result": {"USDGUSD": {"b": [str(answer)]}}}, None


def _setup(monkeypatch, answers, now):
    import sniper.execution.evm_wallet as ew
    http = _Http(answers)
    monkeypatch.setattr(ew, "_http", http)
    monkeypatch.setattr(w, "_PAYMENT_PRICES", {})
    clock = [now]
    monkeypatch.setattr(w, "_payment_clock", lambda: clock[0])
    w._configure("4663")
    return http, clock


def test_a_warm_price_serves_the_detection_with_no_http(monkeypatch):
    http, _clock = _setup(monkeypatch, [0.9998], 1000.0)
    w._refresh_payment_prices()
    assert http.calls == 1
    assert w._payment_usd_price(USDG, 2650.0) == 0.9998
    assert http.calls == 1, "the detection path must not fetch a warm price"


def test_a_failed_refresh_keeps_the_good_price_until_it_ages_out(monkeypatch):
    http, clock = _setup(monkeypatch, [0.9998, None, 1.0001], 1000.0)
    w._refresh_payment_prices()
    clock[0] = 1010.0
    w._refresh_payment_prices()                       # fails: must not store None
    assert w._payment_usd_price(USDG, 2650.0) == 0.9998 and http.calls == 2
    clock[0] = 1031.0                                 # the good price is now older than 30 s
    assert w._payment_usd_price(USDG, 2650.0) == 1.0001 and http.calls == 3, "inline fetch, as before"


def test_the_refresher_runs_under_the_ttl_and_stops(monkeypatch):
    runs = []

    class _Stop:
        def __init__(self):
            self.waits = []

        def wait(self, timeout):
            self.waits.append(timeout)
            return len(self.waits) > 2
    monkeypatch.setattr(w, "_refresh_payment_prices", lambda: runs.append(1))
    w._payment_price_loop(_Stop())
    assert runs == [1, 1, 1], "refreshes once at start, then per wait"


def test_weth_is_still_the_native_price_and_never_fetched(monkeypatch):
    http, _clock = _setup(monkeypatch, [], 1000.0)
    assert w._payment_usd_price(w.WETH, 2650.0) == 2650.0 and http.calls == 0


def test_the_watcher_process_starts_the_refresher(monkeypatch):
    import threading
    from sniper.settings import memecoin as cfg
    started = threading.Event()
    monkeypatch.setattr(w, "_payment_price_loop", lambda stop: started.set())
    monkeypatch.setattr(w, "quote_usd_price", lambda chain, **k: 2650.0)
    monkeypatch.setattr(cfg, "memecoin_evm_ws_url", lambda chain: "wss://test")
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_PUSH_ENABLED", True, raising=False)
    monkeypatch.setattr(w, "run_push", lambda *a, **k: None)
    w.run(chain="4663")
    assert started.wait(2.0), "run() must start the payment-price refresher"
