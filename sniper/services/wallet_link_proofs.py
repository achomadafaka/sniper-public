"""Pure receipt-bound research facts. A transfer never proves common ownership."""
from decimal import Decimal, localcontext
import re

from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC, wallet_paid_token_flows
from sniper.services.memecoin_source_initiation import direct_receipt_initiation


def _hex(value, size):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{' + str(size * 2) + '}', value):
        raise ValueError('malformed_identity')
    return value.lower()


def _quantity(value):
    if type(value) is int and value >= 0: return value
    if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]+', value): return int(value, 16)
    raise ValueError('malformed_quantity')


def _mined(receipt, transaction, header, chain):
    txhash, blockhash = _hex(transaction['hash'], 32), _hex(transaction['blockHash'], 32)
    block, index = _quantity(transaction['blockNumber']), _quantity(transaction['transactionIndex'])
    sender, target = _hex(transaction['from'], 20), _hex(transaction['to'], 20)
    if (not block or _quantity(receipt['status']) != 1 or _hex(receipt['transactionHash'], 32) != txhash
            or _hex(receipt['blockHash'], 32) != blockhash or _quantity(receipt['blockNumber']) != block
            or _quantity(receipt['transactionIndex']) != index or _hex(receipt['from'], 20) != sender
            or _hex(receipt['to'], 20) != target or _hex(header['hash'], 32) != blockhash
            or _quantity(header['number']) != block or not _quantity(header['timestamp'])
            or (transaction.get('chainId') is not None and _quantity(transaction['chainId']) != int(chain))):
        raise ValueError('mined_identity_mismatch')
    logs = receipt['logs']
    if not isinstance(logs, list): raise ValueError('malformed_logs')
    for log in logs:
        if (log.get('removed') is True or _hex(log['transactionHash'], 32) != txhash
                or _hex(log['blockHash'], 32) != blockhash or _quantity(log['blockNumber']) != block
                or _quantity(log['transactionIndex']) != index): raise ValueError('log_identity_mismatch')
        _hex(log['address'], 20)
        topics = log['topics']
        if not isinstance(topics, list): raise ValueError('malformed_topics')
        if topics and str(topics[0]).lower() == TRANSFER_TOPIC:
            if len(topics) != 3: raise ValueError('malformed_transfer')
            for topic in topics[1:]:
                if _hex(topic, 32)[2:26] != '0' * 24: raise ValueError('malformed_topic_address')
            _hex(log['data'], 32)
    return sender, target, txhash, [block, index], _quantity(header['timestamp'])


def evm_buy(receipt, transaction, header, *, chain, mint, quotes):
    """Direct signed payer + linked acquisition, excluding unsupported relay attribution."""
    try:
        sender, _target, signature, order, at = _mined(receipt, transaction, header, chain)
        mint = _hex(mint, 20)
        payments = []
        for quote in quotes:
            details = {}
            received, spent = wallet_paid_token_flows(receipt, sender, _hex(quote, 20), transaction,
                                                     payment_details=details)
            if mint not in received or not spent: continue
            if (details.get('native_paid') and details.get('quote_paid')) or details.get('unlinked_quote_outflow'):
                return None
            payments.append((received[mint], spent, 'native' if details.get('native_paid') else quote.lower()))
        # Native payment can be detected against multiple trusted quote currencies.
        payments = list(dict.fromkeys(payments))
        if len(payments) != 1 or not direct_receipt_initiation(receipt, transaction, sender, mint, chain=chain):
            return None
        acquired, spent, quote = payments[0]
        return dict(address=sender, buy_signature=signature, order=order, t0=at,
                    acquired_atomic=str(acquired), payment_atomic=str(spent), payment_asset=quote,
                    proof_method='direct-wallet-paid-v1', ownership_proven=False)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None


def evm_funding(receipt, transaction, header, *, chain, assets, recipient_code=None, recipient_codes=None):
    """Signed native transfer or a trusted token's directed Transfer from the signer."""
    try:
        sender, target, signature, _order, at = _mined(receipt, transaction, header, chain)
        amounts,credits = {},{}
        recipient_codes=recipient_codes or {}
        value = _quantity(transaction['value'])
        if value and transaction.get('input') == '0x' and sender != target and recipient_code=='0x':
            amounts[('native', sender, target)] = value
        for log in receipt['logs']:
            topics = log['topics']; asset = log['address'].lower()
            if asset not in assets or len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC: continue
            src, dst = '0x' + topics[1][-40:].lower(), '0x' + topics[2][-40:].lower()
            amount = int(log['data'], 16)
            credits[(asset,src)]=credits.get((asset,src),0)-amount
            credits[(asset,dst)]=credits.get((asset,dst),0)+amount
            if src == sender and src != dst and int(dst, 16) and amount:
                key = asset, src, dst; amounts[key] = amounts.get(key, 0) + amount
        rows = []
        for (asset, src, dst), amount in amounts.items():
            if asset == 'native':
                symbol, decimals = ('USDC' if chain == '5042' else 'BNB' if chain == '56' else 'ETH', 18)
            else:
                if credits.get((asset,dst),0)<amount or recipient_codes.get(dst)!='0x':continue
                symbol, decimals = assets[asset]
            if type(decimals) is not int or not 0 <= decimals <= 36: raise ValueError('unknown_asset_decimals')
            with localcontext() as context:
                context.prec=100
                human=format(Decimal(amount).scaleb(-decimals).normalize(),'f')
            rows.append(dict(chain=chain, kind='verified_funding', verified=True, funder=src, recipient=dst,
                funding_signature=signature, funded_at=at, amount_atomic=str(amount),
                amount=human, asset=asset, symbol=symbol,
                decimals=decimals, method='confirmed_native_transfer' if asset == 'native' else 'confirmed_erc20_transfer',
                ownership_proven=False))
        return rows
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return []


def sol_cached_buys(unit, result):
    """Read current authenticated venue actors; incomplete windows still retain positives."""
    from sniper.services.wallet_universe_flow import METHOD, SOL_QUOTES
    try:
        state = result['state']; source = state['source']; identity = state['binding']['identity']
        needed = ('chain', 'uid', 'address', 'mint', 'signature', 't0')
        if (unit['chain'] != 'solana' or result['method'] != METHOD or state['version'] != METHOD
                or any(identity.get(k) != unit.get(k) for k in needed)
                or source['signature'] != unit['signature'] or source['t0'] != unit['t0']): return []
        order = source['order']
        if len(order) != 2 or any(type(x) is not int or x < 0 for x in order): return []
        rows = []
        for rec in state['records']:
            peer = rec.get('payment_signer') or (rec.get('actor') if rec.get('actor_verified') is True else None)
            if not isinstance(peer, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', peer) or peer == unit['address']: continue
            try:
                anchor = state['seen'][rec['signature']]
                if (len(rec['order']) != 2 or any(type(x) is not int or x < 0 for x in rec['order'])
                        or anchor[1:] != rec['order'] or type(anchor[0]) is not int
                        or not unit['t0'] - 60 <= anchor[0] <= unit['t0'] + 60
                        or rec['signature'] == unit['signature'] or rec['order'] == order): continue
                if not any(leg['asset'] in SOL_QUOTES and SOL_QUOTES[leg['asset']][1] == leg['decimals']
                           and isinstance(leg['amount'], str) and str(int(leg['amount'])) == leg['amount']
                           and int(leg['amount']) > 0 for leg in rec['legs']): continue
                rows.append(dict(uid=unit['uid'], chain='solana', main_address=unit['address'], mint=unit['mint'],
                    source_signature=unit['signature'], source_order=order, source_t0=unit['t0'], address=peer,
                    buy_signature=rec['signature'], order=rec['order'], t0=anchor[0],
                    proof_method='solana_route_payment_signer' if rec.get('payment_signer') else 'solana_venue_signer',
                    protocol=rec.get('protocol'), ownership_proven=False))
            except (KeyError, TypeError, ValueError, IndexError): continue
        return rows
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return []
