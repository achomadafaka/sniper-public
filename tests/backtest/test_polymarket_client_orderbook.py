from data.polymarket_client import PolymarketClient


def test_get_orderbook_calls_public_book_endpoint(monkeypatch):
    calls = []
    client = PolymarketClient()

    def fake_get(url, params=None):
        calls.append((url, params))
        return {"asset_id": "tok", "bids": [], "asks": []}

    monkeypatch.setattr(client, "_get", fake_get)

    out = client.get_orderbook("tok")

    assert out["asset_id"] == "tok"
    assert calls == [("https://clob.polymarket.com/book", {"token_id": "tok"})]
