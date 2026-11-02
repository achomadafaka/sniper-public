"""Tests for the read-only HL data helpers (l2Book / fundingHistory / candleSnapshot)
used by the copy-fidelity capture + scorecard. Mock _post_info; assert payload + normalization."""
from sniper.markets import hyperliquid_api as HL
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)


def test_post_info_reuses_process_session(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class Session:
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Response()

    monkeypatch.setattr(HL, "_SESSION", Session(), raising=False)
    monkeypatch.setattr(HL, "_INFO_LIMITER", None)
    assert HL._post_info({"type": "meta"}) == {"ok": True}
    assert HL._post_info({"type": "meta"}) == {"ok": True}
    assert len(calls) == 2


def test_info_limiter_refuses_weight_over_capacity(tmp_path):
    limiter = HL.InfoRateLimiter(tmp_path / "limit.state", capacity=1200, window_s=60)
    assert limiter.try_reserve(1200, now=10.0)
    assert not limiter.try_reserve(1, now=10.0)
    assert limiter.try_reserve(1, now=71.0)


def test_info_limiter_429_penalty_blocks_every_process_until_backoff(tmp_path):
    limiter = HL.InfoRateLimiter(tmp_path / "limit.state", capacity=60, window_s=60)
    limiter.penalize(5.0, now=10.0)

    assert not limiter.try_reserve(1, now=14.0)
    assert not limiter.try_reserve(1, now=15.0)
    assert limiter.try_reserve(1, now=16.0)


def test_post_info_retries_429_after_retry_after(monkeypatch):
    calls = []
    sleeps = []

    class Response:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"Retry-After": "2"}
        def raise_for_status(self):
            if self.status_code == 429:
                raise HL.requests.HTTPError(response=self)
        def json(self):
            return {"ok": True}

    class Session:
        def post(self, *args, **kwargs):
            calls.append(1)
            return Response(429 if len(calls) == 1 else 200)

    class Limiter:
        penalties = []

        def reserve(self, weight):
            assert weight == 20

        def penalize(self, delay):
            self.penalties.append(delay)

    monkeypatch.setattr(HL, "_SESSION", Session())
    monkeypatch.setattr(HL, "_INFO_LIMITER", Limiter())
    patch_local(monkeypatch, "sleep", sleeps.append)
    assert HL._post_info({"type": "metaAndAssetCtxs"}) == {"ok": True}
    assert len(calls) == 2 and sleeps == [5.0]
    assert HL._INFO_LIMITER.penalties == [5.0]


def test_post_info_uses_bounded_exponential_backoff_for_repeated_429(monkeypatch):
    calls = []
    sleeps = []

    class Response:
        status_code = 429
        headers = {}

        def raise_for_status(self):
            calls.append(1)
            if len(calls) < 4:
                raise HL.requests.HTTPError(response=self)

        def json(self):
            return {"ok": True}

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    class Limiter:
        penalties = []

        def reserve(self, _weight):
            pass

        def penalize(self, delay):
            self.penalties.append(delay)

    monkeypatch.setattr(HL, "_SESSION", Session())
    monkeypatch.setattr(HL, "_INFO_LIMITER", Limiter())
    patch_local(monkeypatch, "sleep", sleeps.append)

    assert HL._post_info({"type": "fundingHistory"}) == {"ok": True}
    assert sleeps == [5.0, 10.0, 20.0]
    assert HL._INFO_LIMITER.penalties == sleeps


def _patch(monkeypatch, capture, response):
    def fake(payload, timeout=HL._TIMEOUT):
        capture["payload"] = payload
        return response
    monkeypatch.setattr(HL, "_post_info", fake)


def test_fetch_l2_book_normalizes_and_computes_spread(monkeypatch):
    cap = {}
    resp = {"coin": "ETH", "levels": [
        [{"px": "1999.0", "sz": "3.0", "n": 2}, {"px": "1998.0", "sz": "10.0"}],   # bids
        [{"px": "2001.0", "sz": "4.0"}, {"px": "2002.0", "sz": "5.0"}],            # asks
    ]}
    _patch(monkeypatch, cap, resp)
    b = HL.fetch_l2_book("ETH")
    assert cap["payload"] == {"type": "l2Book", "coin": "ETH"}
    assert b["bids"][0] == {"px": 1999.0, "sz": 3.0}
    assert b["asks"][0] == {"px": 2001.0, "sz": 4.0}
    assert b["mid"] == 2000.0
    assert round(b["spread_bps"], 4) == round((2001.0 - 1999.0) / 2000.0 * 1e4, 4)  # 10 bps


def test_fetch_l2_book_handles_empty(monkeypatch):
    cap = {}
    _patch(monkeypatch, cap, {})
    b = HL.fetch_l2_book("WIF")
    assert b["bids"] == [] and b["asks"] == [] and b["mid"] is None and b["spread_bps"] is None


def test_fetch_funding_history_sorts_and_normalizes(monkeypatch):
    cap = {}
    resp = [{"coin": "SOL", "fundingRate": "0.0000125", "time": 200},
            {"coin": "SOL", "fundingRate": "-0.0000050", "time": 100}]
    _patch(monkeypatch, cap, resp)
    f = HL.fetch_funding_history("SOL", 50, 300)
    assert cap["payload"] == {"type": "fundingHistory", "coin": "SOL", "startTime": 50, "endTime": 300}
    assert [r["time"] for r in f] == [100, 200]          # sorted ascending
    assert f[0]["rate"] == -0.000005 and f[1]["rate"] == 0.0000125


def test_fetch_funding_history_omits_endtime_when_none(monkeypatch):
    cap = {}
    _patch(monkeypatch, cap, [])
    HL.fetch_funding_history("BTC", 1000)
    assert "endTime" not in cap["payload"] and cap["payload"]["startTime"] == 1000


def test_fetch_user_fills_by_time_is_bounded_recovery_request(monkeypatch):
    cap = {}
    _patch(monkeypatch, cap, [{"tid": 1, "time": 150}])
    rows = HL.fetch_user_fills_by_time("0xAbC", 100, 200)
    assert cap["payload"] == {
        "type": "userFillsByTime", "user": "0xabc", "startTime": 100, "endTime": 200,
    }
    assert rows == [{"tid": 1, "time": 150}]


def test_fetch_marks_from_meta_and_ctxs(monkeypatch):
    cap = {}
    resp = [{"universe": [{"name": "BTC"}, {"name": "eth"}]},
            [{"markPx": "60000.5"}, {"midPx": "3000.0"}]]     # 2nd has no markPx -> midPx
    _patch(monkeypatch, cap, resp)
    m = HL.fetch_marks()
    assert cap["payload"] == {"type": "metaAndAssetCtxs"}
    assert m["BTC"] == 60000.5 and m["ETH"] == 3000.0        # names uppercased


def test_fetch_provider_marks_preserves_hyperliquid_symbol_identity(monkeypatch):
    cap = {}
    resp = [{"universe": [{"name": "kPEPE"}, {"name": "KAS"}]},
            [{"markPx": "0.01"}, {"markPx": "0.02"}]]
    _patch(monkeypatch, cap, resp)

    marks = HL.fetch_provider_marks()

    assert cap["payload"] == {"type": "metaAndAssetCtxs"}
    assert marks == {"kPEPE": 0.01, "KAS": 0.02}


def test_fetch_candles_wraps_req_and_sorts(monkeypatch):
    cap = {}
    resp = [{"t": 2000, "o": "10", "h": "12", "l": "9", "c": "11", "v": "100"},
            {"t": 1000, "o": "8", "h": "10", "l": "7", "c": "9", "v": "80"}]
    _patch(monkeypatch, cap, resp)
    cs = HL.fetch_candles("BTC", "1h", 500, 2500)
    assert cap["payload"] == {"type": "candleSnapshot",
                              "req": {"coin": "BTC", "interval": "1h", "startTime": 500, "endTime": 2500}}
    assert [k["t"] for k in cs] == [1000, 2000]
    assert cs[0]["c"] == 9.0 and cs[1]["h"] == 12.0
