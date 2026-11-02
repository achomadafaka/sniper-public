"""A v3 or Pons read that FAILED is not "this coin has no route".

The route ladder already reports a blind rung before `no_route` (`classify_quote_miss`), and `_run`
already turns a raised exception's `failure_type` into `blind_reason`. Three of six rungs could not
feed it. v2 was fixed in b6945ab2; these are v3 — the PRIMARY rung — and Pons.

Demonstrated before the fix by injecting failures into `_make_call_fn`:
    transport down -> None, no exception
    JSON-RPC -32005 -> None, no exception
    HTTP 429 -> None, no exception
so `blind_reason` stayed None and an RPC outage was published as a TERMINAL `no_route`.

The distinction that has to survive: A REVERT IS A REAL ANSWER. QuoterV2 reverts on a fee tier with
no pool, so typing every error as unreadable would make a coin with genuinely no v3 pool re-quote
forever. And blindness is reported only when NOTHING answered — one tier reverting proves we could
see.
"""
import pytest

from sniper.execution import evm_dex_quote as D
from sniper.execution import evm_pons_v2_quote as P


# --- the classifier ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ({"result": "0x01"}, None),
    ({"error": {"message": "execution reverted"}}, "revert"),
    ({"error": {"message": "Reverted 0x"}}, "revert"),
    ({"error": {"code": -32005, "message": "limit exceeded"}}, "unavailable"),
    ({"error": {"message": "header not found"}}, "unavailable"),
    ({}, "unavailable"),
    (None, "unavailable"),
])
def test_classify_splits_a_revert_from_an_unreadable_call(payload, expected):
    assert D._classify_rpc_error(payload) == expected


# --- v3 -----------------------------------------------------------------------------------------

RPC = "https://rpc.test.invalid"


@pytest.fixture(autouse=True)
def _rpc_configured(monkeypatch):
    """Tests do not load `.env`, so the settings lookup returns nothing and every call would read
    as `no_rpc_configured` instead of exercising the transport paths under test."""
    monkeypatch.setattr("sniper.settings.memecoin.evm_rpc_for_chain", lambda c: RPC)
    monkeypatch.setattr("sniper.execution.evm_rpc_pool.chain_of", lambda u, cfg=None: "56")
    monkeypatch.setattr("sniper.execution.evm_rpc_pool.pool_for", lambda u, cfg=None: [RPC])
    monkeypatch.setattr("sniper.execution.evm_rpc_pool.choose", lambda u, *, verify_fn=None, candidates=None: RPC)
    monkeypatch.setattr("sniper.execution.evm_rpc_pool.penalize",
                        lambda u, reason="", quarantine_s=None: None)


def _call_fn(chain, http):
    return D._make_call_fn(chain, http)


def test_v3_records_a_transport_failure_as_unavailable():
    def dead(method, url, body=None, **kw):
        raise ConnectionError("endpoint down")
    cf = _call_fn("56", dead)
    assert cf("0xdead", "0xbeef") is None
    assert cf.read_state["answered"] is False
    assert cf.read_state["unavailable"][0] == "rpc_transport"


def test_v3_records_a_json_rpc_error_as_unavailable():
    def err(method, url, body=None, **kw):
        return 200, {"error": {"code": -32005, "message": "limit exceeded"}}, None
    cf = _call_fn("56", err)
    assert cf("0xdead", "0xbeef") is None
    assert cf.read_state["answered"] is False
    assert cf.read_state["unavailable"][0] == "unavailable"


def test_v3_records_a_revert_as_ANSWERED_so_a_poolless_coin_stays_no_route():
    """The guard against the opposite failure: retrying forever on a coin with no pool."""
    def rev(method, url, body=None, **kw):
        return 200, {"error": {"message": "execution reverted"}}, None
    cf = _call_fn("56", rev)
    assert cf("0xdead", "0xbeef") is None
    assert cf.read_state["answered"] is True
    assert cf.read_state["unavailable"] is None


def test_v3_a_real_result_marks_answered():
    def ok(method, url, body=None, **kw):
        return 200, {"result": "0x" + "00" * 32}, None
    cf = _call_fn("56", ok)
    assert cf("0xdead", "0xbeef") == "0x" + "00" * 32
    assert cf.read_state["answered"] is True


def test_v3_one_answering_tier_beats_a_later_failure():
    """Blindness only when NOTHING answered. One revert proves the node was reachable."""
    seq = [(200, {"error": {"message": "execution reverted"}}, None),
           (200, {"error": {"code": -32005, "message": "limit exceeded"}}, None)]

    def mixed(method, url, body=None, **kw):
        return seq.pop(0) if seq else (200, {"error": {"code": -32005}}, None)
    cf = _call_fn("56", mixed)
    cf("0xa", "0xb")
    cf("0xc", "0xd")
    assert cf.read_state["answered"] is True, "a revert answered; this quote is not blind"


def test_v3_quote_raises_only_when_nothing_answered(monkeypatch):
    """The end-to-end contract: unreadable -> typed raise; reverted -> plain None."""
    def dead(method, url, body=None, **kw):
        raise ConnectionError("down")
    blind = _call_fn("56", dead)
    with pytest.raises(D.V3ReadUnavailable) as exc:
        D.dex_v3_quote("56", "0x" + "11" * 20, "0x" + "22" * 20, 1000, "0x" + "33" * 20,
                       cfg_addr={"quoter": "0x" + "44" * 20, "router": "0x" + "55" * 20},
                       slippage_bps=100, fee_tiers=(500, 3000), connectors=(), call_fn=blind)
    assert exc.value.failure_type == "rpc_transport", "the ladder reads failure_type"


def test_v3_quote_returns_none_when_a_tier_answered_with_a_revert():
    def rev(method, url, body=None, **kw):
        import json
        request = json.loads(body)
        error = {"code": 3, "message": "execution reverted"}
        if isinstance(request, list):
            return 200, [{"jsonrpc": "2.0", "id": item["id"], "error": error}
                         for item in request], None
        return 200, {"jsonrpc": "2.0", "id": request["id"], "error": error}, None
    seeing = _call_fn("56", rev)
    out = D.dex_v3_quote("56", "0x" + "11" * 20, "0x" + "22" * 20, 1000, "0x" + "33" * 20,
                         cfg_addr={"quoter": "0x" + "44" * 20, "router": "0x" + "55" * 20},
                         slippage_bps=100, fee_tiers=(500, 3000), connectors=(), call_fn=seeing)
    assert out is None, "a coin with no v3 pool must stay a real no_route, not retry forever"
    assert seeing.read_state == {"answered": True, "unavailable": None}


def test_a_custom_call_fn_without_read_state_behaves_as_before():
    """Test stubs and other callers pass their own call_fn; they must not start raising."""
    out = D.dex_v3_quote("56", "0x" + "11" * 20, "0x" + "22" * 20, 1000, "0x" + "33" * 20,
                         cfg_addr={"quoter": "0x" + "44" * 20, "router": "0x" + "55" * 20},
                         slippage_bps=100, fee_tiers=(500,), connectors=(),
                         call_fn=lambda to, data: None)
    assert out is None


# --- pons ---------------------------------------------------------------------------------------

def test_pons_raises_on_an_unreadable_call(monkeypatch):
    monkeypatch.setattr("sniper.execution.evm_wallet._rpc",
                        lambda *a, **k: {"error": {"code": -32005, "message": "limit exceeded"}})
    with pytest.raises(P.PonsReadUnavailable) as exc:
        P._make_call_fn("http://x")("0xa", "0xb")
    assert exc.value.failure_type == "rpc_transport"


def test_pons_returns_none_on_a_revert(monkeypatch):
    monkeypatch.setattr("sniper.execution.evm_wallet._rpc",
                        lambda *a, **k: {"error": {"message": "execution reverted"}})
    assert P._make_call_fn("http://x")("0xa", "0xb") is None


def test_pons_returns_the_result_on_success(monkeypatch):
    monkeypatch.setattr("sniper.execution.evm_wallet._rpc", lambda *a, **k: {"result": "0xabc"})
    assert P._make_call_fn("http://x")("0xa", "0xb") == "0xabc"


# --- the docstring alibi --------------------------------------------------------------------

def test_the_fail_closed_alibi_is_gone():
    """v3's docstring stated the conflation as intent, and that sentence was the bug's alibi."""
    import inspect
    src = inspect.getsource(D._make_call_fn)
    assert "a reverted quoter tier is 'no pool here'" not in src, "the alibi must not survive"
    assert "Fail-closed" in src and "is a claim" in src, \
        "say what fail-closed would actually have been, and that no_route is a claim"
