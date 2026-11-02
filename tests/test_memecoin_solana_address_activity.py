"""S2 helper: one page of mint-address signatures as a capped ACTIVITY count (operator spec
docs/superpowers/specs/2026-09-17-solana-hourly-floor-activity-design.md). Counts never become dollars.
One call, a 600 ms monotonic total deadline, one request at a time, 1 request per second with no wait,
a 120 s cooldown after a 429, no retry, and a late response changes nothing."""
import threading
import time

import pytest

from sniper.services import memecoin_solana_address_activity as A

SRC = "5" * 88
T = 1_758_000_000


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _sig(i):
    head = _B58[i // 58 // 58 % 58] + _B58[i // 58 % 58] + _B58[i % 58]
    return head + "B" * 85


def _entry(i, *, bt=T - 60, err=None, status="confirmed", slot=100, sig=None, drop=()):
    e = {"signature": sig or _sig(i), "slot": slot, "err": err, "blockTime": bt, "confirmationStatus": status}
    for k in drop:
        e.pop(k)
    return e


def test_count_page_counts_only_confirmed_successful_in_window_timestamped_non_source_rows():
    page = [
        _entry(1), _entry(2, status="finalized"),
        _entry(3, err={"InstructionError": [0, "x"]}),       # failed
        _entry(4, bt=None),                                   # missing time
        _entry(5, bt=T - 3601),                               # before the hour
        _entry(6, bt=T),                                      # at T: outside [T-3600, T)
        _entry(7, status="processed"),                        # not confirmed
        _entry(8, drop=("err",)),                             # err field absent
        _entry(9, slot=None),                                 # invalid slot
        _entry(10, sig="not-a-signature"),                    # invalid signature
        _entry(11, sig=SRC),                                  # the source itself
        _entry(12, bt=T - 3600),                              # window start is inside
    ]
    c = A.count_page(page, decision_ts=T, source_signature=SRC)
    assert c["valid_signature_count"] == 3
    assert (c["failed_count"], c["missing_time_count"], c["excluded_source_count"]) == (1, 1, 1)
    assert c["returned_count"] == 12 and c["page_saturated"] is False
    assert c["oldest_block_time"] == T - 3601


def test_a_full_page_is_saturated_and_its_count_is_only_a_lower_bound():
    c = A.count_page([_entry(i) for i in range(1000)], decision_ts=T, source_signature=SRC)
    assert (c["valid_signature_count"], c["page_saturated"]) == (1000, True)


@pytest.mark.parametrize("fid,sig", [
    ("swap:raybot:%s:w:m" % SRC, SRC), ("onchain:%s" % SRC, SRC), ("pump:%s:m:buy:w" % SRC, SRC),
    ("swap:3ac25ded-177d-4bac-af84-45a07e00bbbc", None), ("swap:raybot:short:w:m", None), (None, None)])
def test_source_signature_accepts_only_validated_formats(fid, sig):
    assert A.source_signature(fid) == sig


def _env(**kw):
    return {"MEMECOIN_SOLANA_RPC_URL": "https://custody.example/key1",
            "MEMECOIN_SOLANA_READ_RPC_URL": "https://api.mainnet-beta.solana.com", **kw}


@pytest.mark.parametrize("url,cutoff,reason", [
    ("", "", "no_endpoint"),
    ("https://api.mainnet-beta.solana.com", "", "public_endpoint"),
    ("https://custody.example/key1", "", "shared_endpoint:MEMECOIN_SOLANA_RPC_URL"),
    ("https://research.example/k", "0", "invalid_cutoff"),
    ("https://research.example/k", "1001", "invalid_cutoff"),
    ("https://research.example/k", "12.5", "invalid_cutoff"),
])
def test_invalid_configuration_disables_the_probe_and_never_raises(url, cutoff, reason):
    cfg = A.load_config(url, cutoff, _env())
    assert cfg.disabled_reason == reason and cfg.enabled is False
    assert "research.example" not in cfg.disabled_reason and "custody" not in cfg.disabled_reason


def test_an_unset_cutoff_keeps_the_probe_for_evidence_only():
    cfg = A.load_config("https://research.example/k", "", _env())
    assert (cfg.enabled, cfg.cutoff) == (True, None)
    assert A.load_config("https://research.example/k", "400", _env()).cutoff == 400


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _probe(post, *, cutoff=10, deadline_s=0.6, clock=None):
    return A.ActivityProbe("https://research.example/k", cutoff, post=post, clock=clock or time.monotonic,
                           deadline_s=deadline_s)


def test_one_call_one_page_no_pagination_and_the_cutoff_decides_the_exception():
    calls = []

    def post(url, body, timeout):
        calls.append(body)
        return 200, {"result": [_entry(i) for i in range(1000)]}
    out = _probe(post, cutoff=400).probe("Mint111", decision_ts=T, source_signature=SRC)
    assert len(calls) == 1
    params = calls[0]["params"]
    assert calls[0]["method"] == "getSignaturesForAddress"
    assert params == ["Mint111", {"limit": 1000, "commitment": "confirmed", "before": SRC}]
    assert (out["result_type"], out["exception_used"], out["call_count"]) == ("exception", True, 1)
    assert out["valid_signature_count"] == 1000 and out["page_saturated"] is True


def test_below_cutoff_and_unset_cutoff_never_grant_the_exception():
    post = lambda url, body, timeout: (200, {"result": [_entry(i) for i in range(5)]})
    assert _probe(post, cutoff=10).probe("M", decision_ts=T, source_signature=SRC)["result_type"] == "below_cutoff"
    many = lambda url, body, timeout: (200, {"result": [_entry(i) for i in range(900)]})
    out = _probe(many, cutoff=None).probe("M", decision_ts=T, source_signature=SRC)
    assert (out["result_type"], out["exception_used"], out["valid_signature_count"]) == ("no_cutoff", False, 900)


def test_no_source_signature_makes_no_call():
    out = _probe(lambda *a: pytest.fail("no call without an anchor")).probe("M", decision_ts=T, source_signature=None)
    assert (out["result_type"], out["budget_state"], out["call_count"]) == ("unknown", "no_source_signature", 0)


def test_an_error_is_unknown_with_one_call_and_no_retry():
    calls = []

    def post(url, body, timeout):
        calls.append(1)
        raise OSError("reset")
    out = _probe(post).probe("M", decision_ts=T, source_signature=SRC)
    assert (out["result_type"], out["call_count"], len(calls)) == ("unknown", 1, 1)


@pytest.mark.parametrize("answer", [(503, {}), (200, {"error": {"code": -32005}}), (200, {"result": "x"})])
def test_a_bad_status_error_body_or_malformed_page_is_unknown(answer):
    out = _probe(lambda *a: answer).probe("M", decision_ts=T, source_signature=SRC)
    assert out["result_type"] == "unknown"


def test_the_total_deadline_returns_unknown_and_a_late_response_changes_nothing():
    release = threading.Event()
    done = threading.Event()

    def post(url, body, timeout):
        release.wait(2)
        done.set()
        return 200, {"result": [_entry(i) for i in range(1000)]}
    clock = time.monotonic
    probe = _probe(post, cutoff=1, deadline_s=0.2, clock=clock)
    t0 = time.monotonic()
    out = probe.probe("M", decision_ts=T, source_signature=SRC)
    elapsed = time.monotonic() - t0
    assert (out["result_type"], out["budget_state"], out["exception_used"]) == ("unknown", "timeout", False)
    assert 0.15 <= elapsed < 0.4
    # The slow request keeps the only slot until it ends.
    assert probe.probe("M", decision_ts=T, source_signature=SRC)["budget_state"] in ("busy", "rate_limited")
    release.set()
    done.wait(2)
    time.sleep(0.05)
    assert out["result_type"] == "unknown"                     # the late page did not rewrite the result


def test_one_request_per_second_without_waiting_and_one_at_a_time():
    clock = _Clock()
    post = lambda *a: (200, {"result": []})
    probe = _probe(post, clock=clock)
    assert probe.probe("M", decision_ts=T, source_signature=SRC)["call_count"] == 1
    clock.t += 0.5
    t0 = time.monotonic()
    second = probe.probe("M", decision_ts=T, source_signature=SRC)
    assert (second["budget_state"], second["call_count"]) == ("rate_limited", 0)
    assert time.monotonic() - t0 < 0.05                         # no wait for a token
    clock.t += 0.6
    assert probe.probe("M", decision_ts=T, source_signature=SRC)["call_count"] == 1


def test_a_429_starts_a_120_second_cooldown_with_no_calls():
    clock = _Clock()
    answers = [(429, {})]
    calls = []

    def post(*a):
        calls.append(1)
        return answers.pop(0) if answers else (200, {"result": []})
    probe = _probe(post, clock=clock)
    assert probe.probe("M", decision_ts=T, source_signature=SRC)["result_type"] == "unknown"
    clock.t += 30
    blocked = probe.probe("M", decision_ts=T, source_signature=SRC)
    assert (blocked["budget_state"], blocked["call_count"], len(calls)) == ("cooldown", 0, 1)
    clock.t += 91
    assert probe.probe("M", decision_ts=T, source_signature=SRC)["call_count"] == 1


def test_the_default_controls_are_the_spec_values():
    assert (A.DEADLINE_S, A.MIN_INTERVAL_S, A.COOLDOWN_S, A.PAGE_LIMIT) == (0.6, 1.0, 120.0, 1000)


@pytest.mark.parametrize("page_doc", [
    None, [], {}, {"response": []}, {"response": {"error": {"code": -32005}}},
    {"response": {"error": {"code": -32005}, "result": []}},
    {"response": {"result": "bad"}}, {"response": {"result": None}},
])
def test_cutoff_study_preserves_unknown_rpc_evidence(page_doc):
    from scripts.research.solana_hourly_activity_cutoff_study import count_item
    item = {"kind": "position", "position_id": 1, "source_signature": SRC,
            "decision_at": "2026-09-17T10:00:00+00:00"}
    result = count_item(item, page_doc)
    assert result["count_state"] == "unknown"
    assert "valid_signature_count" not in result


def test_cutoff_study_counts_a_successful_empty_page_as_zero():
    """A successful empty page is zero ONLY when the saved request proves what was asked
    (inherited review finding W2, closed 2026-09-23); the same page with no request is unknown."""
    from scripts.research.solana_hourly_activity_cutoff_study import count_item
    item = {"kind": "position", "position_id": 1, "source_signature": SRC, "mint": "MINT",
            "decision_at": "2026-09-17T10:00:00+00:00"}
    proven = {"response": {"result": []}, "rtt_ms": 10, "fetched_at": "2026-09-17T10:01:00+00:00",
              "request": {"method": "getSignaturesForAddress",
                          "params": ["MINT", {"before": SRC, "limit": 1000,
                                              "commitment": "confirmed"}]}}
    result = count_item(item, proven)
    assert result["count_state"] == "counted"
    assert result["valid_signature_count"] == 0
    bare = count_item(item, {"response": {"result": []}, "rtt_ms": 10})
    assert bare["count_state"] == "unknown" and bare["unknown_reason"] == "request_unverified"
