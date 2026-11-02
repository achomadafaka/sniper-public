from types import SimpleNamespace
from sniper.services.memecoin_signal import normalize, is_actionable, CopySignal

def test_solana_processed_is_not_actionable():
    assert is_actionable("processed", "solana") is False
    assert is_actionable("finalized", "solana") is True

def test_evm_reorged_is_not_actionable():
    assert is_actionable("reorged", "4663") is False
    assert is_actionable("finalized", "4663") is True

def test_normalize_maps_feed_event():
    ev = SimpleNamespace(mint="MINTA", source_user_id="u1", fomo_event_id="e1",
                         network_id="solana", their_price=0.001, their_size_usd=2000.0,
                         source_ts=None, received_at_normalized="2026-07-12T10:00:00+00:00")
    sig = normalize(ev)
    assert isinstance(sig, CopySignal)
    assert sig.token_address == "MINTA" and sig.chain_id == "solana"
    assert sig.source_wallet == "u1" and sig.source_tx == "e1"
