"""Gated live smoke for the Goldsky fill fetch. Skips unless SNIPER_LIVE_SUBGRAPH=1 so CI
never hits the network. The endpoint is the public Goldsky orderbook-subgraph (no key)."""
import os

import pytest

LIVE = os.environ.get("SNIPER_LIVE_SUBGRAPH") == "1"


@pytest.mark.skipif(not LIVE, reason="set SNIPER_LIVE_SUBGRAPH=1 to run the live Goldsky smoke")
def test_live_fetch_returns_fills_for_known_wallet(tmp_path):
    from sniper.feeds.goldsky_fills import fetch_wallet_fills
    from sniper.analysis.wallet_protocol import subgraph_reader as SR
    fills = fetch_wallet_fills(
        "0xdbade4c82fb72780a0db9a38f821d8671aba9c95",
        cache_dir=str(tmp_path),
        fetch_fn=lambda w: SR.fetch_order_filled_events(w, side="both", max_rows=5),
    )
    assert isinstance(fills, list) and fills                 # real call; known active wallet
    f = fills[0]
    assert f.tx_hash and f.maker and f.ts > 0
