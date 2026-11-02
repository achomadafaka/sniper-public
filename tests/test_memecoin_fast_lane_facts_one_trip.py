"""BSC cut 4 (Alpha, 2026-10-14): the lane facts in ONE round trip.

Gamma's 56 baseline since 10-12: cold inline inputs n=42, p50 207 / p90 265 ms, floor 178 ms on every
row, while a cold DexScreener GET can take 41 ms. `_inline_inputs` waits for max(stats, facts), and
lane_facts read decimals, THEN totalSupply: two sequential requests on the lane host. Now one
JSON-RPC batch [decimals, totalSupply] on the same host, the same values, paired by id, never by
position. Fail closed as today: an error item, a missing or extra id, a 429 or a malformed answer
raises. Only a host that answers "batch not supported" (the evm_dex_quote rule) gets today's two
single calls. The 4663 lane's facts (the shared gate) read the same way and keep their own semantics:
any batch failure is today's exact code. The trace gains facts_ms and stats_ms beside
inline_inputs_path_ms, so the post-deploy read names the long pole.
"""
import json

import pytest

from sniper.services import memecoin_bsc_fast_lane as bsc

MINT = "0x" + "ab" * 18 + "7777"
DEC, SUPPLY = "0x313ce567", "0x18160ddd"


class Host:
    """The lane's http seam on one URL; `answer(body)` builds each reply; every request is kept."""

    def __init__(self, answer, status=200):
        self.answer, self.status, self.requests = answer, status, []

    def __call__(self, method, target, headers=None, body=None, timeout=10):
        req = json.loads(body)
        self.requests.append(req)
        return self.status, self.answer(req), {}


def _item(req, value=None, error=None):
    out = {"jsonrpc": "2.0", "id": req["id"]}
    out.update({"error": error} if error else {"result": value})
    return out


def _chain(decimals="0x12", supply=hex(10 ** 27), *, decimals_error=None, supply_error=None,
           reverse=False, drop=None, extra=False):
    def answer(req):
        if isinstance(req, dict):                    # a single call (today's shape, or the fallback)
            data = req["params"][0]["data"]
            if data == DEC:
                return _item(req, decimals, decimals_error)
            return _item(req, supply, supply_error)
        replies = []
        for r in req:
            data = r["params"][0]["data"]
            if data == drop:
                continue
            replies.append(_item(r, decimals, decimals_error) if data == DEC
                           else _item(r, supply, supply_error))
        if extra:
            replies.append({"jsonrpc": "2.0", "id": 99, "result": "0x0"})
        return list(reversed(replies)) if reverse else replies
    return answer


def _facts(host):
    return bsc.lane_facts(bsc.LaneHost("https://lane", host))(MINT)


def test_the_facts_are_one_request_with_both_calls():
    host = Host(_chain())
    assert _facts(host) == (18, 10 ** 27)
    assert len(host.requests) == 1, "the facts went back to %d round trips" % len(host.requests)
    assert [r["params"][0]["data"] for r in host.requests[0]] == [DEC, SUPPLY]


def test_replies_pair_by_id_not_by_position():
    assert _facts(Host(_chain(reverse=True))) == (18, 10 ** 27)


@pytest.mark.parametrize("shape", [
    dict(decimals_error={"code": 3, "message": "execution reverted"}),   # a non-standard token
    dict(supply_error={"code": -32000, "message": "header not found"}),  # decimals ok, supply errors
    dict(drop=SUPPLY),                                                    # a partial answer
    dict(extra=True),                                                     # an id we never sent
    dict(decimals="0x"),                                                  # decimals unreadable
])
def test_any_failed_or_partial_answer_raises_as_today(shape):
    with pytest.raises((RuntimeError, ValueError)):
        _facts(Host(_chain(**shape)))


def test_a_zero_supply_is_unknown_as_today():
    assert _facts(Host(_chain(supply="0x0"))) == (18, None)


@pytest.mark.parametrize("status, body", [
    (429, {"jsonrpc": "2.0", "id": None, "error": {"code": -32005, "message": "Too Many Requests"}}),
    (200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal error"}}),
    (200, "not json-rpc"),
])
def test_a_refused_or_malformed_batch_raises_and_is_not_retried(status, body):
    host = Host(lambda req: body, status=status)
    with pytest.raises(RuntimeError):
        _facts(host)
    assert len(host.requests) == 1


def test_only_an_explicit_batch_unsupported_answer_falls_back_to_todays_two_calls():
    unsupported = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32600, "message": "Batch requests are not supported"}}
    single = _chain()
    host = Host(lambda req: unsupported if isinstance(req, list) else single(req))
    assert _facts(host) == (18, 10 ** 27)
    assert [type(r) for r in host.requests] == [list, dict, dict]


def test_the_background_budget_counts_and_paces_the_batch_as_one_call():
    from sniper.services.memecoin_rh_fast_lane import PacedHttp
    slept = []
    paced = PacedHttp(Host(_chain()), min_gap_s=1.0, clock=lambda: 100.0, sleep=slept.append)
    facts = bsc.lane_facts(bsc.LaneHost("https://lane", bsc.lane_only_http(paced, "https://lane")))
    assert facts(MINT) == (18, 10 ** 27)
    assert paced.counts["calls"] == 1 and slept == []       # one paced request, no gap waited


# --- 4663: the shared gate's facts -------------------------------------------------------------

def _rh_facts(monkeypatch, host):
    """The 4663 lane's facts function with its own host and http seam."""
    from sniper.services import memecoin_rh_fast_lane as rh
    return rh.lane_facts_once(MINT, http=host, host="https://inline")


def test_4663_facts_are_one_request_too(monkeypatch):
    host = Host(_chain())
    assert _rh_facts(monkeypatch, host) == (18, 10 ** 27)
    assert len(host.requests) == 1


def test_4663_falls_back_to_todays_reads_on_any_batch_failure(monkeypatch):
    from sniper.execution import evm_wallet
    calls = []
    monkeypatch.setattr(evm_wallet, "erc20_decimals", lambda mint, host, http=None: calls.append("dec") or 9)
    monkeypatch.setattr(evm_wallet, "_rpc", lambda host, method, params, http=None:
                        calls.append("supply") or {"result": hex(5)})
    host = Host(lambda req: {"jsonrpc": "2.0", "error": {"code": -32603, "message": "boom"}})
    assert _rh_facts(monkeypatch, host) == (9, 5)
    assert calls == ["dec", "supply"]                      # today's exact reads, in today's order


def test_4663_keeps_its_unknowns_a_short_supply_is_none(monkeypatch):
    assert _rh_facts(monkeypatch, Host(_chain(supply="0x"))) == (18, None)


def test_4663_a_supply_error_in_the_batch_is_read_again_as_today(monkeypatch):
    from sniper.execution import evm_wallet
    asked = []
    monkeypatch.setattr(evm_wallet, "_rpc", lambda host, method, params, http=None:
                        asked.append(params[0]["data"]) or {"result": hex(7)})
    host = Host(_chain(supply_error={"code": -32000, "message": "header not found"}))
    assert _rh_facts(monkeypatch, host) == (18, 7)
    assert asked == [SUPPLY] and len(host.requests) == 1


# --- Gamma LOWs on 80ad4e078 ---------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["repeat", "extra"])
def test_4663_a_repeated_or_extra_reply_id_is_todays_reads(monkeypatch, bad):
    from sniper.execution import evm_wallet
    calls = []
    monkeypatch.setattr(evm_wallet, "erc20_decimals", lambda mint, host, http=None: calls.append("dec") or 9)
    monkeypatch.setattr(evm_wallet, "_rpc", lambda host, method, params, http=None:
                        calls.append("supply") or {"result": hex(5)})

    def answer(req):
        replies = [_item(r, "0x12" if r["params"][0]["data"] == DEC else hex(10 ** 27)) for r in req]
        return replies + [dict(replies[0], result="0x06" if bad == "repeat" else "0x0",
                               id=1 if bad == "repeat" else 3)]
    assert _rh_facts(monkeypatch, Host(answer)) == (9, 5)
    assert calls == ["dec", "supply"]


def test_a_host_that_refuses_batching_is_remembered_and_never_asked_again():
    unsupported = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32600, "message": "Batch requests are not supported"}}
    single = _chain()
    host = Host(lambda req: unsupported if isinstance(req, list) else single(req))
    facts = bsc.lane_facts(bsc.LaneHost("https://lane", host))
    assert facts(MINT) == facts(MINT) == (18, 10 ** 27)
    assert [type(r) for r in host.requests] == [list, dict, dict, dict, dict]
    other = Host(lambda req: unsupported if isinstance(req, list) else single(req))
    facts.for_http(other)(MINT)                       # a job's seam on the same host remembers it
    assert [type(r) for r in other.requests] == [dict, dict]
