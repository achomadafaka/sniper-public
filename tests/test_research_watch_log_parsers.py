"""The research log parsers read the watcher's transfer/detection lines in both forms: the old
12-char tx prefix and the full hash the watcher prints since 6ee9b54f0 (2026-10-17). Keys stay the
12-char prefix, which every lookup uses (tx[:12])."""
from scripts.research import evm_watch_miss_replay as replay
from scripts.research import rh_feed_shadow_read as shadow

FULL = "0x" + "c6fde48a1d" + "ab" * 27
OLD = FULL[:12]


def _lines(tx):
    return ["2026-10-17T12:52:32.644231+00:00 [onchain-evm] transfer %s arrival=2026-10-17T12:52:32.605038+00:00 "
            "outcome=no_buy retries=0 receipt_host=tx_read/20ms decode=not_relay_envelope" % tx,
            "[onchain-evm] detection %s arrival=2026-10-17T12:52:33+00:00 decoded_at=x" % tx]


def test_the_miss_replay_parses_full_and_prefix_hashes_to_one_key():
    for tx in (FULL, OLD):
        assert replay.arrival_outcomes(_lines(tx)) == {OLD: ["no_buy", "detected"]}


def test_the_feed_shadow_read_parses_full_and_prefix_hashes_to_one_key(tmp_path):
    for tx in (FULL, OLD):
        path = tmp_path / ("w-%d.log" % len(tx))
        path.write_text("\n".join(_lines(tx)) + "\n")
        got = shadow.watcher_log(path)
        assert list(got) == [OLD] and got[OLD][2:] == ("no_buy", "not_relay_envelope")
