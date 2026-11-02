"""Every quote-price warmer is visible in the heartbeat, per chain (2026-10-15, Alpha).

MEMECOIN_EVM_QUOTE_PRICE_WARMER_CHAINS=56,4663 went live at 14:03:50Z, but the heartbeat only showed
56's warmer (fastlane.bsc_price_preparation), so 4663's could not be seen. `quote_price_warmers`
publishes each configured chain's status -- ready, last_error, thread_alive and the age of its
freshest price -- read from the memo without ever reading a price."""
import json

import pytest

from sniper.execution import evm_real_seams as S
from sniper.services import memecoin_real_runner as R
from sniper.services import memecoin_runner as MR


class _Peek:
    def __init__(self, status=None, error=None):
        self._status, self._error = status, error

    def __call__(self):
        pytest.fail("the heartbeat must never read a price")

    def status(self):
        if self._error is not None:
            raise self._error
        return dict(self._status)


def test_each_configured_chain_reports_its_own_warmer():
    contexts = {"56": {"quote_price_peek_fn": _Peek({"ready": True, "last_error": None,
                                                      "thread_alive": True, "age_s": 0.4})},
                "4663": {"quote_price_peek_fn": _Peek({"ready": False, "last_error": "unavailable",
                                                        "thread_alive": True, "age_s": 3.2})}}
    got = R.quote_price_warmer_status(contexts, {"56", "4663"})
    assert got == {"4663": {"ready": False, "last_error": "unavailable", "thread_alive": True,
                            "age_s": 3.2},
                   "56": {"ready": True, "last_error": None, "thread_alive": True, "age_s": 0.4}}


@pytest.mark.parametrize("ctx", [None, {}, {"quote_price_peek_fn": object()}])
def test_a_chain_without_a_status_reads_unavailable(ctx):
    contexts = {} if ctx is None else {"4663": ctx}
    assert R.quote_price_warmer_status(contexts, {"4663"}) == {"4663": {
        "ready": None, "last_error": "unavailable", "thread_alive": None, "age_s": None}}


def test_a_status_that_raises_names_the_class_only():
    got = R.quote_price_warmer_status(
        {"4663": {"quote_price_peek_fn": _Peek(error=OSError("private provider details"))}}, {"4663"})
    assert got["4663"]["last_error"] == "OSError" and "private" not in json.dumps(got)


def test_the_memo_status_carries_the_age_of_its_freshest_price():
    from eth_account import Account
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
                                   http=lambda *a, **k: (200, {"result": "0x0"}, None),
                                   price_fn=lambda: 2655.73)
    status = ex["quote_price_peek_fn"].status
    assert status()["age_s"] is None                              # no price yet
    ex["warm_quote_price_fn"]()
    got = status()
    assert got["ready"] is True and 0 <= got["age_s"] < 1.0


def test_the_heartbeat_publishes_the_key(tmp_path):
    path = tmp_path / "hb.json"
    MR.write_heartbeat({}, True, path=path, quote_price_warmers={
        "4663": {"ready": True, "last_error": None, "thread_alive": True, "age_s": 0.3}})
    assert json.loads(path.read_text())["quote_price_warmers"] == {
        "4663": {"ready": True, "last_error": None, "thread_alive": True, "age_s": 0.3}}
    MR.write_heartbeat({}, True, path=path)
    assert "quote_price_warmers" not in json.loads(path.read_text())


def test_the_runner_loop_passes_the_contexts_and_the_status_reads_the_knob(monkeypatch):
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts/memecoin_runner_loop.py").read_text()
    assert "quote_price_warmers=(_quote_price_warmer_status(contexts) if contexts else None)" in src
    monkeypatch.setenv("MEMECOIN_EVM_QUOTE_PRICE_WARMER_CHAINS", "56,4663")
    assert set(R.quote_price_warmer_status({})) == {"56", "4663"}


@pytest.mark.parametrize("where", ["context_for", "chains"])
def test_the_status_never_raises_into_the_heartbeat_argument_list(monkeypatch, where):
    """Alpha follow-up: the call is evaluated as a write_heartbeat argument, outside that
    function's guard, so a raise would skip the whole heartbeat."""
    if where == "context_for":
        def boom(*_a, **_k):
            raise KeyError("no context table")
        monkeypatch.setattr(R, "context_for", boom)
        chains = {"4663"}
    else:
        class Chains:
            def __iter__(self):
                raise RuntimeError("bad knob")
        chains = Chains()
    got = R.quote_price_warmer_status({"4663": {}}, chains)
    assert got == {"_error": {"last_error": "KeyError" if where == "context_for" else "RuntimeError"}}


@pytest.mark.parametrize("selected_at, prepared, want", [
    (4.0, (7.0, 3000.0), 3.0),        # the warmer's preparation is fresher: max, not min (6.0)
    (8.0, (2.0, 3000.0), 2.0),        # the live selection is fresher
    (None, (9.5, 3000.0), 0.5),
    (9.0, None, 1.0),
    (None, None, None),
])
def test_age_is_measured_from_the_fresher_price(selected_at, prepared, want):
    assert S.freshest_price_age_s(10.0, selected_at, prepared) == want


@pytest.mark.parametrize("value", [
    {"_error": {"last_error": "KeyError"}},                 # the helper's own error shape
    {"4663": "not-a-dict", "56": None},                     # a value write_heartbeat must not dict()
    "not-a-mapping",
])
def test_an_error_shape_still_writes_the_heartbeat_with_every_other_key(tmp_path, value):
    """Gamma HOLD on 748bffd23: the error shape reached write_heartbeat's dict(v) outside its try."""
    path = tmp_path / "hb.json"
    MR.write_heartbeat({"events": 3}, True, path=path, quote_price_warmers=value,
                       gate_balance_warm={"passes": 1})
    hb = json.loads(path.read_text())
    assert hb["events"] == 3 and hb["auth_ok"] is True and hb["gate_balance_warm"] == {"passes": 1}
    qpw = hb["quote_price_warmers"]
    assert all(isinstance(v, dict) for v in qpw.values()), qpw
    if value == {"_error": {"last_error": "KeyError"}}:
        assert qpw == {"_error": {"last_error": "KeyError"}}


def test_the_helper_error_flows_through_write_heartbeat(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise KeyError("no context table")
    monkeypatch.setattr(R, "context_for", boom)
    path = tmp_path / "hb.json"
    MR.write_heartbeat({}, True, path=path, quote_price_warmers=R.quote_price_warmer_status({"4663": {}}, {"4663"}))
    assert json.loads(path.read_text())["quote_price_warmers"] == {"_error": {"last_error": "KeyError"}}
