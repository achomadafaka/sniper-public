"""Fast-lane DexScreener on a kept-alive connection (operator 2026-10-14: "fix the dexscreener thing,
we need a faster way"). 73851 (56): the lane's inline market-stats read was the 185 ms setup; a cold
urllib GET measured 41-237 ms from this host, the same GET on a warm pooled connection 14-20 ms.
Same request, same JSON, same 429 stop; only the connection is kept and kept warm."""
import urllib.error

import pytest

from sniper.services import memecoin_rh_fast_lane as lane
from sniper.settings import memecoin as cfg


class FakeClient:
    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def request(self, method, url, headers=None, body=None, timeout=None):  # PooledHttpClient's
        self.asked.append((method, url, dict(headers or {})))
        return self.answers.pop(0)


def test_one_client_serves_every_request_and_the_warmer_starts_once():
    made, warmers = [], []
    client = FakeClient([(200, {"pairs": []}, None), (200, {"pairs": None}, None)])

    def factory():
        made.append(1)
        return client
    warm = lane.WarmDexHttp(client_factory=factory, start_warmer=lambda fn, s: warmers.append((fn, s)))
    assert warm("GET", "https://api.dexscreener.com/latest/dex/tokens/0xa") == (200, {"pairs": []}, None)
    assert warm("GET", "https://api.dexscreener.com/latest/dex/tokens/0xb")[0] == 200
    assert made == [1] and len(warmers) == 1 and warmers[0][1] == cfg.MEMECOIN_DEX_WARM_INTERVAL_S
    assert client.asked[0][2]["accept"] == "application/json" and "user-agent" in client.asked[0][2]


@pytest.mark.parametrize("status", [429, 403, 500])
def test_an_error_status_raises_like_urllib_so_the_429_stop_still_works(status):
    warm = lane.WarmDexHttp(client_factory=lambda: FakeClient([(status, "x", None)]),
                            start_warmer=lambda fn, s: None)
    budget = lane.DexBudget(warm)
    with pytest.raises(urllib.error.HTTPError) as err:
        budget.http("GET", "https://api.dexscreener.com/latest/dex/tokens/0xa")
    assert err.value.code == status
    assert (budget.stopped == "dexscreener_429") is (status == 429)


def test_the_lanes_take_the_warm_base_unless_a_base_is_injected(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(lane, "_WARM_DEX", None)
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_WARM_HTTP", True, raising=False)
    assert lane.lane_dex_base(sentinel) is sentinel            # tests and callers keep theirs
    first, second = lane.lane_dex_base(None), lane.lane_dex_base(None)
    # today's deadline-bound research worker, sending through ONE shared warm client
    from sniper.execution import solana_quote as q
    assert first.func is q._ds_default_http and first.keywords["funded"] is False
    assert isinstance(first.keywords["transport"], lane.WarmDexHttp)
    assert second.keywords["transport"] is first.keywords["transport"]
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_WARM_HTTP", False)  # rollback: today's per-call urllib
    assert lane.lane_dex_base(None) is None


def test_the_warm_send_keeps_the_whole_request_deadline(monkeypatch):
    """The 09-25 DNS stall: a stuck send must still come back as TimeoutError at the deadline."""
    import threading
    from sniper.execution import solana_quote as q
    monkeypatch.setattr(q, "_DS_DEADLINE_S", 0.05)
    release, sent = threading.Event(), threading.Event()

    def stuck(method, url, headers=None, body=None, timeout=None):
        sent.set()
        release.wait(2)
        return 200, {}, None
    try:
        with pytest.raises(TimeoutError):
            q._ds_default_http("GET", "https://api.dexscreener.invalid/x", transport=stuck)
        assert sent.wait(1), "the transport was ignored"   # Gamma LOW: mutant `send = _http`
    finally:
        release.set()


@pytest.mark.parametrize("raw,want", [(0, 1.0), (-5, 1.0), ("x", 20.0), (float("nan"), 20.0),
                                      (1e9, 3600.0), (20, 20.0)])
def test_the_warm_interval_is_clamped_never_a_crash(monkeypatch, raw, want):
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_WARM_INTERVAL_S", raw, raising=False)
    assert lane.cfg_interval() == want


def test_a_warmer_that_will_not_start_never_fails_the_read():
    def boom(fn, s):
        raise ValueError("warming interval must be positive")
    warm = lane.WarmDexHttp(client_factory=lambda: FakeClient([(200, "", None)]), start_warmer=boom)
    assert warm("GET", "https://api.dexscreener.com/latest/dex/tokens/0xa") == (200, {}, None)


def test_the_warmer_never_pings_while_the_breaker_cools(monkeypatch):
    from sniper.execution import solana_quote as q
    client = FakeClient([(200, {"pairs": None}, None)])
    warm = lane.WarmDexHttp(client_factory=lambda: client, start_warmer=lambda fn, s: None)
    monkeypatch.setattr(q, "dexscreener_cooldown_active", lambda funded=False: True)
    warm._ping()
    assert client.asked == []
    monkeypatch.setattr(q, "dexscreener_cooldown_active", lambda funded=False: False)
    warm._ping()
    assert [a[1] for a in client.asked] == [lane.WarmDexHttp.PING_URL]


@pytest.mark.parametrize("interval,want", [(20, 60.0), (5, 15.0), (3600, 60.0)])
def test_the_idle_connection_never_outlives_sixty_seconds(monkeypatch, interval, want):
    from sniper.execution import fast_http
    seen = {}
    monkeypatch.setattr(fast_http, "PooledHttpClient", lambda **kw: seen.update(kw) or object())
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_WARM_INTERVAL_S", interval, raising=False)
    lane.WarmDexHttp._default_client()
    assert seen["keepalive_expiry_s"] == want and seen["max_connections"] == 8
