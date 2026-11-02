"""Item 13 — custody: keypair gen/load (chmod 600, never in git/DB/logs) + JSON-RPC balance
reads (SOL via getBalance, SPL token via getTokenAccountsByOwner). RPC calls use injected http so
the money path is unit-tested without a live endpoint."""
import os
import stat
from sniper.execution import solana_wallet as w


def test_keypair_gen_load_roundtrip(tmp_path):
    p = str(tmp_path / "kp.json")
    w.generate_keypair_file(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600      # secrets are 0600
    kp = w.load_keypair(p)
    assert w.pubkey_str(kp) == w.pubkey_str(w.load_keypair(p))   # stable, loadable
    assert len(w.pubkey_str(kp)) >= 32                    # base58 pubkey string


def test_sol_balance_from_get_balance():
    def http(method, url, headers=None, body=None, timeout=10):
        assert '"getBalance"' in body
        return 200, {"result": {"value": 2_000_000_000}}, None   # 2 SOL in lamports
    assert abs(w.sol_balance("PUB", "http://rpc", http=http) - 2.0) < 1e-9


def test_typed_sol_balance_distinguishes_zero_from_failure():
    zero = w.sol_balance_read(
        "PUB", "http://rpc",
        http=lambda *_args, **_kwargs: (200, {"result": {"value": 0}}, None),
    )
    failed = w.sol_balance_read(
        "PUB", "http://rpc",
        http=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    assert zero.ok is True and zero.balance == 0.0 and zero.failure is None
    assert failed.ok is False and failed.balance is None and failed.failure == "rpc_error"


def test_typed_sol_balance_rejects_json_rpc_error_and_malformed_result():
    rpc_error = w.sol_balance_read(
        "PUB", "http://rpc",
        http=lambda *_args, **_kwargs: (
            200, {"error": {"code": -32000, "message": "failed"}}, None),
    )
    malformed = w.sol_balance_read(
        "PUB", "http://rpc",
        http=lambda *_args, **_kwargs: (200, {"result": {}}, None),
    )

    assert rpc_error.failure == "json_rpc_error" and rpc_error.balance is None
    assert malformed.failure == "malformed_response" and malformed.balance is None


def test_token_balance_from_token_accounts():
    def http(method, url, headers=None, body=None, timeout=10):
        assert '"getTokenAccountsByOwner"' in body
        return 200, {"result": {"value": [
            {"pubkey": "account", "account": {"owner": w._TOKEN_PROGRAM,
             "data": {"parsed": {"type": "account", "info": {"owner": "PUB", "mint": "USDC_MINT", "tokenAmount": {
                "amount": "42500000", "uiAmount": 42.5, "decimals": 6,
            }}}}}}
        ]}}, None
    assert abs(w.token_balance("PUB", "USDC_MINT", "http://rpc", http=http) - 42.5) < 1e-9


def test_token_balance_with_decimals_reuses_the_same_rpc_response():
    calls = {"n": 0}
    def http(method, url, headers=None, body=None, timeout=10):
        calls["n"] += 1
        return 200, {"result": {"value": [
                {"pubkey": "account", "account": {"owner": w._TOKEN_PROGRAM,
                 "data": {"parsed": {"type": "account", "info": {"owner": "PUB", "mint": "MINT", "tokenAmount": {
                    "amount": "42500000", "uiAmount": 42.5, "decimals": 6,
            }}}}}}
        ]}}, None
    assert w.token_balance_with_decimals("PUB", "MINT", "http://rpc", http=http) == (42.5, 6)
    assert calls["n"] == 1


def test_token_balance_with_decimals_uses_atomic_amount_when_ui_amount_is_null():
    atomic = 9_007_199_254_740_993_000
    def http(method, url, headers=None, body=None, timeout=10):
        return 200, {"result": {"value": [
            {"pubkey": "account", "account": {"owner": w._TOKEN_PROGRAM,
             "data": {"parsed": {"type": "account", "info": {"owner": "PUB", "mint": "MINT", "tokenAmount": {
                "amount": str(atomic), "uiAmount": None, "decimals": 6,
            }}}}}}
        ]}}, None
    snapshot = w.token_balance_snapshot("PUB", "MINT", "http://rpc", http=http)
    assert snapshot.atomic == atomic
    assert snapshot.decimals == 6
    balance, decimals = snapshot.human, snapshot.decimals
    assert decimals == 6
    assert balance > 0


def test_token_balance_zero_when_no_account():
    def http(method, url, headers=None, body=None, timeout=10):
        return 200, {"result": {"value": []}}, None       # no token account -> 0, not error
    assert w.token_balance("PUB", "MINT", "http://rpc", http=http) == 0.0


def test_balance_reads_use_confirmed_commitment():
    """Executors confirm txs at 'confirmed'; balance reads at the default (finalized) lag behind
    and booked a real $5 fill as 'anomaly' (2026-07-10 smoke test). All reads must pin confirmed."""
    seen = []
    def http(method, url, headers=None, body=None, timeout=10):
        seen.append(body)
        if '"getBalance"' in body:
            return 200, {"result": {"value": 1}}, None
        return 200, {"result": {"value": []}}, None
    w.sol_balance("PUB", "http://rpc", http=http)
    w.token_balance("PUB", "MINT", "http://rpc", http=http)
    w.token_balance_raw("PUB", "MINT", "http://rpc", http=http)
    assert len(seen) == 3
    assert all('"commitment": "confirmed"' in b for b in seen)
