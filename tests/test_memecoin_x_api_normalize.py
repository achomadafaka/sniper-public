import copy
import json
from pathlib import Path


FIX = Path(__file__).parent / "fixtures" / "memecoin_x" / "api"
NOW = "2026-07-26T12:00:05+00:00"
ROSTER = {
    "1001": {"x_user_id": "1001", "handle": "alpha",
             "display_handle": "Alpha", "role": "tier1_trader"},
}


def _load(name):
    return json.loads((FIX / name).read_text())


def test_direct_structured_cashtag_normalizes_exact_actor_post_and_coin():
    from sniper.analysis.memecoin_x.api_normalize import normalize_api_payload

    event = normalize_api_payload(_load("direct.json"), roster_by_user_id=ROSTER,
                                  observed_at=NOW)
    assert event["event_type"] == "cashtag_mention"
    assert event["relationship"] == "direct"
    assert event["cashtag_source"] == "actor_text"
    assert event["cashtags"] == ["MEME"]
    assert event["post_url"] == "https://x.com/alpha/status/101"
    assert event["actor_user_id"] == "1001"
    assert event["identity_state"] == "exact_coin"
    assert event["network_id"] == "4663"
    assert event["mint"] == "0x1111111111111111111111111111111111111111"


def test_quote_with_tag_only_in_reference_is_amplification():
    from sniper.analysis.memecoin_x.api_normalize import normalize_api_payload

    event = normalize_api_payload(
        _load("quote_reference_tag.json"), roster_by_user_id=ROSTER, observed_at=NOW)
    assert event["event_type"] == "cashtag_amplification"
    assert event["relationship"] == "quote"
    assert event["cashtag_source"] == "referenced_post"
    assert event["cashtags"] == ["MEME"]
    assert event["post_url"] == "https://x.com/alpha/status/103"
    assert event["identity_state"] == "ticker_only"
    assert event["referenced_post_id"] == "900"


def test_actor_entities_are_authoritative_over_text_regex():
    from sniper.analysis.memecoin_x.api_normalize import normalize_api_payload

    payload = _load("direct.json")
    payload["data"]["text"] = "$500 $FAKE"
    payload["data"]["entities"] = {"cashtags": [], "urls": []}
    assert normalize_api_payload(
        payload, roster_by_user_id=ROSTER, observed_at=NOW) is None


def test_multiple_cashtags_are_uppercase_and_deduplicated():
    from sniper.analysis.memecoin_x.api_normalize import normalize_api_payload

    payload = _load("direct.json")
    payload["data"]["entities"]["cashtags"] = [
        {"start": 0, "end": 5, "tag": "meme"},
        {"start": 6, "end": 11, "tag": "MEME"},
        {"start": 12, "end": 18, "tag": "other"},
    ]
    event = normalize_api_payload(payload, roster_by_user_id=ROSTER, observed_at=NOW)
    assert event["cashtags"] == ["MEME", "OTHER"]
    assert event["post_id"] == "101"


def test_handle_change_keeps_stable_actor_user_id():
    from sniper.analysis.memecoin_x.api_normalize import normalize_api_payload

    payload = _load("direct.json")
    payload["includes"]["users"][0]["username"] = "alpha_new"
    event = normalize_api_payload(payload, roster_by_user_id=ROSTER, observed_at=NOW)
    assert event["actor_user_id"] == "1001"
    assert event["actor_handle"] == "alpha_new"


def test_unknown_actor_or_missing_reference_is_ineligible():
    from sniper.analysis.memecoin_x.api_normalize import normalize_api_payload

    direct = _load("direct.json")
    assert normalize_api_payload(direct, roster_by_user_id={}, observed_at=NOW) is None
    quote = _load("quote_reference_tag.json")
    quote["includes"]["tweets"] = []
    event = normalize_api_payload(quote, roster_by_user_id=ROSTER, observed_at=NOW)
    assert event is None


def test_reply_and_repost_relationships_are_labeled_without_claiming_promotion():
    from sniper.analysis.memecoin_x.api_normalize import normalize_api_payload

    reply = _load("direct.json")
    reply["data"]["referenced_tweets"] = [{"type": "replied_to", "id": "800"}]
    reply["includes"]["tweets"] = [{
        "id": "800", "text": "Earlier", "author_id": "8001",
        "created_at": "2026-07-26T11:00:00.000Z",
        "edit_history_tweet_ids": ["800"], "entities": {"cashtags": [], "urls": []},
    }]
    assert normalize_api_payload(
        reply, roster_by_user_id=ROSTER, observed_at=NOW)["relationship"] == "reply"

    repost = _load("quote_reference_tag.json")
    repost["data"]["referenced_tweets"][0]["type"] = "retweeted"
    event = normalize_api_payload(repost, roster_by_user_id=ROSTER, observed_at=NOW)
    assert event["relationship"] == "repost"
    assert event["event_type"] == "cashtag_amplification"
    assert "promotion" not in json.dumps(event).lower()
