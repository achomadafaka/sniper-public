"""The exotic warmer names WHY a refresh failed (2026-10-17, Alpha; Codex speed audit item 3).

Live: 3 seeded candidates, 1,368 failed refreshes, 0 successful, last_error=ValueError. refresh_once
kept only the exception class, so quote fetch, quote identity, ALT validation, signer identity and
instruction build all read the same. Now each failure has a typed stage, a non-secret category (the
HTTP status and provider error code, or the first missing/invalid field), the quote mint and the exact
clip, in the heartbeat's stats and one WARNING per distinct (stage, category) per minute. No validation
is relaxed: _valid_quote is the same predicate (pinned against every mutation below)."""
import copy
import json
import logging
from pathlib import Path

import pytest

from sniper.execution import pump_curve_buy as P
from sniper.execution import raydium_launchlab_buy as LL
from test_slot_lane_exotic_warm_leg import _real_warm

FIXTURE = json.loads((Path(__file__).parent / "fixtures/memecoin/jupiter_quote_exotic_leg.json").read_text())


def _quote(mint, pool):
    text = json.dumps(FIXTURE["ok"]).replace("__QUOTE_MINT__", mint).replace("__POOL__", pool)
    return json.loads(text)


def _run(monkeypatch, *, quote_over=None, get_status=200, get_body=None, signer=None, drop=()):
    from test_slot_lane_pump_nonsol import SPCX
    good = _quote(SPCX["quote_mint"], SPCX["pool"])
    good.update(quote_over or {})
    for key in drop:
        good.pop(key)
    warm, b, clock, calls, facts = _real_warm(monkeypatch, signer=signer)
    real = P._funded_jup_call

    def jup(method, path, body=None, *, http=None):
        if method == "GET":
            return (get_status, get_body) if get_status != 200 else (200, good)
        return real(method, path, body, http=http)
    monkeypatch.setattr(P, "_funded_jup_call", jup)
    warm.refresh_once()
    return warm, SPCX


def test_the_saved_fixture_quote_refreshes_with_no_failure(monkeypatch):
    warm, _ = _run(monkeypatch)
    assert warm.stats["refreshed"] == 1 and warm.stats["refresh_failed"] == 0
    assert warm.stats["last_failure"] is None and warm.stats["failure_stages"] == {}


@pytest.mark.parametrize("over, drop, category", [
    ({"slippageBps": 50}, (), "invalid:slippageBps"),
    ({"inAmount": "10000001"}, (), "invalid:inAmount_exact"),
    ({"swapMode": "ExactOut"}, (), "invalid:swapMode"),
    ({"otherAmountThreshold": "101"}, (), "invalid:amounts"),
    ({"routePlan": []}, (), "invalid:routePlan"),
    ({"outAmount": 1.5}, (), "invalid:outAmount"),
    ({}, ("otherAmountThreshold",), "missing:otherAmountThreshold"),
    ({}, ("swapMode",), "missing:swapMode"),
])
def test_a_quote_the_leg_refuses_names_its_field(monkeypatch, over, drop, category):
    warm, spcx = _run(monkeypatch, quote_over=over, drop=drop)
    assert warm.stats["refreshed"] == 0 and warm.stats["refresh_failed"] == 1
    failure = warm.stats["last_failure"]
    assert (failure["stage"], failure["category"]) == ("quote_identity", category)
    assert failure["quote_mint"] == spcx["quote_mint"] and failure["clip_atoms"] == 10_000_000
    assert failure["http_status"] == 200
    assert warm.stats["failure_stages"] == {"quote_identity:" + category: 1}


def test_a_refused_quote_request_is_a_fetch_failure_with_its_status_and_code(monkeypatch):
    warm, _ = _run(monkeypatch, get_status=400, get_body=FIXTURE["no_route"])
    failure = warm.stats["last_failure"]
    assert (failure["stage"], failure["category"], failure["http_status"]) == (
        "quote_fetch", "http_400:COULD_NOT_FIND_ANY_ROUTE", 400)


def test_a_foreign_signer_is_named(monkeypatch):
    warm, _ = _run(monkeypatch, signer=P.USDC_MINT)
    assert (warm.stats["last_failure"]["stage"], warm.stats["last_failure"]["category"]) == (
        "signer_identity", "warm_signer_identity")


def test_a_refused_quote_token_safety_is_named(monkeypatch):
    from test_slot_lane_pump_nonsol import SPCX
    good = _quote(SPCX["quote_mint"], SPCX["pool"])
    warm, b, clock, calls, facts = _real_warm(monkeypatch, quote_over=good, account={"owner": "nobody"})
    warm.refresh_once()
    assert warm.stats["last_failure"]["stage"] == "quote_safety"
    assert not [c for c in calls if c[0] == "GET"], "a refused quote token never asks for a quote"


def test_one_warning_per_distinct_failure_per_minute(monkeypatch, caplog):
    warm, _ = _run(monkeypatch, quote_over={"slippageBps": 50})
    caplog.set_level(logging.WARNING)
    caplog.clear()                                   # the refresh above already warned once
    for _ in range(3):
        warm._note_failure("quote_identity", "invalid:slippageBps", 200, "Q", 1)
    warm._note_failure("quote_fetch", "http_429", 429, "Q", 1)
    lines = [r.getMessage() for r in caplog.records if "exotic warm refresh failed" in r.getMessage()]
    assert len(lines) == 1 and "quote_fetch" in lines[0] and "http_429" in lines[0], lines
    warm.clock.t += 61
    warm._note_failure("quote_identity", "invalid:slippageBps", 200, "Q", 1)
    assert len([r for r in caplog.records if "exotic warm refresh failed" in r.getMessage()]) == 2


def test_the_identity_check_is_the_same_predicate():
    mint, pool = "QuoteMint1111", "Pool1111"
    base = _quote(mint, pool)
    cases = [base, None, [], {**base, "inputMint": "x"}, {**base, "outputMint": "x"},
             {**base, "inAmount": "1"}, {**base, "inAmount": "-1"}, {**base, "slippageBps": "150"},
             {**base, "slippageBps": True}, {**base, "otherAmountThreshold": "0"},
             {**base, "outAmount": str(2 ** 64)}, {**base, "routePlan": [{"swapInfo": {"label": "x"}}]},
             {**base, "routePlan": [1]}, {k: v for k, v in base.items() if k != "inputMint"}]
    for quote in cases:
        failure = LL.WarmExoticLegs._quote_identity_failure(copy.deepcopy(quote), mint, 10_000_000)
        assert LL.WarmExoticLegs._valid_quote(copy.deepcopy(quote), mint, 10_000_000) is (failure is None), (
            quote, failure)
    assert LL.WarmExoticLegs._quote_identity_failure(base, mint, 10_000_000) is None


def test_the_heartbeat_carries_the_typed_failure_and_nothing_else(tmp_path):
    from sniper.services import memecoin_runner as run
    path = tmp_path / "heartbeat.json"
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={"exotic_warm": dict(
        refresh_failed=3, last_error="ValueError",
        last_failure=dict(stage="quote_identity", category="invalid:slippageBps", http_status=200,
                          quote_mint="SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb", clip_atoms=10_000_000,
                          url="https://hidden"),
        failure_stages={"quote_identity:invalid:slippageBps": 3, "https://hidden": 1,
                        "quote_fetch:http_400:COULD_NOT_FIND_ANY_ROUTE": 2})})
    warm = json.loads(path.read_text())["slot_lane"]["exotic_warm"]
    assert warm["last_failure"] == dict(stage="quote_identity", category="invalid:slippageBps",
                                        http_status=200,
                                        quote_mint="SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb",
                                        clip_atoms=10_000_000)
    assert warm["failure_stages"] == {"quote_identity:invalid:slippageBps": 3,
                                      "quote_fetch:http_400:COULD_NOT_FIND_ANY_ROUTE": 2}
    assert "hidden" not in path.read_text()
