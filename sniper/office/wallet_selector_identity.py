"""Canonical identity of locally submitted wallet selectors; never a provider ACK."""
import hashlib
import json
import re


def selector_digest(wallets):
    if wallets is None or isinstance(wallets,(str,bytes)):
        raise ValueError('selector_set_invalid')
    try:keys=set(wallets)
    except TypeError:raise ValueError('selector_set_invalid') from None
    if any(not isinstance(k,str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',k) for k in keys):
        raise ValueError('selector_set_invalid')
    return hashlib.sha256(json.dumps(sorted(keys),separators=(',',':')).encode('ascii')).hexdigest()
