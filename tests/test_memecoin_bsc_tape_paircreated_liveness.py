"""The tape resets its socket when the PairCreated subscription (id 6) goes silent (2026-10-14:
publicnode's id 6 stopped at 15:23:00Z while heads, Swaps and Syncs flowed; the stall check covered
only 2-5, so new hop targets and the BSC lane's presignal safety candidates stopped for an hour)."""
import json

import pytest

from sniper.services import memecoin_bsc_v2_live as tape

A = "0x" + "4b" * 20


def _run(monkeypatch, tmp_path, capsys, *, pair_every_s, seconds, reject=False):
    """The run loop over a fake socket and a fake clock: one head per fake second, a PairCreated
    delivery every `pair_every_s` fake seconds (None: never), for `seconds` fake seconds. Returns the
    printed output once the loop ends: the second connection ends it."""
    from sniper.execution import evm_wallet
    from sniper.services import memecoin_bsc_fast_lane as bsc
    import sniper.services.memecoin_research_pacer as rp
    monkeypatch.setattr(tape, "db_path", lambda: tmp_path / "tape.db")
    monkeypatch.setattr(bsc, "hop_pairs", lambda: {A: "0xq"})          # discovery on: id 6 subscribed
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "runner_shadow")
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: None)
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: None)
    monkeypatch.setattr(tape, "_open_pools", lambda: ({}, {}))
    monkeypatch.setattr(tape, "read_route_pins", lambda: {})
    monkeypatch.setattr(evm_wallet, "quote_usd_price", lambda chain: 600.0)
    monkeypatch.setattr(tape, "_rpc", lambda method, params, url=tape.RPC, timeout=3: (
        "0x38" if method == "eth_chainId" else "0x" + "00" * 96))
    clock = {"now": 1790000000.0}
    monkeypatch.setattr(tape, "real_utc_now", lambda: clock["now"])
    script = [{"id": 1, "result": "0x38"}, {"id": 2, "result": "s2"}, {"id": 3, "result": "s3"},
              {"id": 4, "result": "s4"}, {"id": 5, "result": "s5"},
              {"id": 6, "error": {"code": -32000, "message": "refused"}} if reject else {"id": 6, "result": "s6"}]
    for second in range(1, seconds + 1):
        script.append(("tick",))
        script.append({"params": {"subscription": "s2", "result": {
            "number": hex(100 + second), "hash": "0x%064x" % (100 + second),
            "timestamp": hex(1790000000 + second), "parentHash": "0x%064x" % (99 + second)}}})
        if pair_every_s and second % pair_every_s == 0:
            # Not a canonical pair (no kept target): still a delivery, so still liveness.
            script.append({"params": {"subscription": "s6", "result": {
                "address": tape.PCS_V2_FACTORY, "topics": [tape.PAIR_CREATED], "data": "0x"}}})
    connections = []

    class WS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, text):
            pass

        def ping(self):
            pass

        def recv(self, timeout=None):
            while script and script[0] == ("tick",):
                script.pop(0)
                clock["now"] += 1.0
            if not script:
                raise KeyboardInterrupt           # ends run(): not an Exception it retries
            return json.dumps(script.pop(0))

    def connect(*a, **k):
        connections.append(1)
        if len(connections) > 1:
            raise KeyboardInterrupt               # the reset reconnected: done
        return WS()
    import websockets.sync.client as wsc
    monkeypatch.setattr(wsc, "connect", connect)
    with pytest.raises(KeyboardInterrupt):
        tape.run(ws_url="wss://test", rpc_url="https://test")
    return capsys.readouterr().out, len(connections)


def test_a_silent_paircreated_subscription_resets_the_socket(tmp_path, monkeypatch, capsys):
    out, connections = _run(monkeypatch, tmp_path, capsys, pair_every_s=None,
                            seconds=int(tape.PAIR_CREATED_SILENT_S) + 10)
    assert "BSC observer reset: TimeoutError: BSC PairCreated stream silent" in out
    assert connections == 2


def test_a_delivering_paircreated_subscription_keeps_the_socket(tmp_path, monkeypatch, capsys):
    out, connections = _run(monkeypatch, tmp_path, capsys, pair_every_s=60,
                            seconds=int(tape.PAIR_CREATED_SILENT_S) * 3)
    assert "PairCreated stream silent" not in out and "BSC observer reset" not in out
    assert connections == 1


def test_a_rejected_paircreated_subscription_keeps_the_core_tape_and_retries_slowly(tmp_path, monkeypatch,
                                                                                   capsys):
    """Gamma MEDIUM on 9e77b420a: an explicit id 6 rejection reset the whole tape every ~121 s."""
    out, connections = _run(monkeypatch, tmp_path, capsys, pair_every_s=None, reject=True,
                            seconds=int(tape.PAIR_CREATED_SILENT_S) * 3)
    assert "BSC pair discovery rejected; core tape remains active" in out
    assert "BSC observer reset" not in out and connections == 1
    out, connections = _run(monkeypatch, tmp_path, capsys, pair_every_s=None, reject=True,
                            seconds=int(tape.PAIR_CREATED_REJECTED_RETRY_S) + 10)
    assert "BSC observer reset: TimeoutError: BSC PairCreated stream rejected" in out
    assert connections == 2
