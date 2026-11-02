"""A canonical entry-swap receipt for executor tests that fake JSON-RPC (Alpha 2026-10-06: every EVM
chain books an entry from its own receipt, so a fake that answered a bare {"status": "0x1"} no
longer books). One status-1 receipt: `paid` atomic of the quote token leaves the owner, `coin`
atomic of the mint arrives, in block 0x10, with the block and transaction answers that prove it."""
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BLOCK_HASH = "0x" + "bc" * 32
POOL = "0x" + "4e" * 20
MISS = object()


def _topic(address):
    return "0x" + "0" * 24 + str(address).lower()[2:]


def answer(method, params, *, owner, mint, quote, coin, paid=0, value="0x0", receipt_extra=None,
           proceeds=0):
    """The JSON-RPC result for one receipt-mode read, or MISS when `method` is not one of them."""
    if method == "eth_getTransactionReceipt":
        h = params[0]
        logs = []
        if paid:
            logs.append((quote, owner, POOL, paid))
        if coin:
            logs.append((mint, POOL, owner, coin))
        if proceeds:                         # an exit: the quote token comes back to us
            logs.append((quote, POOL, owner, proceeds))
        return {**(receipt_extra or {}), "status": "0x1", "transactionHash": h,
                "blockHash": BLOCK_HASH, "blockNumber": "0x10", "from": str(owner).lower(),
                "logs": [{"address": t, "topics": [TRANSFER, _topic(s), _topic(d)],
                          "data": "0x%064x" % a, "transactionHash": h, "blockHash": BLOCK_HASH,
                          "logIndex": hex(i)} for i, (t, s, d, a) in enumerate(logs)]}
    if method == "eth_getBlockByNumber" and params and params[0] == "0x10":
        return {"hash": BLOCK_HASH, "number": "0x10", "timestamp": "0x6a2c7000"}
    if method == "eth_getTransactionByHash":
        return {"hash": params[0], "from": str(owner).lower(), "blockHash": BLOCK_HASH,
                "blockNumber": "0x10", "value": value, "to": POOL}
    return MISS


def wrap(http, *, owner, mint, quote, coin, paid=0, value="0x0", decimals=0, receipt_extra=None,
         proceeds=0):
    """`http` with the receipt-mode reads (and the coin's decimals()) answered for one entry; every
    other call still goes to `http`, which also sees the receipt reads for its own counters."""
    import json

    def wrapped(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth, params = req.get("method"), req.get("params") or []
        if (meth == "eth_call" and params and isinstance(params[0], dict)
                and str(params[0].get("to") or "").lower() == str(mint).lower()
                and str(params[0].get("data") or "").startswith("0x313ce567")):
            return 200, {"result": "0x%064x" % decimals}, None
        hit = answer(meth, params, owner=owner, mint=mint, quote=quote, coin=coin, paid=paid,
                     value=value, receipt_extra=receipt_extra, proceeds=proceeds)
        if hit is MISS:
            return http(method, url, headers=headers, body=body, timeout=timeout)
        if meth == "eth_getTransactionReceipt":
            # The inner fake still decides WHETHER a receipt exists and its status; only a status-1
            # receipt gets this entry's canonical shape.
            reply = http(method, url, headers=headers, body=body, timeout=timeout)
            inner = (reply[1] or {}).get("result") if isinstance(reply, tuple) else None
            if not isinstance(inner, dict) or str(inner.get("status")).lower() not in ("0x1", "1"):
                return reply
        return 200, {"result": hit}, None
    return wrapped
