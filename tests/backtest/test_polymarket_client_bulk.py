"""Tests for bulk price/trade/market fetch methods on PolymarketClient."""
from data.polymarket_client import PolymarketClient


def test_get_prices_series_returns_all_points(monkeypatch):
    client = PolymarketClient()
    canned = {"history": [{"t": 1700000000, "p": 0.5}, {"t": 1700000060, "p": 0.52}]}
    monkeypatch.setattr(client, "_get", lambda *a, **kw: canned)
    series = client.get_prices_series("tokX", start_ts=0, end_ts=99999, fidelity=1)
    assert len(series) == 2
    assert series[0] == {"timestamp": 1700000000, "price": 0.5}
    assert series[1] == {"timestamp": 1700000000 + 60, "price": 0.52}


def test_get_prices_series_handles_missing_history(monkeypatch):
    client = PolymarketClient()
    monkeypatch.setattr(client, "_get", lambda *a, **kw: None)
    assert client.get_prices_series("tokX", 0, 1) == []


def test_get_prices_series_handles_empty_history(monkeypatch):
    client = PolymarketClient()
    monkeypatch.setattr(client, "_get", lambda *a, **kw: {"history": []})
    assert client.get_prices_series("tokX", 0, 1) == []


def test_get_prices_series_sorts_by_timestamp(monkeypatch):
    client = PolymarketClient()
    out_of_order = {"history": [{"t": 200, "p": 0.9}, {"t": 100, "p": 0.8}]}
    monkeypatch.setattr(client, "_get", lambda *a, **kw: out_of_order)
    series = client.get_prices_series("tokX", 0, 300, fidelity=1)
    assert [p["timestamp"] for p in series] == [100, 200]


def test_get_prices_series_skips_bad_points(monkeypatch):
    client = PolymarketClient()
    messy = {"history": [{"t": 100, "p": 0.5}, {"t": None, "p": 0.6}, {"t": 200, "p": None}, {"t": 300, "p": 0.7}]}
    monkeypatch.setattr(client, "_get", lambda *a, **kw: messy)
    series = client.get_prices_series("tokX", 0, 999, fidelity=1)
    assert len(series) == 2
    assert [p["timestamp"] for p in series] == [100, 300]


def test_get_prices_series_chunks_long_windows(monkeypatch):
    client = PolymarketClient()
    calls = []

    def fake_get(url, params):
        calls.append(params)
        return {"history": [{"t": params["startTs"], "p": 0.5}, {"t": params["endTs"], "p": 0.6}]}

    monkeypatch.setattr(client, "_get", fake_get)
    series = client.get_prices_series("tokX", 0, 20, fidelity=1, max_window_seconds=10)
    assert [(call["startTs"], call["endTs"]) for call in calls] == [(0, 9), (10, 19), (20, 20)]
    assert [point["timestamp"] for point in series] == [0, 9, 10, 19, 20]


def _canned_trades_page(timestamps, offset=0):
    """Simulate Polymarket /trades: descending timestamps, page-limited."""
    return [{"timestamp": ts, "price": 0.5, "size": 10.0, "side": "BUY", "outcome": "YES", "transactionHash": f"0x{ts:x}"} for ts in timestamps]


def test_get_trades_window_filters_by_timestamp(monkeypatch):
    # All trades on a single page; filter to [start_ts, end_ts]
    client = PolymarketClient()
    page = _canned_trades_page([2000, 1500, 1000, 500])  # descending
    monkeypatch.setattr(client, "_get", lambda *a, **kw: page)
    trades = client.get_trades_window("cid", start_ts=800, end_ts=1700)
    kept_ts = sorted(t["_ts_sec"] for t in trades)
    assert kept_ts == [1000, 1500]


def test_get_trades_window_paginates(monkeypatch):
    # 3 pages of 200 each, descending; third page partial -> stop
    pages = [
        _canned_trades_page(list(range(1000, 800, -1))),   # offset 0: ts 1000..801
        _canned_trades_page(list(range(800, 600, -1))),    # offset 200: ts 800..601
        _canned_trades_page(list(range(600, 500, -1))),    # offset 400: 100 rows (< 200) -> stop
    ]
    calls = {"n": 0}
    def fake_get(url, params):
        i = calls["n"]
        calls["n"] += 1
        return pages[i] if i < len(pages) else []
    client = PolymarketClient()
    monkeypatch.setattr(client, "_get", fake_get)
    trades = client.get_trades_window("cid", start_ts=0, end_ts=2000, page_size=200)
    assert calls["n"] == 3
    assert len(trades) == 500


def test_get_trades_window_stops_when_page_fully_older(monkeypatch):
    # First page has some in-window; second page fully older than start_ts -> stop
    page1 = _canned_trades_page([1500, 1400, 1300, 1200])  # in-window
    page2 = _canned_trades_page([900, 800, 700, 600])      # all < start_ts=1000
    calls = {"n": 0}
    def fake_get(url, params):
        i = calls["n"]; calls["n"] += 1
        return [page1, page2][i] if i < 2 else []
    # page_size=4 so each page is "full"; otherwise len<page_size causes the len-based stop first.
    client = PolymarketClient()
    monkeypatch.setattr(client, "_get", fake_get)
    trades = client.get_trades_window("cid", start_ts=1000, end_ts=2000, page_size=4, max_pages=10)
    assert calls["n"] == 2  # stopped after page 2 (fully older)
    assert len(trades) == 4
    assert all(t["_ts_sec"] >= 1000 for t in trades)


def test_get_trades_window_respects_max_pages(monkeypatch):
    # Infinite pages of in-window trades; max_pages=3 caps it
    calls = {"n": 0}
    def fake_get(url, params):
        calls["n"] += 1
        return _canned_trades_page([2000 - calls["n"]] * 4)  # 4 trades per page, page_size=4
    client = PolymarketClient()
    monkeypatch.setattr(client, "_get", fake_get)
    trades = client.get_trades_window("cid", start_ts=0, end_ts=9999, page_size=4, max_pages=3)
    assert calls["n"] == 3
    # Each page has 4 trades, so 3 pages = 12
    assert len(trades) == 12


def test_get_trades_window_normalizes_ms_timestamps(monkeypatch):
    # Some Polymarket endpoints return ms; the method should normalize to seconds via _ts_sec
    client = PolymarketClient()
    page = [{"timestamp": 1700000000000, "price": 0.5, "size": 1, "side": "BUY", "outcome": "YES", "transactionHash": "0xabc"}]
    monkeypatch.setattr(client, "_get", lambda *a, **kw: page)
    trades = client.get_trades_window("cid", start_ts=1600000000, end_ts=1800000000)
    assert len(trades) == 1
    assert trades[0]["_ts_sec"] == 1700000000


# ---------------------------------------------------------------------------
# get_resolved_tennis_markets tests
# ---------------------------------------------------------------------------

def _mock_event(title, end_date, slug, tags, markets=None):
    return {
        "id": f"ev-{slug}",
        "title": title,
        "endDate": end_date,
        "startDate": end_date,  # close enough for test
        "slug": slug,
        "tags": [{"slug": t} for t in tags],
        "markets": markets or [],
    }


def _mock_h2h_market(condition_id, title, token_ids):
    import json as _json
    return {
        "conditionId": condition_id,
        "question": title,
        "slug": "some-h2h-slug",
        "clobTokenIds": _json.dumps(token_ids),
    }


def test_get_resolved_tennis_markets_filters_tennis_tag(monkeypatch):
    client = PolymarketClient()
    events = [
        _mock_event(
            "Madrid Open: Alcaraz vs Nadal", "2024-05-10T12:00:00Z", "atp-alcaraz-nadal-2024-05-10",
            ["tennis", "atp"],
            markets=[_mock_h2h_market("0xabc", "Madrid Open: Alcaraz vs Nadal", ["tokA", "tokB"])],
        ),
        # NOT tennis — must be excluded
        _mock_event(
            "NBA: Lakers vs Warriors", "2024-05-10T12:00:00Z", "nba-lakers-warriors-2024-05-10",
            ["basketball", "nba"],
            markets=[_mock_h2h_market("0xdef", "NBA: Lakers vs Warriors", ["tokX", "tokY"])],
        ),
    ]
    calls = {"n": 0}
    def fake_get(url, params):
        calls["n"] += 1
        return events if calls["n"] == 1 else []  # one page, then empty
    monkeypatch.setattr(client, "_get", fake_get)
    out = client.get_resolved_tennis_markets(
        since_ts=1704067200,  # 2024-01-01
        until_ts=1735689600,  # 2025-01-01
    )
    assert len(out) == 1
    assert out[0]["condition_id"] == "0xabc"
    assert out[0]["title"] == "Madrid Open: Alcaraz vs Nadal"
    assert out[0]["token_id_yes"] == "tokA"
    assert out[0]["token_id_no"] == "tokB"
    assert out[0]["tour_prefix"] == "atp"


def test_get_resolved_tennis_markets_requires_h2h_market(monkeypatch):
    # Event with tennis tag and 'vs' in title, but no market where question==title -> excluded
    client = PolymarketClient()
    e = _mock_event(
        "Madrid Open: Alcaraz vs Nadal", "2024-05-10T12:00:00Z", "atp-alcaraz-nadal-2024-05-10",
        ["tennis"],
        markets=[
            {
                "conditionId": "0xother",
                "question": "Total Sets O/U 2.5",  # different from title
                "slug": "sets",
                "clobTokenIds": '["tokA","tokB"]',
            }
        ],
    )
    calls = {"n": 0}
    def fake_get(url, params):
        calls["n"] += 1
        return [e] if calls["n"] == 1 else []
    monkeypatch.setattr(client, "_get", fake_get)
    out = client.get_resolved_tennis_markets(
        since_ts=1704067200, until_ts=1735689600,
    )
    assert out == []


def test_get_resolved_tennis_markets_uses_moneyline_title_variant(monkeypatch):
    client = PolymarketClient()
    e = _mock_event(
        "Madrid Open: Raphael Collignon vs Matteo Berrettini",
        "2026-04-29T12:00:00Z",
        "atp-collign-berrett-2026-04-22",
        ["tennis"],
        markets=[
            {
                "conditionId": "0xset",
                "question": "Set 1 Winner: Collignon vs Berrettini",
                "sportsMarketType": "tennis_first_set_winner",
                "clobTokenIds": '["setA","setB"]',
            },
            {
                "conditionId": "0xmatch",
                "question": "Collignon vs. Berrettini",
                "sportsMarketType": "moneyline",
                "clobTokenIds": '["matchA","matchB"]',
            },
        ],
    )
    calls = {"n": 0}
    def fake_get(url, params):
        calls["n"] += 1
        return [e] if calls["n"] == 1 else []
    monkeypatch.setattr(client, "_get", fake_get)
    out = client.get_resolved_tennis_markets(since_ts=1767225600, until_ts=1798761600)
    assert len(out) == 1
    assert out[0]["condition_id"] == "0xmatch"
    assert out[0]["title"] == "Madrid Open: Raphael Collignon vs Matteo Berrettini"


def test_get_resolved_tennis_markets_skips_doubles(monkeypatch):
    client = PolymarketClient()
    e = _mock_event(
        "Madrid Open Doubles: Arevalo/Pavic vs Bolelli/Vavassori",
        "2026-04-29T12:00:00Z",
        "atp-arevalo-pavic-bolelli-vavassori-2026-04-22",
        ["tennis"],
        markets=[
            {
                "conditionId": "0xdoubles",
                "question": "Madrid Open Doubles: Arevalo/Pavic vs Bolelli/Vavassori",
                "sportsMarketType": "moneyline",
                "clobTokenIds": '["a","b"]',
            }
        ],
    )
    calls = {"n": 0}
    def fake_get(url, params):
        calls["n"] += 1
        return [e] if calls["n"] == 1 else []
    monkeypatch.setattr(client, "_get", fake_get)
    assert client.get_resolved_tennis_markets(since_ts=1767225600, until_ts=1798761600) == []


def test_get_resolved_tennis_markets_requires_vs_in_title(monkeypatch):
    # Outright-winner event with tennis tag but no "vs" -> excluded
    client = PolymarketClient()
    e = _mock_event(
        "French Open Winner", "2024-06-09T12:00:00Z", "french-open-winner-2024",
        ["tennis"],
        markets=[_mock_h2h_market("0xxx", "French Open Winner", ["a", "b"])],
    )
    calls = {"n": 0}
    def fake_get(url, params):
        calls["n"] += 1
        return [e] if calls["n"] == 1 else []
    monkeypatch.setattr(client, "_get", fake_get)
    out = client.get_resolved_tennis_markets(since_ts=1704067200, until_ts=1735689600)
    assert out == []


def test_get_resolved_tennis_markets_dedupes_by_event_id(monkeypatch):
    # Same event appearing in two per-year windows (e.g. 2024 + 2025 boundary) should dedupe by id
    client = PolymarketClient()
    e = _mock_event(
        "Madrid: A vs B", "2024-12-31T23:00:00Z", "atp-a-b",
        ["tennis"],
        markets=[_mock_h2h_market("0xcid", "Madrid: A vs B", ["tokA","tokB"])],
    )
    # Return the same event on every _get call
    monkeypatch.setattr(client, "_get", lambda *a, **kw: [e] if kw.get('params', {}).get('offset', 0) == 0 else [])
    # Force two year-windows to yield the same event
    out = client.get_resolved_tennis_markets(since_ts=1704067200, until_ts=1767225600)  # 2024-01-01 .. 2026-01-01
    assert len(out) == 1  # deduped


def test_get_resolved_tennis_markets_paginates_offsets(monkeypatch):
    client = PolymarketClient()
    page1 = [_mock_event(
        f"Madrid: P{i} vs Q{i}", "2024-05-10T12:00:00Z", f"atp-p{i}-q{i}",
        ["tennis"],
        markets=[_mock_h2h_market(f"0xcid{i}", f"Madrid: P{i} vs Q{i}", [f"tA{i}", f"tB{i}"])],
    ) for i in range(500)]
    page2 = [_mock_event(
        f"Madrid: R{i} vs S{i}", "2024-05-15T12:00:00Z", f"atp-r{i}-s{i}",
        ["tennis"],
        markets=[_mock_h2h_market(f"0xcid5{i}", f"Madrid: R{i} vs S{i}", [f"tR{i}", f"tS{i}"])],
    ) for i in range(50)]  # partial page -> stops
    calls = {"n": 0}
    def fake_get(url, params):
        calls["n"] += 1
        if calls["n"] == 1: return page1
        if calls["n"] == 2: return page2
        return []
    monkeypatch.setattr(client, "_get", fake_get)
    out = client.get_resolved_tennis_markets(
        since_ts=int(__import__('datetime').datetime(2024, 1, 1).timestamp()),
        until_ts=int(__import__('datetime').datetime(2024, 12, 31, 23, 59, 59).timestamp()),
        page_size=500,
    )
    assert len(out) == 550
