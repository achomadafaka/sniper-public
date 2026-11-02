"""verify_solana_fomo_buys reads v1 transactions (2026-10-02): asked with maxSupportedTransactionVersion
0, Helius refused every window that held a v1 transaction (-32015), so most candidates could not be
checked at all (sunnydog06's dEQ1pBbN and CdmUMzFw)."""
from scripts.research import verify_solana_fomo_buys as v


def test_the_window_read_accepts_v1_transactions():
    asked = []

    def rpc(method, params):
        asked.append(params[1])
        return {"data": []}
    assert v.check(rpc, "Addr", [("Mint", "2026-10-02T10:00:00Z")], guard=lambda: None) == [("Mint", None)]
    assert asked[0]["maxSupportedTransactionVersion"] == 1
