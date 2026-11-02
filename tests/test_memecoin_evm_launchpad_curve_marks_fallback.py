"""`_launchpad_curve_price_fallback` (memecoin_real_runner.py): fills a price_map gap for a
curve-phase 4663 position DexScreener has no row for, and never touches an existing entry."""
import sniper.services.memecoin_real_runner as runner

MINT = "0x5e9ca04c6fe6397f287091130a63072a91afd14f"
OTHER_MINT = "0x" + "22" * 20


def _on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", True)
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "https://rpc.example")


def test_off_by_default_leaves_price_map_untouched():
    positions = [{"network_id": "4663", "mint": MINT}]
    out = runner._launchpad_curve_price_fallback(positions, {}, "now")
    assert out == {}


def test_fills_a_gap_for_a_curve_phase_mint(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(
        "sniper.execution.evm_launchpad_curve.mark_price_entry",
        lambda chain, mint, rpc_url, observed_at=None: {
            "price": 1.5, "observed_at": observed_at, "source": "launchpad_curve",
            "quality": "live", "liquidity": None, "mcap": None})
    positions = [{"network_id": "4663", "mint": MINT}]
    out = runner._launchpad_curve_price_fallback(positions, {}, "now")
    assert out
    (only_key,) = out.keys()
    assert out[only_key]["source"] == "launchpad_curve"
    assert out[only_key]["price"] == 1.5


def test_never_overrides_an_existing_entry(monkeypatch):
    _on(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "sniper.execution.evm_launchpad_curve.mark_price_entry",
        lambda *a, **k: calls.append(1) or {"price": 999, "source": "launchpad_curve",
                                            "quality": "live", "observed_at": None,
                                            "liquidity": None, "mcap": None})
    from sniper.persistence import db_memecoin as m
    key = m.coin_identity("4663", MINT)
    existing = {key: {"price": 1.23, "source": "dexscreener", "quality": "live"}}
    positions = [{"network_id": "4663", "mint": MINT}]
    out = runner._launchpad_curve_price_fallback(positions, dict(existing), "now")
    assert out[key] == existing[key]
    assert calls == []                                             # never even called


def test_skips_a_non_4663_position(monkeypatch):
    _on(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "sniper.execution.evm_launchpad_curve.mark_price_entry",
        lambda *a, **k: calls.append(1))
    positions = [{"network_id": "8453", "mint": OTHER_MINT}]
    out = runner._launchpad_curve_price_fallback(positions, {}, "now")
    assert out == {}
    assert calls == []


def test_a_read_failure_for_one_mint_never_raises(monkeypatch):
    _on(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr("sniper.execution.evm_launchpad_curve.mark_price_entry", boom)
    positions = [{"network_id": "4663", "mint": MINT}]
    out = runner._launchpad_curve_price_fallback(positions, {}, "now")
    assert out == {}
