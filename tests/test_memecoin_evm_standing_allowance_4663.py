"""Robinhood chain (4663) standing WETH allowance on Permit2 (operator "3 yes sure" 2026-10-21 via
Alpha, brain 205ef146; same terms as 56/8453: $500 cap, never MAX). 6 of the 10 4663 entries since
10-18 that paid an exact approve (176-277 ms blocking each) were V4 in-swap-unwrap buys pulling WETH
through Permit2. Proven on chain 2026-10-21 by scripts/research/prove_standing_spender_8453.py <dRPC
research url> 4663: 9,152 bytes of code and DOMAIN_SEPARATOR() equal to Permit2's own EIP-712
domain for chainId 4663 at the canonical address. Kyber and the Pons router keep the exact approve."""
import json
import sqlite3

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

import sniper.execution.evm_real_seams as S
from sniper.execution import evm_standing_allowance as sa
from sniper.execution.evm_wallet import CHAIN_QUOTE

CHAIN = "4663"
WETH = CHAIN_QUOTE[CHAIN]["token"].lower()
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
CANONICAL = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
E18 = 10 ** 18


@pytest.fixture(autouse=True)
def _clean():
    sa.reset_state()
    yield
    sa.reset_state()


def test_the_4663_pin_is_canonical_permit2_proven_for_chain_4663_only():
    assert sa.PINNED_SPENDERS[CHAIN] == frozenset({PERMIT2})
    assert to_checksum_address(PERMIT2) == CANONICAL                    # byte-for-byte the canonical
    type_hash = keccak(text="EIP712Domain(string name,uint256 chainId,address verifyingContract)")
    domain = "0x" + keccak(encode(["bytes32", "bytes32", "uint256", "address"],
                                  [type_hash, keccak(text="Permit2"), 4663, CANONICAL])).hex().removeprefix("0x")
    proof = sa.PIN_PROOFS[(CHAIN, PERMIT2)]
    assert proof["domain_separator"] == domain and proof["code_bytes"] == 9152
    assert proof["domain_separator"] != sa.PIN_PROOFS[("8453", PERMIT2)]["domain_separator"]
    for lookalike in ("0x000000000022d473030f116ddee9f6b43ac78ba4", "0x00000000022d473030f116ddee9f6b43ac78ba3",
                      " " + PERMIT2):
        assert not sa.pinned(CHAIN, lookalike)
    assert sa.pinned(CHAIN, CANONICAL)                                   # case-insensitive match only


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 2400.0)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")            # 4663 is pipelined:
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", CHAIN)   # the shared nonce floor
    monkeypatch.setenv("MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS", CHAIN)
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_default_lock_dir", lambda: str(tmp_path))
    from sniper.persistence import db_memecoin as m
    db_path = tmp_path / "standing.sqlite"
    with sqlite3.connect(db_path) as conn:
        m.ensure_memecoin_tables(conn)
    monkeypatch.setenv("DB_PATH", str(db_path))
    return monkeypatch


def _http(on_receipt=None):
    seen = {"sends": [], "receipts": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_chainId":
            return 200, {"result": hex(int(CHAIN))}, None
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        if meth == "eth_sendRawTransaction":
            seen["sends"].append(req["params"][0])
            return 200, {"result": "0x" + "cd" * 32}, None
        if meth == "eth_getTransactionReceipt":
            seen["receipts"] += 1
            if on_receipt is not None:
                on_receipt()
            return 200, {"result": {"status": "0x1", "gasUsed": "0xb4a0",
                                    "effectiveGasPrice": "0x3b9aca00", "logs": []}}, None
        return 200, {"result": "0x0"}, None
    return http, seen


def _ex(http):
    acct = Account.create()
    return S.build_real_evm_executor(acct=acct, chain_id=CHAIN, rpc_url="https://rpc", http=http), acct


def _key(acct, spender=PERMIT2):
    return sa.book_key(CHAIN, acct.address, WETH, spender)


def test_a_covered_4663_entry_skips_the_approve(env):
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)
    used = []
    assert ex["standing_approve_fn"](PERMIT2, E18 // 100, 2400.0, used) == {"standing": True}
    assert seen["sends"] == [] and len(used) == 1 and sa.value(k) == E18 - E18 // 100


@pytest.mark.parametrize("book", ["unknown", "exhausted"])
def test_an_unknown_or_exhausted_book_falls_back_to_the_exact_approve(env, book):
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    if book == "exhausted":
        sa.topup_begin(k)
        sa.topup_end(k, "confirmed", cap_atomic=E18 // 1000)            # less than the clip
    result = ex["standing_approve_fn"](PERMIT2, E18 // 100, 2400.0, [])
    assert result is not None and result != {"standing": True}
    assert len(seen["sends"]) == 1                                       # today's exact approve


@pytest.mark.parametrize("spender", ["0x6131b5fae19ea4f9d964eac0408e4408b66337b5", "0x" + "77" * 20])
def test_kyber_and_any_unpinned_4663_spender_keep_the_exact_approve(env, spender):
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct, spender)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)
    assert ex["standing_approve_fn"](spender, E18 // 100, 2400.0, []) != {"standing": True}
    assert len(seen["sends"]) == 1


def test_4663_off_the_standing_chain_list_keeps_todays_approve(env):
    env.setenv("MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS", "56,8453")
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)
    assert ex["standing_approve_fn"](PERMIT2, E18 // 100, 2400.0, []) != {"standing": True}
    assert len(seen["sends"]) == 1 and sa.value(k) == E18


def test_the_revoke_script_takes_4663():
    from pathlib import Path
    text = (Path(__file__).parent.parent / "scripts" / "memecoin_evm_revoke_standing_allowance.py").read_text()
    assert '"4663"' in text and "if not args.confirm" in text
