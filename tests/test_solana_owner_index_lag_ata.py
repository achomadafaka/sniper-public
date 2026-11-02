"""Owner-index lag after our own buy (2026-10-23, pos 74149): getTokenAccountsByOwner answered [] for seconds,
the trail fired at -25% but the sell could not be sized until -43.6%. Exit reads fall back to our derived
ATAs read by address; the entry path never pays that extra read."""
import json

from solders.keypair import Keypair

from sniper.execution import solana_real_seams as S
from sniper.execution import solana_wallet as W
from sniper.settings import memecoin as cfg

OWNER = str(Keypair().pubkey())
MINT = "7UGNXBZ2Wr1uyHVXkg2CBTS87VSHrTEFgTfNAgPypump"


def _account(amount="1542769777573", program=W._TOKEN_PROGRAM, owner=OWNER, mint=MINT):
    return {"owner": program, "lamports": 2039280, "data": {"parsed": {"type": "account", "info": {
        "owner": owner, "mint": mint, "tokenAmount": {"amount": amount, "decimals": 6}}}}}


def _http(ata_values, calls):
    def http(method, url, headers=None, body=None):
        req = json.loads(body)
        calls.append(req["method"])
        if req["method"] == "getTokenAccountsByOwner":
            return 200, {"result": {"context": {"slot": 9}, "value": []}}, None
        if req["method"] == "getMultipleAccounts":
            return 200, {"result": {"context": {"slot": 9}, "value": ata_values}}, None
        raise AssertionError(req["method"])
    return http


def test_exit_read_sizes_the_bag_from_the_derived_ata_while_the_index_is_empty():
    calls = []
    snap = W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_http([_account(), None], calls), ata_fallback=True)
    assert (snap.atomic, snap.decimals) == (1542769777573, 6)
    assert calls == ["getTokenAccountsByOwner", "getMultipleAccounts"]


def test_token_2022_ata_counts_too():
    snap = W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_http([None, _account(program=W._TOKEN_2022_PROGRAM)], []),
                                    ata_fallback=True)
    assert snap.atomic == 1542769777573


def test_entry_path_never_reads_the_ata():
    calls = []
    snap = W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_http([_account(), None], calls))
    assert snap.atomic == 0 and snap.decimals is None and calls == ["getTokenAccountsByOwner"]


def test_no_ata_either_stays_unproved_and_a_foreign_account_is_refused():
    assert W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_http([None, None], []), ata_fallback=True).atomic == 0
    import pytest
    with pytest.raises(W.TokenBalanceReadError):
        W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_http([_account(owner=str(Keypair().pubkey())), None], []),
                                 ata_fallback=True)


def test_exit_preview_quotes_the_ata_balance(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_READ_RPC_URL", "")
    monkeypatch.setattr(S, "_sleep", lambda _s: None)
    asked = {}
    monkeypatch.setattr(S, "jupiter_exit_quote",
                        lambda mint, atomic, slip, http=None: asked.update(atomic=atomic) or {"outAmount": "18000000"})
    kp = Keypair()
    owner = str(kp.pubkey())
    calls = []

    def http(method, url, headers=None, body=None):
        req = json.loads(body)
        calls.append(req["method"])
        if req["method"] == "getTokenAccountsByOwner":
            return 200, {"result": {"value": []}}, None
        return 200, {"result": {"context": {"slot": 1}, "value": [_account(owner=owner), None]}}, None
    ex = S.build_real_solana_executor(keypair=kp, rpc_url="https://custody", http=http,
                                      usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)
    res = ex["preview_exit_fn"]({"id": 74149, "mint": MINT})
    assert res.failure is None and asked["atomic"] == 1542769777573
    assert res.provider_id == "solana:exact_quote"


def test_both_programs_present_fails_closed_and_a_wrong_program_owner_is_refused():
    import pytest
    with pytest.raises(W.TokenBalanceReadError, match="conflicting_token_program"):
        W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_http(
            [_account(), _account(program=W._TOKEN_2022_PROGRAM)], []), ata_fallback=True)
    with pytest.raises(W.TokenBalanceReadError, match="account_identity_or_amount"):
        W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_http(
            [_account(program=W._TOKEN_2022_PROGRAM), None], []), ata_fallback=True)


def test_stale_ata_context_is_refused():
    import pytest

    def http(method, url, headers=None, body=None):
        req = json.loads(body)
        if req["method"] == "getTokenAccountsByOwner":
            return 200, {"result": {"context": {"slot": 20}, "value": []}}, None
        return 200, {"result": {"context": {"slot": 5}, "value": [_account(), None]}}, None
    with pytest.raises(W.TokenBalanceReadError, match="stale_context"):
        W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=http, min_context_slot=10, ata_fallback=True)


def test_every_exit_read_opts_in_and_the_entry_snapshot_does_not():
    """AST pin over solana_real_seams: every token_balance_snapshot / token_balance_raw call opts in, except
    inside entry_token_snapshot (the pre-buy read, where an empty index is the normal answer and the entry
    path must not pay a second RPC). Named exit seams: preview, exit balance, exit quote, executor rung,
    partial exit, source-partial token_snapshot_fn, exported source_partial_snapshot_fn, source-partial proof."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(S))
    seen = {"entry": 0, "exit": 0}
    for top in tree.body:
        for node in ast.walk(top):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("token_balance_snapshot", "token_balance_raw")):
                opted = any(k.arg == "ata_fallback" and isinstance(k.value, ast.Constant) and k.value.value is True
                            for k in node.keywords)
                if isinstance(top, ast.FunctionDef) and top.name == "entry_token_snapshot":
                    assert not opted
                    seen["entry"] += 1
                else:
                    assert opted, "exit read without ata_fallback at line %d" % node.lineno
                    seen["exit"] += 1
    assert seen["entry"] == 1 and seen["exit"] == 8


def _ctx_http(index_slot, direct_context, accounts):
    def http(method, url, headers=None, body=None):
        req = json.loads(body)
        if req["method"] == "getTokenAccountsByOwner":
            return 200, {"result": {"context": {"slot": index_slot}, "value": []}}, None
        assert req["params"][1]["minContextSlot"] >= index_slot, "the direct read carries the index floor"
        return 200, {"result": {**({"context": direct_context} if direct_context is not None else {}),
                                "value": accounts}}, None
    return http


def test_snapshot_reports_the_direct_reads_own_slot():
    snap = W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_ctx_http(100, {"slot": 120}, [_account(), None]),
                                    ata_fallback=True)
    assert snap.context_slot == 120 and snap.atomic == 1542769777573


import pytest  # noqa: E402


@pytest.mark.parametrize("ctx,err", [(None, "malformed_response"), ({"slot": True}, "malformed_response"),
                                     ({"slot": -1}, "malformed_response"), ({"slot": "120"}, "malformed_response"),
                                     ({"slot": 99}, "stale_context")])
def test_a_missing_bool_negative_wrong_or_older_direct_context_is_refused(ctx, err):
    with pytest.raises(W.TokenBalanceReadError, match=err):
        W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_ctx_http(100, ctx, [_account(), None]), ata_fallback=True)


def test_a_frozen_direct_account_is_refused():
    frozen = _account()
    frozen["data"]["parsed"]["info"]["state"] = "frozen"
    with pytest.raises(W.TokenBalanceReadError, match="account_frozen"):
        W.token_balance_snapshot(OWNER, MINT, "https://rpc", http=_ctx_http(100, {"slot": 120}, [frozen, None]),
                                 ata_fallback=True)
