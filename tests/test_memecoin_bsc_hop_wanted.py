"""An armed buy on an untracked BSC hop quote gets that WBNB/quote pair pinned for the next buy.

73844 (0x2f8a339b...), 73812 (0x02fca66c..., no_sync) and 72879 (0x5407912f...) missed the 56 lane
on hop_state_stale; the same quotes recurred (0x02fca66c x3, 0x2f8a339b x2, 0x205812cd x2) because
pins came only from our own fills and PairCreated targets churn out of the 128-slot cap.
"""
import pytest

from scripts.research import onchain_evm_wallet_watch as watcher
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_bsc_v2_live as tape
from test_memecoin_bsc_native_hop_pins import OLD_HOP, OLD_QUOTE, files  # noqa: F401

QUOTE = "0x2f8a339b5889ffac4c5a956787cda593b3c36867"
OTHER = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"
NOW = 1_800_000_000.0


def test_wanted_quotes_are_recent_distinct_valid_and_bounded(tmp_path):
    path = tmp_path / "wanted.txt"
    bsc.want_hop_quote(QUOTE, now=NOW - 10, path=path)
    bsc.want_hop_quote(OTHER, now=NOW - 5, path=path)
    bsc.want_hop_quote(QUOTE.upper().replace("0X", "0x"), now=NOW - 1, path=path)
    bsc.want_hop_quote("0x" + "aa" * 20, now=NOW - bsc.HOP_WANTED_MAX_AGE_S - 1, path=path)
    with open(path, "a") as f:
        f.write("garbage\n0xzz 1\n%s %d\n%s\n" % (bsc.WBNB, NOW, OTHER))
    assert bsc.wanted_hop_quotes(now=NOW, path=path) == [QUOTE, OTHER]
    for i in range(bsc.HOP_WANTED_LIMIT + 3):
        bsc.want_hop_quote("0x%040x" % (i + 1), now=NOW, path=path)
    assert len(bsc.wanted_hop_quotes(now=NOW, path=path)) == bsc.HOP_WANTED_LIMIT
    assert bsc.wanted_hop_quotes(now=NOW, path=tmp_path / "absent.txt") == []


@pytest.mark.parametrize("reason,wanted", [("hop_state_stale:untracked", True),
    ("hop_state_stale:no_sync", True), ("hop_state_stale:head_old", False),
    ("hop_state_stale:head_behind", False), ("would_send", False), ("float_short", False)])
def test_the_watcher_wants_only_an_untracked_or_unsynced_hop(tmp_path, monkeypatch, reason, wanted):
    path = tmp_path / "wanted.txt"
    monkeypatch.setattr(watcher, "_HOP_WANTED_SINK", lambda quote: bsc.want_hop_quote(quote, path=path))
    signal = lambda *a, **k: {"reason": reason, "sent": False, "quote": QUOTE}
    row = {"tx_hash": "0x" + "11" * 32, "buy": {"mint": "0x" + "22" * 20}}
    watcher._signal_bsc_lane(signal, row, {"_receipt": {"blockNumber": "0x1"}}, 600.0, 0.1)
    assert bsc.wanted_hop_quotes(now=bsc.time.time(), path=path) == ([QUOTE] if wanted else [])


def test_the_tape_pins_wanted_quotes_beside_its_route_pins(files, monkeypatch):  # noqa: F811
    path = files.parent / "bsc_hop_wanted.txt"
    bsc.want_hop_quote(QUOTE, path=path)
    assert tape.read_route_pins() == {OLD_HOP: OLD_QUOTE, bsc.our_pair(bsc.WBNB, QUOTE): QUOTE}
