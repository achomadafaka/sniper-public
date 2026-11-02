# tests/test_relayer_code_basis.py
"""A pruned node answers `0x` for an old block, so the code check may fall back to head.

The 4663 fast lane sat dark from 2026-09-16: `read_relayer_purchase` pinned its `eth_getCode`
reads to the receipt block, the custody node keeps state for 1.7-16.8 minutes, and an older block
came back `0x` with no error. Four of twenty-four events failed there. No historical event on 4663
could ever pass.

Operator decision 2026-09-23, "no waiting": an empty read retries once at `latest`, and the
verification records WHICH block answered.

The distinction the tests below protect:

    empty at the receipt block   unknown  -> may re-ask at head   -> code_basis 'head'
    wrong hash at that block     evidence -> never re-asked       -> contract_code_mismatch
    empty at both                unknown  -> contract_code_unavailable

A head basis is exact for a LIVE event, because head and the receipt block are the same block or
seconds apart. For a BACKFILLED event it proves only that the contract is right NOW, so the report
label must say `verified_at_head` and never plain confirmation.
"""
import pytest

import sniper.services.axiom_address_verify as verify
import sniper.services.memecoin_evm_onchain_watch as watch

FIXTURE_RECEIPT = {"blockNumber": "0x430a1b0", "blockHash": "0xbh", "status": "0x1"}


def _codes(*, at_receipt, at_head):
    """A read_fn whose answer depends on the block tag, and which counts its calls."""
    calls = []

    def read_fn(method, params):
        calls.append((method, tuple(params)))
        if method == "eth_chainId":
            return "0x1237"
        if method == "eth_getCode":
            return at_head if params[1] == "latest" else at_receipt
        raise AssertionError("unexpected method %s" % method)

    read_fn.calls = calls
    return read_fn


def _run(monkeypatch, read_fn, *, proof=None, live=False):
    """Drive read_relayer_purchase past the decoder, so only the code check is under test."""
    monkeypatch.setattr(watch, "relayer_purchase_from_receipt",
                        lambda *a, **kw: dict(proof if proof is not None else {"mint": "0xm"}))
    monkeypatch.setattr(watch, "_integer", lambda v: 4663)
    evidence = {}
    got = watch.read_relayer_purchase(FIXTURE_RECEIPT, {"from": "0xr"}, "0xw",
                                      chain="4663", read_fn=read_fn, evidence=evidence, live=live)
    return got, evidence


# --- the code read ------------------------------------------------------------------------------

REAL_CODE = "0xfeed"


def _accept_real_code(monkeypatch):
    """Point all three expected code hashes at REAL_CODE, so only the BASIS decides the outcome.

    The hashes are re-derived here rather than hard-coded: a literal would pass even if the
    production check stopped hashing at all.
    """
    from eth_utils import keccak
    import sniper.execution.evm_relay_quote as relay
    digest = "0x" + keccak(bytes.fromhex(REAL_CODE[2:])).hex()
    monkeypatch.setattr(relay, "RELAY_APPROVAL_PROXY_CODE_HASH", digest, raising=False)
    monkeypatch.setattr(relay, "RELAY_ROUTER_CODE_HASH", digest, raising=False)
    monkeypatch.setattr(watch, "KYBER_CODE_HASH_4663", digest, raising=False)


def test_empty_at_the_receipt_block_retries_once_at_head(monkeypatch):
    """The whole point: a contract that exists today stops failing on an old block."""
    _accept_real_code(monkeypatch)
    read_fn = _codes(at_receipt="0x", at_head=REAL_CODE)
    got, evidence = _run(monkeypatch, read_fn)
    tags = [params[1] for method, params in read_fn.calls if method == "eth_getCode"]
    assert "latest" in tags, "an empty receipt-block read must be re-asked at head"
    assert got is not None, evidence
    assert got["code_basis"] == "head" and evidence["code_basis"] == "head"


def test_a_receipt_block_answer_never_asks_head_and_keeps_its_basis(monkeypatch):
    """Unchanged path. The node still holds the block, so nothing falls back."""
    _accept_real_code(monkeypatch)
    read_fn = _codes(at_receipt=REAL_CODE, at_head="0x")
    got, evidence = _run(monkeypatch, read_fn)
    assert got is not None, evidence
    assert got["code_basis"] == "receipt_block"
    assert all(params[1] != "latest" for method, params in read_fn.calls
               if method == "eth_getCode")


def test_one_empty_contract_makes_the_whole_proof_head(monkeypatch):
    """Three contracts, one basis. A single head read downgrades the whole proof, never the reverse."""
    _accept_real_code(monkeypatch)
    seen = []

    def read_fn(method, params):
        if method == "eth_chainId":
            return "0x1237"
        seen.append(params[1])
        if params[1] == "latest":
            return REAL_CODE
        return "0x" if len(seen) == 2 else REAL_CODE      # only the SECOND contract is pruned

    got, evidence = _run(monkeypatch, read_fn)
    assert got is not None and got["code_basis"] == "head"


def test_a_non_empty_wrong_hash_is_never_retried(monkeypatch):
    """A real answer at that block is evidence. Re-asking at head would hide a different contract."""
    read_fn = _codes(at_receipt="0xdeadbeef", at_head="0xfeed")
    got, evidence = _run(monkeypatch, read_fn)
    assert got is None
    assert evidence["reason"] == "contract_code_mismatch"
    assert all(params[1] != "latest" for method, params in read_fn.calls
               if method == "eth_getCode"), "a wrong hash must not fall back to head"


def test_empty_at_both_blocks_stays_unavailable(monkeypatch):
    """Unknown is not a pass. The node simply cannot answer."""
    read_fn = _codes(at_receipt="0x", at_head="0x")
    got, evidence = _run(monkeypatch, read_fn)
    assert got is None
    assert evidence["reason"] == "contract_code_unavailable"


def test_empty_code_reads_unknown_not_absent():
    assert watch._empty_code("0x") and watch._empty_code("") and watch._empty_code(None)
    assert not watch._empty_code("0xfeed")


# --- the basis, and the label it forces -----------------------------------------------------------

def test_basis_is_head_when_any_contract_needed_head():
    events = [{"purchase": {"code_basis": "receipt_block"}},
              {"purchase": {"code_basis": "head"}}]
    assert verify._code_basis(events) == "head"


def test_basis_is_receipt_block_when_every_read_answered_there():
    assert verify._code_basis([{"purchase": {"code_basis": "receipt_block"}}]) == "receipt_block"


def test_basis_is_none_when_no_event_carried_one():
    """Every non-4663 chain, and every 4663 route the decoder does not support."""
    assert verify._code_basis([]) is None
    assert verify._code_basis([{"purchase": None}, {"verdict": "no_data"}]) is None


def test_a_head_basis_confirmation_never_reads_as_plain_verified():
    assert verify._report_status(verify.CONFIRMED, "head") == verify.VERIFIED_AT_HEAD
    assert verify.VERIFIED_AT_HEAD != verify.CONFIRMED


def test_a_receipt_block_confirmation_is_unchanged():
    assert verify._report_status(verify.CONFIRMED, "receipt_block") == verify.CONFIRMED
    assert verify._report_status(verify.CONFIRMED, None) == verify.CONFIRMED


def test_a_head_basis_does_not_promote_a_weaker_verdict():
    """The label weakens a confirmation. It must never strengthen anything else."""
    for status in (verify.UNVERIFIED, verify.REFUTED, verify.SHARED):
        assert verify._report_status(status, "head") == status


# --- the operator-facing count --------------------------------------------------------------------

def _seeded(tmp_path, rows):
    """A database holding axiom_verified_addresses rows with the given (address, detail)."""
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute("CREATE TABLE axiom_verified_addresses(source_user_id TEXT, handle TEXT, "
                 "x_handle TEXT, network_id TEXT, address TEXT, verify_status TEXT, matches INT, "
                 "contradictions INT, no_data INT, checked INT, verified_at TEXT, detail TEXT)")
    conn.execute("CREATE TABLE memecoin_wallet_addresses(source_user_id TEXT, handle TEXT, "
                 "network_id TEXT, address TEXT, verify_status TEXT, method TEXT)")
    for address, detail in rows:
        conn.execute("INSERT INTO axiom_verified_addresses(source_user_id, handle, network_id, "
                     "address, verify_status, detail) VALUES (?,?,?,?,?,?)",
                     ("u-" + address, "h", "4663", address, verify.CONFIRMED, detail))
    conn.commit()
    return conn


def _detail_json(basis, address, mints=("a", "b", "c")):
    """A detail blob that passes the real 4663 proof filter, with the given basis.

    `_current_proofs` demands MIN_MATCHES anchored, linked purchases, so the fixture has to satisfy
    the production rule rather than a simplified copy of it. A shortcut here would pass whatever
    the filter later became.
    """
    import json
    return json.dumps({"proof_version": verify.RELAY_KYBER_PROOF_VERSION, "contradictions": 0,
                       "code_basis": basis,
                       "event_evidence": [
                           {"mint": m, "verdict": "bought", "anchor": {"tx_sig": "0xsig" + m},
                            "purchase": {"beneficiary": address.lower(), "mint": m,
                                         "proof_version": verify.RELAY_KYBER_PROOF_VERSION,
                                         "code_basis": basis}} for m in mints]})


def test_head_basis_addresses_counts_only_head_rows(tmp_path):
    conn = _seeded(tmp_path, [("0xaaa", _detail_json("head", "0xaaa")),
                              ("0xbbb", _detail_json("receipt_block", "0xbbb"))])
    got = verify.head_basis_addresses(conn)
    assert ("4663", "0xaaa") in got
    assert ("4663", "0xbbb") not in got
    conn.close()


def test_a_row_written_before_code_basis_existed_is_not_head(tmp_path):
    """Old rows passed the strict check, which only ever accepted a receipt-block answer."""
    import json
    old = json.loads(_detail_json("head", "0xccc"))
    old.pop("code_basis")
    for event in old["event_evidence"]:
        event["purchase"].pop("code_basis")
    conn = _seeded(tmp_path, [("0xccc", json.dumps(old))])
    assert verify.head_basis_addresses(conn) == set()
    conn.close()


# --- an ERRORED receipt-block read (2026-09-28, notanicecat69 0x29225b3297) ----------------------
# The watcher decided a delivered relay buy `no_buy` at 11:56:31Z, 2 min after a restart emptied its
# code cache. The tx and receipt came from the tx-read host in ~50 ms; the code check then asked the
# READ host for eth_getCode at the receipt block about 0.6 s after arrival. A read node that has not
# reached a block answers an ERROR, not '0x', and an error went straight to
# contract_code_unavailable while an empty answer re-asked head. Both are "unknown at that block".

def _errors_at_receipt(*, at_head):
    calls = []

    def read_fn(method, params):
        calls.append((method, tuple(params)))
        if method == "eth_chainId":
            return "0x1237"
        if params[1] == "latest":
            if isinstance(at_head, Exception):
                raise at_head
            return at_head
        raise RuntimeError("header not found")

    read_fn.calls = calls
    return read_fn


def test_an_errored_receipt_block_read_is_re_asked_at_head(monkeypatch):
    _accept_real_code(monkeypatch)
    got, evidence = _run(monkeypatch, _errors_at_receipt(at_head=REAL_CODE), live=True)
    assert got is not None, evidence
    assert got["code_basis"] == "head" and evidence["code_basis"] == "head"


def test_a_historical_caller_never_falls_back_on_an_error(monkeypatch):
    """Address verification replays OLD events; current code proves nothing about them (09-16)."""
    _accept_real_code(monkeypatch)
    read_fn = _errors_at_receipt(at_head=REAL_CODE)
    got, evidence = _run(monkeypatch, read_fn)
    assert got is None and evidence["reason"] == "contract_code_unavailable"
    assert all(params[1] != "latest" for method, params in read_fn.calls if method == "eth_getCode")


def test_an_errored_read_still_needs_the_pinned_hash_at_head(monkeypatch):
    _accept_real_code(monkeypatch)
    got, evidence = _run(monkeypatch, _errors_at_receipt(at_head="0xdeadbeef"), live=True)
    assert got is None and evidence["reason"] == "contract_code_mismatch"


def test_an_error_at_head_too_stays_unavailable(monkeypatch):
    _accept_real_code(monkeypatch)
    got, evidence = _run(monkeypatch, _errors_at_receipt(at_head=RuntimeError("down")), live=True)
    assert got is None and evidence["reason"] == "contract_code_unavailable"
