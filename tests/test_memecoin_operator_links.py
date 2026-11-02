"""Check chain identity and public links for operator results."""
from urllib.parse import parse_qs, urlsplit

import pytest

MINT = 'EDkoM2qSnKjjr3DjyQVbvz4wyFqf3CncKrFCu9MQAoDv'
TOKEN = '0x1c1daef0300551adbfbe403e7d567b6c5aff566f'
TX = '0x' + 'a' * 64


@pytest.mark.parametrize('alias, chain, slug, explorer', [
    ('robin', '4663', 'robinhood', 'robinhoodchain.blockscout.com'),
    ('bsc', '56', 'bnb', 'bscscan.com'),
    ('base', '8453', 'base', 'basescan.org'),
    ('ethereum', '1', 'ethereum', 'etherscan.io'),
])
def test_chain_alias_has_the_same_token_and_transaction_links(alias, chain, slug, explorer):
    from sniper.services.memecoin_links import operator_links
    links = operator_links(alias, TOKEN, tx_sig=TX)
    assert links == operator_links(chain, TOKEN, tx_sig=TX)
    assert links.get('token') == ('https://axiom.trade/t/%s?chain=bnb' % TOKEN if chain == '56' else None)
    assert links['transaction'] == f'https://{explorer}/tx/{TX}'


def test_solana_case_and_profile_segment_are_preserved():
    from sniper.services.memecoin_links import operator_links
    links = operator_links('SOL', MINT, handle='name/with ?#')
    assert links['token'] == 'https://axiom.trade/t/' + MINT
    assert links['chart'] == f'https://gmgn.ai/sol/token/{MINT}'
    assert links['trader'] == 'https://fomo.family/profile/name%2Fwith%20%3F%23'


def test_internal_links_select_the_exact_event_and_position():
    from sniper.services.memecoin_links import operator_links
    links = operator_links('robinhood', TOKEN, origin_event_id='swap:a/b?&', position_id=48515)
    assert parse_qs(urlsplit(links['entry']).query) == {
        'tab': ['memecoin'], 'mc_view': ['entries'], 'mc_chain': ['4663'], 'mc_origin': ['swap:a/b?&'],
    }
    assert parse_qs(urlsplit(links['position']).query) == {
        'tab': ['memecoin'], 'mc_view': ['live'], 'mc_chain': ['4663'], 'mc_position': ['48515'],
    }


def test_unknown_chain_does_not_guess_token_or_transaction_destinations():
    from sniper.services.memecoin_links import operator_links
    assert operator_links('999999', TOKEN, tx_sig=TX, origin_event_id='x', position_id=1) == {}


@pytest.mark.parametrize('chain, mint, expected', [
    ('solana', MINT, 'https://solscan.io/token/' + MINT),
    ('56', TOKEN, 'https://bscscan.com/address/' + TOKEN),
    ('4663', TOKEN, 'https://robinhoodchain.blockscout.com/address/' + TOKEN),
])
def test_public_token_explorer_does_not_require_fomo_login(chain, mint, expected):
    from sniper.services.memecoin_links import operator_links
    assert operator_links(chain, mint)['explorer'] == expected


@pytest.mark.parametrize('pair_url', [
    'https://dexscreener.com.evil.example/robinhood/' + TOKEN,
    'https://dexscreener.com/robinhood/' + TOKEN,
    f'https://dexscreener.com/robinhood/{"0x" + "1" * 40}?utm_source=x',
    None,
])
def test_pair_url_never_changes_the_chart_link(pair_url):
    from sniper.services.memecoin_links import operator_links
    # 2026-09-23: "completely remove dex links". A provider pair record, verified or not, is ignored.
    assert operator_links('4663', TOKEN, pair_url=pair_url)['chart'] == f'https://gmgn.ai/robinhood/token/{TOKEN}'


@pytest.mark.parametrize('chain, mint, expected', [
    ('solana', MINT, 'https://gmgn.ai/sol/token/' + MINT),
    ('56', TOKEN, 'https://gmgn.ai/bsc/token/' + TOKEN),
    ('8453', TOKEN, 'https://gmgn.ai/base/token/' + TOKEN),
    ('4663', TOKEN, 'https://gmgn.ai/robinhood/token/' + TOKEN),
    ('5042', TOKEN, 'https://gmgn.ai/arc/token/' + TOKEN),
])
def test_chart_link_uses_gmgn_where_it_covers_the_chain(chain, mint, expected):
    """Operator, 2026-09-22: DexScreener is slow and often shows 'no pair found'. GMGN renders the
    token page by mint, curve coins included; Robinhood has no GMGN page and keeps DexScreener."""
    from sniper.services.memecoin_links import operator_links
    links = operator_links(chain, mint, pair_url=f'https://dexscreener.com/solana/{MINT}')
    assert links['chart'] == expected


@pytest.mark.parametrize('chain,mint,tx', [('solana', '../bad', 'not-a-signature'), ('56', 'x/y', 'javascript:1')])
def test_malformed_public_identifiers_do_not_become_links(chain, mint, tx):
    from sniper.services.memecoin_links import operator_links
    assert operator_links(chain, mint, tx_sig=tx) == {}


@pytest.mark.parametrize('handle', ['(real)', '(manual)'])
def test_placeholder_handles_do_not_create_trader_links(handle):
    from sniper.services.memecoin_links import operator_links
    assert 'trader' not in operator_links('solana', handle=handle)
