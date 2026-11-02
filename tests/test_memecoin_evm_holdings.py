"""Robinhood (4663) other holdings: scan and operator sell through the funded LI.FI validator.

2026-09-14, operator: "get it so I can sell all the Robinhood stuff" (BABA, RETAIL, USO ~ $38).
"""
import sqlite3
import json
from pathlib import Path
import pytest
from eth_abi import decode, encode
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_holdings as eh
from sniper.execution.evm_wallet import CHAIN_QUOTE

T0 = "2026-09-14T23:00:00+00:00"
ADDR = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
WETH = CHAIN_QUOTE["4663"]["token"].lower()
TOKEN = "0xad25ac6c84d497db898fa1e8387bf6af3532a1c4"
SPENDER = "0xb477751b76cf82d00a686a1232f5fcd772414af3"


def test_explorer_cursor_omits_null_and_preserves_zero():
    from urllib.parse import urlparse, parse_qs
    reads = []
    def get(url, headers):
        reads.append(url)
        if len(reads) == 1:
            return {"items": [], "next_page_params": {
                "id": 42, "value": "0", "fiat_value": None, "items_count": 50}}
        assert parse_qs(urlparse(url).query) == {
            "type": ["ERC-20"], "id": ["42"], "value": ["0"], "items_count": ["50"]}
        return {"items": [{"value": "5", "token": {
            "address_hash": TOKEN, "decimals": "0", "symbol": "T"}}]}
    assert eh.wallet_tokens(ADDR, get_fn=get)[TOKEN][0] == 5


def test_explorer_page_limit_does_not_replace_snapshot_with_partial_list():
    with pytest.raises(ValueError, match="pagination"):
        eh.wallet_tokens(ADDR, max_pages=1, get_fn=lambda *_: {
            "items": [], "next_page_params": {"id": 42}})


@pytest.mark.parametrize('page', [None, {}, {'items': None}])
def test_malformed_explorer_page_is_not_an_empty_wallet(page):
    with pytest.raises(ValueError, match='page'):
        eh.wallet_tokens(ADDR, get_fn=lambda *_: page)


def _manual_quote(tool):
    response = json.loads((Path(__file__).parent / 'fixtures/memecoin_holding_quotes.json').read_text())[tool]
    action = response['action']
    request = {'fromChain': '4663', 'toChain': '4663',
               'fromToken': action['fromToken']['address'], 'toToken': action['toToken']['address'],
               'fromAddress': action['fromAddress'], 'toAddress': action['toAddress'],
               'fromAmount': action['fromAmount'], 'allowBridges': [],
               'allowExchanges': ['nordstern', 'fly', 'kyberswap'], 'fee': '0', 'simulation': True,
               'integrator': 'lifi-api', 'referrer': '0x' + '0' * 40}
    return request, response


@pytest.mark.parametrize('tool', ['nordstern', 'fly', 'kyberswap'])
def test_manual_routes_decode_exact_tokens_amount_and_recipient(tool):
    request, response = _manual_quote(tool)
    decoded = eh.decode_holding_quote(response, request, eh._manifest())
    assert decoded.tool == tool
    assert decoded.input_atomic == int(request['fromAmount'])
    assert decoded.output_min_atomic == int(response['estimate']['toAmountMin'])
    assert decoded.custody == request['fromAddress'].lower()
    from sniper.execution.lifi_route import decode_quote, LifiQuoteError
    if tool != 'nordstern':
        with pytest.raises(LifiQuoteError):
            decode_quote(response, request, eh._manifest())


@pytest.mark.parametrize('tool', ['fly', 'kyberswap'])
@pytest.mark.parametrize('field', ['recipient', 'input', 'amount', 'minimum', 'target'])
def test_manual_routes_refuse_changed_calldata(tool, field):
    request, response = _manual_quote(tool)
    types = eh._manifest()['top_level']['types']
    raw = bytes.fromhex(response['transactionRequest']['data'][2:])
    outer = list(decode(types, raw[4:])); swaps = [list(x) for x in outer[-1]]
    if field == 'target':
        swaps[1][0] = '0x' + '33' * 20
    elif tool == 'fly':
        data = bytearray(swaps[1][5])
        if field == 'recipient': data[72:92] = b'\x33' * 20
        if field == 'input': data[92:112] = b'\x33' * 20
        if field == 'amount': data[132:164] = (1).to_bytes(32, 'big')
        if field == 'minimum': data[167:170] = b'\xff\x00\x00'
        swaps[1][5] = bytes(data)
    else:
        kt = ['(address,address,bytes,(address,address,address[],uint256[],address[],uint256[],address,uint256,uint256,uint256,bytes),bytes)']
        data = swaps[1][5]; nested = list(decode(kt, data[4:])[0]); desc = list(nested[3])
        idx = {'recipient': 6, 'input': 0, 'amount': 7, 'minimum': 8}[field]
        desc[idx] = '0x' + '33' * 20 if field in ('recipient', 'input') else 1
        nested[3] = desc; swaps[1][5] = data[:4] + encode(kt, [nested])
    outer[-1] = swaps
    response['transactionRequest']['data'] = '0x' + (raw[:4] + encode(types, outer)).hex()
    from sniper.execution.lifi_route import LifiQuoteError
    reason = {'recipient': 'recipient', 'input': 'tokens', 'amount': 'amount',
              'minimum': 'minimum', 'target': 'target'}[field]
    with pytest.raises(LifiQuoteError, match=reason):
        eh.decode_holding_quote(response, request, eh._manifest())


def test_manual_small_sale_is_explicit_and_zero_output_still_fails(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0, allow_small=True)
    chain = _Chain()
    assert _sell(c, chain, tmp_path, client=_client(min_out=10**14))['state'] == 'sold'
    m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0, allow_small=True)
    chain = _Chain()
    assert _sell(c, chain, tmp_path, client=_client(min_out=0))['state'] == 'failed'
    assert chain.events == []


@pytest.mark.parametrize('failure_at', [1, 2])
def test_code_proof_failure_never_submits_a_swap(tmp_path, failure_at):
    c = _conn()
    m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0)
    chain = _Chain()
    calls = []

    def verify(*args):
        calls.append(True)
        if len(calls) == failure_at:
            raise ValueError('runtime code mismatch')

    result = _sell(c, chain, tmp_path, verify_route_fn=verify)
    assert result['state'] == 'error'
    assert chain.events == ([] if failure_at == 1 else [('send', 'raw-approve')])
    assert chain.signed == ([] if failure_at == 1 else ['approve'])


def test_final_simulation_failure_never_submits_a_swap(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30)
    ctx = chain.ctx()
    calls = []
    ctx['lifi_local']['simulate_fn'] = lambda tx, block: calls.append((tx['nonce'], block)) or False
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                         lock_dir=str(tmp_path), client=_client(),
                         verify_route_fn=lambda *args: SimpleNamespace(block_number=123))
    assert result['state'] == 'failed'
    assert calls == [(7, 123)] and chain.events == []
    assert chain.signed == []


def test_fly_core_mismatch_refuses_the_quote(monkeypatch):
    from sniper.execution import holding_routes as hr, lifi_executor as ex
    request, response = _manual_quote('fly')
    decoded = eh.decode_holding_quote(response, request, eh._manifest())
    manifests = []

    def proof(*args, **kwargs):
        manifests.append(kwargs['manifest'])
        return SimpleNamespace(block_number=123)

    monkeypatch.setattr(ex, 'prove_quote_authority', proof)
    monkeypatch.setattr(ex, '_call', lambda *args, **kw: {'result': '0x' + '0' * 64})
    with pytest.raises(hr.r.LifiQuoteError, match='wrong Fly core'):
        hr.verify_holding_quote(decoded, {'rpc_url': 'https://custody.invalid'}, eh._manifest())
    assert manifests[0]['steps'][1]['executor'] == hr.PINS['fly']['executor']
    assert manifests[0]['steps'][1]['executor_code_hash'] == hr.PINS['fly']['executor_code_hash']


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_real(c, mint, network_id="4663"):
    return m.open_position(c, source_user_id="u1", source_handle="a", mint=mint, ticker="T",
                           entry_price=0.01, entry_notional=10.0, entry_fill_source="lifi",
                           entry_lag_s=0.1, their_size_usd=1.0, opened_at=T0,
                           network_id=network_id, execution_mode="real")


def test_the_scan_lists_leftovers_but_never_the_bankroll_or_a_position():
    c = _conn()
    _open_real(c, "0xheld")
    held = {TOKEN: (160774000000000000, 18, "BABA"), WETH: (8754130000000000, 18, "WETH"),
            "0xheld": (5, 18, "H"), "0xspam": (10**19, 18, "UNIFROG")}
    t = eh.snapshot_once(c, address=ADDR, now_iso=T0, list_fn=lambda a: held,
                         price_fn=lambda keys: {("4663", TOKEN): {"price": 109.48, "symbol": "BABA",
                                                                  "liquidity": 2.1e5}},
                         balance_fn=lambda a, ts: {t: held[t][0] for t in ts})
    assert t == {"state": "ok", "rows": 2, "unpriced": 1}
    rows = {r["mint"]: r for r in m.other_holdings(c)["4663"]["rows"]}
    assert set(rows) == {TOKEN, "0xspam"}
    assert abs(rows[TOKEN]["value_usd"] - 0.160774 * 109.48) < 1e-9
    assert rows["0xspam"]["symbol"] == "UNIFROG" and rows["0xspam"]["value_usd"] is None


def test_an_unreadable_explorer_keeps_the_old_snapshot():
    c = _conn()
    eh.snapshot_once(c, address=ADDR, now_iso=T0, list_fn=lambda a: {TOKEN: (10**18, 18, "B")},
                     price_fn=lambda keys: {}, balance_fn=lambda a, ts: {TOKEN: 10**18})

    def down(address):
        raise OSError("403")
    assert eh.snapshot_once(c, address=ADDR, now_iso=T0, list_fn=down,
                            price_fn=lambda keys: {})["state"] == "explorer_unreadable"

    def rpc_down(address, tokens):
        raise OSError("rpc 503")
    assert eh.snapshot_once(c, address=ADDR, now_iso=T0, list_fn=lambda a: {TOKEN: (10**18, 18, "B")},
                            price_fn=lambda keys: {}, balance_fn=rpc_down)["state"] == "rpc_unreadable"
    assert [r["mint"] for r in m.other_holdings(c)["4663"]["rows"]] == [TOKEN]


@pytest.mark.parametrize('balances', [{}, {TOKEN: None}, {TOKEN: 0}])
def test_snapshot_distinguishes_unknown_balance_from_zero(balances):
    c = _conn()
    kwargs = dict(address=ADDR, now_iso=T0, list_fn=lambda _: {TOKEN: (10**18, 18, 'B')},
                  price_fn=lambda _: {})
    eh.snapshot_once(c, **kwargs, balance_fn=lambda *_: {TOKEN: 10**18})
    result = eh.snapshot_once(c, **kwargs, balance_fn=lambda *_: balances)
    if balances.get(TOKEN) == 0:
        assert result['state'] == 'ok' and m.other_holdings(c)['4663']['rows'] == []
    else:
        assert result['state'] == 'rpc_unreadable'
        assert [r['mint'] for r in m.other_holdings(c)['4663']['rows']] == [TOKEN]


class _Chain:
    """Fake 4663 seams: balances, allowance, gas, signing, broadcast and receipts."""

    def __init__(self, allowance=0, receipt_status="0x1", send_result="0xhash", swap_receipt=True):
        self.bal = {TOKEN: 160774000000000000, WETH: 10**16}
        self.allowance = allowance
        self.receipt_status, self.send_result, self.swap_receipt = receipt_status, send_result, swap_receipt
        self.events, self.nonce = [], 7
        self.signed = []

    def ctx(self):
        return {"chain": "4663", "address": ADDR, "pending_nonce_fn": lambda: self.nonce,
                "confirmed_nonce_fn": lambda: self.nonce,
                "warm_quote_price_fn": lambda: 2540.0,
                "lifi_local": {
                    "balance_raw_fn": lambda t: self.bal.get(t.lower(), 0),
                    "balance_raw_checked_fn": lambda t: self.bal.get(t.lower(), 0),
                    "allowance_fn": lambda t, o, s: self.allowance,
                    "estimate_gas_fn": lambda tx: 100_000,
                    "construct_tx_fn": lambda tx, gas: {**tx, "gas": gas, "nonce": self.nonce},
                    "sign_complete_fn": self.sign, "send_fn": self.send,
                    "receipt_fn": self.receipt}}

    def sign(self, tx):
        kind = "approve" if tx["to"] == TOKEN else "swap"
        self.signed.append(kind)
        return ("raw-" + kind, "0x" + kind, tx["nonce"])

    def send(self, raw):
        self.events.append(("send", raw))
        if raw == "raw-approve":
            self.allowance = 10**30
            return "0xapprove"
        if self.send_result is not False:
            self.bal[TOKEN] = 0
            self.bal[WETH] += 6_700_000_000_000_000        # 0.0067 WETH ~ $17.02
        return self.send_result

    def receipt(self, h):
        if h == "0xswap" and not self.swap_receipt:
            return None
        return {"status": self.receipt_status}


def _client(ok=True, min_out=6_568_014_788_822_637):
    decoded = SimpleNamespace(input_atomic=160774000000000000, output_min_atomic=min_out,
                              spender=SPENDER, transaction={"to": SPENDER, "data": "0xabc", "value": "0x0"})
    return SimpleNamespace(quote=lambda req, role, stage: SimpleNamespace(
        ok=ok, failure_type=None if ok else "quote_invalid", decoded=decoded if ok else None))


def _never(*args, **kwargs):
    """A verifier that must never be called on a ladder route."""
    raise AssertionError("the LI.FI proof ran on a route it cannot prove")


def _sell(c, chain, tmp_path, **kw):
    kw.setdefault("client", _client())
    kw.setdefault("verify_route_fn", lambda *args: None)
    return eh.sell_once(c, ctx=chain.ctx(), now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                        sleep_fn=lambda s: None, wait_s=0, **kw)


def test_a_sell_approves_the_exact_amount_writes_ahead_and_books_the_weth_delta(tmp_path):
    c = _conn()
    iid = m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=0)
    seen = []
    orig_send = chain.send

    def send(raw):
        seen.append((raw, c.execute("SELECT state, sig FROM memecoin_holding_sell_intents")
                     .fetchone()[:2]))
        return orig_send(raw)
    chain.send = send
    t = _sell(c, chain, tmp_path)
    assert t["state"] == "sold" and t["intent_id"] == iid
    assert [r for r, _ in seen] == ["raw-approve", "raw-swap"]
    assert seen[1][1] == ("submitted", "0xswap")                 # hash stored before the broadcast
    row = c.execute("SELECT state, usd_received FROM memecoin_holding_sell_intents").fetchone()
    assert row[0] == "sold" and abs(row[1] - 0.0067 * 2540.0) < 1e-6


def test_no_approval_when_the_allowance_already_covers_it(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30)
    assert _sell(c, chain, tmp_path)["state"] == "sold"
    assert chain.events == [("send", "raw-swap")]


def test_a_sell_waits_while_a_trade_or_a_transaction_is_live(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    pid = _open_real(c, "0xother")
    chain = _Chain()
    assert _sell(c, chain, tmp_path)["state"] == "book_busy" and chain.events == []
    c.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=?", (pid,))
    chain.nonce_pending = 8
    ctx = chain.ctx()
    ctx["pending_nonce_fn"] = lambda: 8                           # a transaction is in flight
    t = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                     sleep_fn=lambda s: None, wait_s=0, client=_client())
    assert t["state"] == "tx_in_flight" and chain.events == []
    assert c.execute("SELECT state FROM memecoin_holding_sell_intents").fetchone()[0] == "queued"


def test_an_invalid_quote_or_a_thin_floor_signs_nothing(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain()
    t = _sell(c, chain, tmp_path, client=_client(ok=False))
    assert t["state"] == "failed" and "no validated LI.FI route" in t["reason"] and chain.events == []
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    t = _sell(c, chain, tmp_path, client=_client(min_out=10**14))    # 0.0001 WETH ~ $0.25
    assert t["state"] == "failed" and "minimum" in t["reason"] and chain.events == []


def test_an_unanswered_swap_waits_for_its_receipt(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30, swap_receipt=False)
    assert _sell(c, chain, tmp_path)["state"] == "unresolved"
    assert m.unresolved_holding_sells(c, "4663")[0]["sig"] == "0xswap"
    chain.swap_receipt = True
    assert eh.resolve_once(c, ctx=chain.ctx(), now_iso=T0) == 1
    assert m.unresolved_holding_sells(c, "4663") == []


def test_the_bankroll_token_is_never_sold(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", WETH, now_iso=T0)
    chain = _Chain()
    t = _sell(c, chain, tmp_path)
    assert t["state"] == "failed" and "bankroll" in t["reason"] and chain.events == []


def test_no_work_starts_no_thread():
    assert eh.start_pass({"address": ADDR}, snapshot_due=False, sell_due=False, resolve_due=False,
                         min_sell_usd=0.5, now_fn=lambda: T0) is False


def test_prices_come_from_the_deepest_pair_where_the_token_is_the_base():
    """The shared batch dropped BABA whenever USDG shared its call (2026-09-14)."""
    usdg = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
    pairs = [
        {"baseToken": {"address": usdg, "symbol": "USDG"}, "quoteToken": {"address": TOKEN},
         "priceUsd": "1.0", "liquidity": {"usd": 9e5}},
        {"baseToken": {"address": TOKEN, "symbol": "BABA"}, "priceUsd": "109.0",
         "liquidity": {"usd": 1e3}},
        {"baseToken": {"address": TOKEN, "symbol": "BABA"}, "priceUsd": "109.48",
         "liquidity": {"usd": 2.1e5}},
    ]
    got = eh.dexscreener_prices([("4663", TOKEN), ("4663", usdg)], get_fn=lambda u, h: pairs)
    assert got[("4663", TOKEN)] == {"price": 109.48, "symbol": "BABA", "liquidity": 2.1e5}
    assert got[("4663", usdg)]["price"] == 1.0

    def down(u, h):
        raise OSError("dexscreener down")
    assert eh.dexscreener_prices([("4663", TOKEN)], get_fn=down) == {}


def test_the_chain_decides_what_is_held_not_the_explorer():
    """2026-09-14 (RETAIL): the explorer listed 2,694,032 RETAIL; position 47302 sold it on 09-01
    and balanceOf is 0. A Sell on it failed "nothing left to sell"."""
    c = _conn()
    retail = "0xff89d5ca203f5665002e44b34e36299ca343c49b"
    held = {TOKEN: (10**18, 18, "BABA"), retail: (2694032 * 10**18, 18, "RETAIL")}
    eh.snapshot_once(c, address=ADDR, now_iso=T0, list_fn=lambda a: held, price_fn=lambda keys: {},
                     balance_fn=lambda a, ts: {TOKEN: 160774000000000000, retail: 0})
    rows = {r["mint"]: r for r in m.other_holdings(c)["4663"]["rows"]}
    assert set(rows) == {TOKEN}                         # confirmed zero on chain is not listed
    assert rows[TOKEN]["amount"] == 0.160774            # the chain's amount, not the explorer's


def test_the_balance_batch_reads_each_token_and_skips_errors():
    answers = [{"id": 0, "result": "0x" + format(7, "064x")}, {"id": 1, "error": {"code": -32000}}]
    got = eh.onchain_balances(ADDR, ["0xa", "0xb"], post_fn=lambda body: answers)
    assert got == {"0xa": 7}


def test_holdings_balance_batches_fit_public_rpc_limit_and_preserve_ids():
    tokens = ['0x%040x' % i for i in range(23)]
    batches = []
    def post(body):
        assert len(body) <= 10
        batches.append([x['id'] for x in body])
        return [{'id': x['id'], 'result': hex(x['id'])} for x in reversed(body)]
    assert eh.onchain_balances(ADDR, tokens, post_fn=post) == {
        token: i for i, token in enumerate(tokens)}
    assert batches == [list(range(10)), list(range(10, 20)), list(range(20, 23))]


def test_unreadable_bankroll_prebalance_stops_before_quote_or_sign(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain()
    ctx = chain.ctx()
    ctx["lifi_local"]["balance_raw_checked_fn"] = lambda t: None if t.lower() == WETH else chain.bal[t.lower()]
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                          sleep_fn=lambda s: None, wait_s=0, client=_client())
    assert result["state"] == "failed"
    assert "bankroll balance" in result["reason"]
    assert chain.events == []


def test_unreadable_bankroll_afterbalance_keeps_proceeds_unknown(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30)
    ctx = chain.ctx()
    ctx["lifi_local"]["balance_raw_checked_fn"] = lambda t: (
        None if t.lower() == WETH and chain.events else chain.bal[t.lower()])
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                          sleep_fn=lambda s: None, wait_s=0, client=_client(),
                          verify_route_fn=lambda *args: None)
    assert result["state"] == "sold" and result["usd_received"] is None
    assert chain.events == [("send", "raw-swap")]
    row = c.execute("SELECT usd_received, reason FROM memecoin_holding_sell_intents").fetchone()
    assert row[0] is None and row[1] == "proceeds not measured"


def test_live_executor_context_supplies_the_checked_balance_reader(monkeypatch):
    from eth_account import Account
    from sniper.execution import evm_real_seams as S
    seen = []
    monkeypatch.setattr(S, "erc20_balance_raw_checked", lambda owner, token, rpc, **kw: seen.append(token) or 123)
    ctx = S.build_real_evm_executor(acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        http=lambda *a, **k: (200, {}, None), usdc_fn=lambda: 0, token_fn=lambda m: 0)
    assert ctx["lifi_local"]["balance_raw_checked_fn"](TOKEN) == 123
    assert seen == [TOKEN]


@pytest.mark.parametrize('nonce', [0, 7])
def test_dropped_holding_sale_preserves_signed_bytes_and_nonce_after_restart(tmp_path, nonce):
    import base64
    from eth_account import Account
    from sniper.execution.evm_real_seams import sign_tx, decode_signed_tx_cost

    path = tmp_path / 'holdings.db'
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    iid = m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30, swap_receipt=False)
    chain.nonce = nonce
    acct = Account.create()
    ctx = chain.ctx()
    ctx['address'] = acct.address
    signed = []
    sent = []

    def sign(tx):
        raw, identity = sign_tx({**tx, 'chainId': 4663, 'gasPrice': 10**9}, acct)
        signed.append(raw)
        return raw, identity, tx['nonce']

    def send(raw):
        row = m.unresolved_holding_sells(c, '4663')[0]
        assert row.get('signed_payload_b64') == base64.b64encode(raw.encode('ascii')).decode('ascii')
        sent.append(raw)
        return None  # transport ambiguity never releases the intent

    ctx['lifi_local'].update(sign_complete_fn=sign, send_fn=send,
                             receipt_fn=lambda h: None)
    client = _client()
    client.quote(None, role='holdings', stage='sell').decoded.transaction['data'] = '0xabcdef'
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                         lock_dir=str(tmp_path), client=client, wait_s=0,
                         verify_route_fn=lambda *a: None)
    assert result['state'] == 'unresolved' and len(sent) == 1
    c.close()
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    assert eh.resolve_once(c, ctx=ctx, now_iso='2026-09-14T23:00:30+00:00') == 0
    row = m.unresolved_holding_sells(c, '4663')[0]
    assert decode_signed_tx_cost(row['signed_payload_b64'])['nonce'] == nonce
    assert sent == signed and len(signed) == 1
    ctx['lifi_local']['receipt_fn'] = lambda h: {'status': '0x1'}
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0) == 1
    assert m.unresolved_holding_sells(c, '4663') == []
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (iid,)).fetchone()[0] == 'sold'
    assert len(sent) == 1
    c.close()


@pytest.mark.parametrize('receipt, state', [(None, 'submitted'), ({}, 'submitted'),
    ({'blockNumber': '0x123'}, 'submitted'), ('partial', 'submitted'),
    ({'status': '0x0'}, 'failed'), ({'status': '0x1'}, 'sold')])
def test_only_terminal_receipts_resolve_holding_sells(receipt, state):
    c = _conn()
    iid = m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0)
    m.resolve_holding_sell(c, iid, 'submitted', now_iso=T0, sig='0xswap', balance_raw=123)
    read = lambda h: receipt
    assert eh._wait_receipt(read, '0xswap', 0, lambda s: None) == (
        receipt if state in ('sold', 'failed') else None)
    assert eh.resolve_once(c, ctx={'chain': '4663', 'address': ADDR, 'lifi_local': {'receipt_fn': read}}, now_iso=T0) == (
        0 if state == 'submitted' else 1)
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (iid,)).fetchone()[0] == state
    c.close()


@pytest.mark.parametrize('fallback_price', ['0.000004474466991', None])
def test_thin_pool_balance_uses_exact_contract_reference_not_marginal_mark(fallback_price):
    reads = []
    def get(url, headers):
        reads.append(url)
        if '/tokens/v1/' in url:
            return [{'baseToken': {'address': TOKEN, 'symbol': 'PLAGUE'},
                     'priceUsd': '2713.32', 'liquidity': {'usd': 60.22}}]
        return {'chainId': 4663, 'address': TOKEN, 'decimals': 18,
                'priceUSD': fallback_price, 'symbol': 'PLAGUE'}
    got = eh.holding_prices([('4663', TOKEN)], get_fn=get,
                            balances={('4663', TOKEN): 88})
    assert len(reads) == 2 and '/v1/token?' in reads[1]
    if fallback_price is None:
        assert got == {}
    else:
        assert got[('4663', TOKEN)]['price'] == float(fallback_price)
        c = _conn()
        eh.snapshot_once(c, address=ADDR, now_iso=T0,
                         list_fn=lambda _: {TOKEN: (88 * 10**18, 18, 'PLAGUE')},
                         balance_fn=lambda *_: {TOKEN: 88 * 10**18},
                         price_fn=lambda _: got)
        row = m.other_holdings(c)['4663']['rows'][0]
        assert row['amount'] == 88
        assert row['value_usd'] == pytest.approx(0.000393753095208)


def test_reference_prices_fill_only_missing_exact_contracts():
    missing = '0x' + '22' * 20
    calls = []

    def get(url, headers):
        calls.append(url)
        if url.startswith(eh.DS_TOKENS):
            return [{'baseToken': {'address': TOKEN, 'symbol': 'BABA'},
                     'priceUsd': '109', 'liquidity': {'usd': 1000}}]
        assert url.endswith(missing)
        return {'address': missing.upper(), 'chainId': 4663, 'priceUSD': '0.25', 'symbol': 'T'}

    prices = eh.holding_prices([('4663', TOKEN), ('4663', missing)], get_fn=get)
    assert prices[('4663', TOKEN)]['price'] == 109
    assert prices[('4663', missing)] == {'price': 0.25, 'symbol': 'T', 'liquidity': None}
    assert len(calls) == 2


@pytest.mark.parametrize('change', [
    {'address': WETH}, {'chainId': 1}, {'priceUSD': 'NaN'},
    {'priceUSD': 'Infinity'}, {'priceUSD': '0'}, {'priceUSD': '-1'},
])
def test_reference_prices_reject_wrong_identity_and_invalid_prices(change):
    token = {'address': TOKEN, 'chainId': 4663, 'priceUSD': '2', 'symbol': 'BABA', **change}
    def get(url, headers):
        return [] if url.startswith(eh.DS_TOKENS) else token
    assert eh.holding_prices([('4663', TOKEN)], get_fn=get) == {}


def test_reference_provider_failure_keeps_existing_prices_and_stops_requests():
    calls = []
    def get(url, headers):
        calls.append(url)
        if url.startswith(eh.DS_TOKENS):
            return [{'baseToken': {'address': TOKEN}, 'priceUsd': '2'}]
        raise OSError('provider unavailable')
    prices = eh.holding_prices([('4663', TOKEN), ('4663', WETH), ('4663', ADDR)], get_fn=get)
    assert prices[('4663', TOKEN)]['price'] == 2
    assert len(calls) == 2


def test_snapshot_uses_reference_price_fallback(monkeypatch):
    c = _conn()
    def prices(keys, *, balances):
        assert balances == {('4663', TOKEN): 2}
        return {('4663', TOKEN): {'price': 0.25}}
    monkeypatch.setattr(eh, 'holding_prices', prices)
    result = eh.snapshot_once(c, address=ADDR, now_iso=T0,
                             list_fn=lambda a: {TOKEN: (2 * 10**18, 18, 'T')},
                             balance_fn=lambda a, ts: {TOKEN: 2 * 10**18})
    assert result['unpriced'] == 0
    assert m.other_holdings(c)['4663']['rows'][0]['value_usd'] == 0.5


# --- the venue-ladder fallback (2026-09-22, operator: dust and airdrops must be sellable) -------

def _ladder_quote(min_out=6_568_014_788_822_637, sell_amount=160774000000000000):
    return {"sellAmount": str(sell_amount), "buyAmount": str(min_out + 1),
            "minBuyAmount": str(min_out), "liquidityAvailable": True,
            "transaction": {"to": SPENDER, "data": "0xladder", "value": "0x0"},
            "issues": {"allowance": {"spender": SPENDER}},
            "route": {"protocol": "uniswap_v2"}}


def test_a_refused_lifi_route_falls_through_to_the_venue_ladder(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30)
    ctx = chain.ctx()
    asked = []

    def ladder(sell_token, buy_token, amount, *, slippage_bps=None):
        asked.append((sell_token.lower(), buy_token.lower(), amount, slippage_bps))
        return _ladder_quote()
    ctx["venue_quote_fn"] = ladder
    t = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                     sleep_fn=lambda s: None, wait_s=0, client=_client(ok=False),
                     verify_route_fn=_never)
    assert t["state"] == "sold", t
    assert asked == [(TOKEN.lower(), WETH.lower(), 160774000000000000, 500)]


def test_the_ladder_route_never_runs_the_lifi_proof(tmp_path):
    """The LI.FI verifier proves a third party's calldata; ours is not that, and a proof that
    cannot apply must not be called — it would fail on shape and lose a sellable bag."""
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30)
    ctx = chain.ctx()
    ctx["venue_quote_fn"] = lambda *a, **k: _ladder_quote()
    t = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                     sleep_fn=lambda s: None, wait_s=0, client=_client(ok=False),
                     verify_route_fn=_never)
    assert t["state"] == "sold"


def test_both_venues_refusing_still_fails_and_signs_nothing(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30)
    ctx = chain.ctx()
    ctx["venue_quote_fn"] = lambda *a, **k: None
    t = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                     sleep_fn=lambda s: None, wait_s=0, client=_client(ok=False),
                     verify_route_fn=_never)
    assert t["state"] == "failed" and "both refused" in t["reason"]
    assert chain.signed == []


def test_a_ladder_quote_selling_more_than_the_balance_is_refused(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30)
    ctx = chain.ctx()
    ctx["venue_quote_fn"] = lambda *a, **k: _ladder_quote(sell_amount=160774000000000001)
    t = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                     sleep_fn=lambda s: None, wait_s=0, client=_client(ok=False),
                     verify_route_fn=_never)
    assert t["state"] == "failed" and chain.signed == []


def test_a_thin_ladder_floor_still_refuses(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "4663", TOKEN, now_iso=T0)
    chain = _Chain(allowance=10**30)
    ctx = chain.ctx()
    ctx["venue_quote_fn"] = lambda *a, **k: _ladder_quote(min_out=1)
    t = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, lock_dir=str(tmp_path),
                     sleep_fn=lambda s: None, wait_s=0, client=_client(ok=False),
                     verify_route_fn=_never)
    assert t["state"] == "failed" and "minimum" in t["reason"] and chain.signed == []


def _chain_context(chain_id, *, min_out, price=600, send_result='0xswap'):
    """Chain-specific checked balances; public/provider operations are fully local."""
    quote = CHAIN_QUOTE[chain_id]['token'].lower()
    balances = {TOKEN: 100, quote: 0}
    sent, signed, quoted = [], [], []

    def send(raw):
        sent.append(raw)
        if send_result:
            balances[TOKEN] = 0
            balances[quote] += min_out
        return send_result

    def sign(tx):
        signed.append(tx)
        return ('raw-swap', '0xswap', tx['nonce'])

    def route(token, output, amount, **kwargs):
        quoted.append((token.lower(), output.lower(), amount))
        return _ladder_quote(min_out=min_out, sell_amount=amount)

    ctx = {'chain': chain_id, 'address': ADDR,
           'pending_nonce_fn': lambda: 7, 'confirmed_nonce_fn': lambda: 7,
           'warm_quote_price_fn': lambda: price, 'venue_quote_fn': route,
           'lifi_local': {
               'balance_raw_checked_fn': lambda token: balances[token.lower()],
               'allowance_fn': lambda *a: 10**30,
               'estimate_gas_fn': lambda tx: 100_000,
               'construct_tx_fn': lambda tx, gas: {**tx, 'gas': gas, 'nonce': 7,
                                                  'chainId': int(chain_id)},
               'sign_complete_fn': sign, 'send_fn': send,
               'receipt_fn': lambda tx: {'status': '0x1'}}}
    return ctx, sent, signed, quoted


@pytest.mark.parametrize('failure', [TimeoutError('node timed out'), RuntimeError(
    'eth_estimateGas RPC error -32603 at https://rpc.example/private-key?token=secret')])
def test_holding_gas_failure_is_not_reported_as_a_proven_revert(tmp_path, failure):
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, sent, signed, quoted = _chain_context('56', min_out=10**16)
    estimates = []

    def estimate(tx):
        estimates.append(tx)
        raise failure

    ctx['lifi_local']['estimate_gas_fn'] = estimate
    ctx['lifi_local']['construct_tx_fn'] = _never
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0, verify_route_fn=_never)
    assert result['state'] == 'failed' and result['intent_id'] == iid
    assert 'swap gas check failed' in result['reason'] and 'would revert' not in result['reason']
    assert type(failure).__name__ in result['reason']
    assert 'private-key' not in result['reason'] and 'secret' not in result['reason']
    assert len(estimates) == len(quoted) == 1 and sent == signed == []
    assert m.unresolved_holding_sells(c, '56') == []


@pytest.mark.parametrize('chain_id,min_out,price,want_usd', [
    ('56', 10**16, 600, 6), ('8453', 10**15, 2500, 2.5),
    ('1', 10**15, 2500, 2.5), ('57073', 10**15, 2500, 2.5),
    ('5042', 2_500_000, None, 2.5),
])
def test_configured_evm_holding_sells_use_chain_quote_units_without_robinhood_lifi(
        tmp_path, monkeypatch, chain_id, min_out, price, want_usd):
    c = _conn()
    iid = m.enqueue_holding_sell(c, chain_id, TOKEN, now_iso=T0)
    ctx, sent, signed, quoted = _chain_context(chain_id, min_out=min_out, price=price)
    monkeypatch.setattr(eh, '_client', _never)
    monkeypatch.setattr(eh, '_manifest', _never)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0, verify_route_fn=_never)
    assert result['state'] == 'sold', result
    assert result['intent_id'] == iid and result['usd_received'] == pytest.approx(want_usd)
    assert quoted == [(TOKEN, CHAIN_QUOTE[chain_id]['token'].lower(), 100)]
    assert len(sent) == len(signed) == 1 and signed[0]['chainId'] == int(chain_id)
    assert m.unresolved_holding_sells(c, chain_id) == []


@pytest.mark.parametrize('send_result', [None, 'raise'])
def test_signed_multichain_sell_never_releases_identity_on_uncertain_send(
        tmp_path, send_result):
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, sent, signed, _ = _chain_context('56', min_out=10**16, send_result=send_result)
    if send_result == 'raise':
        def down(raw):
            raise TimeoutError('transport lost after request')
        ctx['lifi_local']['send_fn'] = down
    ctx['lifi_local']['receipt_fn'] = lambda tx: None
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] in ('unresolved', 'error'), result
    rows = m.unresolved_holding_sells(c, '56')
    assert len(rows) == 1 and rows[0]['intent_id'] == iid and rows[0]['sig'] == '0xswap'
    assert rows[0]['signed_payload_b64']
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0) == 0
    ctx['lifi_local']['receipt_fn'] = lambda tx: {'status': '0x1'}
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0) == 1
    assert len(signed) == 1


@pytest.mark.parametrize('chain_id', ['56', '4663'])
@pytest.mark.parametrize('phase', ['approval', 'swap'])
def test_explicit_send_rejection_is_terminal_but_keeps_signed_audit(
        tmp_path, chain_id, phase):
    from sniper.execution import exec_lock
    c = _conn()
    iid = m.enqueue_holding_sell(c, chain_id, TOKEN, now_iso=T0)
    ctx, _, signed, _ = _chain_context(chain_id, min_out=10**16)
    local, sent = ctx['lifi_local'], []
    local['allowance_fn'] = lambda *a: 0 if phase == 'approval' else 10**30

    def sign(tx):
        signed.append(tx)
        return ('raw-' + phase, '0x' + phase, tx['nonce'])

    def reject(raw):
        row = c.execute('SELECT state,sig,transaction_phase FROM memecoin_holding_sell_intents '
                        'WHERE intent_id=?', (iid,)).fetchone()
        assert tuple(row) == ('approval_submitted' if phase == 'approval' else 'submitted',
                              '0x' + phase, phase)
        sent.append(raw)
        return False  # The production send seam maps only explicit rejection to False.

    local.update(sign_complete_fn=sign, send_fn=reject, receipt_fn=_never)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0, client=_client(ok=False))
    assert result['state'] == 'failed' and 'refused the ' + phase in result['reason']
    row = c.execute('SELECT state,sig,signed_payload_b64,transaction_phase '
                    'FROM memecoin_holding_sell_intents WHERE intent_id=?', (iid,)).fetchone()
    assert row['state'] == 'failed' and row['sig'] == '0x' + phase
    assert row['signed_payload_b64'] and row['transaction_phase'] == phase
    assert len(signed) == len(sent) == 1 and sent == ['raw-' + phase]
    assert m.unresolved_holding_sells(c, chain_id) == []
    # The funded-entry and gas-maintenance guards share this exact chain-local count.
    assert m.unresolved_holding_sell_count(c, chain_id) == 0
    key = exec_lock.evm_exec_lock_key(chain_id, ADDR)
    enter = exec_lock.serialize_executor(lambda *_: 'allowed', key, lock_dir=str(tmp_path),
                                         holding_entry_chain=chain_id, maintenance_chain=chain_id)
    assert enter(c, None) == 'allowed'
    assert eh.book_busy(c, ctx, lock_dir=str(tmp_path)) is None
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0, lock_dir=str(tmp_path)) == 0
    assert eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                        lock_dir=str(tmp_path), wait_s=0)['state'] == 'idle'
    assert len(signed) == len(sent) == 1  # Terminal rejection does not automatically retry.


def test_ladder_required_pretransactions_are_refused_before_approval_or_signing(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, sent, signed, _ = _chain_context('56', min_out=10**16)
    quote = _ladder_quote(min_out=10**16, sell_amount=100)
    quote['pre_transactions'] = [{'to': TOKEN, 'data': '0xunwrap', 'value': 0}]
    ctx['venue_quote_fn'] = lambda *a, **k: quote
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'failed' and 'pre-transaction' in result['reason']
    assert signed == sent == []


def test_multichain_snapshot_uses_known_inventory_exact_decimals_and_chain_protection(monkeypatch):
    c = _conn()
    _open_real(c, TOKEN, network_id='8453')  # Same contract on another chain is not protected.
    protected_token = '0x' + '22' * 20
    _open_real(c, protected_token, network_id='56')
    candidates = [{'mint': t, 'symbol': 'T', 'decimals': None}
                  for t in (TOKEN, protected_token, CHAIN_QUOTE['56']['token'].lower())]
    monkeypatch.setattr(m, 'holding_candidates', lambda *a, **k: candidates, raising=False)
    reads = []

    def inventory(address, tokens, *, chain_id):
        reads.append((address, tokens, chain_id))
        return {TOKEN: (12_345_678, 6)}

    result = eh.snapshot_once(c, address=ADDR, chain_id='56', now_iso=T0,
                              inventory_fn=inventory,
                              price_fn=lambda keys: {('56', TOKEN): {'price': 2}})
    assert result['state'] == 'ok', result
    rows = m.other_holdings(c)['56']['rows']
    assert len(rows) == 1 and rows[0]['amount'] == pytest.approx(12.345678)
    stored = c.execute('SELECT amount_raw,decimals FROM memecoin_other_holdings '
                       "WHERE network_id='56' AND mint=?", (TOKEN,)).fetchone()
    assert tuple(stored) == ('12345678', 6)
    assert rows[0]['value_usd'] == pytest.approx(24.691356)
    assert reads == [(ADDR, [TOKEN], '56')]


@pytest.mark.parametrize('inventory', [{}, {TOKEN: (None, 18)}, {TOKEN: (1, None)},
                                        {TOKEN: (1, 256)}, {TOKEN: (True, 18)}])
def test_incomplete_generic_inventory_preserves_snapshot(monkeypatch, inventory):
    c = _conn()
    m.replace_other_holdings(c, '56', [{'mint': TOKEN, 'amount': 3, 'amount_raw': '3',
                                       'decimals': 0}], observed_at=T0, unpriced_count=1)
    monkeypatch.setattr(m, 'holding_candidates', lambda *a, **k: [
        {'mint': TOKEN, 'symbol': 'T', 'decimals': 18}], raising=False)
    result = eh.snapshot_once(c, address=ADDR, chain_id='56', now_iso=T0,
                              inventory_fn=lambda *a, **k: inventory, price_fn=lambda keys: {})
    assert result['state'] == 'rpc_unreadable'
    assert m.other_holdings(c)['56']['rows'][0]['amount'] == 3


def test_generic_inventory_candidate_limit_preserves_previous_snapshot(monkeypatch):
    c = _conn()
    m.replace_other_holdings(c, '56', [{'mint': TOKEN, 'amount': 3, 'amount_raw': '3',
                                       'decimals': 0}], observed_at=T0, unpriced_count=1)
    monkeypatch.setattr(m, 'holding_candidates', lambda *a, **k: [
        {'mint': '0x%040x' % i, 'decimals': None} for i in range(501)], raising=False)
    result = eh.snapshot_once(c, address=ADDR, chain_id='56', now_iso=T0,
                              inventory_fn=_never, price_fn=_never)
    assert result['state'] == 'candidate_limit'
    assert m.other_holdings(c)['56']['rows'][0]['amount'] == 3


@pytest.mark.parametrize('chain_identity', ['0x1', None, True])
def test_public_inventory_wrong_or_unknown_chain_returns_no_values(chain_identity):
    calls = []
    def post(body):
        calls.append(body)
        return {'id': body['id'], 'result': chain_identity}
    with pytest.raises((ValueError, RuntimeError), match='identity'):
        eh.onchain_inventory(ADDR, [TOKEN], chain_id='56', post_fn=post)
    assert len(calls) == 1 and calls[0]['method'] == 'eth_chainId'


def test_public_inventory_reads_decimals_only_for_positive_balances():
    zero = '0x' + '22' * 20
    calls = []
    def post(body):
        calls.append(body)
        if isinstance(body, dict):
            return {'id': body['id'], 'result': '0x38'}
        result = []
        for item in body:
            data = item['params'][0]['data']
            token = item['params'][0]['to']
            value = 6 if data == '0x313ce567' else (12_345_678 if token == TOKEN else 0)
            result.append({'id': item['id'], 'result': hex(value)})
        return list(reversed(result))
    assert eh.onchain_inventory(ADDR, [TOKEN, zero], chain_id='56', post_fn=post) == {
        TOKEN: (12_345_678, 6), zero: (0, None)}
    assert len(calls) == 3
    assert [x['params'][0]['to'] for x in calls[-1]] == [TOKEN]


def test_multichain_reference_prices_never_cross_contract_chain_identity():
    calls = []
    def get(url, headers):
        calls.append(url)
        if '/tokens/v1/bsc/' in url:
            return [{'chainId': 'bsc', 'baseToken': {'address': TOKEN}, 'priceUsd': '2'},
                    {'chainId': 'ethereum', 'baseToken': {'address': TOKEN}, 'priceUsd': '99'}]
        if '/tokens/v1/arc/' in url:
            return []
        assert 'chain=5042' in url
        return {'chainId': 5042, 'address': TOKEN, 'priceUSD': '0.5'}
    prices = eh.holding_prices([('56', TOKEN), ('5042', TOKEN)], get_fn=get)
    assert prices[('56', TOKEN)]['price'] == 2
    assert prices[('5042', TOKEN)]['price'] == 0.5


def test_holdings_workers_do_not_block_other_chains_or_mutate_default_chain(monkeypatch):
    import threading
    from sniper.persistence import db_core
    entered, release = threading.Event(), threading.Event()
    observed = []
    monkeypatch.setattr(eh, '_WORKER', {})
    monkeypatch.setattr(db_core, 'get_connection', _conn)
    def scan(conn, **kwargs):
        observed.append(kwargs['chain_id'])
        entered.set()
        release.wait(2)
        return {'state': 'ok'}
    monkeypatch.setattr(eh, 'snapshot_once', scan)
    kwargs = dict(snapshot_due=True, sell_due=False, resolve_due=False,
                  min_sell_usd=0.5, now_fn=lambda: T0, log_fn=lambda _: None)
    try:
        assert eh.start_pass({'chain': '56', 'address': ADDR}, **kwargs)
        assert entered.wait(2)
        assert not eh.start_pass({'chain': '56', 'address': ADDR}, **kwargs)
        assert eh.start_pass({'chain': '5042', 'address': ADDR}, **kwargs)
    finally:
        release.set()
        for state in eh._WORKER.values():
            state['thread'].join(3)
    assert set(observed) == {'56', '5042'} and eh.CHAIN == '4663'


@pytest.mark.parametrize('send_result', [None, 'raise'])
def test_unknown_approval_preserves_signed_identity_and_never_resolves_as_a_sale(
        tmp_path, send_result):
    c = _conn()
    iid = m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0)
    chain = _Chain(allowance=0)
    ctx = chain.ctx()
    observed = []

    def send(raw):
        row = c.execute('SELECT state,sig FROM memecoin_holding_sell_intents WHERE intent_id=?',
                         (iid,)).fetchone()
        observed.append((raw, tuple(row)))
        if send_result == 'raise':
            raise TimeoutError('approval transport lost after request')
        return None

    ctx['lifi_local']['send_fn'] = send
    ctx['lifi_local']['receipt_fn'] = lambda _: None
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, client=_client(),
                          verify_route_fn=lambda *a: None, lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == ('error' if send_result == 'raise' else 'unresolved'), result
    assert observed == [('raw-approve', ('approval_submitted', '0xapprove'))]
    # The previous release resolves only state='submitted'; rollback cannot delete this holding
    # after seeing a successful approval receipt.
    assert c.execute("SELECT COUNT(*) FROM memecoin_holding_sell_intents "
                     "WHERE state='submitted'").fetchone()[0] == 0
    row = m.unresolved_holding_sells(c, '4663')[0]
    assert row['transaction_phase'] == 'approval' and row['signed_payload_b64']
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0) == 0
    ctx['lifi_local']['receipt_fn'] = lambda _: {'status': '0x1'}
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0) == 1
    row = c.execute('SELECT state,reason FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (iid,)).fetchone()
    assert row[0] == 'failed' and 'approval' in row[1]
    assert chain.signed == ['approve']


def test_supported_chains_only_include_enabled_existing_evm_capabilities(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_ALLOWLIST', {'56', '5042', '999', 'solana'})
    assert eh.supported_chains() == {'56', '5042'}


def test_unresolved_holding_identity_blocks_another_sell_even_when_nonce_is_not_visible(tmp_path):
    c = _conn()
    old = m.enqueue_holding_sell(c, '56', '0x' + '22' * 20, now_iso=T0)
    m.resolve_holding_sell(c, old, 'submitted', now_iso=T0, sig='0xunseen')
    new = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, sent, signed, _ = _chain_context('56', min_out=10**16)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'holding_unresolved'
    assert signed == sent == []
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (new,)).fetchone()[0] == 'queued'


@pytest.mark.parametrize('outstanding,want', [
    (True, 'standing_unresolved'), (None, 'book_unreadable'),
])
def test_standing_intent_fences_holding_before_claim_or_nonce_read(
        tmp_path, monkeypatch, outstanding, want):
    from sniper.execution import exec_lock
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, sent, signed, quoted = _chain_context('56', min_out=10**16)
    ctx['pending_nonce_fn'] = ctx['confirmed_nonce_fn'] = _never
    namespace = str(tmp_path / 'standing')
    checked = []

    def outstanding_fn(chain, *, lock_dir=None, required=False):
        with exec_lock.wallet_exec_lock(
                exec_lock.evm_exec_lock_key('56', ADDR),
                lock_dir=str(tmp_path), blocking=False) as held:
            assert not held, 'standing fence must run while the wallet lock is held'
        checked.append((chain, lock_dir, required))
        return outstanding

    # The intent owner/helper is developed separately; this proves the holdings integration.
    monkeypatch.setattr(exec_lock, 'standing_intent_lock_dir',
                        lambda lock_dir=None: namespace, raising=False)
    monkeypatch.setattr(exec_lock, 'intents_outstanding', outstanding_fn)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == want
    assert checked == [('56', namespace, True)]
    assert sent == signed == quoted == []
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (iid,)).fetchone()[0] == 'queued'
    assert exec_lock.exec_generation(
        exec_lock.evm_exec_lock_key('56', ADDR), lock_dir=str(tmp_path)) == 0


def test_holding_busy_uses_count_without_loading_unresolved_payloads(monkeypatch):
    c = _conn()
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    monkeypatch.setattr(m, 'unresolved_holding_sell_count', lambda *_: 1)
    monkeypatch.setattr(m, 'unresolved_holding_sells', _never)
    ctx['pending_nonce_fn'] = ctx['confirmed_nonce_fn'] = _never
    assert eh.book_busy(c, ctx) == 'holding_unresolved'


def test_retained_standing_intent_is_chain_local_and_independent_of_optional_fence(
        tmp_path, monkeypatch):
    from eth_account import Account
    from sniper.execution import exec_lock
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_EXEC_INTENT_STAMP', False)
    c = _conn()
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    other_ctx, _, _, _ = _chain_context('1', min_out=10**16)
    signed = Account.create().sign_transaction({
        'chainId': 56, 'nonce': 7, 'to': bytes.fromhex(SPENDER[2:]), 'value': 0,
        'gas': 21_000, 'gasPrice': 1, 'data': '0x'})
    namespace = exec_lock.standing_intent_lock_dir(lock_dir=str(tmp_path))
    intent = exec_lock.intent_send('0x' + signed.raw_transaction.hex(), 'standing-approval',
                                   lock_dir=namespace, required=True)
    assert intent is not None
    assert exec_lock.intents_outstanding('56', lock_dir=str(tmp_path), required=True) is False
    assert eh.book_busy(c, ctx, lock_dir=str(tmp_path)) == 'standing_unresolved'
    assert eh.book_busy(c, other_ctx, lock_dir=str(tmp_path)) is None
    # Only the owning terminal-proof path removes the receipt-lived identity.
    Path(intent[0]).unlink()
    assert eh.book_busy(c, ctx, lock_dir=str(tmp_path)) is None


def test_unreadable_gas_ledger_preserves_queued_holding_sell(tmp_path, monkeypatch):
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, sent, signed, quoted = _chain_context('56', min_out=10**16)
    ctx['pending_nonce_fn'] = ctx['confirmed_nonce_fn'] = _never
    reads = []

    def unreadable(conn, chain):
        reads.append(chain)
        raise OSError('gas ledger unavailable')

    monkeypatch.setattr(m, 'has_unresolved_evm_gas_topup', unreadable)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'book_unreadable'
    assert reads == ['56']
    assert sent == signed == quoted == []
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (iid,)).fetchone()[0] == 'queued'


@pytest.mark.parametrize('debt_chain,booked,want', [
    ('56', False, 'exit_unresolved'), ('1', False, 'sold'), ('56', True, 'sold'),
])
def test_closed_signed_derisk_debt_fences_holding_sell_until_booked(
        tmp_path, monkeypatch, debt_chain, booked, want):
    from sniper.execution import exec_lock
    c = _conn()
    pos = _open_real(c, TOKEN, network_id=debt_chain)
    claim = m.claim_position_derisk(c, pos, T0)
    assert claim is not None
    assert m.mark_position_derisk_submitted(c, pos, claim, T0, '0xprivate_partial')
    m.close_position(c, pos, T0, 0, 'legacy_close', recorded_at=T0)
    if booked:
        m.insert_fill(c, position_id=pos, kind='derisk', price=0.01, qty=100,
                      notional=1, fill_source='rpc', fraction=0.1, created_at=T0,
                      tx_sig='0xprivate_partial', network_id=debt_chain)
    before = dict(c.execute('SELECT * FROM memecoin_positions WHERE id=?', (pos,)).fetchone())
    assert m.has_unresolved_real_exit(c, '56') == (debt_chain == '56' and not booked)
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, sent, signed, quoted = _chain_context('56', min_out=10**16)
    real_check, checked, nonce_reads = m.has_unresolved_real_exit, [], []

    def locked_check(conn, chain):
        with exec_lock.wallet_exec_lock(exec_lock.evm_exec_lock_key('56', ADDR),
                                        lock_dir=str(tmp_path), blocking=False) as held:
            assert not held, 'exit debt fence must run while the wallet lock is held'
        checked.append(chain)
        return real_check(conn, chain)

    def nonce():
        nonce_reads.append('read')
        return 7  # Public pending/latest can both miss the private signed transaction.

    monkeypatch.setattr(m, 'has_unresolved_real_exit', locked_check)
    ctx['pending_nonce_fn'] = ctx['confirmed_nonce_fn'] = nonce
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == want
    assert dict(c.execute('SELECT * FROM memecoin_positions WHERE id=?', (pos,)).fetchone()) == before
    if want == 'exit_unresolved':
        assert checked == ['56']
        assert sent == signed == quoted == nonce_reads == []
        assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                         (iid,)).fetchone()[0] == 'queued'
        assert exec_lock.exec_generation(exec_lock.evm_exec_lock_key('56', ADDR),
                                        lock_dir=str(tmp_path)) == 0
    else:
        assert len(sent) == len(signed) == 1 and nonce_reads


def test_unreadable_exit_debt_ledger_preserves_queued_holding_sell(tmp_path, monkeypatch):
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, sent, signed, quoted = _chain_context('56', min_out=10**16)
    reads = []

    def unreadable(conn, chain):
        reads.append(chain)
        raise sqlite3.OperationalError('exit debt ledger unavailable')

    monkeypatch.setattr(m, 'has_unresolved_real_exit', unreadable)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'book_unreadable'
    assert reads == ['56']
    assert sent == signed == quoted == []
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (iid,)).fetchone()[0] == 'queued'


@pytest.mark.parametrize('bad', ['duplicate', 'overflow', 'bool_id', 'error'])
def test_public_inventory_refuses_ambiguous_or_malformed_balance_answers(bad):
    def post(body):
        if isinstance(body, dict):
            return {'id': body['id'], 'result': '0x38'}
        if body[0]['params'][0]['data'] == '0x313ce567':
            return [{'id': 0, 'result': '0x6'}]
        answer = {'id': 0, 'result': '0x1'}
        if bad == 'duplicate':
            return [answer, {'id': 0, 'result': '0x0'}]
        if bad == 'overflow':
            answer['result'] = '0x1' + '0' * 64
        if bad == 'bool_id':
            answer['id'] = False
        if bad == 'error':
            answer['error'] = {'code': -32000}
        return [answer]
    with pytest.raises(ValueError):
        eh.onchain_inventory(ADDR, [TOKEN], chain_id='56', post_fn=post)


def _inventory_http(monkeypatch, *, chain='1', failure=None):
    """Keep endpoint selection, identity proof and inventory decoding real; replace only HTTP."""
    import io
    import urllib.request
    calls = []

    def urlopen(req, timeout):
        body = json.loads(req.data)
        calls.append((req.full_url, body, timeout))
        if failure is not None:
            raise failure
        if isinstance(body, dict):
            answer = {'jsonrpc': '2.0', 'id': body['id'], 'result': hex(int(chain))}
        else:
            answer = [{'jsonrpc': '2.0', 'id': item['id'],
                       'result': '0x6' if item['params'][0]['data'] == '0x313ce567' else '0x7b'}
                      for item in body]
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    return calls


def test_missing_ethereum_observer_uses_keyless_reader_without_mutating_watcher_or_custody(monkeypatch):
    monkeypatch.delenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_1', raising=False)
    monkeypatch.setenv('MEMECOIN_EVM_RPC_URL_1', 'https://custody.invalid/private-key')
    monkeypatch.setenv('MEMECOIN_EVM_RPC_POOL_URLS_1', 'https://pool.invalid/private-key')
    calls = _inventory_http(monkeypatch)
    assert eh.onchain_inventory(ADDR, [TOKEN], chain_id='1') == {TOKEN: (123, 6)}
    assert {url for url, _, _ in calls} == {'https://ethereum-rpc.publicnode.com'}
    assert calls[0][1]['method'] == 'eth_chainId'
    assert len(calls) == 3 and all(timeout == 6.0 for _, _, timeout in calls)
    assert 'MEMECOIN_ONCHAIN_EVM_RPC_URL_1' not in eh.os.environ
    assert eh.os.environ['MEMECOIN_EVM_RPC_URL_1'] == 'https://custody.invalid/private-key'


@pytest.mark.parametrize('chain', ['1', '56', '8453', '4663', '5042', '57073'])
def test_inventory_uses_explicit_independent_observer_on_each_chain(monkeypatch, chain):
    observer = 'https://observer.invalid/rpc?api-key=synthetic-test-secret'
    monkeypatch.setenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_' + chain, observer)
    calls = _inventory_http(monkeypatch, chain=chain)
    assert eh.onchain_inventory(ADDR, [TOKEN], chain_id=chain) == {TOKEN: (123, 6)}
    assert {url for url, _, _ in calls} == {observer}
    assert calls[0][1]['method'] == 'eth_chainId' and len(calls) == 3


@pytest.mark.parametrize('chain', ['1', '56'])
@pytest.mark.parametrize('observer', ['http://observer.invalid', 'https://', 'not-a-url'])
def test_inventory_invalid_explicit_observer_does_not_fall_back(monkeypatch, chain, observer):
    monkeypatch.setenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_' + chain, observer)
    calls = _inventory_http(monkeypatch, chain=chain)
    with pytest.raises(ValueError, match='observer'):
        eh.onchain_inventory(ADDR, [TOKEN], chain_id=chain)
    assert calls == []


def test_selected_observer_chain_identity_is_proved_before_balances(monkeypatch):
    monkeypatch.setenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_56', 'https://observer.invalid')
    calls = _inventory_http(monkeypatch, chain='1')
    with pytest.raises(ValueError, match='identity'):
        eh.onchain_inventory(ADDR, [TOKEN], chain_id='56')
    assert len(calls) == 1 and calls[0][0] == 'https://observer.invalid'


def test_inventory_identity_uses_gateway_compatible_positive_id_before_balances(monkeypatch):
    import io
    import urllib.request
    from urllib.error import HTTPError
    monkeypatch.setenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_56', 'https://observer.invalid')
    calls = _inventory_http(monkeypatch, chain='56')
    serve = urllib.request.urlopen

    def gateway(req, timeout):
        body = json.loads(req.data)
        if isinstance(body, dict) and body['id'] < 0:
            # Measured defibit reply: otherwise valid eth_chainId with id=-1 returns HTTP 400.
            raw = b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}'
            raise HTTPError(req.full_url, 400, 'Bad Request', {}, io.BytesIO(raw))
        return serve(req, timeout)

    monkeypatch.setattr(urllib.request, 'urlopen', gateway)
    assert eh.onchain_inventory(ADDR, [TOKEN], chain_id='56') == {TOKEN: (123, 6)}
    assert calls[0][1] == {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []}
    assert len(calls) == 3


@pytest.mark.parametrize('reply', [
    None, {'id': 1, 'result': None}, {'id': 1, 'result': '0x1'},
    {'result': '0x38'}, {'id': None, 'result': '0x38'}, {'id': 2, 'result': '0x38'},
    {'id': True, 'result': '0x38'}, {'id': 1.0, 'result': '0x38'}, {'id': '1', 'result': '0x38'},
    {'id': 1, 'result': '0x38', 'error': {'code': -32000}},
    [{'id': 1, 'result': '0x38'}, {'id': 1, 'result': '0x38'}],
])
def test_inventory_identity_refuses_unknown_wrong_missing_boolean_or_duplicate_reply(reply):
    calls = []

    def post(body):
        calls.append(body)
        return reply

    with pytest.raises(ValueError, match='identity'):
        eh.onchain_inventory(ADDR, [TOKEN], chain_id='56', post_fn=post)
    assert calls == [{'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []}]


@pytest.mark.parametrize('failure_kind', ['http', 'timeout', 'partial'])
def test_observer_failure_preserves_inventory_without_retry_or_secret_log(monkeypatch, caplog,
                                                                       failure_kind):
    from urllib.error import HTTPError
    observer = 'https://observer.invalid/rpc?api-key=synthetic-test-secret'
    monkeypatch.setenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_56', observer)
    c = _conn()
    m.replace_other_holdings(c, '56', [{'mint': TOKEN, 'amount': 3, 'amount_raw': '3',
                                       'decimals': 0}], observed_at=T0, unpriced_count=1)
    monkeypatch.setattr(m, 'holding_candidates', lambda *a, **k: [
        {'mint': TOKEN, 'symbol': 'T', 'decimals': None}], raising=False)
    failure = (HTTPError(observer, 429, 'synthetic-test-secret', {}, None)
               if failure_kind == 'http' else TimeoutError('synthetic-test-secret'))
    calls = _inventory_http(monkeypatch, chain='56',
                            failure=None if failure_kind == 'partial' else failure)
    if failure_kind == 'partial':
        import io
        import urllib.request

        def partial(req, timeout):
            body = json.loads(req.data)
            calls.append((req.full_url, body, timeout))
            answer = {'jsonrpc': '2.0', 'id': body['id'], 'result': '0x38'} if isinstance(body, dict) else []
            return io.BytesIO(json.dumps(answer).encode())

        monkeypatch.setattr(urllib.request, 'urlopen', partial)
    result = eh.snapshot_once(c, address=ADDR, chain_id='56', now_iso=T0,
                              price_fn=_never)
    assert result['state'] == 'rpc_unreadable'
    assert m.other_holdings(c)['56']['rows'][0]['amount'] == 3
    assert all(url == observer for url, _, _ in calls)
    assert len(calls) == (2 if failure_kind == 'partial' else 1)
    assert 'synthetic-test-secret' not in caplog.text and 'observer.invalid' not in caplog.text
    if failure_kind == 'http':
        assert 'HTTPError status=429' in caplog.text


def test_receipt_resolver_cannot_finalize_an_action_while_its_wallet_is_locked(tmp_path):
    from sniper.execution.exec_lock import wallet_exec_lock
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    m.resolve_holding_sell(c, iid, 'submitted', now_iso=T0, sig='0xswap')
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    with wallet_exec_lock('evm-56-' + ADDR, lock_dir=str(tmp_path)) as held:
        assert held
        assert eh.resolve_once(c, ctx=ctx, now_iso=T0, lock_dir=str(tmp_path)) == 0
    assert len(m.unresolved_holding_sells(c, '56')) == 1
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0, lock_dir=str(tmp_path)) == 1


def test_legacy_public_balance_reader_refuses_duplicate_identity_instead_of_accepting_zero():
    with pytest.raises(ValueError, match='duplicate'):
        eh.onchain_balances(ADDR, [TOKEN], post_fn=lambda body: [
            {'id': 0, 'result': '0x0'}, {'id': 0, 'result': '0x1'}])


def test_real_context_factory_chain_drives_holding_claim_quote_and_signed_chain(tmp_path, monkeypatch):
    from test_memecoin_evm_context_keys import _production_context
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    wrong = m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0)
    ctx = _production_context(monkeypatch, tmp_path, '56')
    assert ctx['chain'] == '56' and 'chain_id' not in ctx
    local, sent, signed, quoted = _chain_context('56', min_out=10**16)
    # Only replace external operations; preserve the production factory's identity fields.
    ctx.update({k: v for k, v in local.items() if k not in ('chain', 'chain_id')})
    ctx['confirm_once_fn'] = lambda _: 'confirmed'
    monkeypatch.setattr(eh, '_client', _never)
    monkeypatch.setattr(eh, '_manifest', _never)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0, verify_route_fn=_never)
    assert result['state'] == 'sold' and result['intent_id'] == iid, result
    assert quoted == [(TOKEN, '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c', 100)]
    assert len(sent) == len(signed) == 1 and signed[0]['chainId'] == 56
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (wrong,)).fetchone()[0] == 'queued'


@pytest.mark.parametrize('identity', [
    {},
    {'chain': '56', 'chain_id': '4663'}, {'chain': '4663', 'chain_id': '56'},
    {'chain': None}, {'chain': ''}, {'chain': True}, {'chain': '999'},
])
def test_invalid_context_identity_never_claims_signs_resolves_or_starts_a_scan(
        tmp_path, monkeypatch, identity):
    c = _conn()
    for chain in ('56', '4663'):
        m.enqueue_holding_sell(c, chain, TOKEN, now_iso=T0)
    ctx, sent, signed, _ = _chain_context('56', min_out=10**16)
    ctx.pop('chain')
    ctx.update(identity)
    monkeypatch.setattr(eh, '_client', _never)
    monkeypatch.setattr(eh, '_manifest', _never)
    monkeypatch.setattr(eh, '_WORKER', {})
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'invalid_context', result
    assert [r[0] for r in c.execute('SELECT state FROM memecoin_holding_sell_intents')] == [
        'queued', 'queued']
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0, lock_dir=str(tmp_path)) == 0
    assert not eh.start_pass(ctx, snapshot_due=True, sell_due=True, resolve_due=True,
                             min_sell_usd=0.5, now_fn=lambda: T0)
    assert eh._WORKER == {} and signed == sent == []


@pytest.mark.parametrize('decimals', [None, '', True, -1, 256, 'wrong'])
def test_legacy_explorer_unknown_decimals_preserve_snapshot_instead_of_inventing_18(decimals):
    c = _conn()
    m.replace_other_holdings(c, '4663', [{'mint': TOKEN, 'amount': 5, 'amount_raw': '5',
                                         'decimals': 0}], observed_at=T0, unpriced_count=1)
    def listing(address):
        return eh.wallet_tokens(address, get_fn=lambda *a: {'items': [{'value': '5', 'token': {
            'address_hash': TOKEN, 'decimals': decimals, 'symbol': 'T'}}]})
    result = eh.snapshot_once(c, address=ADDR, now_iso=T0, list_fn=listing,
                              price_fn=_never, balance_fn=_never)
    assert result['state'] == 'explorer_unreadable'
    assert m.other_holdings(c)['4663']['rows'][0]['amount'] == 5


def test_legacy_explorer_integer_zero_decimals_is_valid():
    assert eh.wallet_tokens(ADDR, get_fn=lambda *a: {'items': [{'value': '5', 'token': {
        'address_hash': TOKEN, 'decimals': 0, 'symbol': 'T'}}]})[TOKEN] == (5, 0, 'T')


def test_production_context_one_shot_receipt_never_calls_blocking_local_poll(tmp_path, monkeypatch):
    from test_memecoin_evm_context_keys import _production_context
    from sniper.execution.evm_real_seams import ReceiptState
    c = _conn()
    m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx = _production_context(monkeypatch, tmp_path, '56')
    local, _, signed, _ = _chain_context('56', min_out=10**16)
    ctx.update({k: v for k, v in local.items() if k not in ('chain', 'chain_id')})
    ctx['lifi_local']['receipt_fn'] = _never
    reads = []
    def once(tx_hash):
        reads.append(tx_hash)
        return ReceiptState('confirmed', receipt={'status': '0x1', 'blockNumber': '0x123'})
    ctx['confirm_once_fn'] = once
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'sold', result
    assert reads == ['0xswap'] and len(signed) == 1


@pytest.mark.parametrize('receipt_state,want_state', [
    ('confirmed', 'sold'), ('failed', 'failed'), ('unknown', 'unresolved'),
    ('not_found', 'unresolved'), (None, 'unresolved'),
])
def test_one_shot_receipt_states_preserve_unknown_sends_without_blocking_fallback(
        tmp_path, receipt_state, want_state):
    c = _conn()
    m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    ctx['lifi_local']['receipt_fn'] = _never
    reads = []
    def once(tx_hash):
        reads.append(tx_hash)
        return receipt_state
    ctx['confirm_once_fn'] = once
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == want_state
    assert reads == ['0xswap']


@pytest.mark.parametrize('status,want_settled,want_state', [
    ('confirmed', 1, 'sold'), ('failed', 1, 'failed'),
    ('unknown', 0, 'submitted'), ('not_found', 0, 'submitted'),
])
def test_receipt_resolver_uses_one_shot_context_seam(tmp_path, status, want_settled, want_state):
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    m.resolve_holding_sell(c, iid, 'submitted', now_iso=T0, sig='0xswap')
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    ctx['lifi_local']['receipt_fn'] = _never
    reads = []
    def once(tx_hash):
        reads.append(tx_hash)
        return status
    ctx['confirm_once_fn'] = once
    assert eh.resolve_once(c, ctx=ctx, now_iso=T0, lock_dir=str(tmp_path)) == want_settled
    assert reads == ['0xswap']
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (iid,)).fetchone()[0] == want_state


def test_one_shot_receipt_confirms_approval_before_the_swap(tmp_path):
    from sniper.execution.evm_real_seams import ReceiptState
    c = _conn()
    m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0)
    chain = _Chain(allowance=0)
    ctx = chain.ctx()
    ctx['lifi_local']['receipt_fn'] = _never
    reads = []
    def once(tx_hash):
        reads.append(tx_hash)
        return ReceiptState('confirmed')
    ctx['confirm_once_fn'] = once
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, client=_client(),
                          verify_route_fn=lambda *a: None, lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'sold', result
    assert reads == ['0xapprove', '0xswap'] and chain.signed == ['approve', 'swap']


@pytest.mark.parametrize('receipt,want', [({'status': '0x01'}, 'sold'), ({}, 'unresolved')])
def test_one_shot_attached_receipt_is_authoritative_over_synthesized_status(tmp_path, receipt, want):
    from sniper.execution.evm_real_seams import ReceiptState
    c = _conn()
    m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    ctx['lifi_local']['receipt_fn'] = _never
    ctx['confirm_once_fn'] = lambda _: ReceiptState('confirmed', receipt=receipt)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == want


def test_explicit_legacy_chain_id_context_keeps_its_own_chain(tmp_path):
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, _, signed, _ = _chain_context('56', min_out=10**16)
    ctx['chain_id'] = ctx.pop('chain')
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'sold' and result['intent_id'] == iid
    assert signed[0]['chainId'] == 56


@pytest.mark.parametrize('state', ['unknown', 'not_found'])
def test_nonterminal_one_shot_state_cannot_be_overruled_by_an_attached_receipt(tmp_path, state):
    from sniper.execution.evm_real_seams import ReceiptState
    c = _conn()
    m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    ctx['lifi_local']['receipt_fn'] = _never
    ctx['confirm_once_fn'] = lambda _: ReceiptState(state, receipt={'status': '0x1'})
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'unresolved'
    assert len(m.unresolved_holding_sells(c, '56')) == 1


def _record_locked_generation(monkeypatch, tmp_path, chain):
    from sniper.execution import exec_lock as locks
    key = locks.evm_exec_lock_key(chain, ADDR)
    original = locks.touch_generation
    observed = []
    def stamp(actual_key, *, lock_dir=None):
        assert actual_key == key and lock_dir == str(tmp_path)
        with locks.wallet_exec_lock(key, lock_dir=lock_dir, blocking=False) as acquired:
            assert not acquired, 'cache generation must change before releasing the wallet lock'
        original(actual_key, lock_dir=lock_dir)
        observed.append(actual_key)
    monkeypatch.setattr(locks, 'touch_generation', stamp)
    assert locks.exec_generation(key, lock_dir=str(tmp_path)) == 0
    return key, observed


@pytest.mark.parametrize('outcome', ['confirmed', 'unknown', 'sign_raises', 'sign_none',
                                   'approval_unknown', 'approval_raises'])
def test_every_holding_signing_attempt_invalidates_generation_under_wallet_lock(
        tmp_path, monkeypatch, outcome):
    from sniper.execution import exec_lock as locks
    c = _conn()
    approval = outcome.startswith('approval')
    chain_id = '4663' if approval else '56'
    m.enqueue_holding_sell(c, chain_id, TOKEN, now_iso=T0)
    if approval:
        ctx = _Chain(allowance=0).ctx()
    else:
        ctx, _, _, _ = _chain_context(chain_id, min_out=10**16)
    if outcome in ('unknown', 'approval_unknown'):
        ctx['lifi_local']['receipt_fn'] = lambda _: None
    if outcome in ('sign_raises', 'approval_raises'):
        def cannot_sign(tx):
            raise RuntimeError('signer failed')
        ctx['lifi_local']['sign_complete_fn'] = cannot_sign
    if outcome == 'sign_none':
        ctx['lifi_local']['sign_complete_fn'] = lambda tx: None
    key, observed = _record_locked_generation(monkeypatch, tmp_path, chain_id)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, client=_client(),
                          verify_route_fn=lambda *a: None, lock_dir=str(tmp_path), wait_s=0)
    expected = {'confirmed': 'sold', 'unknown': 'unresolved', 'sign_raises': 'error',
                'sign_none': 'failed', 'approval_unknown': 'unresolved', 'approval_raises': 'error'}
    assert result['state'] == expected[outcome]
    assert observed == [key]
    assert locks.exec_generation(key, lock_dir=str(tmp_path)) > 0


@pytest.mark.parametrize('state', ['idle', 'no_route', 'unknown_balance'])
def test_unsigned_holding_pass_keeps_cached_generation(tmp_path, monkeypatch, state):
    from sniper.execution import exec_lock as locks
    c = _conn()
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    if state != 'idle':
        m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    if state == 'no_route':
        ctx['venue_quote_fn'] = lambda *a, **k: None
    if state == 'unknown_balance':
        ctx['lifi_local']['balance_raw_checked_fn'] = lambda _: None
    key, observed = _record_locked_generation(monkeypatch, tmp_path, '56')
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == ('idle' if state == 'idle' else 'failed')
    assert observed == [] and locks.exec_generation(key, lock_dir=str(tmp_path)) == 0


@pytest.mark.parametrize('phase', ['swap', 'approval'])
@pytest.mark.parametrize('status', ['0x1', '0x0', None])
def test_terminal_holding_recovery_invalidates_generation_before_releasing_lock(
        tmp_path, monkeypatch, phase, status):
    from sniper.execution import exec_lock as locks
    c = _conn()
    iid = m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    m.resolve_holding_sell(c, iid, 'approval_submitted' if phase == 'approval' else 'submitted',
                           now_iso=T0, sig='0xswap', transaction_phase=phase)
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    ctx['lifi_local']['receipt_fn'] = lambda _: {'status': status} if status else None
    key, observed = _record_locked_generation(monkeypatch, tmp_path, '56')
    settled = eh.resolve_once(c, ctx=ctx, now_iso=T0, lock_dir=str(tmp_path))
    assert settled == (1 if status else 0)
    assert observed == ([key] if status else [])
    assert (locks.exec_generation(key, lock_dir=str(tmp_path)) > 0) == bool(status)


def test_holding_generation_stamp_failure_latches_fault_without_changing_sale_result(
        tmp_path, monkeypatch):
    from sniper.execution import exec_lock as locks
    c = _conn()
    m.enqueue_holding_sell(c, '56', TOKEN, now_iso=T0)
    ctx, _, _, _ = _chain_context('56', min_out=10**16)
    faults = []
    def broken_stamp(key, *, lock_dir=None):
        raise OSError('generation unavailable')
    def latch(reason):
        with locks.wallet_exec_lock(locks.evm_exec_lock_key('56', ADDR),
                                    lock_dir=str(tmp_path), blocking=False) as held:
            assert not held
        faults.append(reason)
    monkeypatch.setattr(locks, 'touch_generation', broken_stamp)
    monkeypatch.setattr(locks, 'latch_generation_fault', latch)
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5,
                          lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'sold'
    assert len(faults) == 1 and 'generation unavailable' in faults[0]


def test_unsigned_approval_construction_failure_does_not_invalidate_generation(tmp_path, monkeypatch):
    from sniper.execution import exec_lock as locks
    c = _conn()
    m.enqueue_holding_sell(c, '4663', TOKEN, now_iso=T0)
    ctx = _Chain(allowance=0).ctx()
    def cannot_construct(tx, gas):
        raise RuntimeError('no unsigned transaction')
    ctx['lifi_local']['construct_tx_fn'] = cannot_construct
    key, observed = _record_locked_generation(monkeypatch, tmp_path, '4663')
    result = eh.sell_once(c, ctx=ctx, now_iso=T0, min_sell_usd=0.5, client=_client(),
                          verify_route_fn=lambda *a: None, lock_dir=str(tmp_path), wait_s=0)
    assert result['state'] == 'error'
    assert observed == [] and locks.exec_generation(key, lock_dir=str(tmp_path)) == 0
