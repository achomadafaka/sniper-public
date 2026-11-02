"""Use the tape's real timestamp and unit contracts."""
from datetime import datetime, timezone
import math
import pytest
from test_memecoin_onchain_volume import _conn, _price, CHAIN, MINT, AT
from sniper.services import memecoin_onchain_volume as ov


def test_iso_block_times_match_the_real_tape_schema():
    c = _conn([(100, 'other', 250)])
    c.execute('UPDATE block_times SET block_ts=?', (datetime.fromtimestamp(AT-10, timezone.utc).isoformat(),))
    assert ov.onchain_volume_m5(c, CHAIN, MINT, AT, price_fn=_price)['usd'] == 250


@pytest.mark.parametrize('field,value', [('quote_side', None), ('token0_decimals', None)])
def test_missing_quote_metadata_never_defaults_to_eighteen(field, value):
    c = _conn([(100, 'other', 250)])
    c.execute(f'UPDATE pool_meta SET {field}=?', (value,))
    assert ov.onchain_volume_m5(c, CHAIN, MINT, AT, price_fn=_price)['usd'] is None


@pytest.mark.parametrize('price', [float('nan'), float('inf'), -1, 0])
def test_invalid_usd_price_is_unknown(price):
    assert ov.onchain_volume_m5(_conn([(100, 'other', 250)]), CHAIN, MINT, AT,
                               price_fn=lambda *_:price)['usd'] is None


def test_unknown_token_is_not_valued_as_the_chain_quote(monkeypatch):
    from sniper.execution import evm_wallet as wallet
    calls=[]
    monkeypatch.setattr(wallet, 'quote_usd_price', lambda *a, **k:calls.append(a) or 2400)
    assert ov._default_price(CHAIN, MINT) is None
    assert not calls


def test_heads_after_the_decision_do_not_prove_historical_coverage():
    out=ov.onchain_volume_m5(_conn([], blocks=[(100, AT+100)]), CHAIN, MINT, AT, price_fn=_price)
    assert out['usd'] is None


def test_native_eth_is_not_one_dollar_on_a_usdc_quoted_chain(monkeypatch):
    from sniper.execution import evm_wallet as wallet
    monkeypatch.setattr(wallet,'quote_usd_price',lambda *a,**k:1.0)
    # Chain 1 quotes in WETH since the eth-mainnet build, so no real chain is USDC-quoted with a
    # separate native coin any more. The rule still matters for one that is: pin that shape.
    monkeypatch.setitem(wallet.CHAIN_QUOTE, '1',
                        {"token": wallet.CHAIN_USDC['1'], "decimals": 6, "kind": "usd"})
    assert ov._default_price('1','0x'+'00'*20) is None
