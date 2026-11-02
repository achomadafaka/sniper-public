"""normalize_thesis_item — parse fomo `thesis_created` feed items into ThesisEvent.

Fixture RAW_THESIS is a real live item (gaylord/WARD, 2026-07-08) with the exact
top-level vs body key layout verified against the live feed.
"""
from sniper.feeds import fomo_feed

normalize_thesis_item = fomo_feed.normalize_thesis_item

# Real live thesis_created item (fields verified against the live /feed 2026-07-08).
RAW_THESIS = {
    "id": "00289ec6-065d-4b15-9d2c-089e00c08244",
    "type": "thesis_created",
    "userId": "a8d76f22-2944-5b97-a2e2-0fcdb11f1a75",
    "tokenAddress": "0x6dc200b21894af4660b549b678ea8df22bf7cfac",
    "networkId": "1",
    "swapId": "swap-abc", "tradeId": "trade-xyz", "transferId": None,
    "createdAt": "2026-07-08T09:12:02.149Z",
    "likes": None, "views": None, "numReplies": 0, "pinned": False,
    "body": {
        "comment": "Numbers look good pa should catch up at some point\n\nhttps://x.com/rayhankoly46/status/2074",
        "commentId": "dc0cce84-5ed5-495d-ae76-87d923777bf5",
        "userHandle": "gaylord", "displayName": "gaylord",
        "userId": "a8d76f22-2944-5b97-a2e2-0fcdb11f1a75",
        "tokenAddress": "0x6dc200b21894af4660b549b678ea8df22bf7cfac",
        "ticker": "WARD",
        "price": 0.00550396595586, "marketCap": 1006068.576793598, "fdv": 5500000.0,
        "positionNotionalUsd": 13789.141294015111,
        "percentageUnrealizedPnl": -29.37976071860803,
        "unrealizedPnlUsd": -5736.622756530199,
        "percentageRealizedPnl": 0.0,
        "tag": None,
    },
}


def test_normalize_real_thesis_populates_event():
    ev = fomo_feed.normalize_thesis_item(RAW_THESIS, "2026-07-08T09:12:05.000Z", None)
    assert isinstance(ev, fomo_feed.ThesisEvent)
    assert ev.fomo_thesis_id == "00289ec6-065d-4b15-9d2c-089e00c08244"
    assert ev.comment_id == "dc0cce84-5ed5-495d-ae76-87d923777bf5"
    assert ev.source_user_id == "a8d76f22-2944-5b97-a2e2-0fcdb11f1a75"
    assert ev.handle == "gaylord"
    assert ev.mint == "0x6dc200b21894af4660b549b678ea8df22bf7cfac"
    assert ev.ticker == "WARD"
    assert ev.comment.startswith("Numbers look good")
    assert ev.has_link is True                       # embeds an x.com URL
    assert ev.their_pnl_pct == -29.37976071860803    # PERCENT, preserved verbatim
    assert ev.their_notional_usd == 13789.141294015111
    assert ev.their_unrealized_pnl_usd == -5736.622756530199
    assert ev.market_cap == 1006068.576793598
    assert ev.swap_id == "swap-abc" and ev.trade_id == "trade-xyz"
    assert ev.network_id == "1"
    assert ev.signal_source == "global_feed"


def test_normalize_rejects_non_thesis_type():
    assert normalize_thesis_item({"type": "large_buy", "body": {}}, "x", None) is None


def test_normalize_requires_user_and_mint():
    no_user = {**RAW_THESIS, "userId": None, "body": {**RAW_THESIS["body"], "userId": None}}
    assert normalize_thesis_item(no_user, "x", None) is None
    no_mint = {**RAW_THESIS, "tokenAddress": None,
               "body": {**RAW_THESIS["body"], "tokenAddress": None}}
    assert normalize_thesis_item(no_mint, "x", None) is None


def test_normalize_skips_empty_comment():
    empty = {**RAW_THESIS, "body": {**RAW_THESIS["body"], "comment": "   \n  "}}
    assert normalize_thesis_item(empty, "x", None) is None
    missing = {**RAW_THESIS, "body": {**RAW_THESIS["body"], "comment": None}}
    assert normalize_thesis_item(missing, "x", None) is None


def test_normalize_keeps_url_only_comment():
    url_only = {**RAW_THESIS, "body": {**RAW_THESIS["body"], "comment": "https://x.com/foo/status/1"}}
    ev = normalize_thesis_item(url_only, "x", None)
    assert ev is not None and ev.has_link is True


def test_normalize_pnl_none_when_absent_not_zero():
    body = dict(RAW_THESIS["body"])
    del body["percentageUnrealizedPnl"]
    ev = normalize_thesis_item({**RAW_THESIS, "body": body}, "x", None)
    assert ev.their_pnl_pct is None      # unknown != 0.0


def test_normalize_synth_id_when_missing():
    no_id = {**RAW_THESIS}
    del no_id["id"]
    ev = normalize_thesis_item(no_id, "x", None)
    assert ev is not None and ev.fomo_thesis_id.startswith("synth-")
    # stable: same input -> same synth id
    ev2 = normalize_thesis_item({k: v for k, v in RAW_THESIS.items() if k != "id"}, "y", None)
    assert ev.fomo_thesis_id == ev2.fomo_thesis_id


def test_normalize_lag_from_server_epoch():
    from datetime import datetime
    created_epoch = datetime.fromisoformat("2026-07-08T09:12:02.149+00:00").timestamp()
    ev = normalize_thesis_item(RAW_THESIS, "2026-07-08T09:12:04.149Z", created_epoch + 2.0)
    assert abs(ev.lag_s - 2.0) < 0.05
    ev_none = normalize_thesis_item(RAW_THESIS, "x", None)
    assert ev_none.lag_s == -1.0
