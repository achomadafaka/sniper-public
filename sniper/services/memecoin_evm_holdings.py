"""List known EVM tokens outside open positions and execute operator sell requests.

The runner checks the whole token balance and returns the chain's bankroll token to the same wallet.
Robinhood manual LI.FI routes use separate Nordstern, Fly, and KyberSwap validation.
Code proofs precede approval and swap signatures. The runner simulates each final swap.
The wallet lock and book checks defer each request while a trade is active.
A swap with an unknown receipt remains submitted until the resolver obtains its receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import math
import os
import re
import threading
import time

from sniper.execution.holding_routes import decode_holding_quote, manifest_for, verify_holding_quote

logger = logging.getLogger(__name__)

CHAIN = "4663"
EXPLORER_TOKENS = ("https://robinhoodchain.blockscout.com/api/v2/addresses/%s/tokens?type=ERC-20")
# The explorer refuses requests that do not look like its own web app (403 otherwise).
EXPLORER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json", "Referer": "https://robinhoodchain.blockscout.com/",
}
HTTP_TIMEOUT_S = 6.0
RECEIPT_WAIT_S = 45.0
SLIPPAGE = "0.05"
# The same tolerance for the ladder fallback, in bps, so both venues quote one policy.
_LADDER_SLIPPAGE_BPS = int(round(float(SLIPPAGE) * 10_000))
DS_TOKENS = "https://api.dexscreener.com/tokens/v1/robinhood/"
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "execution",
                             "lifi_4663_manifest.json")
MAX_KNOWN_CANDIDATES = 500


def supported_chains():
    """Enabled, already implemented EVM chains; this grants no new execution context."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.settings import memecoin as cfg
    return {str(c) for c in cfg.MEMECOIN_EVM_ALLOWLIST if str(c) in CHAIN_QUOTE}


def _chain(ctx):
    """Production contexts carry `chain`; legacy injected contexts may carry `chain_id`.

    Explicit absent/invalid or contradictory identity never falls back to Robinhood.
    """
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.services.memecoin_chains import canonical_chain_id
    identities = []
    for key, value in (('chain', ctx.get('chain')), ('chain_id', ctx.get('chain_id'))):
        if key not in ctx:
            continue
        if type(value) not in (str, int) or not str(value).strip():
            raise ValueError('unreadable EVM context chain identity')
        chain = canonical_chain_id(value)
        if chain not in CHAIN_QUOTE:
            raise ValueError('unsupported EVM context chain identity')
        identities.append(chain)
    if not identities:
        raise ValueError('missing EVM context chain identity')
    if len(set(identities)) > 1:
        raise ValueError('conflicting EVM context chain identities')
    return identities[0]


def _get_json(url, headers):
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.load(resp)


class _LadderRoute:
    """A venue-ladder sell quote, shaped like a decoded LI.FI route so one execution path runs.

    Carries `venue_ladder = True` so the sell skips the LI.FI-specific proof, which cannot apply
    to calldata we encoded ourselves against a configured router.
    """

    venue_ladder = True

    def __init__(self, quote, fallback_input):
        tx = quote["transaction"]
        allowance = ((quote.get("issues") or {}).get("allowance") or {})
        self.transaction = {"to": tx["to"], "data": tx.get("data") or "0x",
                            "value": tx.get("value") or 0}
        self.spender = allowance.get("spender") or tx["to"]
        self.input_atomic = int(quote.get("sellAmount") or fallback_input)
        self.output_min_atomic = int(quote.get("minBuyAmount") or 0)
        self.route = quote.get("route") or {}
        self.pre_transactions = quote.get('pre_transactions')


def _ladder_route(ctx, *, token, weth, raw):
    """Quote `raw` of `token` into the chain's quote token through its funded exit ladder.

    Never raises into the sell: a ladder that fails leaves the LI.FI refusal exactly as it was.
    The taker is bound when the ladder is built, so the proceeds land in the same wallet.
    """
    quote_fn = ctx.get("venue_quote_fn")
    if quote_fn is None:
        return None
    try:
        quote = quote_fn(token, weth, raw, slippage_bps=_LADDER_SLIPPAGE_BPS)
    except Exception:
        logger.exception("holding sell: the venue ladder raised for %s", token)
        return None
    if not isinstance(quote, dict):
        return None
    tx = quote.get("transaction") or {}
    if not tx.get("to") or not tx.get("data"):
        return None
    try:
        route = _LadderRoute(quote, raw)
    except (KeyError, TypeError, ValueError):
        logger.warning("holding sell: unreadable ladder quote for %s", token)
        return None
    # Never sell more than the balance the caller read, and never sign a route that promises
    # nothing: the floor check below prices `output_min_atomic`, so a zero there is not a sell.
    if route.output_min_atomic <= 0 or not 0 < route.input_atomic <= int(raw):
        return None
    logger.warning("%s holding sell: venue ladder answered for %s via %s", _chain(ctx), token,
                   route.route.get("protocol") or "unknown")
    return route


def wallet_tokens(address, *, get_fn=None, max_pages=10):
    """{token: (raw, decimals, symbol)} for every ERC-20 the explorer lists. Raises on a failed
    read: a partial list would drop holdings from the snapshot."""
    get_fn = get_fn or _get_json
    out, url = {}, EXPLORER_TOKENS % address
    for _ in range(max_pages):
        page = get_fn(url, EXPLORER_HEADERS)
        if not isinstance(page, dict) or not isinstance(page.get('items'), list):
            raise ValueError('unreadable explorer page')
        for item in page['items']:
            try:
                raw = int(item['value'])
                if raw < 0 or isinstance(item['value'], bool):
                    raise ValueError('invalid token balance')
                if raw > 0:
                    token = item['token']
                    value = token.get('decimals')
                    if (type(value) not in (str, int) or
                            not re.fullmatch(r'[0-9]+', str(value)) or not 0 <= int(value) <= 255):
                        raise ValueError('unknown token decimals')
                    out[str(token["address_hash"]).lower()] = (
                        raw, int(value), token.get("symbol"))
            except (KeyError, TypeError, ValueError):
                raise ValueError('unreadable explorer token inventory') from None
        nxt = (page or {}).get("next_page_params")
        if not nxt:
            return out
        from urllib.parse import urlencode
        url = EXPLORER_TOKENS % address + "&" + urlencode({k: v for k, v in nxt.items()
                                                        if v is not None})
    raise ValueError("explorer pagination exceeded page limit")


def dexscreener_prices(keys, *, get_fn=None):
    """{(chain, token): {"price", "symbol", "liquidity"}} from the deepest pair where the token is
    the BASE, 30 tokens per call. The shared batch keys a pair to one token only, and it dropped
    BABA (11 pairs) whenever USDG shared its call (2026-09-14). A failed call prices nothing."""
    get_fn = get_fn or _get_json
    from sniper.execution.solana_quote import FOMO_TO_DEX
    grouped = {}
    for chain, token in keys:
        grouped.setdefault(str(chain), []).append(token)
    out = {}
    for chain, tokens in grouped.items():
        slug = FOMO_TO_DEX.get(chain)
        if not slug:
            continue
        for i in range(0, len(tokens), 30):
            chunk = tokens[i:i + 30]
            try:
                pairs = get_fn('https://api.dexscreener.com/tokens/v1/' + slug + '/' +
                               ','.join(chunk), {"User-Agent": "Mozilla/5.0"}) or []
            except Exception as exc:
                logger.warning("%s other holdings: DexScreener unavailable (%s)", chain, exc)
                continue
            best = {}
            for p in pairs if isinstance(pairs, list) else ():
                try:
                    base = str(p["baseToken"]["address"]).lower()
                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                    price = float(p.get("priceUsd") or 0)
                    if p.get('chainId') not in (None, slug):
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                if (base in chunk and math.isfinite(price) and price > 0
                        and math.isfinite(liq) and liq >= best.get(base, {}).get("liquidity", -1)):
                    best[base] = {"price": price, "symbol": p["baseToken"].get("symbol"),
                                  "liquidity": liq}
            out.update({(chain, t): v for t, v in best.items()})
    return out


def holding_prices(keys, *, get_fn=None, balances=None):
    """Fill missing or depth-incompatible balance marks by exact chain and contract.

    `balances` are confirmed human token quantities, not atomic units. This runs only in the
    holdings worker; the existing bounded fallback never delays funded order filling.
    """
    from urllib.error import HTTPError
    from urllib.parse import urlencode

    get_fn = get_fn or _get_json
    out = dexscreener_prices(keys, get_fn=get_fn)
    for key, stats in list(out.items()):
        amount = (balances or {}).get(key)
        liquidity = stats.get('liquidity')
        if (all(isinstance(x, (int, float)) and not isinstance(x, bool)
                and math.isfinite(x) and x >= 0 for x in (amount, liquidity))
                and amount * stats['price'] > liquidity):
            del out[key]  # A tiny pool's marginal mark cannot value this balance.
    deadline = time.monotonic() + 12
    for chain, token in keys:
        if (chain, token) in out:
            continue
        # Limit the fallback pass to 12 seconds plus one request timeout.
        if time.monotonic() >= deadline:
            break
        try:
            data = get_fn('https://li.quest/v1/token?' + urlencode(
                {'chain': chain, 'token': token}), {'User-Agent': 'Mozilla/5.0'})
        except HTTPError as exc:
            if exc.code in (400, 404):
                continue
            logger.warning('%s holdings reference prices unavailable (%s)', chain, exc)
            break
        except Exception as exc:
            logger.warning('%s holdings reference prices unavailable (%s)', chain, exc)
            break
        try:
            if str(data['chainId']) != str(chain) or str(data['address']).lower() != token.lower():
                continue
            price = float(data['priceUSD'])
            if math.isfinite(price) and price > 0:
                out[(chain, token)] = {'price': price, 'symbol': data.get('symbol'),
                                       'liquidity': None}
        except (KeyError, TypeError, ValueError):
            continue
    return out


PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"   # the chain owner's endpoint, not custody


def _public_post(chain):
    """Independent read endpoint; never consume the funded custody pool for discovery."""
    import urllib.request
    from urllib.parse import urlsplit
    from sniper.execution.free_endpoint_registry import free_endpoints
    chain = str(chain)
    urls = [e.url for e in free_endpoints(chain) if e.role == 'public_rpc']
    url = os.getenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_' + chain, '').strip()
    if url:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname:
            raise ValueError('invalid independent observer endpoint')
        urls = [url]
    elif chain == '1':
        # Reuse the panel's keyless reader without changing the on-chain watcher's env or
        # borrowing custody capacity. onchain_inventory proves its chain before any balance.
        from sniper.services.memecoin_native_float import CHAIN1_PUBLIC_RPC, _https_post
        return _https_post(CHAIN1_PUBLIC_RPC)
    if not urls:
        raise RuntimeError('no independent public read endpoint configured')

    def post(body):
        req = urllib.request.Request(urls[0], data=json.dumps(body).encode(), headers={
            'User-Agent': EXPLORER_HEADERS['User-Agent'], 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as response:
            return json.load(response)
    return post


def _rpc_uint(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', value):
        raise ValueError('unreadable RPC integer')
    return int(value, 16)


def _prove_identity(post, chain):
    answer = post({'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []})
    try:
        if (not isinstance(answer, dict) or type(answer.get('id')) is not int
                or answer.get('id') != 1 or answer.get('error')
                or _rpc_uint(answer.get('result')) != int(chain)):
            raise ValueError('wrong identity')
    except (TypeError, ValueError):
        raise ValueError('public RPC chain identity unavailable or wrong') from None


def onchain_inventory(address, tokens, *, chain_id, post_fn=None):
    """Exact balances plus on-chain decimals for positive known contracts, with chain proof.

    Every dependency must answer completely; unknown metadata cannot silently become 18 decimals.
    Zero balances need no decimals and are returned as (0, None).
    """
    if not tokens:
        return {}
    if any(not re.fullmatch(r'0x[0-9a-fA-F]{40}', str(t)) for t in tokens):
        raise ValueError('invalid candidate contract')
    post = post_fn or _public_post(str(chain_id))
    _prove_identity(post, chain_id)
    balances = onchain_balances(address, tokens, post_fn=post, strict=True)
    if any(t not in balances for t in tokens):
        raise ValueError('incomplete on-chain balance batch')
    positive = [t for t in tokens if balances[t] > 0]
    decimals = {}
    for start in range(0, len(positive), 10):
        chunk = positive[start:start + 10]
        requests = [{'jsonrpc': '2.0', 'id': i, 'method': 'eth_call',
                     'params': [{'to': token, 'data': '0x313ce567'}, 'latest']}
                    for i, token in enumerate(chunk)]
        answers = post(requests)
        if not isinstance(answers, list):
            raise ValueError('unreadable token decimals batch')
        seen = set()
        for answer in answers:
            if not isinstance(answer, dict):
                raise ValueError('unreadable token decimals')
            identity = answer.get('id')
            if (type(identity) is not int or not 0 <= identity < len(chunk)
                    or identity in seen or answer.get('error')):
                raise ValueError('unreadable token decimals identity')
            value = _rpc_uint(answer.get('result'))
            if value > 255:
                raise ValueError('unreadable token decimals range')
            seen.add(identity)
            decimals[chunk[identity]] = value
        if len(seen) != len(chunk):
            raise ValueError('incomplete token decimals batch')
    return {t: (balances[t], decimals.get(t)) for t in tokens}


def onchain_balances(address, tokens, *, post_fn=None, strict=False):
    """{token: raw} read in public-RPC-sized JSON-RPC batches. A token whose
    call errors is absent (unknown, not zero). Raises when the batch itself fails.

    WHY (2026-09-14, RETAIL). The explorer's token list said we held 2,694,032 RETAIL; position
    47302 bought and sold exactly that on 09-01, and balanceOf is 0. Its balances can be stale, so
    it only nominates tokens; the chain decides the amount."""
    if not tokens:
        return {}
    if post_fn is None:
        post_fn = _public_post(CHAIN)
        _prove_identity(post_fn, CHAIN)
    owner = "0" * 24 + str(address).lower().replace("0x", "")
    body = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
             "params": [{"to": t, "data": "0x70a08231" + owner}, "latest"]}
            for i, t in enumerate(tokens)]
    out = {}
    # The official host returns HTTP 429 for the whole 79-call wallet batch.
    for start in range(0, len(body), 10):
        batch = body[start:start + 10]
        answers = post_fn(batch)
        if not isinstance(answers, list):
            raise RuntimeError("balance batch answered %r" % (str(answers)[:80],))
        ids = {request['id'] for request in batch}
        seen = set()
        for a in answers:
            if isinstance(a, dict) and type(a.get('id')) is int and a['id'] in seen:
                raise ValueError('duplicate balance response identity')
            try:
                identity = a['id']
                if type(identity) is not int or identity not in ids or identity in seen:
                    raise ValueError('unreadable balance response identity')
                seen.add(identity)
                if a.get('error'):
                    raise ValueError('balance response error')
                out[tokens[identity]] = _rpc_uint(a.get('result'))
            except (TypeError, ValueError, KeyError, IndexError, AttributeError):
                if strict:
                    raise ValueError('unreadable or duplicate balance response') from None
                continue
        if strict and seen != ids:
            raise ValueError('incomplete balance response batch')
    return out


def snapshot_once(conn, *, address, now_iso, chain_id=CHAIN, list_fn=None, price_fn=None,
                  balance_fn=None, inventory_fn=None):
    """Replace one chain's complete known-inventory snapshot; preserve it on partial reads."""
    from sniper.persistence import db_memecoin as m
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    chain = str(chain_id)
    tally = {"state": None, "rows": 0, "unpriced": 0}
    try:
        if chain not in CHAIN_QUOTE:
            tally['state'] = 'unsupported_chain'
            return tally
        protected = m.protected_holding_mints(conn, chain)
        if protected is None:
            tally["state"] = "protected_unreadable"
            return tally
        protected = {str(p).lower() for p in protected}
        if chain == CHAIN:
            try:
                held = (list_fn or wallet_tokens)(address)
            except Exception as exc:
                logger.warning("%s other holdings: explorer unreadable (%s)", chain, type(exc).__name__)
                tally["state"] = "explorer_unreadable"
                return tally
        else:
            candidates = m.holding_candidates(conn, chain, limit=MAX_KNOWN_CANDIDATES + 1)
            if len(candidates) > MAX_KNOWN_CANDIDATES:
                tally['state'] = 'candidate_limit'
                return tally
            held = {str(row['mint']).lower(): (None, None, row.get('symbol'))
                    for row in candidates}
        just_sold = {str(x).lower() for x in
                     m.recently_sold_holdings(conn, chain, now_iso=now_iso, within_s=60)}
        quote_token = CHAIN_QUOTE[chain]["token"].lower()
        tokens = [t for t in held if t != quote_token and t not in protected and t not in just_sold]
        try:
            if chain == CHAIN:
                onchain = (balance_fn or onchain_balances)(address, tokens)
                inventory = {t: (onchain.get(t), held[t][1]) for t in tokens}
            else:
                inventory = (inventory_fn or onchain_inventory)(address, tokens, chain_id=chain)
                onchain = {t: v[0] for t, v in inventory.items()}
            for token in tokens:
                raw, decimals = inventory.get(token, (None, None))
                if (type(raw) is not int or raw < 0 or
                        (raw > 0 and (type(decimals) is not int or not 0 <= decimals <= 255))):
                    raise ValueError('incomplete on-chain inventory batch')
        except Exception as exc:
            from urllib.error import HTTPError
            status = exc.code if isinstance(exc, HTTPError) and type(exc.code) is int else None
            detail = (' status=%d' % status) if status is not None else ''
            logger.warning("%s other holdings: inventory unreadable (%s%s)",
                           chain, type(exc).__name__, detail)
            tally["state"] = "rpc_unreadable"            # keep the old snapshot
            return tally
        # The chain decides: a token it does not confirm above zero is not listed.
        tokens = [t for t in tokens if onchain[t] > 0]
        held = {t: (onchain[t], inventory[t][1], held[t][2]) for t in tokens}
        keys = [(chain, t) for t in tokens]
        prices = (price_fn(keys) if price_fn else holding_prices(keys, balances={
            (chain, t): held[t][0] / (10 ** held[t][1]) for t in tokens})) if tokens else {}
        rows = []
        for t in tokens:
            raw, decimals, symbol = held[t]
            amount = raw / (10 ** decimals)
            stats = prices.get((chain, t)) or {}
            price = stats.get("price")
            rows.append({"mint": t, "symbol": stats.get("symbol") or symbol, "amount": amount,
                         "amount_raw": raw, "decimals": decimals, "price_usd": price,
                         "value_usd": (amount * price) if price else None,
                         "liquidity_usd": stats.get("liquidity")})
        unpriced = sum(1 for r in rows if r["price_usd"] is None)
        m.replace_other_holdings(conn, chain, rows, observed_at=now_iso, unpriced_count=unpriced)
        tally.update(state="ok", rows=len(rows), unpriced=unpriced)
        return tally
    except Exception:
        logger.exception("%s other holdings snapshot failed", chain)
        tally["state"] = "error"
        return tally


def book_busy(conn, ctx, *, lock_dir=None):
    """None when this chain's wallet is quiet; unreadable money state counts as busy."""
    from sniper.persistence import db_memecoin as m
    chain = 'unknown'
    try:
        chain = _chain(ctx)
        n = conn.execute(
            "SELECT (SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
            "AND is_open=1 AND network_id=?) + (SELECT COUNT(*) FROM memecoin_pending_entries "
            "WHERE status='reserved' AND network_id=?)", (chain, chain)).fetchone()[0]
        if n:
            return "book_busy"
        if m.has_unresolved_real_exit(conn, chain):
            return 'exit_unresolved'
        if m.unresolved_lifi_lifecycles(conn, chain, str(ctx["address"])):
            return "lifi_unresolved"
        if m.unresolved_holding_sell_count(conn, chain):
            return 'holding_unresolved'
        if m.has_unresolved_evm_gas_topup(conn, chain):
            return 'gas_unresolved'
        from sniper.execution.exec_lock import intents_outstanding, standing_intent_lock_dir
        outstanding = intents_outstanding(
            chain, lock_dir=standing_intent_lock_dir(lock_dir=lock_dir), required=True)
        if outstanding is None:
            return 'book_unreadable'
        if outstanding:
            return 'standing_unresolved'
        pending, latest = ctx["pending_nonce_fn"](), ctx["confirmed_nonce_fn"]()
        if pending is None or latest is None:
            return "nonce_unreadable"
        if int(pending) != int(latest):
            return "tx_in_flight"
        return None
    except Exception:
        logger.exception("%s holding sell: could not read the book", chain)
        return "book_unreadable"


def _wait_receipt(receipt_fn, tx_hash, wait_s, sleep_fn):
    deadline = time.monotonic() + wait_s
    while True:
        try:
            receipt = receipt_fn(tx_hash)
        except Exception:
            receipt = None
        if _known_receipt(receipt):
            return receipt
        if time.monotonic() >= deadline:
            return None
        sleep_fn(0.5)


# The receipt read itself failed (timeout, refusal, malformed answer, a failed canonical check on
# chains 1/57073): the outcome is UNKNOWN, never "no receipt" (review EVM01, 2026-10-08).
_RECEIPT_UNKNOWN = object()


def _receipt_reader(ctx):
    """Prefer the production one-shot read; its unknown answer never falls into a long poll. Returns
    the receipt, None for a clean 'not_found', or _RECEIPT_UNKNOWN when the read failed."""
    once = ctx.get('confirm_once_fn')
    if not callable(once):
        return (ctx.get('lifi_local') or {}).get('receipt_fn')

    def read(tx_hash):
        state = once(tx_hash)
        if state == 'not_found':
            return None
        if state not in ('confirmed', 'failed'):
            return _RECEIPT_UNKNOWN
        receipt = getattr(state, 'receipt', None)
        if receipt is not None:
            return receipt
        if state == 'confirmed':
            return {'status': '0x1'}
        if state == 'failed':
            return {'status': '0x0'}
        return None
    return read


def _known_receipt(receipt):
    return isinstance(receipt, dict) and str(receipt.get("status")).lower() in (
        "0x0", "0x00", "0", "0x1", "0x01", "1")


def _ok(receipt):
    return str((receipt or {}).get("status")).lower() in ("0x1", "0x01", "1")


def _client():
    from sniper.execution.lifi_executor import LifiClient
    from sniper.util.clock import real_utc_now_iso

    def http(method, url, headers=None):
        import urllib.error
        import urllib.request
        try:
            req = urllib.request.Request(url, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return resp.status, json.load(resp), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except Exception:
                body = {}
            return exc.code, body, dict(exc.headers or {})
    # KEYLESS on purpose: the funded LI.FI quota and its authentication verdict are not touched.
    return LifiClient("https://li.quest/v1/quote", api_key="", manifest=_manifest(), http=http,
                      decoder=decode_holding_quote,
                      corrected_now=real_utc_now_iso)


def _manifest():
    with open(MANIFEST_PATH) as fh:
        return manifest_for(json.load(fh), 'nordstern')


@contextmanager
def _generation_after_action(key, *, lock_dir):
    """Finish before the enclosing wallet lock releases, including failed signing attempts."""
    changed = [False]  # Local to this locked pass, never shared between workers.
    try:
        yield changed
    finally:
        if changed[0]:
            from sniper.execution.exec_lock import touch_generation, latch_generation_fault
            try:
                touch_generation(key, lock_dir=lock_dir)
            except Exception as exc:
                latch_generation_fault('holding execution stamp %s: %s: %s'
                                       % (key, type(exc).__name__, exc))


def sell_once(conn, *, ctx, now_iso, min_sell_usd, client=None, eth_usd_fn=None,
              sleep_fn=None, lock_dir=None, wait_s=RECEIPT_WAIT_S, verify_route_fn=None):
    """Claim one chain-local sell intent and return its balance to the chain's quote token.
    Returns a tally; never raises. Sells nothing unless the wallet is quiet."""
    from sniper.persistence import db_memecoin as m
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.execution.evm_real_seams import encode_approve, rung_error_text
    from sniper.execution.lifi_runtime import build_lifi_request
    sleep_fn = sleep_fn or time.sleep
    local = ctx.get("lifi_local") or {}
    tally = {"state": None, "intent_id": None, "sig": None}
    try:
        chain = _chain(ctx)
        address = str(ctx['address'])
    except (KeyError, TypeError, ValueError):
        tally['state'] = 'invalid_context'
        return tally
    quote_meta = CHAIN_QUOTE.get(chain)
    if not quote_meta:
        tally['state'] = 'unsupported_chain'
        return tally
    weth = quote_meta['token']
    quote_decimals = quote_meta['decimals']
    receipt_fn = _receipt_reader(ctx)
    try:
        from sniper.execution.exec_lock import wallet_exec_lock, evm_exec_lock_key
        key = evm_exec_lock_key(chain, address)
        with (
            wallet_exec_lock(key, blocking=False, lock_dir=lock_dir) as held,
            _generation_after_action(key, lock_dir=lock_dir) as changed,
        ):
            if not held:
                tally["state"] = "wallet_busy"
                return tally
            busy = book_busy(conn, ctx, lock_dir=lock_dir)
            if busy:
                tally["state"] = busy
                return tally
            intent = m.claim_holding_sell(conn, chain, now_iso=now_iso)
            if intent is None:
                tally["state"] = "idle"
                return tally
            iid, token = intent["intent_id"], str(intent["mint"]).lower()
            tally["intent_id"] = iid

            def fail(reason, *, terminal=False):
                state = ('approval_submitted' if tally.get('transaction_phase') == 'approval'
                         else 'submitted') if tally.get('sig') and not terminal else 'failed'
                m.resolve_holding_sell(conn, iid, state, now_iso=now_iso, reason=reason)
                tally.update(state='failed' if state == 'failed' else 'unresolved', reason=reason)
                return tally

            def persist(signed, phase):
                import base64
                state = 'approval_submitted' if phase == 'approval' else 'submitted'
                m.resolve_holding_sell(conn, iid, state, now_iso=now_iso, sig=signed[1],
                                       balance_raw=raw, transaction_phase=phase,
                                       signed_payload_b64=base64.b64encode(
                                           signed[0].encode('ascii')).decode('ascii'))
                tally['sig'] = signed[1]
                tally['transaction_phase'] = phase

            def send(raw_signed, phase):
                factory = local.get('send_swap_fn_factory')
                send_fn = (factory(conn, iid, action='approval' if phase == 'approval' else 'exit',
                                   owner_kind='holding_sell') if factory else local['send_fn'])
                return send_fn(raw_signed)

            protected = m.protected_holding_mints(conn, chain)
            if protected is None:
                return fail("could not read the open positions; sold nothing")
            if token in {str(p).lower() for p in protected} or token == weth.lower():
                return fail("a position or the bankroll owns this token")
            def read_balance(mint):
                try:
                    return local["balance_raw_checked_fn"](mint)
                except Exception:
                    return None

            raw = read_balance(token)
            if type(raw) is not int or raw < 0:
                return fail("could not read the token balance; sold nothing")
            if raw <= 0:
                return fail("nothing left to sell; the balance is 0")
            weth_before = read_balance(weth)
            if type(weth_before) is not int or weth_before < 0:
                return fail("could not read the bankroll balance; sold nothing")

            decoded, result = None, None
            if chain == CHAIN:
                request = build_lifi_request("sell", raw, mint=token, sell_token=weth,
                                             custody=address, manifest=_manifest(), slippage=SLIPPAGE)
                result = (client or _client()).quote(request, role="holdings", stage="sell")
                decoded = getattr(result, "decoded", None)
                if not getattr(result, 'ok', False) or not getattr(decoded, 'transaction', None):
                    decoded = None
            if decoded is None:
                decoded = _ladder_route(ctx, token=token, weth=weth, raw=raw)
                if decoded is None:
                    if chain != CHAIN:
                        return fail('no route: the chain venue ladder refused')
                    if ctx.get("venue_quote_fn") is None:
                        return fail("no validated LI.FI route (%s)"
                                    % getattr(result, "failure_type", None))
                    return fail("no route: LI.FI (%s) and the venue ladder both refused"
                                % getattr(result, "failure_type", None))
            if getattr(decoded, 'pre_transactions', None):
                return fail('the route requires unsupported pre-transactions; sold nothing')
            eth_usd = (1.0 if quote_meta['kind'] == 'usd' else float(
                (eth_usd_fn or ctx.get("warm_quote_price_fn") or (lambda: 0))() or 0))
            floor_usd = decoded.output_min_atomic / (10 ** quote_decimals) * eth_usd
            if not math.isfinite(eth_usd) or eth_usd <= 0:
                return fail("the %s price is unavailable; sold nothing"
                            % quote_meta.get('usd_symbol', 'quote token'))
            if decoded.output_min_atomic <= 0:
                return fail("the route has no positive minimum output")
            if not intent.get("allow_small") and floor_usd < float(min_sell_usd):
                return fail("a sell returns at least $%.2f, under the $%.2f minimum"
                            % (floor_usd, float(min_sell_usd)))

            # The LI.FI verifier proves a THIRD PARTY's calldata: pinned code hashes, a decoded
            # swap, the Fly core. A ladder route has nothing for it to prove — the router comes
            # from our own immutable connector config and we encoded the calldata — so running it
            # there would fail on shape, not on safety. What guards a ladder route instead is its
            # own `minBuyAmount` (checked as the floor below) and `estimate_gas_fn`, which reverts
            # before anything is signed.
            verify = verify_route_fn or verify_holding_quote
            if not getattr(decoded, "venue_ladder", False):
                verify(decoded, ctx, _manifest())

            # EXACT approval for this amount to the validated spender, only when short.
            if int(local["allowance_fn"](token, address, decoded.spender)) < decoded.input_atomic:
                atx = {"to": token, "data": encode_approve(decoded.spender, decoded.input_atomic),
                       "value": 0}
                gas = int(local["estimate_gas_fn"](atx) * 1.3)
                transaction = local['construct_tx_fn'](atx, gas)
                changed[0] = True
                signed = local["sign_complete_fn"](transaction)
                if not signed:
                    return fail("the approval did not sign")
                persist(signed, 'approval')
                if send(signed[0], 'approval') is False:
                    return fail("the node refused the approval", terminal=True)
                receipt = _wait_receipt(receipt_fn, signed[1], wait_s, sleep_fn)
                if receipt is None:
                    return fail("the approval was not confirmed; sold nothing")
                if not _ok(receipt):
                    return fail('the approval reverted on chain', terminal=True)
                # Terminal approval proof permits the next action; any crash before its swap
                # binding still resolves as an approval, never as a completed sale.
                tally['sig'] = None

            tx = decoded.transaction
            stx = {"to": tx["to"], "data": tx.get("data", "0x"), "value": tx.get("value") or 0}
            try:
                gas = int(local["estimate_gas_fn"](stx) * 1.25)   # a revert fails here, unsigned
            except Exception as exc:
                return fail("swap gas check failed (%s)" % rung_error_text(exc))
            transaction = local["construct_tx_fn"](stx, gas)
            proof = None if getattr(decoded, "venue_ladder", False) else verify(
                decoded, ctx, _manifest())
            if proof is not None:
                if local["simulate_fn"](transaction, proof.block_number) is not True:
                    return fail("the swap simulation failed")
            changed[0] = True
            signed = local["sign_complete_fn"](transaction)
            if not signed:
                return fail("the swap did not sign")
            tx_hash = signed[1]
            persist(signed, 'swap')
            if send(signed[0], 'swap') is False:
                return fail("the node refused the swap", terminal=True)
            receipt = _wait_receipt(receipt_fn, tx_hash, wait_s, sleep_fn)
            if receipt is None:
                tally["state"] = "unresolved"       # evm_resolve_once settles it
                logger.warning("%s holding sell UNRESOLVED token=%s tx=%s", chain, token, tx_hash)
                return tally
            if not _ok(receipt):
                return fail("the swap reverted on chain", terminal=True)
            weth_after = read_balance(weth)
            got = weth_after - weth_before if type(weth_after) is int and weth_after >= 0 else 0
            usd = round(got / (10 ** quote_decimals) * eth_usd, 6) if got > 0 else None
            m.resolve_holding_sell(conn, iid, "sold", now_iso=now_iso, sig=tx_hash,
                                   usd_received=usd,
                                   reason=None if usd else "proceeds not measured")
            tally.update(state="sold", usd_received=usd)
            logger.warning("%s holding sell CONFIRMED intent=%s usd=%s tx=%s", chain, iid, usd, tx_hash)
            return tally
    except Exception:
        logger.exception("%s holding sell pass failed", chain)
        if tally.get("intent_id") and tally.get("state") is None:
            try:
                state = ('approval_submitted' if tally.get('transaction_phase') == 'approval'
                         else 'submitted') if tally.get('sig') else 'failed'
                m.resolve_holding_sell(conn, tally["intent_id"], state,
                                       now_iso=now_iso, reason="internal error; check the runner log")
            except Exception:
                logger.exception("%s holding sell: could not record the failure", chain)
        tally["state"] = "error"
        return tally


def resolve_once(conn, *, ctx, now_iso, lock_dir=None):
    """Settle terminal receipts under the same wallet lock as signing; never wait for the lock."""
    from sniper.execution.exec_lock import wallet_exec_lock, evm_exec_lock_key
    chain = 'unknown'
    try:
        chain = _chain(ctx)
        key = evm_exec_lock_key(chain, ctx['address'])
        with (
            wallet_exec_lock(key, blocking=False, lock_dir=lock_dir) as held,
            _generation_after_action(key, lock_dir=lock_dir) as changed,
        ):
            return _resolve_locked(conn, ctx=ctx, now_iso=now_iso, changed=changed) if held else 0
    except Exception:
        logger.exception('%s holding sell resolver failed', chain)
        return 0


# A SELL THAT NEVER MINES (Gamma HIGH on PR #407). Per (chain, intent): when its nonce was first seen spent
# by another transaction, when the node was first seen holding nothing of ours, whether it paged.
# Process memory, behind one lock: one resolver per chain runs at a time, but every chain shares it.
_STUCK = {}
_STUCK_LOCK = threading.Lock()
_NONCE_VIEW = {}                 # chain -> (monotonic, confirmed nonce, pending nonce)
NONCE_CHECK_S = 15.0             # the two nonce reads, at most this often per chain


def _payload_nonce(signed_payload_b64):
    """The nonce our signed bytes carry, or None (unknown: never a reason to fail the sell)."""
    from sniper.execution.evm_real_seams import decode_signed_tx_cost
    got = decode_signed_tx_cost(signed_payload_b64) if signed_payload_b64 else None
    try:
        return int(got["nonce"]) if got else None
    except (KeyError, TypeError, ValueError):
        return None


def _nonce_view(ctx, chain, clock):
    """(confirmed, pending) account nonces, read at most every NONCE_CHECK_S; None when unread."""
    hit = _NONCE_VIEW.get(chain)
    if hit is not None and clock() - hit[0] < NONCE_CHECK_S:
        return hit[1], hit[2]
    try:
        latest, pending = ctx["confirmed_nonce_fn"](), ctx["pending_nonce_fn"]()
    except Exception:
        logger.exception("%s holding sell: nonce read failed", chain)
        return None
    if latest is None or pending is None:
        return None
    _NONCE_VIEW[chain] = (clock(), int(latest), int(pending))
    return int(latest), int(pending)


def _page(text):
    """The operator page, straight to send_alert: no context seam a production context could lack
    (Alpha, tests/test_memecoin_evm_context_keys.py)."""
    try:
        from sniper.services.memecoin_alerts import send_alert
        send_alert("HOLDING-SELL-STUCK", text)
    except Exception:
        logger.exception("holding sell: page failed: %s", text)


def _unreadable_receipt(row, *, chain, clock):
    """Our receipt read failed: the sell keeps its row, bytes and the chain's fence, and pages once
    after MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN. A moved nonce proves SOME transaction at it mined,
    and ours is one candidate, so this never settles anything (review EVM01)."""
    from sniper.settings import memecoin as cfg
    iid, sig = row["intent_id"], row.get("sig")
    phase = row.get("transaction_phase") or "swap"
    with _STUCK_LOCK:
        st = _STUCK.setdefault((chain, iid), {})
    seen = st.setdefault("receipt_unknown_seen", clock())
    if (clock() - seen >= float(cfg.MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN) * 60.0
            and not st.get("paged_receipt")):
        st["paged_receipt"] = True
        _page("chain %s holding sell %s (%s tx %s): its receipt has been unreadable for %.0f+ min, "
              "so whether it mined is unknown. It is NOT failed; funded entries on chain %s stay "
              "fenced until a receipt reads. Not rebroadcast."
              % (chain, iid, phase, sig, float(cfg.MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN), chain))


def _unmined(conn, row, *, ctx, chain, now_iso, changed, receipt_fn, clock):
    """A signed sell whose node answered cleanly that it has no receipt. Returns 1 when it settled.
    Our nonce advanced past ours (confirmed nonce past it) for
    MEMECOIN_EVM_HOLDING_NONCE_SPENT_CONFIRM_S, with a second clean 'no receipt' for our hash, fails
    it: the reason says what was seen, not which transaction used the nonce. Our nonce unspent and
    nothing of ours in the node (pending == confirmed) for MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN pages
    the operator once with the chain, nonce and hash for a cancel. Never rebroadcasts. An
    unreadable nonce (payload or RPC view) pages once and never fails it (R-02)."""
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    iid, sig = row["intent_id"], row.get("sig")
    phase = row.get("transaction_phase") or "swap"
    with _STUCK_LOCK:
        st = _STUCK.setdefault((chain, iid), {})
    nonce = _payload_nonce(row.get("signed_payload_b64"))
    if nonce is None:
        seen = st.setdefault("unknown_seen", clock())
        if (clock() - seen >= float(cfg.MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN) * 60.0
                and not st.get("paged_unknown")):
            st["paged_unknown"] = True
            _page("chain %s holding sell %s (%s tx %s): its signed bytes carry no readable "
                       "nonce, so whether it can still mine is unknown. It is NOT failed; funded "
                       "entries on chain %s stay fenced until it resolves. Check the hash by hand."
                  % (chain, iid, phase, sig, chain))
        return 0
    view = _nonce_view(ctx, chain, clock)
    if view is None:
        seen = st.setdefault("nonce_view_unknown_seen", clock())
        if (clock() - seen >= float(cfg.MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN) * 60.0
                and not st.get("paged_nonce_view")):
            st["paged_nonce_view"] = True
            _page("chain %s holding sell %s (%s tx %s at nonce %d): the account nonce has been "
                  "unreadable for %.0f+ min, so whether it can still mine is unknown. It is NOT "
                  "failed; funded entries on chain %s stay fenced until it resolves. Not rebroadcast."
                  % (chain, iid, phase, sig, nonce, float(cfg.MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN),
                     chain))
        return 0
    st.pop("nonce_view_unknown_seen", None)
    latest, pending = view
    if latest > nonce:
        seen = st.setdefault("spent_seen", clock())
        if clock() - seen < float(cfg.MEMECOIN_EVM_HOLDING_NONCE_SPENT_CONFIRM_S):
            return 0
        try:                        # one more look at OUR hash: an index lag is not another tx
            again = receipt_fn(sig) if receipt_fn and sig else None
        except Exception:
            return 0
        if again is _RECEIPT_UNKNOWN or again:
            return 0                # unreadable, or ours mined after all: never settle on this pass
        # ACCEPTED RISK (operator, 2026-10-08, review EVM01 acceptance 4 met in wording only): two
        # clean not_found answers 60 s apart with the nonce past ours settle this as failed and
        # release the fence. A lagging or load-balanced node that has not indexed our mined receipt
        # would book a sale that happened as failed. The exact-balance re-read on any later sell
        # bounds that to accounting, not money; the strict alternative fenced the chain until a human.
        changed[0] = True
        waited = clock() - seen
        m.resolve_holding_sell(conn, iid, "failed", now_iso=now_iso,
                               reason="nonce %d advanced past ours (confirmed nonce %d) and the node "
                                      "has no receipt for our hash after %.0fs; this %s is treated "
                                      "as never mined" % (nonce, latest, waited, phase))
        logger.warning("%s holding sell FAILED intent=%s: nonce %d advanced, no receipt for tx=%s "
                       "after %.0fs", chain, iid, nonce, sig, waited)
        with _STUCK_LOCK:
            _STUCK.pop((chain, iid), None)
        return 1
    st.pop("spent_seen", None)
    if pending == latest:
        st.pop("mempool_seen", None)
        seen = st.setdefault("dropped_seen", clock())
        if (clock() - seen >= float(cfg.MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN) * 60.0
                and not st.get("paged_stuck")):
            st["paged_stuck"] = True
            _page("chain %s holding sell %s: %s tx %s at nonce %d has not mined and the node "
                       "holds nothing of ours (pending = confirmed nonce %d) for %.0f+ min. It will "
                       "not mine by itself: cancel it with a 0-value self-transfer at nonce %d. "
                       "Funded entries on chain %s stay fenced until it resolves. Not rebroadcast."
                  % (chain, iid, phase, sig, nonce, latest,
                     float(cfg.MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN), nonce, chain))
    else:
        # SOMETHING OF OURS IS STILL IN A MEMPOOL. Usually it mines; one that sits there for
        # MEMECOIN_EVM_HOLDING_MEMPOOL_PAGE_MIN is likely underpriced and would hold the fence
        # silently (Gamma LOW), so it pages once too.
        st.pop("dropped_seen", None)
        seen = st.setdefault("mempool_seen", clock())
        if (clock() - seen >= float(cfg.MEMECOIN_EVM_HOLDING_MEMPOOL_PAGE_MIN) * 60.0
                and not st.get("paged_mempool")):
            st["paged_mempool"] = True
            _page("chain %s holding sell %s: %s tx %s at nonce %d is still pending in a mempool "
                       "after %.0f+ min (pending nonce %d, confirmed %d): likely underpriced. Replace "
                       "it at nonce %d with a higher fee, or cancel it there. Funded entries on chain "
                       "%s stay fenced until it resolves. Not rebroadcast."
                  % (chain, iid, phase, sig, nonce,
                     float(cfg.MEMECOIN_EVM_HOLDING_MEMPOOL_PAGE_MIN), pending, latest, nonce, chain))
    return 0


def _resolve_locked(conn, *, ctx, now_iso, changed, clock=time.monotonic):
    """Receipt reads and finalization share one lock, including approval-to-swap transitions."""
    from sniper.persistence import db_memecoin as m
    receipt_fn = _receipt_reader(ctx)
    chain = _chain(ctx)
    settled = 0
    rows = m.unresolved_holding_sells(conn, chain)
    with _STUCK_LOCK:
        live = {(chain, r["intent_id"]) for r in rows}
        for key in [k for k in _STUCK if k[0] == chain and k not in live]:
            _STUCK.pop(key, None)
    for row in rows:
        try:
            receipt = receipt_fn(row["sig"]) if receipt_fn and row.get("sig") else None
        except Exception:
            logger.exception("%s holding sell receipt unreadable intent=%s", chain, row["intent_id"])
            continue
        if receipt is _RECEIPT_UNKNOWN:
            _unreadable_receipt(row, chain=chain, clock=clock)
            continue                # unknown outcome: the row and its fence stay
        with _STUCK_LOCK:
            st = _STUCK.get((chain, row["intent_id"]))
            if st is not None:
                st.pop("receipt_unknown_seen", None)
        if receipt is not None and not _known_receipt(receipt):
            logger.error("%s holding sell receipt malformed intent=%s", chain, row["intent_id"])
            continue
        if not receipt:
            settled += _unmined(conn, row, ctx=ctx, chain=chain, now_iso=now_iso, changed=changed,
                                receipt_fn=receipt_fn, clock=clock)
            continue
        ok = _ok(receipt)
        phase = row.get('transaction_phase') or 'swap'
        if phase not in ('swap', 'approval'):
            logger.error('%s holding sell has unknown transaction phase', chain)
            continue
        if phase == 'approval':
            state = 'failed'
            reason = ('approval confirmed; request the sell again' if ok
                      else 'the approval reverted on chain')
        else:
            state = 'sold' if ok else 'failed'
            reason = 'proceeds not measured' if ok else 'the swap reverted on chain'
        changed[0] = True
        m.resolve_holding_sell(conn, row['intent_id'], state, now_iso=now_iso, reason=reason)
        settled += 1
    return settled


_WORKER = {}
_WORKER_LOCK = threading.Lock()
SELL_RETRY_S = 15.0        # a sell waiting for a quiet wallet re-checks at most this often
QUIET_STATES = ("wallet_busy", "idle", "book_busy", "tx_in_flight", "lifi_unresolved",
                "exit_unresolved", "holding_unresolved", "gas_unresolved", "standing_unresolved")


def start_pass(ctx, *, snapshot_due, sell_due, resolve_due, min_sell_usd, now_fn, log_fn=print):
    """Run one scan / sell / resolve pass on a worker thread with its own connection.
    Returns True when a pass started; False while the previous one still runs or there is no work.
    The main loop never waits on the explorer, LI.FI, or a receipt."""
    try:
        chain = _chain(ctx)
    except (TypeError, ValueError):
        logger.warning('holding worker refused invalid context chain identity')
        return False
    def run():
        from sniper.persistence.db_core import get_connection
        conn = get_connection()
        try:
            if snapshot_due:
                t = snapshot_once(conn, address=str(ctx["address"]), now_iso=now_fn(), chain_id=chain)
                if t.get("state") != "ok":
                    log_fn("[memecoin] %s other holdings scan: %s" % (chain, t))
            if sell_due:
                t = sell_once(conn, ctx=ctx, now_iso=now_fn(), min_sell_usd=min_sell_usd)
                if t.get("state") not in QUIET_STATES:
                    log_fn("[memecoin] %s other holdings sell: %s" % (chain, t))
            if resolve_due:
                n = resolve_once(conn, ctx=ctx, now_iso=now_fn())
                if n:
                    log_fn("[memecoin] %s other holdings resolved: %d" % (chain, n))
        except Exception:
            logger.exception("%s other holdings pass failed", chain)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # Main-loop maintenance never waits for another scheduler or a chain's I/O. The small
    # nonblocking lock only makes check+start atomic when callers race on the same chain.
    if not _WORKER_LOCK.acquire(blocking=False):
        return False
    try:
        state = _WORKER.setdefault(chain, {'thread': None, 'last_sell_try': 0.0})
        running = state['thread']
        if running is not None and running.is_alive():
            return False
        if sell_due and time.monotonic() - state['last_sell_try'] < SELL_RETRY_S:
            sell_due = False
        if not (snapshot_due or sell_due or resolve_due):
            return False
        if sell_due:
            state['last_sell_try'] = time.monotonic()
        thread = threading.Thread(target=run, name='memecoin-%s-holdings' % chain, daemon=True)
        state['thread'] = thread
        thread.start()
        return True
    finally:
        _WORKER_LOCK.release()
