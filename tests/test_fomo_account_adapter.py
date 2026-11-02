from sniper.feeds.fomo_account import get_profile, fetch_recent_swaps, fetch_balances


def _ro(obj):
    return {"responseObject": obj}


def test_fetch_balances_unwraps_and_hits_balances_path():
    def fetch(auth, path):
        assert path == "/v2/users/u1/balances"
        return 200, _ro({"balances": [{"balance": {"tokenAddress": "M"}}],
                         "livePerpPnl": 5.0, "otherPnl": 1.0}), "date"
    b = fetch_balances(None, "u1", fetch=fetch)
    assert b["livePerpPnl"] == 5.0 and len(b["balances"]) == 1


def test_fetch_balances_returns_empty_on_error():
    def fetch(auth, path):
        return 500, {"message": "boom"}, "date"
    assert fetch_balances(None, "u1", fetch=fetch) == {}


def test_get_profile_unwraps_response_object():
    def fetch(auth, path):
        assert path == "/v2/users/u1"
        return 200, _ro({"id": "u1", "userHandle": "alice"}), "date"
    p = get_profile(None, "u1", fetch=fetch)
    assert p["userHandle"] == "alice"


def test_fetch_recent_swaps_single_call_no_pagination():
    calls = []

    def fetch(auth, path):
        calls.append(path)
        return 200, _ro({"swaps": [{"id": "a"}, {"id": "b"}], "hasNextPage": True}), "date"

    swaps = fetch_recent_swaps(None, "u1", fetch=fetch)
    assert [s["id"] for s in swaps] == ["a", "b"]
    # exactly ONE call — fomo has no working pagination; hasNextPage=True is ignored
    assert len(calls) == 1
    assert "limit=100" in calls[0]


def test_fetch_recent_swaps_dedups_by_id():
    # fomo occasionally repeats rows; dedup by id, preserve first-seen order
    def fetch(auth, path):
        return 200, _ro({"swaps": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}), "date"
    swaps = fetch_recent_swaps(None, "u1", fetch=fetch)
    assert [s["id"] for s in swaps] == ["a", "b"]
