from dataclasses import replace

import pytest
from eth_utils import keccak

from sniper.execution.lifi_executor import (
    LifiAuthorityError,
    audit_receipt_block,
    prove_execution_block,
    prove_quote_authority,
)
from sniper.execution.lifi_route import DecodedQuote


CUSTODY = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
OUTPUT = "0x3333333333333333333333333333333333333333"
DIAMOND = "0x4444444444444444444444444444444444444444"
FACET = "0x5555555555555555555555555555555555555555"
FEE = "0x6666666666666666666666666666666666666666"
SWAP = "0x7777777777777777777777777777777777777777"
EXECUTOR = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PERMIT2 = "0x8888888888888888888888888888888888888888"
PERMIT2_PROXY = "0x9999999999999999999999999999999999999999"
BLOCK = {"number": "0x2a", "hash": "0x" + "ab" * 32}
CODE = {
    DIAMOND: "0x6001",
    FACET: "0x6002",
    FEE: "0x6003",
    SWAP: "0x6004",
    EXECUTOR: "0x6005",
}


def _code_hash(code):
    return "0x" + keccak(bytes.fromhex(code[2:])).hex()


def _manifest():
    return {
        "chain_id": "4663",
        "reject": {"addresses": [PERMIT2, PERMIT2_PROXY], "selectors": []},
        "top_level": {
            "target": DIAMOND,
            "spender": DIAMOND,
            "selector": "0x12345678",
            "tool": "nordstern",
            "facet": FACET,
            "code_hash": _code_hash(CODE[DIAMOND]),
            "facet_code_hash": _code_hash(CODE[FACET]),
        },
        "steps": [
            {
                "target": FEE,
                "approve_to": FEE,
                "spender": DIAMOND,
                "selector": "0x11111111",
                "code_hash": _code_hash(CODE[FEE]),
            },
            {
                "target": SWAP,
                "approve_to": SWAP,
                "spender": DIAMOND,
                "selector": "0x22222222",
                "code_hash": _code_hash(CODE[SWAP]),
                "executor": EXECUTOR,
                "executor_code_hash": _code_hash(CODE[EXECUTOR]),
            },
        ],
    }


def _decoded():
    return DecodedQuote(
        calldata_sha256="c" * 64,
        decoded_semantics_sha256="d" * 64,
        fee_atomic=2,
        input_atomic=1000,
        output_min_atomic=900,
        path=(TOKEN, TOKEN, OUTPUT),
        request_sha256="a" * 64,
        response_sha256="b" * 64,
        spender=DIAMOND,
        swap_input_atomic=998,
        target=DIAMOND,
        tool="nordstern",
    )


class Rpc:
    def __init__(self, *, block=None, code=None, facet=FACET):
        self.block = block or dict(BLOCK)
        self.code = dict(CODE if code is None else code)
        self.facet = facet
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_getBlockByNumber":
            return {"result": self.block}
        if method == "eth_getCode":
            return {"result": self.code[params[0].lower()]}
        if method == "eth_call":
            return {"result": "0x" + "00" * 12 + self.facet[2:]}
        raise AssertionError(method)


def test_quote_proof_binds_two_rpcs_code_facet_stage_and_hashes():
    custody_rpc = Rpc()
    proof_rpc = Rpc()
    used = set()

    proof = prove_quote_authority(
        _decoded(),
        "a" * 64,
        "b" * 64,
        custody_rpc,
        proof_rpc,
        manifest=_manifest(),
        stage="initial_buy",
        received_at="2026-08-30T20:00:00+00:00",
        used_quote_hashes=used,
        block_tag="0x2a",
    )

    assert proof.block_number == 42
    assert proof.block_hash == BLOCK["hash"]
    assert proof.stage == "initial_buy"
    assert proof.facet == FACET
    assert set(proof.code_hashes) == set(CODE)
    assert ("a" * 64, "b" * 64) in used
    for rpc in (custody_rpc, proof_rpc):
        assert ("eth_getBlockByNumber", ["0x2a", False]) in rpc.calls
        assert ("eth_getCode", [DIAMOND, "0x2a"]) in rpc.calls
        assert any(method == "eth_call" and params[1] == "0x2a" for method, params in rpc.calls)


def test_quote_proof_rejects_reuse_block_disagreement_and_code_change():
    used = {("a" * 64, "b" * 64)}
    with pytest.raises(LifiAuthorityError, match="reused"):
        prove_quote_authority(
            _decoded(), "a" * 64, "b" * 64, Rpc(), Rpc(), manifest=_manifest(),
            stage="initial_sell", received_at="now", used_quote_hashes=used,
        )

    wrong_block = dict(BLOCK, hash="0x" + "cd" * 32)
    with pytest.raises(LifiAuthorityError, match="block"):
        prove_quote_authority(
            _decoded(), "a" * 64, "b" * 64, Rpc(), Rpc(block=wrong_block),
            manifest=_manifest(), stage="initial_buy", received_at="now",
        )

    wrong_code = dict(CODE, **{FEE: "0x6005"})
    with pytest.raises(LifiAuthorityError, match="code"):
        prove_quote_authority(
            _decoded(), "a" * 64, "b" * 64, Rpc(), Rpc(code=wrong_code),
            manifest=_manifest(), stage="initial_buy", received_at="now",
        )


def test_quote_proof_rejects_hash_and_facet_mismatch():
    with pytest.raises(LifiAuthorityError, match="request"):
        prove_quote_authority(
            _decoded(), "f" * 64, "b" * 64, Rpc(), Rpc(), manifest=_manifest(),
            stage="initial_buy", received_at="now",
        )

    with pytest.raises(LifiAuthorityError, match="facet"):
        prove_quote_authority(
            _decoded(), "a" * 64, "b" * 64, Rpc(), Rpc(facet=FEE),
            manifest=_manifest(), stage="initial_buy", received_at="now",
        )

    forged = replace(_decoded(), target=FEE, spender=FEE, tool="fly")
    with pytest.raises(LifiAuthorityError, match="decoded authority"):
        prove_quote_authority(
            forged, "a" * 64, "b" * 64, Rpc(), Rpc(), manifest=_manifest(),
            stage="initial_buy", received_at="now",
        )


def test_execution_proof_uses_exact_order_and_allowance_bounds():
    events = []
    decoded = _decoded()

    def estimate(tx):
        events.append("estimate")
        return 210_000

    def construct(tx, gas):
        events.append("construct")
        return dict(tx, gas=gas, to=decoded.target, data_hash=decoded.calldata_sha256)

    def allowance(token, owner, spender, block):
        events.append("allowance:" + spender)
        assert (token, owner, block) == (TOKEN, CUSTODY, 42)
        return decoded.input_atomic if spender == decoded.spender else 0

    def simulate(tx, block):
        events.append("simulate")
        assert block == 42
        return True

    def sign(tx):
        events.append("sign")
        return {"hash": "0xabc", "raw": "0xsigned", "tx": tx}

    def persist(proof, signed):
        events.append("persist")
        assert proof.block_number == 42
        assert signed["hash"] == "0xabc"

    result = prove_execution_block(
        {"from": CUSTODY, "chainId": 4663, "to": DIAMOND, "data_hash": "c" * 64},
        decoded,
        rpc=Rpc(),
        proof_rpc=Rpc(),
        manifest=_manifest(),
        stage="final_buy",
        received_at="now",
        custody=CUSTODY,
        estimate_gas=estimate,
        construct_tx=construct,
        allowance_reader=allowance,
        simulate=simulate,
        sign=sign,
        persist=persist,
    )

    assert result.signed["hash"] == "0xabc"
    assert events[:2] == ["estimate", "construct"]
    assert events[-3:] == ["simulate", "sign", "persist"]
    assert events.index("simulate") > max(
        index for index, value in enumerate(events) if value.startswith("allowance:")
    )


def test_execution_proof_selects_a_new_current_block_after_quote_proof():
    decoded = _decoded()
    custody_rpc = Rpc()
    proof_rpc = Rpc()
    authority = prove_quote_authority(
        decoded, decoded.request_sha256, decoded.response_sha256,
        custody_rpc, proof_rpc, manifest=_manifest(), stage="refreshed_buy",
        received_at="now",
    )
    next_block = {"number": "0x2b", "hash": "0x" + "cd" * 32}
    custody_rpc = Rpc(block=next_block)
    proof_rpc = Rpc(block=next_block)
    simulated = []

    result = prove_execution_block(
        {"from": CUSTODY, "chainId": 4663, "to": DIAMOND, "data_hash": "c" * 64},
        decoded, rpc=custody_rpc, proof_rpc=proof_rpc, manifest=_manifest(),
        stage="refreshed_buy", received_at="now", custody=CUSTODY,
        estimate_gas=lambda _tx: 1,
        construct_tx=lambda tx, gas: dict(tx, gas=gas),
        allowance_reader=lambda _token, _owner, spender, _block: (
            decoded.input_atomic if spender == decoded.spender else 0),
        simulate=lambda tx, block: simulated.append((tx, block)) or True,
        sign=lambda _tx: {"hash": "0xabc"}, persist=lambda *_args: None,
        authority=authority,
    )

    assert result.authority is not authority
    assert result.authority.block_number == 43
    assert simulated == [(result.transaction, 43)]
    assert ("eth_getBlockByNumber", ["latest", False]) in custody_rpc.calls


def test_execution_proof_rejects_allowance_or_semantics_before_sign():
    signed = False

    def sign(_tx):
        nonlocal signed
        signed = True

    with pytest.raises(LifiAuthorityError, match="allowance"):
        prove_execution_block(
            {"from": CUSTODY, "chainId": 4663, "to": DIAMOND, "data_hash": "c" * 64},
            _decoded(), rpc=Rpc(), proof_rpc=Rpc(), manifest=_manifest(), stage="final_buy",
            received_at="now", custody=CUSTODY, estimate_gas=lambda _tx: 1,
            construct_tx=lambda tx, gas: dict(tx, gas=gas),
            allowance_reader=lambda *_args: 1, simulate=lambda *_args: True,
            sign=sign, persist=lambda *_args: None,
        )
    assert not signed

    bad = replace(_decoded(), target=FEE)
    with pytest.raises(LifiAuthorityError, match="semantic"):
        prove_execution_block(
            {"from": CUSTODY, "chainId": 4663, "to": DIAMOND, "data_hash": "c" * 64},
            bad, rpc=Rpc(), proof_rpc=Rpc(), manifest=_manifest(), stage="final_buy",
            received_at="now", custody=CUSTODY, estimate_gas=lambda _tx: 1,
            construct_tx=lambda tx, gas: dict(tx, gas=gas),
            allowance_reader=lambda *_args: 0, simulate=lambda *_args: True,
            sign=sign, persist=lambda *_args: None,
        )
    assert not signed


def test_receipt_audit_rejects_malformed_facet_result():
    rpc = Rpc()

    def malformed(method, params):
        if method == "eth_call":
            return {"result": "0x" + FACET[2:]}
        return rpc(method, params)

    result = audit_receipt_block(
        {"blockNumber": "0x2a", "blockHash": BLOCK["hash"]},
        _decoded(), rpc=Rpc(), proof_rpc=malformed, manifest=_manifest(),
        stage="entry_receipt",
    )
    assert not result.ok


def test_receipt_block_audit_uses_the_canonical_receipt_block():
    receipt = {"blockNumber": "0x2a", "blockHash": BLOCK["hash"]}
    result = audit_receipt_block(
        receipt,
        _decoded(),
        rpc=Rpc(),
        proof_rpc=Rpc(),
        manifest=_manifest(),
        stage="entry_receipt",
    )

    assert result.ok
    assert result.proof.block_number == 42

    bad = {"blockNumber": "0x2a", "blockHash": "0x" + "00" * 32}
    result = audit_receipt_block(
        bad,
        _decoded(),
        rpc=Rpc(),
        proof_rpc=Rpc(),
        manifest=_manifest(),
        stage="entry_receipt",
    )
    assert not result.ok
    assert result.code == "receipt_block_mismatch"
