"""Concurrent wallet movements never substitute for a signed transaction's own receipt."""
import contextlib
import copy
import sqlite3
import time
from types import SimpleNamespace

import pytest

from sniper.execution import solana_swap_executor as entry
from sniper.execution import solana_exit_executor as exit
from sniper.execution.solana_wallet import USDC_MINT
from sniper.execution.submission_transport import AttemptState
from sniper.persistence import db_memecoin as m
from test_memecoin_entry_roundtrip_cap import _Deps as EntryDeps
from test_memecoin_exit_executor import _c, _Deps as ExitDeps
from test_memecoin_fast_submit_wiring import StubTransport


def receipt_metadata_entry(*, evidence=None, receipt_first=True, proof=(9.9, 123., 6),
                           confirm="confirmed", early_quote=None, quotes=None, oversize=False,
                           source_price=None, pending_mint="MINT", trace_attempt="mint-attempt"):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint=pending_mint, source_user_id="u1", strategy="alpha_b",
        notional=10., reserved_at="2026-10-13T11:29:02Z", network_id="solana",
        attempt_id="mint-attempt", source_price=source_price)
    calls = []
    expected = int(quotes[0]["outAmount"]) if quotes else 1000000000
    trace = SimpleNamespace(attempt_id=trace_attempt, mark_substage=lambda *a, **k: None,
        mark=lambda *a: None, stage_completed_at=lambda stage: "2026-10-13T11:29:02Z")
    def quote(mint, usd, **kw):
        calls.append("quote")
        if quotes is not None:
            return quotes.pop(0)
        return {"inAmount": "10000000", "outAmount": "1000000000",
                "otherAmountThreshold": "990000000", "priceImpactPct": .01}
    def reverse(mint, amount):
        calls.append("reverse")
        out = int(amount) * 9900000 // expected
        return {"inAmount": str(amount), "outAmount": str(out),
                "otherAmountThreshold": str(max(1, out - 1))}
    def usdc():
        calls.append("usdc")
        return 100.
    def token(mint):
        calls.append("token")
        return (0., 6)
    def sign(q):
        import base64
        calls.append("sign")
        data = b"x" * (1400 if oversize and calls.count("sign") == 1 else 10)
        return base64.b64encode(data).decode(), "SIG0"
    kwargs = dict(pending_id=pid, mint="MINT", usd_amount=10., strategy="alpha_b", source_user_id="u1",
        now_iso="2026-10-13T11:29:02Z", quote_fn=quote, reverse_quote_fn=reverse,
        sign_fn=sign, send_fn=lambda signed: calls.append("send") or True,
        confirm_fn=lambda sig: confirm, usdc_fn=usdc, token_fn=token, trace=trace,
        receipt_first=receipt_first, receipt_fill_fn=(None if proof == "missing" else
            lambda sig, mint, bound: proof), chase_max_pct=30., early_quote=early_quote,
        mint_read_evidence=evidence)
    result = entry.execute_entry(c, **kwargs)
    row = m.pending_row(c, pid)
    c.close()
    return result, calls, row


def mint_evidence(decimals=6):
    return {"network_id": "solana", "mint": "MINT", "attempt_id": "mint-attempt",
            "decimals": decimals}


@pytest.mark.parametrize("decimals", [0, 6, 18])
def test_receipt_first_reuses_bound_decimals_without_unused_baseline_reads(decimals):
    result, calls, row = receipt_metadata_entry(evidence=mint_evidence(decimals))
    assert result["status"] == "filled" and row["status"] == "confirmed"
    assert calls == ["quote", "reverse", "sign", "send"]


@pytest.mark.parametrize("bad", ["network_id", "mint", "attempt_id", "none", "bool", "float",
                                   "negative", "large", "missing"])
def test_receipt_first_keeps_live_baselines_when_mint_evidence_unusable(bad):
    evidence = mint_evidence()
    if bad in ("network_id", "mint", "attempt_id"): evidence[bad] = "another"
    else:
        evidence["decimals"] = {"none": None, "bool": True, "float": 6.,
                                "negative": -1, "large": 19, "missing": None}[bad]
        if bad == "missing": evidence.pop("decimals")
    result, calls, row = receipt_metadata_entry(evidence=evidence)
    assert result["status"] == "filled" and row["status"] == "confirmed"
    assert calls.count("usdc") == 1 and calls.count("token") == 1


@pytest.mark.parametrize("confirm,proof,status,pending", [
    ("confirmed", None, "anomaly", "reserved"), ("pending", None, "unknown", "reserved"),
    ("failed", None, "failed", "failed")])
def test_reused_decimals_do_not_substitute_for_receipt_or_release_ambiguity(confirm, proof, status, pending):
    result, calls, row = receipt_metadata_entry(evidence=mint_evidence(), confirm=confirm, proof=proof)
    assert result["status"] == status and row["status"] == pending
    assert row["tx_sig"] == "SIG0" and calls.count("send") == 1
    assert "usdc" not in calls and "token" not in calls


def test_fresh_refusal_requote_keeps_bound_decimals_without_baseline_reads():
    pricey = {"inAmount": "10000000", "outAmount": "250000000",
              "otherAmountThreshold": "235000000", "priceImpactPct": .01}
    early = SimpleNamespace(take=lambda usd: pricey, observed_at="2026-10-13T11:29:02Z")
    result, calls, row = receipt_metadata_entry(evidence=mint_evidence(), early_quote=early,
                                               quotes=[pricey], source_price=.02)
    assert result["status"] == "skipped_chase" and row["status"] == "failed"
    assert calls == ["reverse", "quote", "reverse"]


def test_oversize_retry_keeps_bound_decimals(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", 48)
    result, calls, row = receipt_metadata_entry(evidence=mint_evidence(), oversize=True)
    assert result["status"] == "filled" and row["status"] == "confirmed"
    assert calls == ["quote", "reverse", "sign", "quote", "reverse", "sign", "send"]


def test_missing_receipt_callback_retains_baseline_reads():
    result, calls, _row = receipt_metadata_entry(evidence=mint_evidence(), proof="missing")
    assert result["status"] == "anomaly"
    assert calls.count("usdc") == 1 and calls.count("token") == 1


@pytest.mark.parametrize("valid", [False, True])
def test_mint_evidence_optimization_preserves_serial_baseline_rollback(monkeypatch, valid):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_OVERLAP_PRE_READS", False)
    evidence = mint_evidence() if valid else None
    result, calls, _row = receipt_metadata_entry(evidence=evidence)
    assert result["status"] == "filled"
    assert calls.count("usdc") == (0 if valid else 1)
    assert calls.count("token") == (0 if valid else 1)


@pytest.mark.parametrize("kwargs", [{"receipt_first": False}, {"pending_mint": "another"},
                                      {"trace_attempt": None}, {"trace_attempt": "another"}])
def test_legacy_or_mismatched_pending_trace_keeps_baseline_reads(kwargs):
    result, calls, _row = receipt_metadata_entry(evidence=mint_evidence(), confirm="pending", **kwargs)
    assert result["status"] == "unknown"
    assert calls.count("usdc") == 1 and calls.count("token") == 1


@pytest.mark.parametrize("injected", [False, True])
def test_production_solana_factory_carries_mint_read_evidence(monkeypatch, injected):
    from sniper.execution import solana_real_seams as seams
    from solders.keypair import Keypair
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
        notional=10., reserved_at="t", network_id="solana", attempt_id="mint-attempt")
    seen = {}
    def execute(conn, **kwargs):
        seen.update(kwargs)
        return {"status": "aborted", "reason": "test before sign"}
    monkeypatch.setattr(entry, "execute_entry", execute)
    monkeypatch.setattr(seams, "_impact_shadow_from_env", lambda: None, raising=False)
    transport = StubTransport(AttemptState.ACCEPTED)
    ex = seams.build_real_solana_executor(keypair=Keypair(), rpc_url="https://rpc.invalid",
        http=lambda *a, **k: pytest.fail("factory must not perform RPC"),
        submission_transport=transport,
        token_fn=(lambda mint: 0.) if injected else None)
    ex["execute_fn"](c, pid, "MINT", 10., "t", mint_read_evidence=mint_evidence(),
        entry_min_slot=100, entry_max_slot=101, entry_head_fn=lambda: (100, time.monotonic()))
    c.close()
    assert seen["mint_read_evidence"] == mint_evidence()
    assert seen["receipt_first"] is (not injected)
    assert transport.calls == 0


def receipt(*, signature='SIG0', owner='owner', mint='MINT', sold_atomic=500_000_000):
    token = lambda idx, coin, amount, decimals: {'accountIndex': idx, 'mint': coin, 'owner': owner,
          'uiTokenAmount': {'amount': str(amount), 'decimals': decimals}}
    return {'transaction': {'signatures': [signature], 'message': {'accountKeys': [
        {'pubkey': owner, 'signer': True}, {'pubkey': 'coin-ata'}, {'pubkey': 'usdc-ata'}]}},
        'meta': {'err': None, 'preTokenBalances': [token(1, mint, 500_000_000, 6),
            token(2, USDC_MINT, 100_000_000, 6)],
            'postTokenBalances': [token(1, mint, 500_000_000-sold_atomic, 6),
                token(2, USDC_MINT, 111_250_000, 6)]}}


def test_exit_receipt_returns_exact_sold_atoms_and_proceeds():
    proof = entry.receipt_exit_fill(receipt(sold_atomic=166_666_666),
                                    signature='SIG0', owner='owner', mint='MINT')
    assert proof == {'received': 11.25, 'sold_atomic': 166_666_666, 'decimals': 6}


@pytest.mark.parametrize('bad', ['signature', 'secondary_signature', 'owner', 'unsigned',
             'mint', 'failed', 'missing', 'decimals', 'duplicate', 'amount', 'index', 'identity'])
def test_wrong_or_partial_receipt_never_proves_exit(bad):
    tx = receipt()
    if bad == 'signature': tx['transaction']['signatures'] = ['other']
    if bad == 'secondary_signature': tx['transaction']['signatures'] = ['other', 'SIG0']
    if bad == 'owner': tx['transaction']['message']['accountKeys'][0]['pubkey'] = 'other'
    if bad == 'unsigned': tx['transaction']['message']['accountKeys'][0]['signer'] = False
    if bad == 'mint':
        for key in ('preTokenBalances', 'postTokenBalances'):
            tx['meta'][key][0]['mint'] = 'another'
    if bad == 'failed': tx['meta']['err'] = {'InstructionError': [1, 'failed']}
    if bad == 'missing': tx['meta'].pop('postTokenBalances')
    if bad == 'decimals': tx['meta']['postTokenBalances'][0]['uiTokenAmount']['decimals'] = 9
    if bad == 'duplicate': tx['meta']['postTokenBalances'].append(copy.deepcopy(tx['meta']['postTokenBalances'][1]))
    if bad == 'amount': tx['meta']['preTokenBalances'][0]['uiTokenAmount']['amount'] = True
    if bad == 'index': tx['meta']['preTokenBalances'][0]['accountIndex'] = 100
    if bad == 'identity': tx['meta']['postTokenBalances'][0]['mint'] = 'another'
    assert entry.receipt_exit_fill(tx, signature='SIG0', owner='owner', mint='MINT') is None


def general_entry(c, *, proof=(9.9, 123.0, 6)):
    pending_id = m.reserve_pending(c, mint='MINT', source_user_id='u1', strategy='alpha_b',
                                   notional=10.0, reserved_at='t')
    d = EntryDeps(quote_impact=.01, usd_back=9.9)
    seen = {'confirmed': False, 'post': 0}
    def confirm(sig):
        seen['confirmed'] = True
        return 'confirmed'
    def usdc():
        if seen['confirmed']:
            seen['post'] += 1
            return 90.0  # plausible, but belongs to another overlapping movement
        return 100.0
    def token(mint):
        return 500.0 if seen['confirmed'] else 0.0
    result = entry.execute_entry(c, pending_id=pending_id, mint='MINT', usd_amount=10.0,
        strategy='alpha_b', source_user_id='u1', now_iso='2026-10-09T23:00:00Z',
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn,
        send_fn=d.send_fn, confirm_fn=confirm, usdc_fn=usdc, token_fn=token,
        receipt_fill_fn=lambda sig, mint, maximum: proof(sig, mint, maximum) if callable(proof) else proof,
        receipt_first=True,
        impact_cap_pct=15, sleep_fn=lambda seconds: seen.setdefault('receipt_sleeps', []).append(seconds)
        if seconds == entry.ENTRY_RECEIPT_READ_SLEEP_S else pytest.fail('post-send balance retry'))
    return result, pending_id, seen


def test_entry_uses_receipt_despite_plausible_overlapping_wallet_delta():
    with contextlib.closing(sqlite3.connect(':memory:')) as c:
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        result, pending, seen = general_entry(c)
        assert result['status'] == 'filled' and result['notional'] == 9.9
        fill = c.execute("SELECT * FROM memecoin_fills WHERE kind='entry'").fetchone()
        assert fill['qty'] == 123.0 and seen['post'] == 0
        assert m.pending_row(c, pending)['status'] == 'confirmed'


def test_entry_unreadable_receipt_keeps_signed_reservation_despite_plausible_delta():
    with contextlib.closing(sqlite3.connect(':memory:')) as c:
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        result, pending, seen = general_entry(c, proof=None)
        assert result['status'] == 'anomaly'
        assert m.pending_row(c, pending)['status'] == 'reserved'
        assert m.pending_row(c, pending)['tx_sig']
        assert c.execute('SELECT COUNT(*) FROM memecoin_fills').fetchone()[0] == 0
        assert seen['post'] == 0


@pytest.mark.parametrize('bad', ['signature', 'secondary_signature', 'owner', 'mint', 'partial'])
def test_entry_mismatched_receipt_cannot_book_the_plausible_wallet_delta(bad):
    tx = receipt(signature='SIG123')
    tx['meta']['preTokenBalances'][0]['uiTokenAmount']['amount'] = '0'
    tx['meta']['postTokenBalances'][0]['uiTokenAmount']['amount'] = '123000000'
    tx['meta']['postTokenBalances'][1]['uiTokenAmount']['amount'] = '90100000'
    if bad == 'signature': tx['transaction']['signatures'] = ['other']
    if bad == 'secondary_signature': tx['transaction']['signatures'] = ['other', 'SIG123']
    if bad == 'owner': tx['transaction']['message']['accountKeys'][0]['pubkey'] = 'other'
    if bad == 'mint': tx['meta']['postTokenBalances'][0]['mint'] = 'another'
    if bad == 'partial': tx['meta'].pop('postTokenBalances')
    with contextlib.closing(sqlite3.connect(':memory:')) as c:
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        result, pending, seen = general_entry(c, proof=lambda sig, mint, maximum:
            entry.receipt_entry_fill(tx, signature=sig, owner='owner', mint=mint, maximum=maximum))
        assert result['status'] == 'anomaly'
        assert m.pending_row(c, pending)['status'] == 'reserved'
        assert seen['post'] == 0
        assert c.execute('SELECT COUNT(*) FROM memecoin_fills').fetchone()[0] == 0


def run_exit(c, pos, d, *, partial=False, proof=True, sold_atomic=None):
    args = dict(quote_fn=lambda mint, qty, slip: {'inAmount': str(sold_atomic or 500_000_000),
            'outAmount': '11250000', 'otherAmountThreshold': '10000000'},
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn, slippage_ladder=[3.0],
        sleep_fn=lambda seconds: pytest.fail('post-send balance retry'), receipt_first=True,
        receipt_fill_fn=lambda sig, mint: entry.receipt_exit_fill(
            receipt(signature=sig, sold_atomic=sold_atomic or 500_000_000) if proof else None,
            signature=sig, owner='owner', mint=mint))
    if partial:
        return exit.execute_partial_exit(c, pos, 1/3, '2026-10-09T23:00:00Z', **args)
    return exit.execute_exit(c, pos, 'manual', '2026-10-09T23:00:00Z', **args)


def test_full_exit_uses_receipt_proceeds_instead_of_plausible_wallet_increase():
    c, pos = _c()
    with contextlib.closing(c):
        d = ExitDeps(['confirmed'], received=99.0)
        result = run_exit(c, pos, d)
        assert result['status'] == 'closed' and result['received'] == 11.25
        assert result['realized'] == 1.25
        fill = c.execute("SELECT * FROM memecoin_fills WHERE kind='manual_close'").fetchone()
        assert fill['qty'] == 500.0 and fill['notional'] == 11.25


@pytest.mark.parametrize('partial', [False, True])
def test_missing_receipt_retains_claim_and_duplicate_call_never_sends(partial):
    c, pos = _c()
    with contextlib.closing(c):
        d = ExitDeps(['confirmed'], received=99.0)
        first = run_exit(c, pos, d, partial=partial, proof=False)
        assert first['status'] == 'unknown'
        assert m.get_position(c, pos['id'])['is_open'] == 1
        second = run_exit(c, pos, d, partial=partial)
        assert second['status'] in ('already_closing', 'already_derisked')
        assert d.calls.count('send') == 1
        assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE kind!='entry'").fetchone()[0] == 0


def test_partial_books_actual_receipt_atoms_and_not_synthetic_fraction():
    c, pos = _c()
    with contextlib.closing(c):
        d = ExitDeps(['confirmed'], received=99.0)
        result = run_exit(c, pos, d, partial=True, sold_atomic=166_666_666)
        assert result['status'] == 'derisked' and result['received'] == 11.25
        fill = c.execute("SELECT * FROM memecoin_fills WHERE kind='derisk'").fetchone()
        assert fill['qty'] == 166.666666
        assert fill['qty'] != 500 / 3
        assert fill['price'] == pytest.approx(11.25 / 166.666666)


@pytest.mark.parametrize('damage', [None, 'missing', 'failed', 'signature', 'owner', 'mint', 'partial',
                                    'balance_only', 'measurement_partial', 'result_signature',
                                    'receipt_refresh'])
def test_ordinary_entry_records_exact_receipt_atoms_without_another_read(monkeypatch, damage):
    import json
    from solders.keypair import Keypair
    from solders.signature import Signature
    from sniper.execution import solana_real_seams as seams
    monkeypatch.setenv('MEMECOIN_RECEIPT_RETRY_ENABLED', '0')
    kp = Keypair()
    owner, sig = str(kp.pubkey()), str(Signature.new_unique())
    tx = receipt(signature=sig, owner=owner)
    tx['slot'] = 456550939
    tx['meta'].update(preBalances=[1000000000, 2039280, 2039280],
                      postBalances=[999475000, 2039280, 2039280], fee=525000)
    tx['meta']['preTokenBalances'][0]['uiTokenAmount']['amount'] = '0'
    tx['meta']['postTokenBalances'][0]['uiTokenAmount']['amount'] = '9007199254740993'
    tx['meta']['postTokenBalances'][1]['uiTokenAmount']['amount'] = '90100000'
    if damage == 'missing': tx = None
    elif damage == 'failed': tx['meta']['err'] = {'InstructionError': [1, 'failed']}
    elif damage == 'signature': tx['transaction']['signatures'] = [str(Signature.new_unique())]
    elif damage == 'owner': tx['transaction']['message']['accountKeys'][0]['pubkey'] = 'another'
    elif damage == 'mint': tx['meta']['postTokenBalances'][0]['mint'] = 'another'
    elif damage == 'partial': tx['meta'].pop('postTokenBalances')
    elif damage == 'measurement_partial': tx['meta'].pop('preBalances')
    reads, events = [], []
    trace = SimpleNamespace(attempt_id='receipt-attempt',
        mark_substage=lambda name, **kw: events.append(dict(name=name, **kw)))
    def http(_method, _url, body=None, **_kw):
        request = json.loads(body)
        assert request['method'] == 'getTransaction'
        assert request['params'][0] == sig
        reads.append(request)
        if damage == 'receipt_refresh' and len(reads) == 1:
            wrong = copy.deepcopy(tx)
            wrong['transaction']['signatures'] = [str(Signature.new_unique())]
            return 200, {'result': wrong}, None
        return 200, {'result': tx}, None
    def execute(_conn, **kw):
        if damage == 'balance_only':
            return {'status': 'filled', 'sig': sig}
        if damage == 'receipt_refresh':
            assert kw['receipt_fill_fn'](sig, 'MINT', 10.) is None
        fill = kw['receipt_fill_fn'](sig, 'MINT', 10.)
        return {'status': 'filled' if fill else 'anomaly',
                'sig': str(Signature.new_unique()) if damage == 'result_signature' else sig}
    monkeypatch.setattr(entry, 'execute_entry', execute)
    with contextlib.closing(sqlite3.connect(':memory:')) as c:
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        pending = m.reserve_pending(c, mint='MINT', source_user_id='u1', strategy='alpha_b',
            notional=10., reserved_at='t', network_id='solana', attempt_id=trace.attempt_id)
        transport = StubTransport(AttemptState.ACCEPTED)
        ex = seams.build_real_solana_executor(keypair=kp, rpc_url='https://rpc.invalid', http=http,
            submission_transport=transport,
            usdc_fn=lambda: 100., token_fn=lambda _mint: 0.)
        result = ex['execute_fn'](c, pending, 'MINT', 10., 't', trace=trace,
            entry_min_slot=456550938, entry_max_slot=456550939,
            entry_head_fn=lambda: (456550939, time.monotonic()))
    assert transport.calls == 0
    assert len(reads) == (0 if damage == 'balance_only' else 2 if damage == 'receipt_refresh' else 1)
    evidence = [ev['evidence'] for ev in events if ev['name'] == 'receipt_execution_evidence']
    if damage in (None, 'receipt_refresh'):
        assert result['status'] == 'filled'
        assert len(evidence) == 1 and evidence[0]['state'] == 'verified'
        assert evidence[0]['tx_sig'] == sig and evidence[0]['custody_pubkey'] == owner
        assert evidence[0]['mint'] == 'MINT' and evidence[0]['network_id'] == 'solana'
        assert evidence[0]['payment_atomic'] == '9900000'
        assert evidence[0]['acquired_atomic'] == '9007199254740993'
        assert evidence[0]['receipt_slot'] == 456550939
    else:
        assert all(ev['state'] == 'unknown' for ev in evidence)
        if damage in ('balance_only', 'measurement_partial', 'result_signature'):
            assert result['status'] == 'filled' and len(evidence) == 1



def test_a_receipt_served_late_is_read_again_and_books_the_fill():
    """2026-10-23: 6 of 7 desk fills went anomaly because ONE read raced getTransaction at 'confirmed'."""
    reads = []

    def late(sig, mint, maximum):
        reads.append(sig)
        return None if len(reads) < 3 else (9.9, 123.0, 6)          # not served on the first two reads
    with contextlib.closing(sqlite3.connect(':memory:')) as c:
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        result, pending, seen = general_entry(c, proof=late)
        assert result['status'] == 'filled' and result['notional'] == 9.9
        assert len(reads) == 3 and seen['receipt_sleeps'] == [entry.ENTRY_RECEIPT_READ_SLEEP_S] * 2


def test_a_receipt_never_served_is_todays_anomaly_after_four_reads(caplog):
    reads = []

    def never(sig, mint, maximum):
        reads.append(sig)
        return None
    never.last_served = False
    with contextlib.closing(sqlite3.connect(':memory:')) as c:
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        result, pending, seen = general_entry(c, proof=never)
        assert result['status'] == 'anomaly' and result['reason'] == 'receipt_unreadable'
        assert m.pending_row(c, pending)['status'] == 'reserved'                       # recovery still books it
        assert len(reads) == entry.ENTRY_RECEIPT_READS and len(seen['receipt_sleeps']) == entry.ENTRY_RECEIPT_READS - 1
    assert entry.ENTRY_RECEIPT_READS * entry.ENTRY_RECEIPT_READ_SLEEP_S <= 1.0
