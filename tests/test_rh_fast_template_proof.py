"""The RH fast-lane template proof's negative controls need a contract revert (Codex EVM-09, 2026-10-06):
a provider error used to count as the expected revert, so rate limits passed both controls."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("rh_fast_template_proof",
                                                  ROOT / "scripts" / "research" / "rh_fast_template_proof.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _mod()
TX = {"to": "0x" + "11" * 20, "data": "0x", "gas": 300000}


def _run(monkeypatch, replies):
    seq = iter(replies)
    monkeypatch.setattr(P, "rpc", lambda url, method, params: next(seq))
    counts = [0, 0]
    P._rows("url", "0x1", "pool", 1000, lambda min_out: TX, counts)
    return counts


def test_provider_errors_are_no_verdict_not_expected_reverts(monkeypatch, capsys):
    counts = _run(monkeypatch, [("0x", None),
                                (None, {"code": -32005, "message": "rate limit exceeded"}),
                                (None, {"code": -32600, "message": "invalid request"})])
    assert counts == [0, 2]                                # two controls unproven, none matched
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r.get("refused") is not None for r in rows] == [False, True, True]


def test_contract_reverts_answer_the_negative_controls(monkeypatch):
    revert = (None, {"code": 3, "message": "execution reverted", "data": "0x8b063d73"})
    assert _run(monkeypatch, [("0x", None), revert, revert]) == [0, 0]


def test_a_missing_result_is_no_verdict_and_a_capped_revert_is_a_mismatch(monkeypatch):
    revert = (None, {"code": 3, "message": "execution reverted"})
    assert _run(monkeypatch, [(None, None), revert, revert]) == [0, 1]
    assert _run(monkeypatch, [revert, revert, revert]) == [1, 0]
    assert _run(monkeypatch, [("0x", None), ("0x", None), revert]) == [1, 0]


def test_malformed_success_bytes_are_no_verdict():
    # Codex night review EVM-09 residual: '0xnot-hex' counted as a positive pass.
    for bad in ("0xnot-hex", "0xabc", "0x0g", "deadbeef", "", 7):
        assert P.call_verdict(bad, None) is None, bad
    assert P.call_verdict("0x", None) == "pass" and P.call_verdict("0x00ff", None) == "pass"
