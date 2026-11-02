"""Live-positions poller normalization: whole-position rows -> upsert dicts + open_keys, league
classified to tag-canonical coarse names. Pure (no live fetch)."""
from __future__ import annotations
import importlib
PL = importlib.import_module("scripts.poll_live_positions")

def test_normalize_whole_positions():
    rows = [{"conditionId": "0x1", "outcomeIndex": 0, "outcome": "LAL", "title": "Lakers ML",
             "eventSlug": "nba-lal-bos", "size": 100, "avgPrice": 0.6, "curPrice": 0.62,
             "currentValue": 62, "cashPnl": 2},
            {"conditionId": "0x2", "outcomeIndex": 1, "slug": "wnba-lv-nyl", "size": 50,
             "avgPrice": 0.4, "curPrice": 0.45, "currentValue": 22.5, "cashPnl": 2.5}]
    norm, keys = PL.normalize_positions("0x" + "A" * 40, rows)
    assert len(norm) == 2 and keys == {("0x1", 0), ("0x2", 1)}
    by_cid = {r["condition_id"]: r for r in norm}
    assert by_cid["0x1"]["league"] == "Basketball"   # nba -> canonical Basketball
    assert by_cid["0x2"]["league"] == "WNBA"         # wnba not mis-read as NBA
    assert by_cid["0x1"]["wallet"] == "0x" + "a" * 40 and by_cid["0x1"]["current_value"] == 62

def test_normalize_skips_malformed():
    norm, keys = PL.normalize_positions("0xw", [{"size": 10}, {"conditionId": "0x9", "outcomeIndex": None}])
    assert norm == [] and keys == set()


def test_normalize_drops_resolved_redeemable_positions():
    # REAL-DATA BUG: Polymarket /positions returns RESOLVED-but-unredeemed positions with
    # redeemable=True (curPrice=0, e.g. a game that ended weeks ago). They are NOT live/copyable
    # bets — including them floods the Signals board with a wall of worthless losers and enrolls
    # dead markets into live-position consumers. Drop them, AND leave them out of open_keys so the
    # close-inference sweep retires any already stored in live_positions.
    rows = [
        {"conditionId": "0xlive", "outcomeIndex": 0, "title": "OKC win Finals", "eventSlug": "nba",
         "size": 100, "avgPrice": 0.4, "curPrice": 0.37, "currentValue": 37, "redeemable": False},
        {"conditionId": "0xdead", "outcomeIndex": 1, "title": "Knicks vs Cavs", "eventSlug": "nba",
         "size": 100, "avgPrice": 0.55, "curPrice": 0.0, "currentValue": 0, "redeemable": True},
    ]
    norm, keys = PL.normalize_positions("0x" + "A" * 40, rows)
    assert {r["condition_id"] for r in norm} == {"0xlive"}
    assert keys == {("0xlive", 0)}   # resolved one excluded from open_keys too


def test_should_close_only_on_clean_fetch():
    # REGRESSION (Codex #1): a transient API refusal (e.g. first-page 429) returns rows=[] with
    # reason='api_refused' -> we must NOT infer closures, else every open position false-closes.
    from sniper.markets.polymarket_api import FetchTermination
    assert PL.should_close_from(FetchTermination(reason="empty_page")) is True
    assert PL.should_close_from(FetchTermination(reason="partial_page")) is True
    assert PL.should_close_from(FetchTermination(reason="api_refused", status=429)) is False
    assert PL.should_close_from(FetchTermination(reason="request_failed")) is False
    assert PL.should_close_from(None) is False
