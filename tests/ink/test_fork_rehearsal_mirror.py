"""The G3 rehearsal's receipt pricer must stay a faithful copy of the funded one (Gamma LOW,
2026-09-30; Alpha chose a divergence test over lifting the closure out of prod).

tests/ink/test_fork_rehearsal.py::_receipt_proceeds mirrors the closure receipt_proceeds_fn that
memecoin_evm_leg.build_real_evm_context builds. This reads both from source and fails when their
signature, the receipt readers they call, or those readers' keyword values for chain 57073 differ.
Runs without anvil or a network.
"""
import ast
import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
READERS = ("erc20_received_in_receipt", "native_received_in_receipt", "exit_proceeds_atomic")
# Keywords naming each side's own variables (the wallet, the quote token, the transport) differ by
# construction; the rule the reader applies at `chain` is what must match.
_IDENTITY = {"http", "owner", "quote_token"}


def _function(path, name):
    tree = ast.parse(Path(path).read_text())
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, (path, name, len(found))
    return found[0]


def _shape(fn, chain="57073"):
    """(positional args, kw-only args with defaults, {reader: {keyword: value at `chain`}})."""
    args = fn.args
    kwonly = [(a.arg, ast.unparse(d) if d is not None else None)
              for a, d in zip(args.kwonlyargs, args.kw_defaults)]
    calls = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in READERS:
            calls[node.func.id] = {
                k.arg: eval(compile(ast.Expression(k.value), "<kw>", "eval"),
                            {"chain": chain, "quote": {"kind": "eth"}})
                for k in node.keywords if k.arg not in _IDENTITY}
    return [a.arg for a in args.args], kwonly, calls


def test_the_rehearsal_pricer_matches_the_funded_evm_context_on_57073():
    funded = _function(REPO / "sniper/services/memecoin_evm_leg.py", "receipt_proceeds_fn")
    harness = _function(REPO / "tests/ink/test_fork_rehearsal.py", "proceeds")
    assert _shape(harness) == _shape(funded)
    assert set(_shape(funded)[2]) == {"exit_proceeds_atomic"}   # the one reader the funded one calls


def test_the_shape_reader_sees_a_divergence():
    """The check itself must fail on a changed keyword, or it proves nothing."""
    changed = ast.parse(inspect.cleandoc('''
        def proceeds(tx_hash, *, quote_asset_usd=None):
            atomic = erc20_received_in_receipt(tx_hash, http=h, require_canonical=False)
            atomic = native_received_in_receipt(tx_hash, http=h, allow_block_delta=False)
    ''')).body[0]
    funded = _function(REPO / "sniper/services/memecoin_evm_leg.py", "receipt_proceeds_fn")
    assert _shape(changed) != _shape(funded)
