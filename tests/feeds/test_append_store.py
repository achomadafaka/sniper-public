from sniper.feeds.append_store import JsonlAppendStore
from sniper.feeds.thesis_feed_types import FeedEvent, payload_hash


def _event(payload):
    return FeedEvent("src", "kind", 1, None, None, None, None, None, payload, payload_hash(payload))


def test_append_store_dedupes_by_payload_hash(tmp_path):
    store = JsonlAppendStore(tmp_path)
    assert store.append("src", _event({"x": 1})) is True
    assert store.append("src", _event({"x": 1})) is False
    rows = list(store.read("src"))
    assert len(rows) == 1


def test_append_store_dedupe_survives_reload(tmp_path):
    JsonlAppendStore(tmp_path).append("src", _event({"y": 2}))
    fresh = JsonlAppendStore(tmp_path)           # new instance reloads seen-hashes from disk
    assert fresh.append("src", _event({"y": 2})) is False
    assert len(list(fresh.read("src"))) == 1
