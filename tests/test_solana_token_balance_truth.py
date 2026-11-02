"""Failed owner reads cannot become a zero holding or an exhausted exit ladder."""
import copy

import pytest

from sniper.execution import solana_wallet as W
from sniper.execution import solana_real_seams as S
from sniper.execution import solana_exit_executor as E
from sniper.persistence import db_memecoin as M
import test_memecoin_exit_executor as T


def account(amount="9007199254740993000", decimals=6, key="account"):
    return {"pubkey": key, "account": {"owner": W._TOKEN_PROGRAM, "executable": False,
        "data": {"parsed": {"type": "account", "info": {"owner": "owner", "mint": "mint",
        "state": "initialized", "tokenAmount": {"amount": amount, "decimals": decimals}}}}}}


def response(rows=None):
    return {"result": {"context": {"slot": 123}, "value": [account()] if rows is None else rows}}


@pytest.mark.parametrize("payload", [None, {}, {"error": {"code": -32005}},
    {"result": None}, {"result": {}}, {"result": {"value": None}},
    {"result": {"value": False}}, {"result": {"value": {}}}])
def test_failed_snapshot_never_returns_zero(payload):
    with pytest.raises(RuntimeError, match="token balance unavailable"):
        W.token_balance_snapshot("owner", "mint", "rpc",
            http=lambda *a, **k: (200, payload, None))


@pytest.mark.parametrize("change", ["owner", "mint", "program", "duplicate", "missing_key",
    "negative", "float", "bool", "overflow", "decimal_string", "decimal_bool", "decimal_range"])
def test_snapshot_rejects_unproved_identity_and_units(change):
    rows = [account()]
    info = rows[0]["account"]["data"]["parsed"]["info"]
    if change in ("owner", "mint"):
        info[change] = "someone_else"
    elif change == "program":
        rows[0]["account"]["owner"] = "wrong_program"
    elif change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "missing_key":
        del rows[0]["pubkey"]
    elif change.startswith("decimal_"):
        info["tokenAmount"]["decimals"] = {"decimal_string": "6", "decimal_bool": True,
                                              "decimal_range": 256}[change]
    else:
        info["tokenAmount"]["amount"] = {"negative": "-1", "float": 1.0,
                                            "bool": True, "overflow": str(2**64)}[change]
    with pytest.raises(RuntimeError, match="token balance unavailable"):
        W.token_balance_snapshot("owner", "mint", "rpc",
            http=lambda *a, **k: (200, response(rows), None))


@pytest.mark.parametrize("reader", [W.token_balance_snapshot, W.token_balance_raw, W.token_balance])
def test_http_failure_cannot_supply_a_success_shaped_balance(reader):
    with pytest.raises(RuntimeError, match="token balance unavailable"):
        reader("owner", "mint", "rpc", http=lambda *a, **k: (429, response([]), None))


def test_snapshot_preserves_exact_atoms_and_known_empty_entry_balance():
    held = W.token_balance_snapshot("owner", "mint", "rpc",
        http=lambda *a, **k: (200, response(), None))
    empty = W.token_balance_snapshot("owner", "mint", "rpc",
        http=lambda *a, **k: (200, response([]), None))
    assert (held.atomic, held.decimals, held.context_slot) == (9007199254740993000, 6, 123)
    assert (empty.atomic, empty.decimals) == (0, None)


@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize("failure", ["raise", "empty", "malformed"])
def test_unknown_exit_balance_stays_open_without_stuck_or_quote(monkeypatch, overlap, failure):
    monkeypatch.setattr(E, "_overlap_reads_on", lambda: overlap)
    c, pos = T._c()
    d = T._Deps(["confirmed"])
    calls = []
    def snap(mint):
        calls.append(mint)
        if failure == "raise":
            raise RuntimeError("offline")
        if failure == "empty":
            return W.TokenBalanceSnapshot(0, None)
        return W.TokenBalanceSnapshot(-1, 6)
    out = E.execute_exit(c, pos, "mirror", "now", quote_fn=d.quote_fn, sign_fn=d.sign_fn,
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
        token_balance_fn=lambda mint: (_ for _ in ()).throw(RuntimeError("offline")),
        token_snapshot_fn=snap, slippage_ladder=[3, 10], sleep_fn=lambda _: None)
    assert out == {"status": "balance_unreadable", "reason": "token_balance_unreadable"}
    row = M.get_position(c, pos["id"])
    assert row["is_open"] and not row["stuck_bag"] and row["close_claim_id"] is None
    assert not d.calls
    assert len(calls) == (1 if overlap else 0)


@pytest.mark.parametrize("overlap", [False, True])
def test_unknown_signing_balance_releases_unsigned_claim(monkeypatch, overlap):
    monkeypatch.setattr(E, "_overlap_reads_on", lambda: overlap)
    c, pos = T._c()
    d = T._Deps(["confirmed"])
    calls = []
    def balance(mint):
        calls.append(mint)
        if len(calls) > 1:
            raise RuntimeError("offline")
        return W.TokenBalanceSnapshot(500_000_000, 6) if overlap else 500.
    def sign(quote, before_sign=None):
        assert before_sign is not None and not before_sign()
        return None
    out = E.execute_exit(c, pos, "mirror", "now", quote_fn=d.quote_fn,
        sign_fn=sign if overlap else d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_balance_fn=balance, token_snapshot_fn=balance,
        slippage_ladder=[3], sleep_fn=lambda _: None)
    assert out == {"status": "balance_unreadable", "reason": "token_balance_unreadable"}
    row = M.get_position(c, pos["id"])
    assert row["close_claim_id"] is None and row["close_sig"] is None and not row["stuck_bag"]
    assert "send" not in d.calls


def test_failed_entry_wallet_read_cannot_borrow_mint_decimals(monkeypatch):
    monkeypatch.setattr(S, "token_decimals", lambda *a, **k: 6)
    with pytest.raises(RuntimeError, match="token balance unavailable"):
        S.entry_token_snapshot("owner", "mint", "rpc",
            http=lambda *a, **k: (200, {"error": {"code": -32005}}, None))


@pytest.mark.parametrize("overlap", [False, True])
def test_fresh_funded_entry_empty_owner_index_is_unproved_even_without_overlap(monkeypatch, overlap):
    from solders.keypair import Keypair
    monkeypatch.setattr(E, "_overlap_reads_on", lambda: overlap)
    c, pos = T._c()
    calls = []
    def http(*args, **kwargs):
        calls.append(1)
        return 200, response([]), None
    executor = S.build_real_solana_executor(keypair=Keypair(), rpc_url="rpc", http=http,
                                           usdc_fn=lambda: 0.)
    out = executor["exit_fn"](c, pos, "mirror", "now")
    row = M.get_position(c, pos["id"])
    assert out == {"status": "balance_unreadable", "reason": "token_balance_unreadable"}
    assert row["is_open"] and not row["stuck_bag"] and row["close_claim_id"] is None
    assert calls == [1]


def test_raw_quote_balance_failure_is_not_an_exhausted_ladder():
    c, pos = T._c()
    d = T._Deps(["confirmed"])
    def quote(*args):
        raise W.TokenBalanceReadError("rpc_transport")
    out = E.execute_exit(c, pos, "mirror", "now", quote_fn=quote, sign_fn=d.sign_fn,
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
        token_balance_fn=d.token_fn, slippage_ladder=[3, 10], sleep_fn=lambda _: None)
    row = M.get_position(c, pos["id"])
    assert out == {"status": "balance_unreadable", "reason": "token_balance_unreadable"}
    assert row["close_claim_id"] is None and not row["stuck_bag"]


@pytest.mark.parametrize("payload", [{"result": {"context": None, "value": []}},
    {"result": {"context": {"slot": True}, "value": []}},
    response([account("10", 6, "first"), account("10", 9, "second")]),
    response([account(str(2**63), 6, "first"), account(str(2**63), 6, "second")])])
def test_partial_or_conflicting_response_is_unknown(payload):
    with pytest.raises(W.TokenBalanceReadError):
        W.token_balance_snapshot("owner", "mint", "rpc", http=lambda *a, **k: (200, payload, None))


@pytest.mark.parametrize("identity", ["unsigned", "signature", "bound_payload"])
def test_entry_balance_failure_releases_only_unsigned_reservation(monkeypatch, identity):
    from solders.keypair import Keypair
    from sniper.execution import solana_swap_executor as swap
    c, _ = T._c()
    pid = M.reserve_pending(c, mint="mint", source_user_id="u", strategy="alpha_b",
        notional=1., reserved_at="now", network_id="solana", signal_source="manual")
    if identity == "signature":
        M.set_pending_sig(c, pid, "possibly-broadcast", "now")
    elif identity == "bound_payload":
        assert M.bind_entry_submission(c, pid, tx_identity="possibly-broadcast",
            signed_payload_b64="signed-bytes", signed_payload_sha256="payload-hash",
            blockhash="blockhash", last_valid_height=123, nonce=None, backend="test", bound_at="now")
    before = M.entry_submission_binding(c, pid)
    monkeypatch.setattr(S, "token_decimals", lambda *a, **k: 6)
    def execute_entry(conn, **kwargs):
        kwargs["token_fn"]("mint")
        pytest.fail("unreadable entry balance was accepted")
    monkeypatch.setattr(swap, "execute_entry", execute_entry)
    executor = S.build_real_solana_executor(keypair=Keypair(), rpc_url="rpc", usdc_fn=lambda: 1.,
        http=lambda *a, **k: (429, {}, None))
    out = executor["execute_fn"](c, pid, "mint", 1., "now")
    row = M.pending_row(c, pid)
    assert out == {"status": "aborted" if identity == "unsigned" else "unknown",
                   "reason": "token_balance_unreadable"}
    assert row["status"] == ("failed" if identity == "unsigned" else "reserved")
    assert M.entry_submission_binding(c, pid) == before
    if identity == "unsigned":
        assert row["resolution_reason"] == "token_balance_unreadable"


def test_entry_boundary_does_not_mask_unrelated_errors(monkeypatch):
    from solders.keypair import Keypair
    from sniper.execution import solana_swap_executor as swap
    c, _ = T._c()
    pid = M.reserve_pending(c, mint="mint", source_user_id="u", strategy="alpha_b",
        notional=1., reserved_at="now", network_id="solana", signal_source="manual")
    def execute_entry(*args, **kwargs):
        raise ValueError("unrelated bug")
    monkeypatch.setattr(swap, "execute_entry", execute_entry)
    executor = S.build_real_solana_executor(keypair=Keypair(), rpc_url="rpc",
        http=lambda *a, **k: pytest.fail("unrelated entry made a provider call"))
    with pytest.raises(ValueError, match="unrelated bug"):
        executor["execute_fn"](c, pid, "mint", 1., "now")
    assert M.pending_row(c, pid)["status"] == "reserved"
