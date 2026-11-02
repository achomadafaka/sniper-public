"""The 4663 ladder records its PROVIDER HTTP calls, so "is this rung's time ours or theirs" has a
number (2026-09-27, P4). The RPC tap (`calls`) sees only JSON-RPC made on the rung's own thread;
kyber's and relay's HTTP APIs and v4_multihop's provider discovery and worker-thread stages were
invisible (v4_multihop: 2% of its 2.85 s p50 was tapped).

Each record: a label the CALL SITE chose (never parsed from the URL), scheme://host[:port] only,
start, RTT and an outcome class. NEVER the path, query or userinfo: keys ride in URLs (the 09-24
recording trap). Recorded in a separate `http` list so the payload replay of `calls` is unchanged.
With no tap installed a call site costs one getattr and records nothing."""
import io
import json
import re
import urllib.error

import pytest

from sniper.execution import evm_wallet as w


def test_a_tapped_call_records_label_host_rtt_and_outcome_only():
    w.start_http_tap()
    with w.http_tap("kyber:routes", "https://user:pw@api.example.com/robinhood/api/v1/routes?key=SECRET1234567890") as box:
        box["outcome"] = "http_200"
    [rec] = w.take_http_tap()
    assert set(rec) == {"label", "host", "started_at", "rtt_ms", "outcome"}
    assert rec["label"] == "kyber:routes" and rec["host"] == "https://api.example.com"
    assert rec["outcome"] == "http_200" and rec["rtt_ms"] >= 0
    assert "SECRET" not in json.dumps(rec) and "pw" not in json.dumps(rec)


def test_extra_counts_ride_along_and_no_url_means_no_host():
    w.start_http_tap()
    with w.http_tap("v4_multihop:path_quotes", n=4):
        pass
    [rec] = w.take_http_tap()
    assert rec["host"] is None and rec["n"] == 4 and rec["outcome"] == "ok"


def test_no_tap_installed_records_nothing_and_changes_nothing():
    w.take_http_tap()
    with w.http_tap("kyber:routes", "https://api.example.com/x") as box:
        box["outcome"] = "http_200"
    assert w.take_http_tap() is None


def test_a_raising_call_is_recorded_with_its_class_and_still_raises():
    w.start_http_tap()
    with pytest.raises(TimeoutError):
        with w.http_tap("relay:quote", "https://api.relay.link/quote/v2"):
            raise TimeoutError("slow")
    with pytest.raises(ValueError):
        with w.http_tap("relay:quote", "https://api.relay.link/quote/v2"):
            raise ValueError("bad json")
    got = w.take_http_tap()
    assert [r["outcome"] for r in got] == ["timeout", "raise:ValueError"]


def test_a_broken_recorder_never_reaches_the_call(monkeypatch):
    class Boom(list):
        def append(self, item):
            raise RuntimeError("recorder down")
    w._HTTP_TAP.calls = Boom()
    try:
        with w.http_tap("kyber:routes", "https://api.example.com") as box:
            box["outcome"] = "http_200"
    finally:
        w.take_http_tap()


def test_every_configured_url_reduces_to_host_only():
    """The live shapes, not invented ones (the 09-24 recording trap): nothing but the host."""
    from dotenv import dotenv_values
    values = dotenv_values("/home/bigpapi/Desktop/Sniper/.env")
    urls = [part.strip() for v in values.values() if v and str(v).startswith("http")
            for part in str(v).split(",") if part.strip().startswith("http")]
    if not urls:
        pytest.skip(".env not readable from this checkout")
    for url in urls:
        w.start_http_tap()
        with w.http_tap("probe", url):
            pass
        [rec] = w.take_http_tap()
        dumped = json.dumps(rec)
        assert rec["host"] == "unknown" or re.fullmatch(r"https?://[^/?#@]+", rec["host"]), rec
        tail = url.split("://", 1)[1]
        for chunk in re.findall(r"[/=@:]([A-Za-z0-9_\-]{16,})", tail):
            assert chunk not in dumped, "a credential survived into the record"


# --- the call sites ------------------------------------------------------------------------------

def test_kyber_http_calls_are_tapped_with_their_site_label_and_status(monkeypatch):
    from sniper.execution import evm_kyber_quote as k

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(k.urllib.request, "urlopen", lambda req, timeout: Resp(b'{"data": 1}'))
    w.start_http_tap()
    assert k._http_json("https://aggregator-api.kyberswap.com/robinhood/api/v1/routes?x=1",
                        label="kyber:routes") == {"data": 1}

    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(k.urllib.request, "urlopen", refuse)
    with pytest.raises(urllib.error.HTTPError):
        k._http_json("https://aggregator-api.kyberswap.com/robinhood/api/v1/route/build",
                     body={"a": 1}, label="kyber:build")
    got = w.take_http_tap()
    assert [(r["label"], r["host"], r["outcome"]) for r in got] == [
        ("kyber:routes", "https://aggregator-api.kyberswap.com", "http_200"),
        ("kyber:build", "https://aggregator-api.kyberswap.com", "http_400")]


def test_relay_quote_calls_are_tapped_in_order(monkeypatch):
    from sniper.execution import evm_relay_quote as r
    answers = iter([(200, {"steps": []}, {}), (429, {"message": "slow down"}, {})])
    w.start_http_tap()
    for _ in range(2):
        try:
            r._payload(lambda *a, **k: next(answers), {"x": 1}, timeout=3)
        except Exception:
            pass
    got = w.take_http_tap()
    assert [(x["label"], x["host"], x["outcome"]) for x in got] == [
        ("relay:quote", "https://api.relay.link", "http_200"),
        ("relay:quote", "https://api.relay.link", "http_429")]


def test_v4_multihop_provider_discovery_is_timed_from_the_rung_thread():
    from sniper.execution import evm_v4_multihop as mh

    def _gt_pools_ranked(chain, token):
        return [("pool", 1.0)]

    class Budget:
        discovery_cut = False

        def expired(self, discovery=False):
            return False

        def remaining_s(self, discovery=False):
            return 5.0
    w.start_http_tap()
    assert mh._provider_pools(_gt_pools_ranked, "4663", "0xabc", budget=Budget()) == [("pool", 1.0)]
    assert mh._provider_pools(_gt_pools_ranked, "4663", "0xabc") == [("pool", 1.0)]   # no budget
    got = w.take_http_tap()
    assert [(x["label"], x["outcome"]) for x in got] == [
        ("v4_multihop:provider:_gt_pools_ranked", "ok"),
        ("v4_multihop:provider:_gt_pools_ranked", "ok")]


def test_a_rung_record_carries_http_beside_calls_and_calls_stays_rpc_only():
    from sniper.execution import evm_real_seams as s
    rec = s.rung_record(name="relay", hit=True, run_id="r", skipped=False, unavailable=False,
                        blind_reason=None, started_at=None, duration_ms=12.0, reason=None,
                        round_trip_pct=-0.3, calls=None,
                        http=[{"label": "relay:quote", "host": "https://api.relay.link",
                               "started_at": 1.0, "rtt_ms": 900.0, "outcome": "http_200"}])
    assert "calls" not in rec and rec["http"][0]["label"] == "relay:quote"
    rec = s.rung_record(name="v4", hit=False, run_id="r", skipped=False, unavailable=False,
                        blind_reason=None, started_at=None, duration_ms=5.0, reason=None,
                        round_trip_pct=None, calls=[{"method": "eth_call"}], http=None)
    assert "http" not in rec and rec["calls"] == [{"method": "eth_call"}]


def test_the_real_ladder_puts_a_rungs_provider_calls_on_its_record(monkeypatch):
    """End to end through `venue_quote_with_v2_fallback`: a rung that makes a tapped provider call
    carries it under `http`, beside (never inside) its RPC `calls`."""
    from test_memecoin_evm_quote_miss_attribution import QUOTE, WETH, MINT
    from sniper.execution import evm_real_seams as seams
    monkeypatch.setattr(w, "_rpc_impl",
                        lambda url, method, params, http=None: {"result": "0x" + "0" * 64})
    rungs = []

    def v3_fn(sell, buy, amount, slippage_bps=None, **kw):
        seams._rpc("https://rpc.4663", "eth_call", [{"to": "0xpool", "data": "0xfeed"}, "latest"])
        with w.http_tap("probe:quote", "https://api.example.com/v1/quote?key=SECRET1234567890") as t:
            t["outcome"] = "http_200"
        return QUOTE

    seams.venue_quote_with_v2_fallback(
        v3_fn, chain="4663", quote_token=WETH, taker="0xtaker", acct=object(),
        rpc_url="https://rpc.4663", on_rung=rungs.append)(WETH, MINT, 10 ** 18)
    [v3] = [r for r in rungs if r["rung"] == "v3"]
    assert v3["calls"][0]["params"] == [{"to": "0xpool", "data": "0xfeed"}, "latest"]
    assert all("label" not in c for c in v3["calls"])        # calls stays JSON-RPC only
    # The v3 rung quotes twice (forward, then the round-trip reverse), so two records.
    assert {(h["label"], h["host"], h["outcome"]) for h in v3["http"]} == {
        ("probe:quote", "https://api.example.com", "http_200")}
    assert len(v3["http"]) == len(v3["calls"]) == 2
    assert "SECRET" not in json.dumps(rungs)
