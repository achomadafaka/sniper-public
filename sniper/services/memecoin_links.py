"""Build operator links from canonical chain and event identity."""
from urllib.parse import quote, urlencode
import re

from sniper.services.memecoin_chains import canonical_chain_id

FOMO_CHAIN_SLUG = {
    'solana': 'solana', '4663': 'robinhood', '56': 'bnb',
    '8453': 'base', '1': 'ethereum', '143': 'monad',
}
# Axiom token pages (operator, 2026-09-13: "change the fomo links to axiom"). Axiom trades Solana
# and BNB Chain only, so the other chains get no token link; the chart and explorer links remain.
AXIOM_CHAIN_QUERY = {'solana': '', '56': '?chain=bnb'}
# CHART: GMGN only (operator, 2026-09-22: DexScreener is slow, ugly, and its pages often fail
# with "no pair found"; 2026-09-23: "completely remove dex links", GMGN renders Robinhood and Arc
# token pages too, verified in the browser on DOAI (4663) and CRA (5042)). GMGN renders a token
# page by mint, including coins still on a curve. A chain with no GMGN slug gets no chart link,
# never a DexScreener one.
GMGN_CHAIN_SLUG = {'solana': 'sol', '56': 'bsc', '8453': 'base', '1': 'eth',
                   '4663': 'robinhood', '5042': 'arc'}
_EXPLORERS = {
    'solana': 'https://solscan.io/tx/',
    '4663': 'https://robinhoodchain.blockscout.com/tx/',
    '56': 'https://bscscan.com/tx/',
    '8453': 'https://basescan.org/tx/',
    '57073': 'https://explorer.inkonchain.com/tx/',
    '1': 'https://etherscan.io/tx/',
}


def _public_id(value, chain, size):
    if chain != 'solana':
        return re.fullmatch(r'0x[0-9a-fA-F]{%d}' % (size * 2), value) is not None
    if not 32 <= len(value) <= 88:
        return False
    import base58
    try:
        return len(base58.b58decode(value)) == size
    except (ValueError, TypeError):
        return False


def operator_links(network_id, mint=None, *, handle=None, tx_sig=None,
                   origin_event_id=None, position_id=None, pair_url=None):
    """Return available provider, explorer, entry, and position URLs.

    `pair_url` is accepted for callers that still pass a provider pair record and is ignored: the
    chart link is the GMGN token page, never a DexScreener pair.
    Internal paths contain public identity only. Telegram must add the configured dashboard origin.
    """
    chain = canonical_chain_id(network_id)
    if not chain and str(network_id or '').strip() == '143':
        chain = '143'  # Fomo supports Monad source notices; this does not grant funded authority.
    links = {}
    handle = str(handle or '').strip()
    if handle and handle not in {'(real)', '(manual)'}:
        links['trader'] = 'https://fomo.family/profile/' + quote(handle, safe='')
    if chain not in FOMO_CHAIN_SLUG and chain not in GMGN_CHAIN_SLUG and chain not in _EXPLORERS:
        return links
    mint = str(mint or '').strip()
    if _public_id(mint, chain, 32 if chain == 'solana' else 20):
        if chain in AXIOM_CHAIN_QUERY:
            links['token'] = f'https://axiom.trade/t/{quote(mint, safe="")}{AXIOM_CHAIN_QUERY[chain]}'
        if chain in _EXPLORERS:
            path = 'token' if chain == 'solana' else 'address'
            links['explorer'] = _EXPLORERS[chain].removesuffix('tx/') + path + '/' + mint
        if chain in GMGN_CHAIN_SLUG:
            links['chart'] = f'https://gmgn.ai/{GMGN_CHAIN_SLUG[chain]}/token/{quote(mint, safe="")}'
    tx = str(tx_sig or '').strip()
    if chain in _EXPLORERS and _public_id(tx, chain, 64 if chain == 'solana' else 32):
        links['transaction'] = _EXPLORERS[chain] + tx
    if origin_event_id is not None and str(origin_event_id).strip():
        links['entry'] = '/?' + urlencode({
            'tab': 'memecoin', 'mc_view': 'entries', 'mc_chain': chain,
            'mc_origin': str(origin_event_id).strip(),
        })
    if type(position_id) is int and position_id > 0:
        links['position'] = '/?' + urlencode({
            'tab': 'memecoin', 'mc_view': 'live', 'mc_chain': chain, 'mc_position': position_id,
        })
    return links
