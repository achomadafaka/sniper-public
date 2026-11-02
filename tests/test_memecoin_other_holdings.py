"""Other holdings (operator, 2026-09-13): list the tokens no position owns, sell them on request.

The web queues a sell intent; the runner sells it under the wallet lock with the funded exit quote.
A mint that an open position or a reservation owns is never listed and never sold.
"""
import sqlite3

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_other_holdings as oh

T0 = "2026-09-13T10:00:00.000000+00:00"
T1 = "2026-09-13T10:00:30.000000+00:00"
OWNER = "Owner1111111111111111111111111111111111111"


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _acct(mint, raw, decimals=6):
    return {"pubkey": "acct-" + mint, "account": {"lamports": 2039280, "data": {"parsed": {"info": {
        "mint": mint, "tokenAmount": {"amount": str(raw), "decimals": decimals}}}}}}


def _rpc(by_program):
    def call(url, method, params):
        assert method == "getTokenAccountsByOwner" and params[0] == OWNER
        return {"result": {"value": by_program.get(params[1]["programId"], [])}}
    return call


def _open_real(c, mint):
    return m.open_position(c, source_user_id="u1", source_handle="a", mint=mint, ticker="T",
                           entry_price=0.01, entry_notional=10.0, entry_fill_source="jupiter",
                           entry_lag_s=0.1, their_size_usd=1.0, opened_at=T0,
                           network_id="solana", execution_mode="real")


def _snapshot(c, accounts, prices):
    return oh.snapshot_once(c, rpc_candidates=["https://rpc"], owner=OWNER, now_iso=T0,
                            call_fn=_rpc(accounts), price_fn=lambda keys: prices,
                            token_facts_fn=lambda mints: {
                                mint: {"symbol": stats.get("symbol")}
                                for (_chain, mint), stats in prices.items()})


def test_the_snapshot_lists_only_tokens_no_position_owns():
    c = _conn()
    _open_real(c, "HELD")
    classic, t22 = oh.TOKEN_PROGRAMS
    tally = _snapshot(c, {classic: [_acct("HELD", 5), _acct(oh.USDC_MINT, 9), _acct("DUST", 0),
                                    _acct("LEFT", 2_500_000)],
                          t22: [_acct("DROP", 1_000, decimals=3)]},
                      {("solana", "LEFT"): {"price": 2.0, "symbol": "LFT", "liquidity": 5e4}})
    assert tally == {"state": "ok", "rows": 2, "unpriced": 1}
    got = m.other_holdings(c)["solana"]
    assert {r["mint"] for r in got["rows"]} == {"LEFT", "DROP"}
    left = next(r for r in got["rows"] if r["mint"] == "LEFT")
    assert left["symbol"] == "LFT" and left["amount"] == 2.5 and left["value_usd"] == 5.0
    assert got["total_usd"] == 5.0 and got["unpriced_count"] == 1


def test_jupiter_names_and_prices_what_dexscreener_left_blank():
    """2026-09-14: 14 of 24 holdings had no name (tokenized stocks, bridged majors)."""
    c = _conn()
    asked = []

    def facts(mints):
        asked.append(sorted(mints))
        return {"STOCK": {"symbol": "MCDx", "price": 260.0, "liquidity": 4e5},
                "NOPRICE": {"symbol": "BISCOTTI", "price": None, "liquidity": 3.0}}
    oh.snapshot_once(c, rpc_candidates=["https://rpc"], owner=OWNER, now_iso=T0,
                     call_fn=_rpc({oh.TOKEN_PROGRAMS[0]: [
                         _acct("LEFT", 2_000_000), _acct("STOCK", 1_000_000),
                         _acct("NOPRICE", 5_000_000)]}),
                     price_fn=lambda keys: {("solana", "LEFT"): {"price": 2.0, "symbol": "LFT"}},
                     token_facts_fn=facts)
    assert asked == [["LEFT", "NOPRICE", "STOCK"]]  # named tokens also need the metadata audit
    rows = {r["mint"]: r for r in m.other_holdings(c)["solana"]["rows"]}
    assert rows["STOCK"]["symbol"] == "MCDx" and rows["STOCK"]["value_usd"] == 260.0
    assert rows["NOPRICE"]["symbol"] == "BISCOTTI" and rows["NOPRICE"]["price_usd"] is None
    assert rows["LEFT"]["symbol"] == "LFT"
    assert rows["LEFT"]["value_usd"] is None  # no metadata lookup result for this token


def test_a_names_failure_never_stops_the_snapshot(monkeypatch):
    monkeypatch.setattr(oh, "_FACTS_CACHE", {})
    def down(url):
        raise OSError("jupiter down")
    assert oh.jupiter_token_facts(["A", "B"], get_fn=down) == {}
    assert oh._FACTS_CACHE == {}                     # a transport failure is not cached


def test_suspicious_token_metadata_cannot_supply_a_cash_value(monkeypatch):
    monkeypatch.setattr(oh, "_FACTS_CACHE", {})
    facts = oh.jupiter_token_facts(["LOOKALIKE"], get_fn=lambda _url: [{
        "id": "LOOKALIKE", "symbol": "WETH", "usdPrice": 2393.16,
        "liquidity": .0078, "audit": {"isSus": True},
    }])
    assert facts["LOOKALIKE"]["symbol"] == "WETH"
    assert facts["LOOKALIKE"]["price"] is None
    assert facts["LOOKALIKE"]["untrusted_price"] is True


def test_suspicious_holding_does_not_inflate_the_total_even_with_a_provider_price():
    c = _conn()
    asked = []
    def facts(mints):
        asked.extend(mints)
        return {"LOOKALIKE": {"symbol": "WETH", "price": None,
                              "untrusted_price": True, "liquidity": .0078}}
    oh.snapshot_once(c, rpc_candidates=["https://rpc"], owner=OWNER, now_iso=T0,
                     call_fn=_rpc({oh.TOKEN_PROGRAMS[0]: [_acct("LOOKALIKE", 2_469_361)]}),
                     price_fn=lambda _keys: {("solana", "LOOKALIKE"): {
                         "symbol": "WETH", "price": 2393.16, "liquidity": None}},
                     token_facts_fn=facts)
    assert asked == ["LOOKALIKE"]
    snapshot = m.other_holdings(c)["solana"]
    row = snapshot["rows"][0]
    assert row["amount"] == 2.469361 and row["symbol"] == "WETH"
    assert row["price_usd"] is None and row["value_usd"] is None
    assert snapshot["total_usd"] == 0 and snapshot["unpriced_count"] == 1


def test_missing_metadata_keeps_the_balance_without_trusting_a_reference_price():
    c = _conn()
    oh.snapshot_once(c, rpc_candidates=["https://rpc"], owner=OWNER, now_iso=T0,
                     call_fn=_rpc({oh.TOKEN_PROGRAMS[0]: [_acct("LOOKALIKE", 2_469_361)]}),
                     price_fn=lambda _keys: {("solana", "LOOKALIKE"): {
                         "symbol": "WETH", "price": 2393.16}},
                     token_facts_fn=lambda _mints: {})
    row = m.other_holdings(c)["solana"]["rows"][0]
    assert row["amount"] == 2.469361
    assert row["value_usd"] is None


def test_jupiter_is_asked_once_per_token_per_ttl(monkeypatch):
    """The runner's main loop calls this every 300 s; Jupiter's limit is shared with funded quotes."""
    monkeypatch.setattr(oh, "_FACTS_CACHE", {})
    calls, clock = [], [1000.0]

    def get(url):
        calls.append(url.split("=", 1)[1])
        return [{"id": "A", "symbol": "AAA", "usdPrice": 1.0}]      # B is unknown to Jupiter
    facts = lambda mints: oh.jupiter_token_facts(mints, get_fn=get, now_fn=lambda: clock[0])
    assert facts(["A", "B"]) == {"A": {"symbol": "AAA", "price": 1.0, "liquidity": None}}
    assert facts(["A", "B"])["A"]["symbol"] == "AAA" and len(calls) == 1   # A and the B miss cached
    assert facts(["A", "B", "C"]) and calls[-1] == "C"                   # only the new token asked
    clock[0] += oh.JUP_FACTS_TTL_S + 1
    facts(["A", "B"])
    assert calls[-1] == "A,B"                                           # refreshed after the TTL


def test_a_failed_wallet_read_keeps_the_old_snapshot():
    c = _conn()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct("LEFT", 1)]}, {})

    def broken(url, method, params):
        return {"error": {"code": -32000}}
    tally = oh.snapshot_once(c, rpc_candidates=["https://rpc"], owner=OWNER, now_iso=T1,
                             call_fn=broken, price_fn=lambda keys: {},
                             token_facts_fn=lambda mints: {})
    assert tally["state"] == "rpc_unreadable"
    assert [r["mint"] for r in m.other_holdings(c)["solana"]["rows"]] == ["LEFT"]


def test_one_live_intent_per_mint_and_stale_ones_expire():
    c = _conn()
    iid = m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0, ttl_s=10)
    try:
        m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
        raise AssertionError("a second live intent must be refused")
    except ValueError:
        pass
    assert m.claim_holding_sell(c, "solana", now_iso=T1) is None      # 30 s > 10 s ttl
    row = c.execute("SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?",
                    (iid,)).fetchone()
    assert row[0] == "expired"
    assert m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T1)


QUOTE = {"inAmount": "2500000", "outAmount": "4900000", "otherAmountThreshold": "4400000",
         "routePlan": []}


def _sell(c, tmp_path, *, quote=QUOTE, confirm="confirmed", events=None, **kw):
    events = events if events is not None else []

    def exit_quote(mint, _tokens, slip, max_accounts=None):
        events.append(("quote", mint, slip, max_accounts))
        return dict(quote) if quote else None

    def send(signed, url):
        row = c.execute("SELECT state, sig FROM memecoin_holding_sell_intents").fetchone()
        events.append(("send", tuple(row)))
        return "SIG"
    args = dict(keypair=object(), pubkey=OWNER, rpc_url="https://rpc", now_iso=T0,
                exit_quote_fn=exit_quote, min_sell_usd=0.5, lock_dir=str(tmp_path),
                build_fn=lambda q, pk, return_metadata=False: {"swapTransaction": "TX",
                                                                "lastValidBlockHeight": 500},
                sign_fn=lambda tx, kp: ("SIGNED", "SIG", {"last_valid_height": 500}),
                send_fn=send, confirm_fn=lambda sig, url: confirm,
                receipt_proceeds_fn=lambda sig: 4.87)
    args.update(kw)
    return oh.sell_once(c, **args), events


def test_a_sell_writes_the_sig_ahead_of_the_broadcast_and_records_the_receipt(tmp_path):
    c = _conn()
    iid = m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    tally, events = _sell(c, tmp_path)
    assert tally["state"] == "sold" and tally["intent_id"] == iid
    assert ("send", ("submitted", "SIG")) in events
    row = c.execute("SELECT state, sig, usd_received, resolved_at, last_valid_height, balance_raw "
                    "FROM memecoin_holding_sell_intents").fetchone()
    assert tuple(row) == ("sold", "SIG", 4.87, T0, 500, "2500000")


def test_a_sold_token_leaves_the_list_at_once(tmp_path):
    """2026-09-14 (GOOGLx): the row outlived its sale until the next 300 s scan, and a second
    sell queued on the empty balance failed "no route sells this token"."""
    c = _conn()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct("LEFT", 2_500_000), _acct("KEEP", 1_000_000)]},
              {("solana", "LEFT"): {"price": 2.0, "symbol": "LFT"},
               ("solana", "KEEP"): {"price": 1.0, "symbol": "KEP"}})
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    tally, _events = _sell(c, tmp_path)
    assert tally["state"] == "sold"
    got = m.other_holdings(c)["solana"]
    assert [r["mint"] for r in got["rows"]] == ["KEEP"] and got["total_usd"] == 1.0


def test_a_rescan_right_after_a_sale_does_not_list_the_token_again(tmp_path):
    """2026-09-14 (GOOGLx, LINK): the rescan a few seconds after a sale read the node's finalized
    balance, listed the sold token again, and a second sell failed "no route"."""
    c = _conn()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct("LEFT", 2_500_000)]},
              {("solana", "LEFT"): {"price": 2.0, "symbol": "LFT"}})
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    assert _sell(c, tmp_path)[0]["state"] == "sold"
    stale = {oh.TOKEN_PROGRAMS[0]: [_acct("LEFT", 2_500_000)]}     # a lagging node's answer
    _snapshot(c, stale, {("solana", "LEFT"): {"price": 2.0, "symbol": "LFT"}})
    assert m.other_holdings(c)["solana"]["rows"] == []


def test_the_wallet_read_asks_for_confirmed_state():
    seen = []

    def call(url, method, params):
        seen.append(params[2].get("commitment"))
        return {"result": {"value": []}}
    oh.wallet_tokens("https://rpc", OWNER, call_fn=call)
    assert seen == ["confirmed", "confirmed"]


def test_a_token_an_open_position_owns_is_never_sold(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "HELD", now_iso=T0)
    _open_real(c, "HELD")
    tally, events = _sell(c, tmp_path)
    assert tally["state"] == "book_busy" and events == []


def test_a_sell_waits_queued_while_any_solana_trade_is_open(tmp_path):
    """2026-09-14: the sell holds the wallet lock through its confirm, and a live exit would wait
    for it. It must not start, and must not be claimed, while the Solana book is not flat."""
    c = _conn()
    iid = m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    pid = _open_real(c, "OTHER")
    tally, events = _sell(c, tmp_path)
    assert tally["state"] == "book_busy" and events == []
    assert c.execute("SELECT state FROM memecoin_holding_sell_intents").fetchone()[0] == "queued"
    c.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=?", (pid,))
    tally, _events = _sell(c, tmp_path)
    assert tally["state"] == "sold" and tally["intent_id"] == iid


def test_an_unreadable_book_sells_nothing(tmp_path, monkeypatch):
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    monkeypatch.setattr(oh, "solana_book_count", lambda conn: None)
    tally, events = _sell(c, tmp_path)
    assert tally["state"] == "book_unreadable" and events == []


def test_a_sell_under_the_minimum_is_refused_before_signing(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    tally, events = _sell(c, tmp_path, quote={**QUOTE, "otherAmountThreshold": "300000"})
    assert tally["state"] == "failed" and "under the $0.50 minimum" in tally["reason"]
    assert [e[0] for e in events] == ["quote"]


def test_explicit_small_sale_accepts_positive_output_only(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, 'solana', 'LEFT', now_iso=T0, allow_small=True)
    tally, events = _sell(c, tmp_path, quote={**QUOTE, 'otherAmountThreshold': '1'})
    assert tally['state'] == 'sold'
    m.enqueue_holding_sell(c, 'solana', 'LEFT', now_iso=T1, allow_small=True)
    tally, events = _sell(c, tmp_path, quote={**QUOTE, 'otherAmountThreshold': '0', 'outAmount': '0'})
    assert tally['state'] == 'failed'
    assert [e[0] for e in events] == ['quote']


def test_ui_can_queue_a_small_sale_but_rejects_non_boolean_consent():
    cl, c = _client()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct('TINY', 1_000_000)]}, {('solana', 'TINY'): {'price': 0.60}})
    data = cl.get('/api/memecoin/holdings').get_json()
    assert data['chains']['solana']['rows'][0]['sellable']
    headers = {'X-CSRF-Token': data['csrf_token']}
    body = {'network_id': 'solana', 'mint': 'TINY', 'allow_small': 'false'}
    assert cl.post('/api/memecoin/holdings/sell', json=body, headers=headers).status_code == 400
    body['allow_small'] = True
    assert cl.post('/api/memecoin/holdings/sell', json=body, headers=headers).status_code == 202
    assert c.execute('SELECT allow_small FROM memecoin_holding_sell_intents').fetchone()[0] == 1


def test_an_oversized_build_requotes_with_the_account_cap(tmp_path, monkeypatch):
    from sniper.execution import solana_exit_executor as X
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", 48)
    sizes = iter([True, False, False])
    monkeypatch.setattr(X, "_oversized", lambda signed: next(sizes))
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    tally, events = _sell(c, tmp_path)
    assert tally["state"] == "sold"
    assert [e[3] for e in events if e[0] == "quote"] == [None, 48]


def _state(c):
    return c.execute("SELECT state, reason, usd_received FROM memecoin_holding_sell_intents "
                     "ORDER BY requested_at DESC").fetchone()


@pytest.mark.parametrize("status", ["pending", "not_found", "unavailable"])
def test_an_unknown_result_keeps_the_sell_open_and_blocks_a_second_one(tmp_path, status):
    """Codex P1 (2026-09-13): these statuses were terminal 'failed' and freed the mint."""
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    tally, _ = _sell(c, tmp_path, confirm=status)
    assert tally["state"] == "unresolved"
    assert _state(c)[0] == "submitted"
    with pytest.raises(ValueError):
        m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T1)


def test_a_send_that_reports_no_signature_is_still_confirmed_from_the_chain(tmp_path):
    """Codex P1: a transport failure became 'not_sent' without asking the chain."""
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    asked = []
    tally, _ = _sell(c, tmp_path, send_fn=lambda signed, url: None,
                     confirm_fn=lambda sig, url: asked.append(sig) or "confirmed")
    assert asked == ["SIG"] and tally["state"] == "sold"


def test_a_sell_that_fails_on_chain_frees_the_mint(tmp_path):
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    tally, _ = _sell(c, tmp_path, confirm="failed")
    assert tally["state"] == "failed" and _state(c)[0] == "failed"
    assert m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T1)


def test_a_quote_is_never_recorded_as_money_received(tmp_path):
    """Codex P2: a missing receipt stored the quote's outAmount as usd_received."""
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    tally, _ = _sell(c, tmp_path, receipt_proceeds_fn=lambda sig: None)
    state, reason, usd = _state(c)
    assert state == "sold" and usd is None and "the quote was $4.90" in reason


def _submitted(c, tmp_path):
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    _sell(c, tmp_path, confirm="not_found")
    assert _state(c)[0] == "submitted"


def _resolve(c, *, status="not_found", height=501, balance=2_500_000, receipt=4.87):
    return oh.resolve_once(c, pubkey=OWNER, rpc_url="https://rpc", now_iso=T1,
                           confirm_once_fn=lambda sig: status, height_fn=lambda: height,
                           receipt_proceeds_fn=lambda sig: receipt,
                           balance_fn=lambda mint: balance)


def test_the_resolver_records_a_late_confirmation(tmp_path):
    c = _conn(); _submitted(c, tmp_path)
    assert list(_resolve(c, status="confirmed").values()) == ["sold"]
    assert _state(c)[0] == "sold" and _state(c)[2] == 4.87


@pytest.mark.parametrize("height, balance", [(500, 2_500_000),    # still inside the window
                                             (501, 1_000_000),    # balance moved: it may have landed
                                             (501, 0)])           # unreadable balance reads as 0
def test_the_resolver_keeps_a_sell_open_without_both_facts(tmp_path, height, balance):
    c = _conn(); _submitted(c, tmp_path)
    assert _resolve(c, height=height, balance=balance) == {}
    assert _state(c)[0] == "submitted"


def test_the_resolver_fails_a_sell_that_provably_never_landed(tmp_path):
    c = _conn(); _submitted(c, tmp_path)
    assert list(_resolve(c).values()) == ["failed"]
    assert _state(c)[:2] == ("failed", "the sell expired and never landed")
    assert m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T1)


def test_a_busy_wallet_leaves_the_intent_queued(tmp_path):
    from sniper.execution.exec_lock import wallet_exec_lock
    c = _conn()
    m.enqueue_holding_sell(c, "solana", "LEFT", now_iso=T0)
    with wallet_exec_lock("sol-%s" % OWNER, lock_dir=str(tmp_path)):
        tally, events = _sell(c, tmp_path)
    assert tally["state"] == "wallet_busy" and events == []
    assert m.holding_sell_queued(c, "solana")


# --- the routes: the web lists and queues, it never sells ---

def _client(**context):
    c = _conn()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c, **context}))
    return app.test_client(), c


def test_known_evm_inventory_includes_closed_bags_and_preserves_snapshot_metadata():
    c = _conn()
    for net, mint in [('56', '0xABC'), ('56', '0xDEF'), ('8453', '0xABC')]:
        pid = m.open_position(c, source_user_id='u1', source_handle='a', mint=mint,
                              ticker='BAG', entry_price=1, entry_notional=10,
                              entry_fill_source='test', entry_lag_s=0, their_size_usd=1,
                              opened_at=T0, network_id=net, execution_mode='real')
        c.execute('UPDATE memecoin_positions SET is_open=0 WHERE id=?', (pid,))
    m.replace_other_holdings(c, '56', [{'mint': '0xAbC', 'symbol': 'OLD', 'decimals': 9},
                                      {'mint': '0xFFF', 'symbol': 'DROP', 'decimals': 6}],
                             observed_at=T0, unpriced_count=2)
    rows = m.holding_candidates(c, '56', limit=501)
    assert {r['mint'] for r in rows} == {'0xabc', '0xdef', '0xfff'}
    assert next(r for r in rows if r['mint'] == '0xabc')['decimals'] == 9
    assert next(r for r in rows if r['mint'] == '0xdef')['decimals'] is None
    assert len(m.holding_candidates(c, '56', limit=2)) == 2


def test_evm_holding_protection_is_chain_scoped_and_unknown_fails_closed():
    c = _conn()
    for net, mint in [('56', '0xABC'), ('8453', '0xDEF')]:
        m.open_position(c, source_user_id='u1', source_handle='a', mint=mint,
                        ticker='BAG', entry_price=1, entry_notional=10,
                        entry_fill_source='test', entry_lag_s=0, their_size_usd=1,
                        opened_at=T0, network_id=net, execution_mode='real')
    assert m.protected_holding_mints(c, '56') == {'0xabc'}
    c.execute('DROP TABLE memecoin_pending_entries')
    assert m.protected_holding_mints(c, '56') is None


def test_holding_approval_identity_survives_reopen_and_is_not_a_sale():
    c = _conn()
    m.replace_other_holdings(c, '56', [{'mint': '0xabc', 'amount': 1}],
                             observed_at=T0, unpriced_count=1)
    iid = m.enqueue_holding_sell(c, '56', '0xabc', now_iso=T0)
    m.resolve_holding_sell(c, iid, 'approval_submitted', now_iso=T0, sig='APPROVAL',
                          signed_payload_b64='BYTES', transaction_phase='approval')
    m.ensure_memecoin_tables(c)
    pending = m.unresolved_holding_sells(c, '56')[0]
    assert pending['transaction_phase'] == 'approval' and pending['sig'] == 'APPROVAL'
    assert pending['signed_payload_b64'] == 'BYTES'
    assert m.other_holdings(c)['56']['rows'][0]['mint'] == '0xabc'
    assert c.execute("SELECT sig FROM memecoin_holding_sell_intents WHERE state='submitted'").fetchall() == []
    assert m.live_holding_sell_mints(c, '56') == {'0xabc'}
    with pytest.raises(ValueError):
        m.enqueue_holding_sell(c, '56', '0xabc', now_iso=T1)
    with pytest.raises(ValueError):
        m.resolve_holding_sell(c, iid, 'submitted', now_iso=T0, transaction_phase='bogus')


def test_holding_write_ahead_must_bind_a_live_row():
    c = _conn()
    with pytest.raises(ValueError, match='durably bound'):
        m.resolve_holding_sell(c, 'missing', 'submitted', now_iso=T0, sig='SIG')
    iid = m.enqueue_holding_sell(c, '56', '0xabc', now_iso=T0)
    m.resolve_holding_sell(c, iid, 'failed', now_iso=T0)
    with pytest.raises(ValueError, match='durably bound'):
        m.resolve_holding_sell(c, iid, 'submitted', now_iso=T0, sig='SIG')


@pytest.mark.parametrize('network,quote', [('56', 'WBNB'), ('8453', 'WETH'), ('5042', 'USDC')])
def test_other_holdings_api_queues_supported_evm_chains(monkeypatch, network, quote):
    from sniper.services import memecoin_evm_holdings as eh
    from sniper.routes import api_memecoin as api
    monkeypatch.setattr(eh, 'supported_chains', lambda: {'56', '8453', '5042'}, raising=False)
    monkeypatch.setattr(api, 'read_heartbeat', lambda _p: {
        'other_holdings': {'evm_ready_chains': ['56', '8453', '5042']}})
    cl, c = _client()
    mint = '0x' + 'a' * 40
    m.replace_other_holdings(c, network, [{'mint': mint, 'amount': 1}],
                             observed_at=T0, unpriced_count=1)
    data = cl.get('/api/memecoin/holdings').get_json()
    row = data['chains'][network]['rows'][0]
    assert row['sellable'] and row['quote_symbol'] == quote
    headers = {'X-CSRF-Token': data['csrf_token']}
    response = cl.post('/api/memecoin/holdings/sell', json={
        'network_id': network, 'mint': '0x' + 'A' * 40}, headers=headers)
    assert response.status_code == 202
    assert m.holding_sell_queued(c, network)


def test_other_holdings_api_does_not_offer_or_queue_an_unsupported_chain(monkeypatch):
    from sniper.services import memecoin_evm_holdings as eh
    monkeypatch.setattr(eh, 'supported_chains', lambda: {'56'}, raising=False)
    cl, c = _client()
    m.replace_other_holdings(c, '57073', [{'mint': '0xabc', 'amount': 1}],
                             observed_at=T0, unpriced_count=1)
    data = cl.get('/api/memecoin/holdings').get_json()
    row = data['chains']['57073']['rows'][0]
    assert row['sellable'] is False and row['why_not']
    response = cl.post('/api/memecoin/holdings/sell', json={
        'network_id': '57073', 'mint': '0xabc'},
        headers={'X-CSRF-Token': data['csrf_token']})
    assert response.status_code == 400 and not m.holding_sell_queued(c, '57073')


def test_other_holdings_requires_a_ready_runner_context_when_reported(monkeypatch, tmp_path):
    import json
    from sniper.services import memecoin_evm_holdings as eh
    monkeypatch.setattr(eh, 'supported_chains', lambda: {'56', '8453'})
    heartbeat = tmp_path / 'runner.heartbeat'
    heartbeat.write_text(json.dumps({'ts': __import__('time').time(),
                                    'other_holdings': {'evm_ready_chains': ['8453']}}))
    cl, c = _client(heartbeat_path=str(heartbeat))
    m.replace_other_holdings(c, '56', [{'mint': '0xabc', 'amount': 1}],
                             observed_at=T0, unpriced_count=1)
    data = cl.get('/api/memecoin/holdings').get_json()
    assert data['chains']['56']['rows'][0]['sellable'] is False
    response = cl.post('/api/memecoin/holdings/sell', json={
        'network_id': '56', 'mint': '0xabc'}, headers={'X-CSRF-Token': data['csrf_token']})
    assert response.status_code == 400 and not m.holding_sell_queued(c, '56')


def test_legacy_runner_cannot_consume_new_chain_holding_sales(monkeypatch):
    from sniper.services import memecoin_evm_holdings as eh
    from sniper.routes import api_memecoin as api
    monkeypatch.setattr(eh, 'supported_chains', lambda: {'56', '4663'})
    monkeypatch.setattr(api, 'read_heartbeat', lambda _p: {'other_holdings': {'alive': True}})
    cl, c = _client()
    m.replace_other_holdings(c, '56', [{'mint': '0xabc', 'amount': 1}],
                             observed_at=T0, unpriced_count=1)
    data = cl.get('/api/memecoin/holdings').get_json()
    assert not data['chains']['56']['rows'][0]['sellable']
    response = cl.post('/api/memecoin/holdings/sell', json={'network_id': '56', 'mint': '0xabc'},
                       headers={'X-CSRF-Token': data['csrf_token']})
    assert response.status_code == 400 and not m.holding_sell_queued(c, '56')


def test_the_routes_list_holdings_and_queue_a_sell():
    cl, c = _client()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct("LEFT", 2_500_000), _acct("TINY", 1_000_000)]},
              {("solana", "LEFT"): {"price": 2.0}, ("solana", "TINY"): {"price": 0.60}})
    j = cl.get("/api/memecoin/holdings").get_json()
    rows = {r["mint"]: r for r in j["chains"]["solana"]["rows"]}
    assert rows["LEFT"]["sellable"] and rows["LEFT"]["sell"] is None
    assert rows["TINY"]["sellable"] and rows["TINY"]["why_not"] is None
    headers = {"X-CSRF-Token": j["csrf_token"]}
    assert cl.post("/api/memecoin/holdings/sell", json={"network_id": "solana", "mint": "LEFT"}
                   ).status_code == 403
    r = cl.post("/api/memecoin/holdings/sell", json={"network_id": "solana", "mint": "LEFT"},
                headers=headers)
    assert r.status_code == 202 and r.get_json()["state"] == "queued"
    assert cl.post("/api/memecoin/holdings/sell", json={"network_id": "solana", "mint": "LEFT"},
                   headers=headers).status_code == 409
    assert cl.post("/api/memecoin/holdings/sell", json={"network_id": "solana", "mint": "GONE"},
                   headers=headers).status_code == 404
    again = cl.get("/api/memecoin/holdings").get_json()["chains"]["solana"]["rows"]
    assert next(r for r in again if r["mint"] == "LEFT")["sell"]["state"] == "queued"


def test_unresolved_withdrawal_defers_holding_sell(tmp_path):
    c = _conn()
    iid = m.enqueue_withdraw(c, 'solana', 'DEST', 10, now_iso=T0)
    m.resolve_withdraw(c, iid, 'submitted', now_iso=T0, sig='WITHDRAW')
    m.enqueue_holding_sell(c, 'solana', 'LEFT', now_iso=T0)
    tally, events = _sell(c, tmp_path)
    assert tally['state'] == 'withdrawal_unresolved'
    assert events == [] and m.holding_sell_queued(c, 'solana')


def test_hidden_holdings_change_only_the_display(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    (tmp_path / 'data').mkdir()
    path = tmp_path / 'data/memecoin_hidden_holdings.json'
    cl, c = _client()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct('LEFT', 2_500_000), _acct('TINY', 1)]},
              {('solana', 'LEFT'): {'price': 2.0}})
    path.write_text(json.dumps({'4663': ['LEFT']}))
    assert len(cl.get('/api/memecoin/holdings').get_json()['chains']['solana']['rows']) == 2
    path.write_text(json.dumps({'solana': ['LEFT']}))
    data = cl.get('/api/memecoin/holdings').get_json()
    assert [r['mint'] for r in data['chains']['solana']['rows']] == ['TINY']
    assert data['chains']['solana']['total_usd'] == 0
    assert data['unpriced_count'] == 1
    assert len(m.other_holdings(c)['solana']['rows']) == 2
    m.enqueue_holding_sell(c, 'solana', 'LEFT', now_iso=T0)
    assert {r['mint'] for r in cl.get('/api/memecoin/holdings').get_json()
            ['chains']['solana']['rows']} == {'LEFT', 'TINY'}


@pytest.mark.parametrize('network', ['solana', '4663'])
def test_under_sixty_cent_holdings_are_completely_hidden(network):
    cl, c = _client()
    m.replace_other_holdings(c, network, [
        {'mint': 'DUST', 'amount': 1, 'price_usd': .599999, 'value_usd': .599999},
        {'mint': 'ZERO', 'amount': 0, 'price_usd': 1, 'value_usd': 0},
        {'mint': 'BOUNDARY', 'amount': 1, 'price_usd': .60, 'value_usd': .60},
        {'mint': 'UNKNOWN', 'amount': 1, 'price_usd': None, 'value_usd': None},
        {'mint': 'PENDING', 'amount': 1, 'price_usd': .001, 'value_usd': .001},
    ], observed_at=T0, unpriced_count=1)
    iid = m.enqueue_holding_sell(c, network, 'PENDING', now_iso=T0)
    data = cl.get('/api/memecoin/holdings').get_json()
    net = data['chains'][network]
    assert {r['mint'] for r in net['rows']} == {'BOUNDARY', 'UNKNOWN'}
    assert net['total_usd'] == .60 and net['dust_count'] == 3
    assert data['unpriced_count'] == 1
    assert len(m.other_holdings(c)[network]['rows']) == 5
    assert c.execute('SELECT state FROM memecoin_holding_sell_intents WHERE intent_id=?',
                     (iid,)).fetchone()[0] == 'queued'


def test_verified_cooldown_hide_expires_at_unlock(tmp_path, monkeypatch):
    import json
    from sniper.routes import api_memecoin as api
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/memecoin_hidden_holdings.json').write_text(json.dumps({
        '4663': [{'mint': 'LOCKED', 'until': T1}, {'mint': 'BAD', 'until': 'invalid'},
                 {'mint': ['BAD'], 'until': T1}],
    }))
    cl, c = _client()
    m.replace_other_holdings(c, '4663', [
        {'mint': 'LOCKED', 'amount': 1, 'price_usd': 1, 'value_usd': 1},
        {'mint': 'BAD', 'amount': 1, 'price_usd': 1, 'value_usd': 1},
    ], observed_at=T0, unpriced_count=0)
    monkeypatch.setattr(api, '_now_iso', lambda: T0)
    net = cl.get('/api/memecoin/holdings').get_json()['chains']['4663']
    assert [r['mint'] for r in net['rows']] == ['BAD'] and net['total_usd'] == 1
    monkeypatch.setattr(api, '_now_iso', lambda: T1)
    net = cl.get('/api/memecoin/holdings').get_json()['chains']['4663']
    assert {r['mint'] for r in net['rows']} == {'LOCKED', 'BAD'}
    assert net['total_usd'] == 2 and len(m.other_holdings(c)['4663']['rows']) == 2


# --- a token no venue quotes is not a sell button (2026-09-22, operator) ------------------------

def _refuse(c, mint, reason, *, network="solana"):
    """Record a finished sell that both venues refused."""
    iid = m.enqueue_holding_sell(c, network, mint, now_iso=T0)
    m.resolve_holding_sell(c, iid, "failed", now_iso=T0, reason=reason)
    return iid


def test_a_route_refusal_with_no_liquidity_stops_offering_the_sell():
    cl, c = _client()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct("DEAD", 2_500_000)]},
              {("solana", "DEAD"): {"price": 2.0}})
    _refuse(c, "DEAD", "no route: LI.FI (no_route) and the venue ladder both refused")
    row = {r["mint"]: r for r in cl.get("/api/memecoin/holdings").get_json()["chains"]["solana"]["rows"]}["DEAD"]
    assert row["sellable"] is False
    assert row["why_not"] == "no market — no venue quotes it"


@pytest.mark.parametrize('network', ['solana', '4663'])
@pytest.mark.parametrize('liquidity', [60.22, 0.0])
def test_balance_mark_above_pool_liquidity_is_unknown_without_blocking_sales(network, liquidity):
    cl, c = _client()
    m.replace_other_holdings(c, network, [
        {'mint': 'THIN', 'amount': 88, 'price_usd': 2713.32, 'value_usd': 238772.16,
         'liquidity_usd': liquidity},
        {'mint': 'LIVE', 'amount': 1, 'price_usd': 2, 'value_usd': 2,
         'liquidity_usd': 5000},
        {'mint': 'UNKNOWN_DEPTH', 'amount': 1, 'price_usd': 3, 'value_usd': 3},
    ], observed_at=T0, unpriced_count=0)
    for queued in (False, True):
        if queued:
            m.enqueue_holding_sell(c, network, 'THIN', now_iso=T1)
        data = cl.get('/api/memecoin/holdings').get_json()
        net = data['chains'][network]
        row = next(r for r in net['rows'] if r['mint'] == 'THIN')
        assert row['value_usd'] is None and row['price_usd'] is None
        assert row['valuation_note'] == 'Reference value exceeds reported pool liquidity; sale value unknown'
        assert row['amount'] == 88 and row['sellable'] is True
        assert net['total_usd'] == 5 and net['unpriced_count'] == 1
        assert data['unpriced_count'] == 1
    assert m.other_holdings(c)[network]['total_usd'] == 238777.16


@pytest.mark.parametrize('network', ['solana', '4663'])
def test_no_market_reference_marks_do_not_count_as_holdings_value(network):
    cl, c = _client()
    m.replace_other_holdings(c, network, [
        {'mint': 'DEAD', 'amount': 88, 'price_usd': 2680, 'value_usd': 235840},
        {'mint': 'LIVE', 'amount': 1, 'price_usd': 2, 'value_usd': 2},
    ], observed_at=T0, unpriced_count=0)
    _refuse(c, 'DEAD', 'no route: LI.FI (no_route) and the venue ladder both refused',
            network=network)
    data = cl.get('/api/memecoin/holdings').get_json()
    net = data['chains'][network]
    row = next(r for r in net['rows'] if r['mint'] == 'DEAD')
    assert row['value_usd'] is None and row['price_usd'] is None
    assert row['amount'] == 88 and row['sell']['state'] == 'failed'
    assert net['total_usd'] == 2 and net['unpriced_count'] == 1
    assert data['unpriced_count'] == 1
    # Only the response changes. A pending retry and recovered liquidity remain visible.
    assert m.other_holdings(c)[network]['total_usd'] == 235842
    iid = m.enqueue_holding_sell(c, network, 'DEAD', now_iso=T1)
    pending = cl.get('/api/memecoin/holdings').get_json()['chains'][network]
    assert pending['total_usd'] == 235842
    assert next(r for r in pending['rows'] if r['mint'] == 'DEAD')['sellable']
    m.resolve_holding_sell(c, iid, 'failed', now_iso=T1,
                          reason='no route: LI.FI (no_route) and the venue ladder both refused')
    m.replace_other_holdings(c, network, [
        {'mint': 'DEAD', 'amount': 88, 'price_usd': 1, 'value_usd': 88,
         'liquidity_usd': 5000},
    ], observed_at=T1, unpriced_count=0)
    recovered = cl.get('/api/memecoin/holdings').get_json()['chains'][network]
    assert recovered['rows'][0]['sellable'] and recovered['total_usd'] == 88


def test_a_refusal_on_a_token_that_still_shows_liquidity_keeps_the_button():
    """Liquidity means a venue exists, so the refusal was about this attempt, not the market."""
    cl, c = _client()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct("LIQ", 2_500_000)]},
              {("solana", "LIQ"): {"price": 2.0, "liquidity": 250000.0}})
    _refuse(c, "LIQ", "no validated LI.FI route (no_route)")
    row = {r["mint"]: r for r in cl.get("/api/memecoin/holdings").get_json()["chains"]["solana"]["rows"]}["LIQ"]
    assert row["sellable"] is True and row["why_not"] is None


def test_a_failure_that_is_not_a_route_refusal_keeps_the_button():
    cl, c = _client()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct("OOPS", 2_500_000)]},
              {("solana", "OOPS"): {"price": 2.0}})
    _refuse(c, "OOPS", "internal error; check the runner log")
    row = {r["mint"]: r for r in cl.get("/api/memecoin/holdings").get_json()["chains"]["solana"]["rows"]}["OOPS"]
    assert row["sellable"] is True and row["why_not"] is None


@pytest.mark.parametrize('failure', ['provider_timeout', 'rate_limit', 'auth', 'None'])
def test_a_provider_failure_in_a_route_message_keeps_the_reference_value(failure):
    cl, c = _client()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct('OOPS', 2_500_000)]},
              {('solana', 'OOPS'): {'price': 2.0}})
    _refuse(c, 'OOPS', f'no route: LI.FI ({failure}) and the venue ladder both refused')
    net = cl.get('/api/memecoin/holdings').get_json()['chains']['solana']
    assert net['rows'][0]['sellable'] is True
    assert net['rows'][0]['value_usd'] == 5 and net['total_usd'] == 5


def test_a_lifi_only_refusal_keeps_the_button_even_with_no_liquidity():
    """That message means the ladder was never asked, so it is not evidence about the market.

    `memecoin_evm_holdings.sell_once` emits "no validated LI.FI route" only when
    `ctx["venue_quote_fn"]` is None; when a ladder exists the message says "both refused".
    UNIFROG carried the first message from 2026-09-22, before the ladder was wired, while its
    whole bag quotes 0.0012983 ETH on a native-paired V4 pool that DexScreener does not index —
    so `liquidity_usd` was None and the old guard hid the only control that could sell it.
    """
    cl, c = _client()
    _snapshot(c, {oh.TOKEN_PROGRAMS[0]: [_acct("FROG", 2_500_000)]},
              {("solana", "FROG"): {"price": 2.0}})          # priced, but no liquidity reading
    _refuse(c, "FROG", "no validated LI.FI route (no_route)")
    row = {r["mint"]: r for r in cl.get("/api/memecoin/holdings").get_json()["chains"]["solana"]["rows"]}["FROG"]
    assert row["sellable"] is True and row["why_not"] is None
