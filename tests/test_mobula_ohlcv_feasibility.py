"""The feasibility check's gates, tested before it touches the network.

The gate that matters most is #4. A candle spanning [t0+3, t0+4) contains trades AFTER the
baseline instant, so reading its close as "the price at t0+3" looks ahead by up to one period.
That is a silent look-ahead bug and it would have flattered every result the check produced.
"""
import json

import pytest

from scripts.research import mobula_ohlcv_feasibility as M


def _candles(start, period, n):
    return [{"open_ts": start + i * period, "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 10.0 + i, "volume": 1.0} for i in range(n)]


# --- gate 4: no candle may include data after the baseline ---------------------------------------

def test_a_candle_that_ends_exactly_at_the_baseline_is_admissible():
    candles = _candles(1000.0, 1.0, 5)          # [1000,1001), [1001,1002), ...
    chosen, verdict = M.admissible(candles, baseline_epoch=1003.0, period_s=1.0)
    assert verdict == "clean"
    assert chosen["open_ts"] == 1002.0          # spans [1002,1003), ends AT the baseline


def test_a_candle_that_straddles_the_baseline_is_rejected():
    """The whole point. [1002,1003) is fine at baseline 1003; at baseline 1002.5 it is not."""
    candles = _candles(1000.0, 1.0, 5)
    chosen, verdict = M.admissible(candles, baseline_epoch=1002.5, period_s=1.0)
    assert chosen["open_ts"] == 1001.0          # falls back to the last one that ENDED
    assert verdict == "clean"

    # nothing has ended yet -> nothing is admissible
    chosen, verdict = M.admissible(candles, baseline_epoch=1000.5, period_s=1.0)
    assert chosen is None
    assert verdict == "rejected_straddles_baseline"


def test_the_look_ahead_variant_is_labelled_not_silently_allowed():
    candles = _candles(1000.0, 1.0, 5)
    chosen, verdict = M.admissible(candles, 1000.5, 1.0, strict=False)
    assert verdict == "LOOK_AHEAD"
    assert chosen["open_ts"] == 1000.0


def test_a_coarse_period_swallows_the_whole_window():
    """A 60s candle cannot price a +3s baseline at all; it must reject, not approximate."""
    candles = _candles(1000.0, 60.0, 3)
    assert M.admissible(candles, 1003.0, 60.0)[1] == "rejected_straddles_baseline"


def test_an_unknown_period_cannot_admit_anything():
    assert M.admissible(_candles(1000.0, 1.0, 5), 1003.0, None) == (None, "unknown_period")


# --- gate 3: granularity is measured from the response, not assumed ------------------------------

def test_granularity_is_inferred_from_the_candles():
    assert M.infer_period_s(_candles(1000.0, 5.0, 6)) == pytest.approx(5.0)


def test_granularity_survives_a_gap():
    """A missing bar must not read as a coarser series; the median gap ignores one outlier."""
    candles = _candles(1000.0, 1.0, 6)
    del candles[3]
    assert M.infer_period_s(candles) == pytest.approx(1.0)


def test_too_few_candles_report_no_granularity():
    assert M.infer_period_s(_candles(1000.0, 1.0, 2)) is None


# --- parsing ------------------------------------------------------------------------------------

def test_millisecond_timestamps_are_normalised():
    parsed = M.parse_candles({"data": [{"time": 1787000000000, "close": 5.0}]})
    assert parsed[0]["open_ts"] == pytest.approx(1787000000.0)


def test_a_shapeless_payload_yields_no_candles():
    assert M.parse_candles({"error": "nope"}) == []
    assert M.parse_candles(None) == []


def test_candles_are_returned_in_time_order():
    """Seconds stay seconds; only values past the 1e11 threshold are treated as milliseconds."""
    parsed = M.parse_candles({"data": [{"time": 3000, "close": 3}, {"time": 1000, "close": 1}]})
    assert [c["open_ts"] for c in parsed] == [1000.0, 3000.0]


# --- gate 2: the sample is frozen before any request ---------------------------------------------

def test_a_tampered_sample_refuses_to_probe(tmp_path, monkeypatch):
    path = tmp_path / "sample.json"
    monkeypatch.setattr(M, "SAMPLE_PATH", path)
    payload = {"since": "2026-08-18", "count": 1,
               "events": [{"event_id": "e1", "mint": "M", "network_id": "solana",
                           "observed_at": "2026-08-18T00:00:00+00:00", "their_price": 1.0}],
               "frozen_at_note": "x"}
    body = json.dumps(payload, sort_keys=True, indent=1)
    import hashlib
    payload["digest"] = hashlib.sha256(body.encode()).hexdigest()
    path.write_text(json.dumps(payload, sort_keys=True, indent=1) + "\n")
    assert M.load_sample()["count"] == 1                 # clean sample loads

    tampered = json.loads(path.read_text())
    tampered["events"][0]["mint"] = "SOMETHING-ELSE"
    path.write_text(json.dumps(tampered, sort_keys=True, indent=1) + "\n")
    with pytest.raises(M.Halt, match="changed after it was stamped"):
        M.load_sample()


def test_a_missing_sample_refuses_to_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "SAMPLE_PATH", tmp_path / "absent.json")
    with pytest.raises(M.Halt, match="no frozen sample"):
        M.load_sample()


# --- gates 1 and 6 --------------------------------------------------------------------------------

def test_the_request_carries_no_session_material(monkeypatch):
    seen = {}

    class _Resp:
        status = 200
        def read(self): return b'{"data": []}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(request, timeout=None):
        seen["headers"] = dict(request.headers)
        return _Resp()

    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    M.fetch({"address": "M", "chainId": "solana"})
    lowered = {k.lower() for k in seen["headers"]}
    assert "cookie" not in lowered and "authorization" not in lowered


@pytest.mark.parametrize("code", [401, 403, 429])
def test_an_auth_or_rate_status_halts_the_run(monkeypatch, code):
    def fake_urlopen(request, timeout=None):
        raise M.urllib.error.HTTPError("u", code, "no", {}, None)
    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(M.Halt):
        M.fetch({"address": "M"})


def test_an_ordinary_server_error_does_not_halt(monkeypatch):
    """Gate 6 names 401/403/429. A 500 is recorded and the run continues."""
    def fake_urlopen(request, timeout=None):
        raise M.urllib.error.HTTPError("u", 500, "boom", {}, None)
    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    assert M.fetch({"address": "M"})["ok"] is False


# --- gate 7 ---------------------------------------------------------------------------------------

def test_nothing_funded_is_imported():
    """The check must not be reachable from, or reach into, the entry or exit path."""
    import inspect
    src = inspect.getsource(M)
    for banned in ("solana_real_seams", "solana_swap_executor", "solana_exit_executor",
                   "memecoin_real_runner", "memecoin_real_leg", "memecoin_exits"):
        assert banned not in src
