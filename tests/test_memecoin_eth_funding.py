import pytest
from scripts import memecoin_bridge_solana_robinhood as bridge


def test_ethereum_funding_quotes_our_wallet_on_chain_one_and_keeps_old_default(monkeypatch):
    sent = []
    monkeypatch.setattr(bridge, "_http_post_json", lambda url, body: sent.append(body) or {})
    bridge.relay_quote("sol-wallet", "our-evm-wallet", 110_000_000, bridge.SOLANA_USDC,
                       destination_chain=1)
    assert sent[-1]["destinationChainId"] == 1
    assert sent[-1]["recipient"] == "our-evm-wallet"
    assert sent[-1]["amount"] == "110000000" and sent[-1]["destinationCurrency"] == bridge.NATIVE
    bridge.relay_quote("sol-wallet", "our-evm-wallet", 110_000_000, bridge.SOLANA_USDC)
    assert sent[-1]["destinationChainId"] == 4663
    with pytest.raises(ValueError):
        bridge.relay_quote("sol-wallet", "our-evm-wallet", 110_000_000, bridge.SOLANA_USDC,
                           destination_chain=8453)
    assert len(sent) == 2


def test_funding_refuses_conflicting_state_root_before_loading_wallets(monkeypatch, tmp_path):
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path / 'actual'))
    monkeypatch.setattr('sys.argv', ['bridge', '--token', 'usdc', '--amount', '110',
        '--destination-chain', '1', '--state-root', str(tmp_path / 'wrong')])
    with pytest.raises(SystemExit, match='conflicts'):
        bridge.main()
