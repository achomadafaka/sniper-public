"""The desk's entries bid their coin pool's measured fee (operator 2026-10-23): the route's coin hop names the pool, and
it is registered with the sampler at quote time."""
from sniper.execution import solana_real_seams as S

USDC, SOL, COIN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "So11111111111111111111111111111111111111112", "CoinMint"


def quote(*hops, out=COIN):
    return {"outputMint": out, "routePlan": [{"swapInfo": {"ammKey": a, "inputMint": i, "outputMint": o}}
                                             for a, i, o in hops]}


def test_a_two_hop_desk_route_names_the_coin_pool():
    q = quote(("ObsidianPool", USDC, SOL), ("PumpCurve", SOL, COIN))
    assert S.entry_bid_pool(q) == "PumpCurve"                       # was None: every desk entry bid the cold fallback


def test_one_pool_routes_and_unreadable_routes():
    assert S.entry_bid_pool(quote(("OnlyPool", USDC, COIN))) == "OnlyPool"
    assert S.entry_bid_pool(quote(("A", USDC, COIN), ("B", USDC, COIN))) is None     # split into the coin: ambiguous
    assert S.entry_bid_pool({"routePlan": "x"}) is None and S.entry_bid_pool(None) is None


def test_the_entry_quote_registers_the_coin_pool(monkeypatch):
    from solders.keypair import Keypair
    registered = []

    class Bids:
        def register(self, pool):
            registered.append(pool)

        def select(self, **kw):
            raise AssertionError("not here")
    ex = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc.invalid",
                                      http=lambda *a, **k: (200, {}, None), usdc_fn=lambda: 0, token_fn=lambda m: 0,
                                      entry_bids=Bids())
    q = quote(("ObsidianPool", USDC, SOL), ("PumpCurve", SOL, COIN))
    monkeypatch.setattr(S.EntryQuoteMissRecorder, "quote", lambda self, *a, **k: q)
    assert ex["entry_quote_fn"](COIN, 25.0) is q and registered == ["PumpCurve"]
