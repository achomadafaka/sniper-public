"""Chain 1 through Flashbots Protect (2026-10-08): the post-send status watch (P0, always on) and
the fee headroom (P1, MEMECOIN_CHAIN1_FEE_HEADROOM, default off). Pending 4945/4947 expired with
Protect simError MaxFeePerGasTooLow while the same wallet's unwraps at a lower maxFee landed."""
import threading
from pathlib import Path

import pytest
from eth_account import Account

from sniper.execution import chain1_protect as c1
import sniper.execution.evm_real_seams as S

GWEI = 10 ** 9


# ---- P0 ----

def _run_watch(answers, **kw):
    rows, pages, polls = [], [], []

    def fetch(h):
        polls.append(h)
        got = answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return got
    t = c1.watch_status("0x" + "ab" * 32, chain="1", action="entry", fetch=fetch, sink=rows.append,
                        alert_fn=pages.append, sleep=lambda s: None, clock=lambda: 1.0, start=False)
    assert t is not None
    t.run()
    return rows, pages, polls


def test_a_simerror_on_a_pending_send_pages_once_and_the_watch_ends_at_terminal():
    pending = {"status": "PENDING", "simError": "MaxFeePerGasTooLow", "isRevert": False,
               "maxBlockNumber": 26144186}
    rows, pages, polls = _run_watch([{"status": "PENDING"}, pending, dict(pending),
                                     dict(pending, status="FAILED"), {"status": "never asked"}])
    assert [r["status"] for r in rows] == ["PENDING", "PENDING", "FAILED"]
    assert rows[1]["sim_error"] == "MaxFeePerGasTooLow" and rows[1]["is_revert"] is False
    assert len(pages) == 1 and "MaxFeePerGasTooLow" in pages[0] and "0x" + "ab" * 32 in pages[0]
    assert len(polls) == 4                                   # stopped at FAILED


def test_an_unreadable_status_is_a_missed_poll_and_an_included_send_never_pages():
    rows, pages, polls = _run_watch([OSError("timeout"), {"status": "INCLUDED"}])
    assert [r["status"] for r in rows] == ["unreadable", "INCLUDED"] and pages == []


def test_a_pending_simulation_error_does_not_predict_final_inclusion():
    rows, pages, _ = _run_watch([
        {"status": "PENDING", "simError": "MaxFeePerGasTooLow", "isRevert": False},
        {"status": "INCLUDED"},
    ])
    assert rows[-1]["status"] == "INCLUDED" and len(pages) == 1
    assert "will not be included" not in pages[0]
    assert "inclusion remains unknown" in pages[0]


def test_one_watch_per_hash_and_a_bounded_number_of_watches(monkeypatch):
    monkeypatch.setattr(c1, "_watching", set())
    h = "0x" + "cd" * 32
    first = c1.watch_status(h, chain="1", action="entry", fetch=lambda _h: {"status": "INCLUDED"},
                            sink=lambda r: None, alert_fn=lambda t: None, start=False)
    assert first is not None
    assert c1.watch_status(h, chain="1", action="entry", start=False) is None      # already watched
    monkeypatch.setattr(c1, "MAX_WATCHERS", 1)
    assert c1.watch_status("0x" + "ef" * 32, chain="1", action="entry", start=False) is None


def test_the_private_relay_accepting_a_chain1_send_starts_its_watch(monkeypatch):
    from sniper.execution import submission_wiring as W
    from sniper.execution.submission_transport import AttemptState
    started = []
    monkeypatch.setattr(c1, "watch_status", lambda h, **k: started.append((h, k["chain"], k["action"])))

    class Result:
        state, message, attempts = AttemptState.ACCEPTED, None, [object()]
    monkeypatch.setattr(W, "guarded_submit", lambda *a, **k: Result())
    monkeypatch.setattr(W, "aggregate_to_seam", lambda r: "0xhash")
    acct = Account.create()
    raw, _h = S.sign_tx({"to": "0x" + "11" * 20, "data": "0x", "value": 0, "gas": 21000,
                         "gasPrice": GWEI, "nonce": 0, "chainId": 1}, acct)
    W.make_evm_send_fn(object(), chain="1", action="entry", owner_kind="reservation",
                       owner_id="1", record=False)(raw)
    assert started and started[0][1:] == ("1", "entry")
    started.clear()
    raw56, _h = S.sign_tx({"to": "0x" + "11" * 20, "data": "0x", "value": 0, "gas": 21000,
                           "gasPrice": GWEI, "nonce": 0, "chainId": 56}, acct)
    W.make_evm_send_fn(object(), chain="56", action="entry", owner_kind="reservation",
                       owner_id="1", record=False)(raw56)
    assert started == []                                     # only the private-only chain


# ---- P1 ----

def _history(base_next_gwei, median_tips_gwei):
    bases = [hex(int(0.1 * GWEI))] * len(median_tips_gwei) + [hex(int(base_next_gwei * GWEI))]
    return {"baseFeePerGas": bases, "reward": [[hex(int(t * GWEI))] for t in median_tips_gwei]}


@pytest.fixture
def headroom(monkeypatch):
    monkeypatch.setenv("MEMECOIN_CHAIN1_FEE_HEADROOM", "1")
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", "0.05")
    monkeypatch.setenv("MEMECOIN_EVM_TIP_CAP_GWEI_1", "2")
    monkeypatch.setenv("MEMECOIN_EVM_GAS_ENTRY_NATIVE_1", "0.001")
    monkeypatch.setenv("MEMECOIN_EVM_GAS_EXIT_NATIVE_1", "0.001")
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {"1": 0.001})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN", {"1": 0.001})
    now = [100.0]
    state = {"hist": _history(0.18, [0.05, 0.06, 0.27, 0.07, 0.1])}
    h = c1.FeeHeadroom(lambda: state["hist"], clock=lambda: now[0])
    monkeypatch.setattr(c1, "_HEADROOM", {"1": h})
    h.refresh()
    return h, now, state


def _decode(raw):
    import rlp
    b = bytes.fromhex(raw[2:])
    assert b[0] == 2
    f = rlp.decode(b[1:])
    ii = lambda x: int.from_bytes(x, "big")
    return {"tip": ii(f[2]), "max_fee": ii(f[3]), "gas": ii(f[4]), "value": ii(f[6])}


def _sign(gas, gas_price, value=0, chain=1):
    raw, _h = S.sign_tx({"to": "0x" + "11" * 20, "data": "0x", "value": value, "gas": gas,
                         "gasPrice": gas_price, "nonce": 3, "chainId": chain}, Account.create())
    return _decode(raw)


def test_the_tip_is_the_p75_of_recent_median_tips_floored_and_capped(headroom):
    h, now, state = headroom
    assert h.snapshot() == (int(0.18 * GWEI), int(0.1 * GWEI))        # p75 of 5 medians
    assert c1.dynamic_tip_wei("1", int(0.05 * GWEI)) == int(0.1 * GWEI)
    state["hist"] = _history(0.18, [0.01, 0.01, 0.01, 0.01, 0.01])
    h.refresh()
    assert c1.dynamic_tip_wei("1", int(0.05 * GWEI)) == int(0.05 * GWEI)     # the knob is a floor
    state["hist"] = _history(0.18, [9, 9, 9, 9, 9])
    h.refresh()
    assert c1.dynamic_tip_wei("1", int(0.05 * GWEI)) == 2 * GWEI            # a spike hits the cap
    assert c1.dynamic_tip_wei("56", int(0.05 * GWEI)) is None


def test_a_signed_chain1_swap_gets_headroom_but_never_less_than_today(headroom):
    got = _sign(700_000, int(0.2543 * GWEI), value=19442165390612544)
    assert got["tip"] == int(0.1 * GWEI)
    assert got["max_fee"] == 2 * int(0.18 * GWEI) + int(0.1 * GWEI)    # 0.46 gwei
    assert got["max_fee"] > int(0.2543 * GWEI) and got["tip"] <= got["max_fee"]
    high = _sign(700_000, int(0.9 * GWEI))
    assert high["max_fee"] == int(0.9 * GWEI)                              # never below today's


def test_the_gas_times_max_fee_never_exceeds_the_native_gas_reserve(headroom):
    h, now, state = headroom
    state["hist"] = _history(2.0, [1.5] * 5)                               # a fee spike
    h.refresh()
    got = _sign(1_200_000, int(0.3 * GWEI), value=17394172195708684)
    reserve_wei = int(0.001 * 1e18)
    assert got["gas"] * got["max_fee"] <= reserve_wei                       # 0.833 gwei, not 5.5
    assert got["max_fee"] == reserve_wei // 1_200_000
    assert got["tip"] <= got["max_fee"]


def test_stale_or_absent_fee_data_or_the_knob_off_signs_todays_bytes(headroom, monkeypatch):
    h, now, state = headroom
    today = dict(tip=int(0.05 * GWEI), max_fee=int(0.2543 * GWEI))
    now[0] += 31
    got = _sign(700_000, int(0.2543 * GWEI))
    assert (got["tip"], got["max_fee"]) == (today["tip"], today["max_fee"])
    now[0] -= 31
    monkeypatch.setenv("MEMECOIN_CHAIN1_FEE_HEADROOM", "0")
    got = _sign(700_000, int(0.2543 * GWEI))
    assert (got["tip"], got["max_fee"]) == (today["tip"], today["max_fee"])


def test_a_failed_fee_read_keeps_the_last_data_until_it_goes_stale(headroom):
    h, now, state = headroom
    state["hist"] = {"baseFeePerGas": [], "reward": []}
    h.refresh()
    assert h.counts["errors"] == 1 and h.snapshot() is not None
    now[0] += 31
    assert h.snapshot() is None


def test_the_executor_starts_one_refresher_and_its_gas_price_carries_the_dynamic_tip(monkeypatch):
    monkeypatch.setenv("MEMECOIN_CHAIN1_FEE_HEADROOM", "1")
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", "0.05")
    monkeypatch.setattr(c1, "_HEADROOM", {})
    started = []
    monkeypatch.setattr(c1, "start_refresher", lambda h, **k: started.append(h))
    import json as _json

    def http(method, url, headers=None, body=None, timeout=10):
        req = _json.loads(body)
        if req["method"] == "eth_feeHistory":
            return 200, {"result": _history(0.18, [0.05, 0.06, 0.27, 0.07, 0.1])}, None
        if req["method"] == "eth_gasPrice":
            return 200, {"result": hex(int(0.2 * GWEI))}, None
        return 200, {"result": "0x0"}, None
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="1", rpc_url="https://rpc",
                                   http=http, gasprice_buffer=1.0)
    S.build_real_evm_executor(acct=Account.create(), chain_id="1", rpc_url="https://rpc",
                              http=http, gasprice_buffer=1.0)
    assert len(started) == 1 and "1" in c1._HEADROOM
    c1._HEADROOM["1"].refresh()
    assert ex["_node_gas_price"]() == int(0.2 * GWEI) + int(0.1 * GWEI)     # raw + the p75 tip


def test_a_simerror_row_keeps_the_whole_protect_answer():
    pending = {"status": "PENDING", "simError": "MaxFeePerGasTooLow", "isRevert": False,
               "transaction": {"maxFeePerGas": "254323421"}, "fastMode": True}
    rows, pages, polls = _run_watch([dict(pending, status="FAILED")])
    assert rows[0]["protect"]["transaction"] == {"maxFeePerGas": "254323421"}
    rows, pages, polls = _run_watch([{"status": "INCLUDED"}])
    assert "protect" not in rows[0]


def test_the_refresher_stops_reading_while_the_knob_is_off():
    reads, on = [], [True, False, False, True]

    class Stop(Exception):
        pass
    h = c1.FeeHeadroom(lambda: reads.append(1) or _history(0.18, [0.1] * 5))
    ticks = iter(range(4))

    def sleep(_s):
        if next(ticks, None) == 3:
            raise Stop()
    t = c1.start_refresher.__wrapped__ if hasattr(c1.start_refresher, "__wrapped__") else None
    flags = iter(on)
    import threading as _t
    real = _t.Thread

    class Inline:
        def __init__(self, target, **k):
            self.target = target

        def start(self):
            try:
                self.target()
            except Stop:
                pass
    c1.threading.Thread = Inline
    try:
        c1.start_refresher(h, sleep=sleep, enabled=lambda: next(flags))
    finally:
        c1.threading.Thread = real
    assert len(reads) == 2                                  # read only while on


# ---- the fee-history error body (operator order 2026-10-08: 166 KeyError 'result' tracebacks) ----

def _error_http(answers):
    """The custody read's http: eth_feeHistory answers from `answers` in turn (an error BODY is what the
    pool returns unchanged for a chain-level JSON-RPC error)."""
    import json as _json

    def http(method, url, headers=None, body=None, timeout=10):
        req = _json.loads(body)
        if req["method"] == "eth_feeHistory":
            return 200, answers.pop(0), None
        if req["method"] == "eth_gasPrice":
            return 200, {"result": hex(int(0.2 * GWEI))}, None
        return 200, {"result": "0x0"}, None
    return http


def test_an_rpc_error_body_is_one_counted_line_not_a_keyerror_traceback(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("MEMECOIN_CHAIN1_FEE_HEADROOM", "1")
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", "0.05")
    monkeypatch.setattr(c1, "_HEADROOM", {})
    monkeypatch.setattr(c1, "start_refresher", lambda h, **k: None)
    err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "header not found"}}
    answers = [err, err, err, {"result": _history(0.18, [0.1] * 5)}, err]
    S.build_real_evm_executor(acct=Account.create(), chain_id="1", rpc_url="https://rpc",
                              http=_error_http(answers), gasprice_buffer=1.0)
    h = c1._HEADROOM["1"]
    with caplog.at_level(logging.INFO, logger=c1.__name__):
        for _ in range(3):
            h.refresh()
    lines = [r for r in caplog.records if r.name == c1.__name__]
    assert len(lines) == 1 and lines[0].exc_info is None             # one line per streak, no traceback
    assert "-32000" in lines[0].getMessage() and "header not found" in lines[0].getMessage()
    assert h.counts["errors"] == 3 and h.counts["streak"] == 3 and "header not found" in h.counts["last_error"]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=c1.__name__):
        h.refresh()                                                  # recovered
        h.refresh()                                                  # a new streak
    msgs = [r.getMessage() for r in caplog.records if r.name == c1.__name__]
    assert len(msgs) == 2 and "recovered after 3" in msgs[0] and "-32000" in msgs[1]
    assert h.snapshot() is not None and h.counts["streak"] == 1 and h.counts["reads"] == 5


def test_the_heartbeat_carries_the_fee_reader_counts(monkeypatch, tmp_path):
    import json as _json
    from sniper.services import memecoin_runner as R
    monkeypatch.setattr(c1, "_HEADROOM", {})
    assert c1.status() is None
    now = [100.0]
    h = c1.FeeHeadroom(lambda: _history(0.18, [0.1] * 5), clock=lambda: now[0])
    c1.register("1", h)
    h.refresh()
    now[0] += 40
    got = c1.status()
    assert got["reads"] == 1 and got["errors"] == 0 and got["age_s"] == 40.0 and got["stale"] is True
    path = tmp_path / "beat.json"
    R.write_heartbeat({}, True, path=str(path), chain1_fee_headroom=got)
    assert _json.loads(path.read_text())["chain1_fee_headroom"]["age_s"] == 40.0
    loop = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py").read_text()
    assert loop.count("chain1_fee_headroom=") >= 2                 # both heartbeat writes pass it
