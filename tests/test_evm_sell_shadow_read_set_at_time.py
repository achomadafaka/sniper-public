"""evm_sell_shadow_read scores each J7 sell against the watched set AT ITS TIME (Alpha, 2026-09-29).

Scored against the CURRENT set, the 03:28-06:37Z read called 25 sells "missed" that were all made by
traders the watcher did not yet subscribe (it watched 7 / 6 / 1 addresses then, 42 / 48 / 32 now).
No address-level history existed, so the watcher now logs its acked transfer set whenever it
changes, and the read rebuilds the set in force at each sell from those lines. A process start
resets the set to UNKNOWN until its first acked line; a sell whose set is unknown is unscorable,
never missed.
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sell_read", REPO / "scripts/research/evm_sell_shadow_read.py")
read = importlib.util.module_from_spec(spec)
spec.loader.exec_module(read)

A, B = "0x" + "aa" * 20, "0x" + "bb" * 20
MINT = "0x" + "cc" * 20


def _log(tmp_path, lines):
    p = tmp_path / "onchain_evm_armed.log"
    p.write_text("\n".join(lines) + "\n")
    return p


def _j7(tx, trader, at):
    return {"tx": tx, "trader": trader, "mint": MINT, "source_user_id": "u-" + trader[2:4],
            "received_at": "2026-09-29T%s+00:00" % at}


def _timeline(tmp_path):
    return read.watched_timeline(_log(tmp_path, [
        "2026-09-29T06:00:00.000000+00:00 [onchain-evm] watching 1 verified address(es)",
        "2026-09-29T06:00:02.000000+00:00 [onchain-evm] acked transfer set (1): %s" % A,
        "2026-09-29T06:30:00.000000+00:00 [onchain-evm] acked transfer set (2): %s,%s" % (A, B),
        "2026-09-29T07:00:00.000000+00:00 [onchain-evm] watching 2 verified address(es)",
        "2026-09-29T07:00:03.000000+00:00 [onchain-evm] acked transfer set (2): %s,%s" % (A, B),
    ]))


def test_a_sell_by_an_address_added_later_does_not_count(tmp_path):
    """Negative control: B sold at 06:10, when the set was {A}; B joined at 06:30."""
    r = read.classify([], [_j7("0x1", B, "06:10:00")], read.set_at(_timeline(tmp_path)), current={A, B})
    assert (r["j7_watched"], r["missed"], r["unscorable"]) == (0, 0, 0), r


def test_a_sell_by_an_address_watched_at_the_time_and_not_delivered_counts(tmp_path):
    """Positive control: A was in the acked set at 06:10 and we have no row."""
    r = read.classify([], [_j7("0x1", A, "06:10:00")], read.set_at(_timeline(tmp_path)), current={A, B})
    assert (r["j7_watched"], r["missed"]) == (1, 1), r


def test_a_sell_before_the_first_ack_of_a_process_is_unscorable(tmp_path):
    at = read.set_at(_timeline(tmp_path))
    r = read.classify([], [_j7("0x1", A, "07:00:01"), _j7("0x2", A, "05:59:00")], at, current={A, B})
    assert (r["missed"], r["unscorable"]) == (0, 2), r


def test_a_delivered_sell_is_matched_even_while_the_set_is_unknown(tmp_path):
    ours = [{"tx": "0x1", "mint": MINT, "address": A, "source_user_id": "u-aa", "proof": "b_relay",
             "detected_at": "2026-09-29T07:00:00.500000+00:00",
             "decoded_at": "2026-09-29T07:00:00.600000+00:00", "post_atomic": "0"}]
    r = read.classify(ours, [_j7("0x1", A, "07:00:01")], read.set_at(_timeline(tmp_path)), current={A})
    assert (r["matched"], r["missed"], r["unscorable"]) == (1, 0, 0), r


def test_a_missing_log_makes_every_sell_unscorable(tmp_path):
    at = read.set_at(read.watched_timeline(tmp_path / "absent.log"))
    r = read.classify([], [_j7("0x1", A, "06:10:00")], at, current={A})
    assert (r["missed"], r["unscorable"]) == (0, 1), r


def test_the_watcher_logs_its_acked_set_once_per_change(monkeypatch):
    import test_onchain_evm_watch_backfill as bf
    from test_onchain_evm_watch_backfill import _heads, _run, env  # noqa: F401
    import scripts.research.onchain_evm_wallet_watch as w
    lines = []
    monkeypatch.setattr(w, "_say", lambda line: lines.append(line))

    def ack(addresses):
        def step(state, kw):
            state.transfer_addresses = tuple(sorted(addresses))
        return step

    def reconnect(state, kw):
        state.transfer_addresses = None
    e = {"inserts": [], "pages": [], "lines": [], "hb": Path(__import__("tempfile").mkdtemp()) / "hb.json"}
    monkeypatch.setattr(w, "get_connection", lambda path: bf._Conn(e["inserts"]))
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {A: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {A: ("u1", "h1")})
    monkeypatch.setattr(w, "_roster_connection", lambda: None)
    monkeypatch.setattr(w, "send_wake", lambda address: True)
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda: None)
    _run(monkeypatch, e, [ack({A}), _heads(2_000, 3), reconnect, _heads(2_003, 2), ack({A}),
                          _heads(2_005, 2), ack({A, B}), _heads(2_007, 2)])
    acked = [l for l in lines if "acked transfer set" in l]
    assert acked == ["[onchain-evm] acked transfer set (1): %s" % A,
                     "[onchain-evm] acked transfer set (2): %s,%s" % (A, B)], acked
