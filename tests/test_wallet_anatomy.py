from __future__ import annotations

import json

from scripts import wallet_anatomy as anatomy


def _trade(*, ts: int, side: str, size: float, price: float, condition: str = "0xcond") -> dict:
    return {
        "type": "TRADE",
        "conditionId": condition,
        "outcomeIndex": 0,
        "outcome": "Yes",
        "title": "Test market",
        "eventSlug": "test-event",
        "timestamp": ts,
        "side": side,
        "size": size,
        "usdcSize": size * price,
    }


def test_resolve_conditions_does_not_cache_unknowns_on_fetch_error(tmp_path, monkeypatch):
    monkeypatch.setattr(anatomy, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(anatomy.P, "build_session", lambda: object())

    def fail_request(*_args, **_kwargs):
        raise RuntimeError("gamma unavailable")

    monkeypatch.setattr(anatomy.P, "_request_json", fail_request)

    out = anatomy.resolve_conditions(["0xaaa"], wallet="0xwallet")

    assert "0xaaa" not in out
    cache_path = tmp_path / "0xwallet_resolutions.json"
    if cache_path.exists():
        assert "0xaaa" not in json.loads(cache_path.read_text())


def test_resolve_conditions_caches_missing_only_after_successful_response(tmp_path, monkeypatch):
    monkeypatch.setattr(anatomy, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(anatomy.P, "build_session", lambda: object())
    monkeypatch.setattr(anatomy.P, "_request_json", lambda *_args, **_kwargs: [])

    out = anatomy.resolve_conditions(["0xaaa"], wallet="0xwallet")

    assert out["0xaaa"] == {"resolved": False}


def test_load_event_tags_uses_scalar_slug_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(anatomy, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(anatomy.P, "build_session", lambda: object())
    seen_slugs = []

    def fake_request(_session, _url, *, params=None):
        slug = params["slug"]
        seen_slugs.append(slug)
        assert isinstance(slug, str)
        return [{"slug": slug, "tags": [{"label": f"{slug}-label"}]}]

    monkeypatch.setattr(anatomy.P, "_request_json", fake_request)

    out = anatomy.load_event_tags(["event-a", "event-b"], wallet="0xwallet", batch_size=40)

    assert seen_slugs == ["event-a", "event-b"]
    assert out == {"event-a": ["event-a-label"], "event-b": ["event-b-label"]}


def test_load_event_tags_does_not_cache_untagged_on_fetch_error(tmp_path, monkeypatch):
    monkeypatch.setattr(anatomy, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(anatomy.P, "build_session", lambda: object())

    def fail_request(*_args, **_kwargs):
        raise RuntimeError("gamma unavailable")

    monkeypatch.setattr(anatomy.P, "_request_json", fail_request)

    out = anatomy.load_event_tags(["event-a"], wallet="0xwallet")

    assert "event-a" not in out
    cache_path = tmp_path / "0xwallet_tags.json"
    if cache_path.exists():
        assert "event-a" not in json.loads(cache_path.read_text())


def test_reconstruct_positions_splits_flat_then_rebuy_into_new_episode():
    activity = [
        _trade(ts=1, side="BUY", size=10, price=0.20),
        _trade(ts=2, side="SELL", size=10, price=0.50),
        _trade(ts=3, side="BUY", size=5, price=0.70),
    ]

    positions = anatomy.reconstruct_positions("0xwallet", activity)
    directional = [p for p in positions.values() if p.is_directional]

    assert len(directional) == 2
    assert [p.buy_shares for p in directional] == [10, 5]
    assert [p.sell_shares for p in directional] == [10, 0]
    assert [round(p.entry_vwap, 2) for p in directional] == [0.20, 0.70]


def test_reconstruct_positions_redeem_marks_all_episodes_for_condition():
    activity = [
        _trade(ts=1, side="BUY", size=10, price=0.20),
        _trade(ts=2, side="SELL", size=10, price=0.50),
        _trade(ts=3, side="BUY", size=5, price=0.70),
        {"type": "REDEEM", "conditionId": "0xcond", "timestamp": 4},
    ]

    positions = anatomy.reconstruct_positions("0xwallet", activity)
    directional = [p for p in positions.values() if p.is_directional]

    assert len(directional) == 2
    assert all(p.redeemed for p in directional)


def test_enrich_accepts_episode_keys_and_sets_resolution_fields():
    positions = anatomy.reconstruct_positions(
        "0xwallet",
        [
            _trade(ts=1, side="BUY", size=10, price=0.20),
            _trade(ts=2, side="SELL", size=10, price=0.50),
            _trade(ts=3, side="BUY", size=5, price=0.70),
        ],
    )

    anatomy.enrich(
        positions,
        {"0xcond": {"resolved": True, "winner": 0, "push": False}},
        {"test-event": ["Sports", "UFC"]},
    )

    assert all(p.resolved for p in positions.values())
    assert all(p.won == 1 for p in positions.values())
    assert {p.category for p in positions.values()} == {"sports"}


# --- categorization is platform-tag driven (no hardcoded entities) ---

def test_parent_category_from_platform_tags():
    assert anatomy.parent_category(["Sports", "UFC", "Games"]) == "sports"
    # geopolitics wins over politics when both present
    assert anatomy.parent_category(["Geopolitics", "Iran", "Politics", "World"]) == "geopolitics"
    assert anatomy.parent_category(["Politics", "World", "Elections"]) == "politics"
    assert anatomy.parent_category([]) == "untagged"


def test_specific_tag_skips_generic():
    # generic tags (Sports/Games/World/Politics) are skipped for the specific grain
    assert anatomy.specific_tag(["Sports", "UFC", "Games"]) == "UFC"
    assert anatomy.specific_tag(["Games", "Sports"]) in {"Games", "Sports"}  # all generic -> fallback


# --- method #12: reward attribution ---

def test_reward_attribution_sums_passive_income_only():
    acts = [
        _trade(ts=1, side="BUY", size=10, price=0.5),
        {"type": "REWARD", "usdcSize": 100.0},
        {"type": "YIELD", "usdcSize": 25.0},
        {"type": "MAKER_REBATE", "usdcSize": 5.0},
    ]
    ra = anatomy.reward_attribution(acts)
    assert ra["total"] == 130.0
    assert ra["counts"]["REWARD"] == 1
    assert "TRADE" not in ra["income"]          # trades are not passive income


def test_reward_share_uses_absolute_trading_pnl_denominator():
    assert anatomy.reward_share_pct(trading_pnl=-100.0, reward_total=25.0) == 20.0


# --- method #27: runnability labelling ---

def _slice_with(n, pnl):
    s = anatomy.Slice(key="X")
    s.n = n
    s.pnl = pnl
    return s


def test_runnability_underpowered_below_min_n():
    label, _ = anatomy.runnability(_slice_with(5, 1000), parent="sports",
                                   held_share=0.9, top1_share=0.1)
    assert label == "underpowered"


def test_runnability_flags_single_position_concentration():
    label, _ = anatomy.runnability(_slice_with(40, 1000), parent="sports",
                                   held_share=0.9, top1_share=0.8)
    assert label == "capital_scaled"


def test_runnability_flags_info_sensitive_categories():
    label, _ = anatomy.runnability(_slice_with(40, 1000), parent="geopolitics",
                                   held_share=0.9, top1_share=0.1)
    assert label == "info_sensitive"


def test_runnability_runnable_for_broad_held_sports():
    label, why = anatomy.runnability(_slice_with(40, 1000), parent="sports",
                                     held_share=0.9, top1_share=0.1)
    assert label == "runnable"
    assert "copy" not in why.lower()
    assert "forward-testable" in why


def test_slice_held_share_is_slice_specific_not_wallet_global():
    held = anatomy.Position(wallet="0xw", condition_id="0x1", outcome_index=0)
    held.resolved = True
    held.won = 1
    held.buy_shares = 10
    held.buy_cost = 5

    sold = anatomy.Position(wallet="0xw", condition_id="0x2", outcome_index=0)
    sold.resolved = True
    sold.won = 1
    sold.buy_shares = 10
    sold.buy_cost = 5
    sold.sell_shares = 10
    sold.sell_proceeds = 8

    slc = anatomy.Slice(key="sold-tag")
    slc.add(sold, sold.realized_pnl() or 0.0)

    assert anatomy.slice_held_share(slc) == 0.0
