"""Dashboard withdrawals (operator, 2026-09-13): the web queues an amount, the runner sends USDC to
the ONE configured address under the wallet lock, and a signed transfer stays open until the chain
answers."""
import sqlite3

import pytest
from flask import Flask
from solders.keypair import Keypair

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_withdraw as wd

T0 = "2026-09-13T13:00:00.000000+00:00"
T1 = "2026-09-13T13:00:30.000000+00:00"
KP = Keypair()
PK = str(KP.pubkey())
TO = str(Keypair().pubkey())


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _rpc(recipient_account_exists=True):
    def call(url, method, params):
        if method == "getAccountInfo":
            return {"result": {"value": {"owner": "x"} if recipient_account_exists else None}}
        if method == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": "11111111111111111111111111111111",
                                         "lastValidBlockHeight": 500}}}
        raise AssertionError(method)
    return call


def _send(c, tmp_path, *, usdc=1000.0, confirm="confirmed", to=TO, events=None, **kw):
    events = events if events is not None else []

    def send(signed, url):
        events.append(("send", tuple(c.execute(
            "SELECT state, sig, last_valid_height FROM memecoin_withdraw_intents").fetchone())))
        return "SIG"
    args = dict(keypair=KP, pubkey=PK, rpc_url="https://rpc", now_iso=T0, to_address=to,
                usdc_fn=lambda: usdc, lock_dir=str(tmp_path), call_fn=_rpc(), send_fn=send,
                confirm_fn=lambda sig, url: confirm, alert_fn=lambda text: events.append(("alert", text)))
    args.update(kw)
    return wd.withdraw_once(c, **args), events


def _row(c):
    return dict(c.execute("SELECT * FROM memecoin_withdraw_intents").fetchone())


def test_a_withdrawal_is_written_ahead_sent_confirmed_and_announced(tmp_path):
    c = _conn()
    m.enqueue_withdraw(c, "solana", TO, 300.0, now_iso=T0)
    tally, events = _send(c, tmp_path)
    assert tally["state"] == "sent"
    sent = [e for e in events if e[0] == "send"][0][1]
    assert sent[0] == "submitted" and sent[1] == tally["sig"] and sent[2] == 500
    assert _row(c)["state"] == "sent"
    assert any(e[0] == "alert" and "$300.00" in e[1] for e in events)


def test_reserved_usdc_is_never_withdrawn(tmp_path, monkeypatch):
    c = _conn()
    monkeypatch.setattr(m, "pending_totals", lambda *_a, **_k: (1, 800.0))
    m.enqueue_withdraw(c, "solana", TO, 300.0, now_iso=T0)
    tally, events = _send(c, tmp_path, usdc=1000.0)
    assert tally["state"] == "failed" and "free balance $200.00" in tally["reason"]
    assert not [e for e in events if e[0] == "send"]


def test_only_the_configured_address_is_ever_paid(tmp_path):
    c = _conn()
    m.enqueue_withdraw(c, "solana", TO, 10.0, now_iso=T0)
    tally, events = _send(c, tmp_path, to=str(Keypair().pubkey()))    # config changed since
    assert tally["state"] == "failed" and not [e for e in events if e[0] == "send"]


def test_an_unreadable_balance_refuses(tmp_path):
    c = _conn()
    m.enqueue_withdraw(c, "solana", TO, 10.0, now_iso=T0)
    assert _send(c, tmp_path, usdc=None)[0]["reason"] == "could not read the USDC balance"


@pytest.mark.parametrize("status", ["pending", "not_found", "unavailable"])
def test_an_unknown_result_stays_open_and_blocks_another(tmp_path, status):
    c = _conn()
    m.enqueue_withdraw(c, "solana", TO, 10.0, now_iso=T0)
    assert _send(c, tmp_path, confirm=status)[0]["state"] == "unresolved"
    assert _row(c)["state"] == "submitted"
    with pytest.raises(ValueError):
        m.enqueue_withdraw(c, "solana", TO, 10.0, now_iso=T1)


def _submitted(c, tmp_path):
    m.enqueue_withdraw(c, "solana", TO, 10.0, now_iso=T0)
    _send(c, tmp_path, confirm="not_found")


def _resolve(c, *, status="not_found", height=501, proof="not_found"):
    return wd.resolve_withdraws(c, now_iso=T1, confirm_once_fn=lambda sig: status,
                                proof_fn=lambda sig: proof, height_fn=lambda: height,
                                alert_fn=lambda text: None)


def test_the_resolver_records_a_late_confirmation(tmp_path):
    c = _conn(); _submitted(c, tmp_path)
    assert list(_resolve(c, status="confirmed").values()) == ["sent"]


@pytest.mark.parametrize("height, proof", [(500, "not_found"), (501, "pending"), (501, "unavailable")])
def test_the_resolver_needs_expiry_and_a_pinned_miss_to_call_it_failed(tmp_path, height, proof):
    c = _conn(); _submitted(c, tmp_path)
    assert _resolve(c, height=height, proof=proof) == {}
    assert _row(c)["state"] == "submitted"


def test_the_resolver_fails_a_transfer_that_expired_unseen(tmp_path):
    c = _conn(); _submitted(c, tmp_path)
    assert list(_resolve(c).values()) == ["failed"]
    assert m.enqueue_withdraw(c, "solana", TO, 10.0, now_iso=T1)


def test_pending_signature_cannot_be_released_by_an_expired_height_and_null_peer(tmp_path):
    c = _conn(); _submitted(c, tmp_path)
    assert wd.resolve_withdraws(
        c, now_iso=T1, confirm_once_fn=lambda _: "pending",
        height_fn=lambda: pytest.fail("positive pending evidence needs no expiry read"),
        proof_fn=lambda _: pytest.fail("a null peer cannot erase positive pending evidence"),
        alert_fn=lambda _: None) == {}
    assert _row(c)["state"] == "submitted"


@pytest.mark.parametrize("send_result, confirmed, expected", [
    ("accepted", "confirmed", "sent"),
    (None, "not_found", "submitted"),
    (False, "not_found", "failed"),
    ("timeout", "not_found", "submitted"),
])
def test_shared_sender_receives_the_persisted_identity_before_broadcast(
        tmp_path, send_result, confirmed, expected):
    from sniper.execution.submission_wiring import solana_tx_identity
    c = _conn()
    iid = m.enqueue_withdraw(c, "solana", TO, 10.0, now_iso=T0)
    calls = []

    def factory(conn, intent_id):
        assert conn is c and intent_id == iid
        assert _row(c)["state"] == "submitted"

        def send(signed, *, validity):
            row = _row(c)
            assert row["state"] == "submitted"
            assert row["sig"] == solana_tx_identity(signed)
            assert row["last_valid_height"] == validity["last_valid_height"] == 500
            calls.append("shared")
            if send_result == "timeout":
                raise TimeoutError("response lost after sending")
            return row["sig"] if send_result == "accepted" else send_result
        return send

    def confirm(sig, url):
        calls.append("confirm")
        assert sig == _row(c)["sig"]
        return confirmed

    _send(c, tmp_path, send_fn=None, send_factory_fn=factory, confirm_fn=confirm)
    assert _row(c)["state"] == expected
    assert calls == (["shared", "confirm"] if send_result in ("accepted", None)
                     else ["shared"])


# --- the routes: the web queues an amount; it never names a destination ---

def _client(monkeypatch, address=TO):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_WITHDRAW_SOLANA_ADDRESS", address)
    c = _conn()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def test_the_routes_queue_an_amount_to_the_configured_address_only(monkeypatch):
    cl, c = _client(monkeypatch)
    j = cl.get("/api/memecoin/withdraw").get_json()
    assert j["configured"] and j["to_address"] == TO and j["intents"] == []
    h = {"X-CSRF-Token": j["csrf_token"]}
    assert cl.post("/api/memecoin/withdraw", json={"amount_usdc": 5}).status_code == 403
    assert cl.post("/api/memecoin/withdraw", json={"amount_usdc": 0}, headers=h).status_code == 400
    assert cl.post("/api/memecoin/withdraw", json={"amount_usdc": True}, headers=h).status_code == 400
    r = cl.post("/api/memecoin/withdraw", json={"amount_usdc": 300, "to_address": "ELSEWHERE"},
                headers=h)
    assert r.status_code == 202 and r.get_json()["state"] == "queued"
    assert _row(c)["to_address"] == TO and _row(c)["amount_usdc"] == 300.0
    assert cl.post("/api/memecoin/withdraw", json={"amount_usdc": 1}, headers=h).status_code == 409
    assert cl.get("/api/memecoin/withdraw").get_json()["intents"][0]["state"] == "queued"


def test_no_configured_address_means_no_withdrawal(monkeypatch):
    cl, _c = _client(monkeypatch, address="")
    j = cl.get("/api/memecoin/withdraw").get_json()
    assert j["configured"] is False
    r = cl.post("/api/memecoin/withdraw", json={"amount_usdc": 5},
                headers={"X-CSRF-Token": j["csrf_token"]})
    assert r.status_code == 503


def test_request_identity_survives_terminal_state_and_rejects_changed_payload():
    import uuid
    c = _conn()
    rid = str(uuid.uuid4())
    assert m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0, request_id=rid) == rid
    m.resolve_withdraw(c, rid, 'sent', now_iso=T1)
    assert m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T1, request_id=rid) == rid
    with pytest.raises(ValueError, match='different'):
        m.enqueue_withdraw(c, 'solana', TO, 11, now_iso=T1, request_id=rid)
    assert c.execute('SELECT COUNT(*) FROM memecoin_withdraw_intents').fetchone()[0] == 1


def test_concurrent_request_identity_creates_one_intent(tmp_path):
    import concurrent.futures
    import threading
    import uuid
    path = str(tmp_path / 'requests.db')
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.close()
    rid = str(uuid.uuid4())
    barrier = threading.Barrier(2)
    def request():
        db = sqlite3.connect(path, timeout=5)
        try:
            barrier.wait()
            return m.enqueue_withdraw(db, 'solana', TO, 10, now_iso=T0, request_id=rid)
        finally:
            db.close()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        assert [f.result() for f in futures] == [rid, rid]
    c = sqlite3.connect(path)
    assert c.execute('SELECT COUNT(*) FROM memecoin_withdraw_intents').fetchone()[0] == 1
    c.close()


def test_claimed_crash_remains_schedulable_and_recovers(tmp_path):
    c = _conn()
    m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    m.claim_withdraw(c, 'solana', now_iso=T0)
    assert m.withdraw_pending(c, 'solana') == 'claimed'
    result, events = _send(c, tmp_path, now_iso='2026-09-14T13:00:00+00:00')
    assert result['state'] == 'idle'
    assert _row(c)['state'] == 'failed'
    assert not [e for e in events if e[0] == 'send']


def test_open_book_defers_withdrawal_without_claim(tmp_path, monkeypatch):
    from sniper.services import memecoin_other_holdings as holdings
    monkeypatch.setattr(holdings, 'solana_book_count', lambda c: 1)
    c = _conn()
    m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    result, events = _send(c, tmp_path)
    assert result['state'] == 'book_busy'
    assert _row(c)['state'] == 'queued'
    assert not events


def test_default_confirm_is_one_receipt_read(tmp_path, monkeypatch):
    from sniper.execution import solana_real_seams as seams
    calls = []
    monkeypatch.setattr(seams, 'rpc_confirm', lambda *a, **kw: calls.append(kw) or 'pending')
    c = _conn()
    m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    assert _send(c, tmp_path, confirm_fn=None)[0]['state'] == 'unresolved'
    assert calls == [{'max_attempts': 1}]


def test_preflight_rejection_fails_immediately_with_the_node_reason(tmp_path, monkeypatch):
    from sniper.execution import solana_real_seams as seams
    seen = []
    def reject(signed, url, **kw):
        seen.append(kw)
        return 'explicit_rejected'
    monkeypatch.setattr(seams, 'rpc_send', reject)
    monkeypatch.setattr(seams, 'take_send_error', lambda: 'BlockhashNotFound')
    c = _conn()
    m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    result, _ = _send(c, tmp_path, send_fn=None, confirm='not_found')
    assert result['state'] == 'failed'
    assert 'BlockhashNotFound' in _row(c)['reason']
    assert seen == [{'preflight_commitment': 'confirmed', 'classify_preflight': True}]


@pytest.mark.parametrize('proof, expected', [('not_found', 'failed'),
                                          ('unavailable', 'submitted'), ('pending', 'submitted')])
def test_worker_uses_the_context_pinned_expiry_proof(
        monkeypatch, proof, expected):
    from sniper.execution import solana_real_seams as seams
    c = _conn()
    iid = m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    m.resolve_withdraw(c, iid, 'submitted', now_iso=T0, sig='SIG', last_valid_height=500)
    calls = []
    final = []
    class WorkerConnection:
        def __getattr__(self, name): return getattr(c, name)
        def close(self):
            final.append(_row(c)['state'])
            c.close()
    monkeypatch.setattr(seams, 'rpc_confirm',
                        lambda sig, url, **kw: calls.append((sig, url)) or proof)
    class InlineThread:
        def __init__(self, *, target, **kw): self.target = target
        def is_alive(self): return False
        def start(self): self.target()
    monkeypatch.setattr(wd.threading, 'Thread', InlineThread)
    monkeypatch.setattr(wd, '_WORKER', None)
    monkeypatch.setattr(wd, '_alert', lambda _: None)
    wd.start_pass({'confirm_once_fn': lambda _: 'not_found',
                   'confirm_proof_once_fn': lambda sig: calls.append((sig, 'context-pinned')) or proof,
                   'block_height_fn': lambda: 501},
                  state='submitted', keypair=None, rpc_url='https://withdraw-rpc',
                  to_address=TO, now_fn=lambda: T1, connection_fn=WorkerConnection,
                  log_fn=lambda _: None)
    assert calls == [('SIG', 'context-pinned')]
    assert final == [expected]


def test_worker_uses_funded_withdrawal_reads_sender_and_one_confirmation(monkeypatch):
    confirmed = []
    calls = []
    call = lambda *a: None
    factory = lambda *a: None

    class Connection:
        def close(self):
            calls.append("closed")

    class InlineThread:
        def __init__(self, *, target, **kw): self.target = target
        def is_alive(self): return False
        def start(self): self.target()

    def withdraw(conn, **kwargs):
        assert kwargs["rpc_url"] == "https://custody-rpc"
        assert kwargs["call_fn"] is call
        assert kwargs["send_factory_fn"] is factory
        assert kwargs["confirm_fn"]("SIG", "https://custody-rpc") == "pending"
        calls.append("withdraw")
        return {"state": "unresolved"}

    monkeypatch.setattr(wd.threading, "Thread", InlineThread)
    monkeypatch.setattr(wd, "_WORKER", None)
    monkeypatch.setattr(wd, "withdraw_once", withdraw)
    assert wd.start_pass(
        {"pubkey": PK, "usdc_fn": lambda: 1000, "withdraw_rpc_url": "https://custody-rpc",
         "withdraw_call_fn": call, "withdraw_send_factory_fn": factory,
         "confirm_once_fn": lambda sig: confirmed.append(sig) or "pending"},
        state="queued", keypair=KP, rpc_url="https://public-fallback", to_address=TO,
        now_fn=lambda: T0, connection_fn=Connection, log_fn=lambda _: None)
    assert calls == ["withdraw", "closed"]
    assert confirmed == ["SIG"]


def test_worker_pins_expiry_proof_to_the_funded_withdrawal_rpc(monkeypatch):
    from sniper.execution import solana_real_seams as seams
    c = _conn()
    iid = m.enqueue_withdraw(c, "solana", TO, 10, now_iso=T0)
    m.resolve_withdraw(c, iid, "submitted", now_iso=T0, sig="SIG", last_valid_height=500)
    calls, final = [], []

    class Connection:
        def __getattr__(self, name): return getattr(c, name)
        def close(self):
            final.append(_row(c)["state"])
            c.close()

    class InlineThread:
        def __init__(self, *, target, **kw): self.target = target
        def is_alive(self): return False
        def start(self): self.target()

    monkeypatch.setattr(wd.threading, "Thread", InlineThread)
    monkeypatch.setattr(wd, "_WORKER", None)
    monkeypatch.setattr(wd, "_alert", lambda _: None)
    monkeypatch.setattr(seams, "rpc_confirm", lambda sig, url, **kw:
                        calls.append((sig, url, kw)) or "not_found")
    assert wd.start_pass(
        {"confirm_once_fn": lambda _: "not_found", "block_height_fn": lambda: 501,
         "withdraw_rpc_url": "https://custody-rpc"},
        state="submitted", keypair=None, rpc_url="https://public-fallback", to_address=TO,
        now_fn=lambda: T1, connection_fn=Connection, log_fn=lambda _: None)
    assert calls == [("SIG", "https://custody-rpc", {"max_attempts": 1, "poll_s": 0})]
    assert final == ["failed"]


def test_api_replay_returns_original_terminal_state(monkeypatch):
    import uuid
    cl, c = _client(monkeypatch)
    csrf = cl.get('/api/memecoin/withdraw').get_json()['csrf_token']
    headers = {'X-CSRF-Token': csrf}
    payload = {'amount_usdc': 10, 'request_id': str(uuid.uuid4())}
    first = cl.post('/api/memecoin/withdraw', json=payload, headers=headers).get_json()
    assert first['intent_id'] == payload['request_id']
    m.resolve_withdraw(c, first['intent_id'], 'sent', now_iso=T1)
    replay = cl.post('/api/memecoin/withdraw', json=payload, headers=headers)
    assert replay.status_code == 202 and replay.get_json()['state'] == 'sent'
    payload['amount_usdc'] = 20
    assert cl.post('/api/memecoin/withdraw', json=payload, headers=headers).status_code == 409
    payload['request_id'] = 'bad-id'
    assert cl.post('/api/memecoin/withdraw', json=payload, headers=headers).status_code == 400


def test_persisted_withdrawal_fences_execution_after_restart(tmp_path):
    from sniper.execution.exec_lock import serialize_executor
    path = str(tmp_path / 'restart.db')
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    iid = m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    m.resolve_withdraw(c, iid, 'submitted', now_iso=T0, sig='SIG', last_valid_height=500)
    c.close()
    c = sqlite3.connect(path)
    calls = []
    fn = serialize_executor(lambda *a: calls.append(a) or {'status': 'filled'},
                            'sol-' + PK, lock_dir=tmp_path, withdrawal_action='exit')
    assert fn(c, {'id': 1}) == {'status': 'deferred', 'reason': 'withdrawal_unresolved'}
    assert calls == []
    other = serialize_executor(lambda *a: {'status': 'filled'}, 'evm-4663-wallet', lock_dir=tmp_path)
    assert other(c)['status'] == 'filled'
    m.resolve_withdraw(c, iid, 'sent', now_iso=T1)
    assert fn(c, {'id': 1})['status'] == 'filled'
    c.close()


def test_withdrawal_worker_returns_while_receipt_is_pending(tmp_path, monkeypatch):
    import threading
    entered, finish = threading.Event(), threading.Event()
    class Connection:
        def close(self):
            pass
    monkeypatch.setattr(wd, 'withdraw_once', lambda *a, **kw: (
        entered.set(), finish.wait(3), {'state': 'unresolved'})[-1])
    assert wd.start_pass({'pubkey': PK, 'usdc_fn': lambda: 100}, state='queued',
                         keypair=KP, rpc_url='rpc', to_address=TO, now_fn=lambda: T0,
                         connection_fn=Connection, log_fn=lambda text: None)
    try:
        assert entered.wait(1)
        assert not wd.start_pass({}, state='queued', keypair=KP, rpc_url='rpc',
                                 to_address=TO, now_fn=lambda: T0, connection_fn=Connection)
    finally:
        finish.set()
        wd._WORKER.join(3)


def test_concurrent_different_requests_admit_only_one(tmp_path):
    import concurrent.futures
    import threading
    import uuid
    path = str(tmp_path / 'different.db')
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.close()
    barrier = threading.Barrier(2)
    def request():
        db = sqlite3.connect(path, timeout=5)
        try:
            barrier.wait()
            try:
                return m.enqueue_withdraw(db, 'solana', TO, 10, now_iso=T0,
                                          request_id=str(uuid.uuid4()))
            except ValueError:
                return None
        finally:
            db.close()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        assert sum(f.result() is not None for f in futures) == 1


@pytest.mark.parametrize('signed_field', [None, 'tx_sig', 'submission_backend',
                                         'signed_payload_sha256', 'cleanup_status'])
@pytest.mark.parametrize('keyword_call', [False, True])
def test_withdrawal_fence_releases_only_unsigned_entries(tmp_path, signed_field, keyword_call):
    from sniper.execution.exec_lock import serialize_executor
    c = _conn()
    iid = m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    m.resolve_withdraw(c, iid, 'submitted', now_iso=T0, sig='WITHDRAW')
    pid = m.reserve_pending(c, mint='TOKEN', source_user_id='WALLET', strategy='test',
                            notional=10, reserved_at=T0, network_id='solana')
    if signed_field:
        c.execute(f'UPDATE memecoin_pending_entries SET {signed_field}=? WHERE id=?',
                  ('bound', pid))
        c.commit()
    before = m.pending_row(c, pid)
    fn = serialize_executor(lambda *a, **kw: pytest.fail('must not execute'), 'sol-' + PK,
                            lock_dir=tmp_path, withdrawal_action='entry')
    result = fn(conn=c, pid=pid) if keyword_call else fn(c, pid)
    assert result['reason'] == 'withdrawal_unresolved'
    after = m.pending_row(c, pid)
    if signed_field:
        assert after == before
    else:
        assert after['status'] == 'failed'
        assert after['resolution_reason'] == 'withdrawal_unresolved'


def test_wallet_lock_contention_keeps_request_queued(tmp_path):
    from sniper.execution.exec_lock import wallet_exec_lock
    c = _conn()
    m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    with wallet_exec_lock('sol-' + PK, lock_dir=tmp_path):
        result, events = _send(c, tmp_path)
    assert result['state'] == 'wallet_busy'
    assert _row(c)['state'] == 'queued' and events == []


def test_claim_alert_emits_once_across_unknown_recovery(tmp_path):
    c = _conn()
    m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    events = []
    _send(c, tmp_path, confirm='pending', events=events)
    _send(c, tmp_path, confirm='pending', events=events)
    assert len([e for e in events if e[0] == 'alert' and 'withdrawal claimed' in e[1]]) == 1


def test_stale_cleanup_preserves_signed_claim_and_other_chain():
    c = _conn()
    sol = m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0)
    other = m.enqueue_withdraw(c, '4663', TO, 10, now_iso=T0)
    m.claim_withdraw(c, 'solana', now_iso=T0)
    m.claim_withdraw(c, '4663', now_iso=T0)
    c.execute('UPDATE memecoin_withdraw_intents SET sig=? WHERE intent_id=?', ('SIG', sol))
    c.commit()
    m.recover_withdraw_claims(c, 'solana', now_iso='2026-09-14T13:00:00+00:00')
    assert m.withdraw_intent(c, sol)['state'] == 'claimed'
    assert m.withdraw_intent(c, other)['state'] == 'claimed'


def test_request_identity_rejects_changed_destination():
    import uuid
    c = _conn()
    rid = str(uuid.uuid4())
    m.enqueue_withdraw(c, 'solana', TO, 10, now_iso=T0, request_id=rid)
    with pytest.raises(ValueError, match='different'):
        m.enqueue_withdraw(c, 'solana', str(Keypair().pubkey()), 10, now_iso=T1, request_id=rid)


def test_withdrawal_alert_uses_shared_queue(monkeypatch):
    from sniper.services import memecoin_telegram as telegram
    queued = []
    monkeypatch.setattr(telegram, 'enqueue_telegram', lambda text, **kw: queued.append(text))
    monkeypatch.setattr(telegram, 'send_telegram', lambda *a, **kw: pytest.fail('synchronous send'))
    wd._alert('withdrawal claimed')
    assert queued == ['withdrawal claimed']
