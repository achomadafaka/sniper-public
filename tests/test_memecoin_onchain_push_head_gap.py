"""Each detection names how far its log push trailed its own block's head on the SAME socket (Alpha
2026-10-14): armed 56 log pushes ran p50 142 ms / p90 3.2 s after their block (n=60) while publicnode
heads ran p50 42 ms / p90 92 ms (n=40). Local timestamps only; no call."""
import importlib
import sys
from pathlib import Path

from sniper.execution import evm_v4_quote as V
from sniper.services import memecoin_onchain_push as P

sys.path.insert(0, str(Path(__file__).parent))
from test_memecoin_onchain_push_lanes import ARMED, MANAGER, _clock, _connect, _push, _stop_after, _sub_reply  # noqa: E402

BLOCK_MS = 1791990123300


def test_the_socket_keeps_each_head_s_first_arrival_and_header_ms():
    state = P.PushState()
    head = {"number": "0x79b40bf", "timestamp": "0x6acf996b", "milliTimestamp": hex(BLOCK_MS)}
    connect_fn = _connect([[_sub_reply(1, "0xheads"), _push("0xheads", head), _push("0xheads", head)]], [])
    seen = []                                                  # read on the socket, before any reconnect
    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      state=state, connect_fn=connect_fn, sleep_fn=lambda _s: None,
                      now_fn=_clock(step=0.5), should_stop=_stop_after(connect_fn, 3),
                      on_head=lambda: seen.append((state.head_for(0x79b40bf), state.head_for(1))))
    (first, none1), (second, _none) = seen[:2]
    assert first[1] == BLOCK_MS and none1 is None
    assert second == first                                     # a repeated head keeps the first arrival


def test_the_map_is_bounded_and_a_head_without_ms_reads_none():
    state = P.PushState()
    for block in range(P._HEADS_SEEN_MAX + 10):
        state.head(1000.0 + block, block, 1)
    assert len(state.heads_seen) == P._HEADS_SEEN_MAX
    assert state.head_for(0) is None and state.head_for(P._HEADS_SEEN_MAX + 9) == (1000.0 + P._HEADS_SEEN_MAX + 9, None)
    assert P.block_ms_of({"timestamp": "0x1"}) is None and P.block_ms_of(None) is None


def _watch():
    return importlib.import_module("scripts.research.onchain_evm_wallet_watch")


def test_a_detection_names_its_log_push_gap_to_its_own_head_or_unknown():
    w = _watch()
    state = P.PushState()
    head_at = BLOCK_MS / 1000.0 + 0.042                          # head 42 ms after the header
    state.head(head_at, 100, 1791990123, block_ms=BLOCK_MS)
    arrival = "2026-10-14T15:02:06.442000+00:00"
    log = {"blockNumber": hex(100)}
    gap = w.push_head_gap(log, arrival, state=state)
    from datetime import datetime
    arrived = datetime.fromisoformat(arrival).timestamp()
    assert gap == {"head_at": head_at, "log_minus_head_ms": round((arrived - head_at) * 1000.0, 1),
                   "head_minus_block_ms": round(head_at * 1000.0 - BLOCK_MS, 1),
                   "log_minus_block_ms": round(arrived * 1000.0 - BLOCK_MS, 1)}
    assert w.push_head_gap({"blockNumber": hex(101)}, arrival, state=state) is None      # head not seen


def test_the_commit_stamps_the_gap_into_the_row_and_the_line(monkeypatch, capsys):
    w = _watch()
    state = P.PushState()
    state.head(1000.0, 100, 1, block_ms=999_900)
    monkeypatch.setattr(w, "PUSH_STATE", state)
    recorded = []
    monkeypatch.setattr(w, "_record", lambda conn, **row: recorded.append(row))
    monkeypatch.setattr(w, "send_wake", lambda address: None)

    class Conn:
        def commit(self):
            pass
    rows = [dict(tx_hash="0x" + "ab" * 32, log={"blockNumber": hex(100)}, now="1970-01-01T00:16:41+00:00",
                 valuation={"decoded_at": "x"}),
            dict(tx_hash="0x" + "cd" * 32, log={"blockNumber": hex(100)}, now="1970-01-01T00:16:41+00:00",
                 valuation={"backfill": True})]
    w._commit_detections(Conn(), rows)
    assert recorded[0]["valuation"]["push_head"]["log_minus_head_ms"] == 1000.0
    assert recorded[0]["valuation"]["push_head"]["head_minus_block_ms"] == 100.0
    assert "push_head" not in recorded[1]["valuation"]                # a backfilled row is no push
    out = capsys.readouterr().out
    assert "head_gap_ms=1000.0" in out and "head_gap_ms=unknown" in out


def test_a_repeated_head_keeps_its_first_arrival_and_a_new_socket_starts_empty():
    """Gamma LOWs on 1422317b1: the first arrival is the measurement; a reconnect or rotation never
    measures a log against the previous socket's head."""
    state = P.PushState()
    state.head(1000.0, 100, 1, block_ms=999_900)
    state.head(1003.0, 100, 1, block_ms=999_900)                   # redelivered later
    assert state.head_for(100) == (1000.0, 999_900)
    state.new_connection(1005.0)
    assert state.head_for(100) is None
    state.head(1006.0, 100, 1, block_ms=999_900)                   # the new socket's own delivery
    assert state.head_for(100) == (1006.0, 999_900)
