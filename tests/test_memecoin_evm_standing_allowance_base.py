"""Base (8453) standing WETH allowance (operator yes 2026-10-15 via Alpha: "yes allow base standing
approval"; same terms as 56: $500 cap, never MAX). Permit2 is pinned first: it is the WETH spender
of every Base V4/Universal Router entry (7 of 12 real Base fills since 09-15; the UR leg uses an
in-transaction PERMIT2_PERMIT signature, so the router itself is never an ERC-20 spender). Proven on
chain 2026-10-15 by scripts/research/prove_standing_spender_8453.py: 9,152 bytes of code and
DOMAIN_SEPARATOR() equal to Permit2's own EIP-712 domain for chainId 8453.

Base is receipt-first (no approval pipeline) but shares the nonce floor through
MEMECOIN_EVM_PREAPPROVE_CHAINS. The standing book keys on that floor, and the top-up sends without
waiting and confirms outside the wallet lock, so a Base top-up never holds an entry behind its
receipt. e44fdf62 (2026-10-13) paid approval 2,258 ms + post-approval re-quote 734 ms."""
import json
import sqlite3

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

import sniper.execution.evm_real_seams as S
from sniper.execution import evm_standing_allowance as sa
from sniper.execution.evm_wallet import CHAIN_QUOTE

CHAIN = "8453"
WETH = CHAIN_QUOTE[CHAIN]["token"].lower()
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
E18 = 10 ** 18


@pytest.fixture(autouse=True)
def _clean():
    sa.reset_state()
    yield
    sa.reset_state()


def test_permit2_is_the_only_base_pin_and_its_proof_matches_the_record():
    assert sa.PINNED_SPENDERS[CHAIN] == frozenset({PERMIT2})
    type_hash = keccak(text="EIP712Domain(string name,uint256 chainId,address verifyingContract)")
    domain = "0x" + keccak(encode(["bytes32", "bytes32", "uint256", "address"],
                                  [type_hash, keccak(text="Permit2"), 8453,
                                   to_checksum_address(PERMIT2)])).hex().removeprefix("0x")
    assert domain == sa.PIN_PROOFS[(CHAIN, PERMIT2)]["domain_separator"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 2400.0)
    monkeypatch.delenv("MEMECOIN_EVM_APPROVAL_PIPELINE", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", CHAIN)          # the shared nonce floor
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


def test_a_covered_base_entry_skips_the_approve(env):
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)
    used = []
    assert ex["standing_approve_fn"](PERMIT2, E18 // 100, 2400.0, used) == {"standing": True}
    assert seen["sends"] == [] and len(used) == 1 and sa.value(k) == E18 - E18 // 100


@pytest.mark.parametrize("spender", ["0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24", "0x" + "77" * 20])
def test_an_unpinned_base_spender_keeps_todays_confirmed_approve(env, spender):
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct, spender)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)          # even a full book cannot authorize it
    assert ex["standing_approve_fn"](spender, E18 // 100, 2400.0, []) is True
    assert len(seen["sends"]) == 1


def test_without_the_shared_nonce_floor_base_keeps_todays_approve(env):
    env.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "4663")
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)
    assert ex["standing_approve_fn"](PERMIT2, E18 // 100, 2400.0, []) is True
    assert len(seen["sends"]) == 1 and sa.value(k) == E18


def test_a_base_topup_confirms_outside_the_wallet_lock(env):
    from sniper.execution.exec_lock import evm_exec_lock_key, wallet_exec_lock
    held = []
    acct_box = {}

    def on_receipt():
        with wallet_exec_lock(evm_exec_lock_key(CHAIN, acct_box["a"].address), blocking=False) as got:
            held.append(bool(got))
    http, seen = _http(on_receipt=on_receipt)
    ex, acct = _ex(http)
    acct_box["a"] = acct
    k = _key(acct)
    sa.reserve(k, E18 // 100, token_usd=2400.0)          # learns clip and price; falls back
    assert ex["standing_topup_fn"](PERMIT2) == "confirmed"
    assert len(seen["sends"]) == 1 and sa.value(k) == sa.cap_atomic(500, 2400.0, 18)
    assert held and all(held), "an entry could take the wallet while the top-up waited"


def test_the_revoke_script_takes_base_and_stays_dry_run_without_confirm():
    from pathlib import Path
    text = (Path(__file__).parent.parent / "scripts" / "memecoin_evm_revoke_standing_allowance.py").read_text()
    assert '"--chain"' in text and '"8453"' in text and "if not args.confirm" in text
    assert "PINNED_SPENDERS[chain]" in text and 'CHAIN_QUOTE[chain]["token"]' in text
