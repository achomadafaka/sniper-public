"""The operator's USDC withdrawal script builds exactly one TransferChecked to the right account."""
import importlib.util
import pathlib

import pytest
from solders.keypair import Keypair

_spec = importlib.util.spec_from_file_location(
    "withdraw", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_withdraw_solana_usdc.py")
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)


@pytest.mark.parametrize("height", [None, 0, -1, True, False, 500.0, "500"])
def test_transfer_refuses_invalid_expiry_before_signing(monkeypatch, height):
    from sniper.execution.solana_usdc_transfer import build_signed_transfer
    import solders.transaction
    sender = Keypair()
    signed = []
    monkeypatch.setattr(solders.transaction, "Transaction", lambda *a: signed.append(a))

    def rpc(url, method, params):
        if method == "getAccountInfo":
            return {"result": {"value": {}}}
        return {"result": {"value": {"blockhash": "11111111111111111111111111111111",
                                     "lastValidBlockHeight": height}}}

    with pytest.raises(RuntimeError, match="last valid block height"):
        build_signed_transfer(sender, str(sender.pubkey()), str(Keypair().pubkey()), 1,
                              rpc_url="https://rpc", call_fn=rpc)
    assert signed == []


@pytest.mark.parametrize("response", [
    None, [], {}, {"error": {"code": -32005}}, {"result": None}, {"result": []},
    {"result": {}}, {"result": {"value": False}}, {"result": {"value": []}},
    {"result": {"value": "unavailable"}},
    {"result": {"value": None}, "error": {"code": -32005}},
])
def test_recipient_account_unknown_never_becomes_missing_or_signs(monkeypatch, response):
    from sniper.execution.solana_usdc_transfer import build_signed_transfer
    import solders.transaction
    sender = Keypair()
    signed, calls = [], []
    monkeypatch.setattr(solders.transaction, "Transaction", lambda *a: signed.append(a))

    def rpc(url, method, params):
        calls.append(method)
        return response

    with pytest.raises(RuntimeError, match="recipient's USDC account"):
        build_signed_transfer(sender, str(sender.pubkey()), str(Keypair().pubkey()), 1,
                              rpc_url="https://rpc", call_fn=rpc)
    assert signed == [] and calls == ["getAccountInfo"]


@pytest.mark.parametrize("create", [False, True])
def test_withdraw_priority_is_bounded_and_precedes_account_and_transfer(create):
    sender, recipient = str(Keypair().pubkey()), str(Keypair().pubkey())
    ixs = w.withdraw_instructions(sender, recipient, 300_000_000,
                                  create_recipient_account=create)
    limit, price = ixs[:2]
    assert str(limit.program_id) == "ComputeBudget111111111111111111111111111111"
    assert str(price.program_id) == str(limit.program_id)
    assert bytes(limit.data) == bytes([2]) + (50_000).to_bytes(4, "little")
    assert bytes(price.data) == bytes([3]) + (2_000_000).to_bytes(8, "little")
    units = int.from_bytes(bytes(limit.data)[1:], "little")
    micro_lamports = int.from_bytes(bytes(price.data)[1:], "little")
    assert units * micro_lamports // 1_000_000 == 100_000
    assert len(ixs) == 3 + int(create)
    assert str(ixs[-1].program_id) == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    assert bytes(ixs[-1].data) == bytes([12]) + (300_000_000).to_bytes(8, "little") + bytes([6])


@pytest.mark.parametrize("account,height,reason", [
    ({"result": {}}, 500, "recipient's USDC account"),
    ({"result": {"value": {}}}, True, "last valid block height"),
])
def test_cli_reuses_strict_recipient_and_expiry_reads_before_signing(
        tmp_path, monkeypatch, account, height, reason):
    from types import SimpleNamespace
    import solders.transaction
    from sniper.execution import solana_wallet
    from sniper.services import memecoin_rent_sweep
    kp, to = Keypair(), str(Keypair().pubkey())
    monkeypatch.setattr(w.sys, "argv", ["withdraw", "--to", to, "--amount", "10",
                                      "--state-root", str(tmp_path)])
    monkeypatch.setattr(w, "load_env_files", lambda *a: None)
    monkeypatch.setattr(w, "open_book", lambda *a: (0, 0))
    monkeypatch.setattr(solana_wallet, "load_keypair", lambda *a: kp)
    monkeypatch.setattr(solana_wallet, "sol_balance_read", lambda *a: SimpleNamespace(ok=True))
    monkeypatch.setattr(solana_wallet, "native_sol_balance_value", lambda *a: 0.1)
    monkeypatch.setattr(solana_wallet, "token_balance_checked", lambda *a: 100)
    signed = []
    monkeypatch.setattr(solders.transaction, "Transaction", lambda *a: signed.append(a))

    def rpc(url, method, params):
        if method == "getAccountInfo":
            return account
        if method == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": "11111111111111111111111111111111",
                                         "lastValidBlockHeight": height}}}
        pytest.fail("an invalid read must not reach simulation or send")

    monkeypatch.setattr(memecoin_rent_sweep, "rpc_call", rpc)
    with pytest.raises(SystemExit, match=reason):
        w.main()
    assert signed == []


@pytest.mark.parametrize("confirm", [False, True])
def test_cli_simulation_and_preflight_match_the_confirmed_blockhash(
        tmp_path, monkeypatch, confirm):
    import sqlite3
    from types import SimpleNamespace
    from sniper.execution import solana_real_seams, solana_wallet
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_rent_sweep
    kp, to = Keypair(), str(Keypair().pubkey())
    path = str(tmp_path / "sniper_tracker.db")
    conn = sqlite3.connect(path)
    m.ensure_memecoin_tables(conn)
    conn.close()
    argv = ["withdraw", "--to", to, "--amount", "10", "--state-root", str(tmp_path)]
    monkeypatch.setattr(w.sys, "argv", argv + (["--confirm"] if confirm else []))
    monkeypatch.setattr(w, "load_env_files", lambda *a: None)
    monkeypatch.setattr(w, "open_book", lambda *a: (0, 0))
    monkeypatch.setattr(solana_wallet, "load_keypair", lambda *a: kp)
    monkeypatch.setattr(solana_wallet, "sol_balance_read", lambda *a: SimpleNamespace(ok=True))
    monkeypatch.setattr(solana_wallet, "native_sol_balance_value", lambda *a: 0.1)
    monkeypatch.setattr(solana_wallet, "token_balance_checked", lambda *a: 100)
    monkeypatch.setattr(w, "build_signed_transfer", lambda *a, **kw: ("BYTES", "SIG", 500, False))
    monkeypatch.setattr(solana_real_seams, "rpc_confirm", lambda *a, **kw: "confirmed")
    calls = []

    def rpc(url, method, params):
        calls.append((method, params))
        if method == "simulateTransaction":
            return {"result": {"value": {"err": None}}}
        if method == "sendTransaction":
            return {"result": "SIG"}
        pytest.fail("unexpected RPC method: " + method)

    monkeypatch.setattr(memecoin_rent_sweep, "rpc_call", rpc)
    w.main()
    assert calls[0] == ("simulateTransaction", ["BYTES", {
        "encoding": "base64", "commitment": "confirmed"}])
    if confirm:
        assert calls[1] == ("sendTransaction", ["BYTES", {
            "encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}])
    else:
        assert len(calls) == 1
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memecoin_withdraw_intents").fetchone()[0] == int(confirm)
    finally:
        conn.close()


def test_transfer_blockhash_uses_the_send_preflight_commitment():
    from sniper.execution.solana_usdc_transfer import build_signed_transfer
    sender = Keypair()
    calls = []
    def rpc(url, method, params):
        calls.append((method, params))
        if method == 'getAccountInfo': return {'result': {'value': {}}}
        return {'result': {'value': {'blockhash': '11111111111111111111111111111111',
                                     'lastValidBlockHeight': 500}}}
    build_signed_transfer(sender, str(sender.pubkey()), str(Keypair().pubkey()), 1,
                          rpc_url='https://rpc', call_fn=rpc)
    assert calls[-1] == ('getLatestBlockhash', [{'commitment': 'confirmed'}])


def test_transfer_checked_moves_the_exact_amount_between_the_two_usdc_accounts():
    sender, recipient = str(Keypair().pubkey()), str(Keypair().pubkey())
    ixs = w.withdraw_instructions(sender, recipient, 300_000_000, create_recipient_account=False)
    assert len(ixs) == 3
    ix = ixs[-1]
    assert bytes(ix.data) == bytes([12]) + (300_000_000).to_bytes(8, "little") + bytes([6])
    src, mint, dest, owner = ix.accounts
    assert str(src.pubkey) == str(w.associated_token_account(sender)) and src.is_writable
    assert str(dest.pubkey) == str(w.associated_token_account(recipient)) and dest.is_writable
    assert str(mint.pubkey) == w.USDC_MINT and str(owner.pubkey) == sender and owner.is_signer


def test_a_missing_recipient_account_is_created_first_and_paid_by_us():
    sender, recipient = str(Keypair().pubkey()), str(Keypair().pubkey())
    _limit, _price, create, _transfer = w.withdraw_instructions(
        sender, recipient, 1, create_recipient_account=True)
    assert str(create.program_id) == "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL" and bytes(create.data) == bytes([1])
    assert str(create.accounts[0].pubkey) == sender and create.accounts[0].is_signer
    assert str(create.accounts[2].pubkey) == recipient


def test_only_a_wallet_address_is_accepted():
    wallet = str(Keypair().pubkey())
    assert w.valid_wallet(wallet)
    assert not w.valid_wallet(str(w.associated_token_account(wallet)))   # a token account
    assert not w.valid_wallet("not-an-address")


def test_cli_transfer_persists_identity_before_send_and_preserves_unknown():
    import sqlite3
    import uuid
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    request_id = str(uuid.uuid4())
    to = str(Keypair().pubkey())
    now = '2026-09-16T12:00:00+00:00'
    sent = []
    def send(signed, rpc):
        row = dict(conn.execute('SELECT * FROM memecoin_withdraw_intents').fetchone())
        assert row['state'] == 'submitted' and row['sig'] == 'SIG'
        assert row['last_valid_height'] == 500
        sent.append(signed)
        raise TimeoutError('send response lost')
    result = w.submit_transfer(conn, to=to, amount=10, request_id=request_id, now_iso=now,
                               signed_b64='BYTES', sig='SIG', last_valid=500, rpc='rpc',
                               send_fn=send, confirm_fn=lambda sig, rpc: 'pending')
    assert result['state'] == 'submitted'
    result = w.submit_transfer(conn, to=to, amount=10, request_id=request_id, now_iso=now,
                               signed_b64='NEW_BYTES', sig='NEW_SIG', last_valid=600, rpc='rpc',
                               send_fn=send, confirm_fn=lambda sig, rpc: 'confirmed')
    assert sent == ['BYTES'] and result['sig'] == 'SIG'


def test_cli_terminal_request_replay_does_not_send():
    import sqlite3
    import uuid
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    now = '2026-09-16T12:00:00+00:00'
    to, rid = str(Keypair().pubkey()), str(uuid.uuid4())
    calls = []
    kwargs = dict(to=to, amount=10, request_id=rid, now_iso=now, signed_b64='BYTES',
                  sig='SIG', last_valid=500, rpc='rpc', send_fn=lambda *a: calls.append(a),
                  confirm_fn=lambda *a: 'confirmed')
    assert w.submit_transfer(conn, **kwargs)['state'] == 'sent'
    assert w.submit_transfer(conn, **kwargs)['state'] == 'sent'
    assert len(calls) == 1


def test_main_replays_sent_request_before_balance_or_rpc(tmp_path, monkeypatch, capsys):
    import sqlite3
    import uuid
    from types import SimpleNamespace
    from sniper.execution import solana_wallet
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_rent_sweep
    kp, to, rid = Keypair(), str(Keypair().pubkey()), str(uuid.uuid4())
    conn = sqlite3.connect(str(tmp_path / 'sniper_tracker.db'))
    m.ensure_memecoin_tables(conn)
    m.enqueue_withdraw(conn, 'solana', to, 300, now_iso='2026-09-16T12:00:00+00:00', request_id=rid)
    m.resolve_withdraw(conn, rid, 'sent', now_iso='2026-09-16T12:00:01+00:00', sig='ORIGINAL_SIG')
    conn.close()
    monkeypatch.setattr(w.sys, 'argv', ['withdraw', '--to', to, '--amount', '300', '--confirm',
                                      '--request-id', rid, '--state-root', str(tmp_path)])
    monkeypatch.setattr(w, 'load_env_files', lambda *a: None)
    monkeypatch.setattr(solana_wallet, 'load_keypair', lambda *a: kp)
    calls = []
    monkeypatch.setattr(solana_wallet, 'sol_balance_read',
                        lambda *a: calls.append('sol') or SimpleNamespace(ok=True))
    monkeypatch.setattr(solana_wallet, 'native_sol_balance_value', lambda *a: 0.1)
    monkeypatch.setattr(solana_wallet, 'token_balance_checked',
                        lambda *a: calls.append('usdc') or 50)
    def unexpected(*a, **kw):
        raise AssertionError('replay must not read the book, build, or call RPC')
    monkeypatch.setattr(w, 'withdraw_instructions', unexpected)
    monkeypatch.setattr(memecoin_rent_sweep, 'rpc_call', unexpected)
    w.main()
    assert calls == []
    output = capsys.readouterr().out
    assert 'sent' in output and 'https://solscan.io/tx/ORIGINAL_SIG' in output
    monkeypatch.setattr(w, 'open_book', unexpected)
    for replacement in [('300', '301'), (to, str(Keypair().pubkey()))]:
        argv = ['withdraw', '--to', to, '--amount', '300', '--confirm',
                '--request-id', rid, '--state-root', str(tmp_path)]
        argv[argv.index(replacement[0])] = replacement[1]
        monkeypatch.setattr(w.sys, 'argv', argv)
        import pytest
        with pytest.raises(SystemExit, match='different withdrawal payload'):
            w.main()
    assert calls == []
