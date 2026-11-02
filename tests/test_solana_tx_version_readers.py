"""Every getTransaction / getBlock request in sniper/ and scripts/ asks maxSupportedTransactionVersion 1
(operator, 2026-10-02).

Asked with 0, or with the field left out, the RPC refuses a v1 transaction (-32015; with the field
left out it refuses v0 as well), and most adapters here turn that error into None, so the
transaction silently reads as missing. v1 was 20% of all transactions, 18% of pump AMM and 36% of
pump curve ones in two blocks read on 2026-10-02. Asking 1 changes nothing for a legacy or v0
transaction. The v1 shape was checked on a live pump AMM v1 transaction: jsonParsed accountKeys are
the same {pubkey, ...} dicts, the json form keeps `header` and `meta.loadedAddresses`, and only
`message.transactionConfig` is new. A v1 transaction carries its priority fee there instead of in
a ComputeBudget instruction; the one reader of other wallets' CU price,
scripts/research/copier_crowd_and_fees.py, reads both.

The scan is static: for each "getTransaction" / "getBlock" method literal it takes the request
around it (the call that names the method, or the JSON-RPC body dict) and every config that request
reaches through local names, module names and local helper functions, and requires the version
field and only the value 1.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# getTransactionsForAddress is Helius's paged history; with transactionDetails "full" it returns
# whole transactions and refuses a v1 one exactly like getTransaction.
METHODS = {"getTransaction", "getBlock", "getTransactionsForAddress"}
KEY = "maxSupportedTransactionVersion"


def _scopes(tree):
    """{node: parent} for the whole tree."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


_DEFS = {}


def _definitions(scope, name):
    """Assignments to `name` and functions named `name` anywhere in `scope`. Memoized per scope."""
    key = (id(scope), name)
    if key not in _DEFS:
        _DEFS[key] = _find_definitions(scope, name)
    return _DEFS[key]


def _find_definitions(scope, name):
    out = []
    for node in ast.walk(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == name and node.value is not None:
                    out.append(node.value)
                elif (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                      and t.value.id == name and isinstance(t.slice, ast.Constant)
                      and t.slice.value == KEY):
                    out.append(ast.Dict(keys=[ast.Constant(KEY)], values=[node.value]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            out.append(node)
    return out


def _versions(node, parents, tree, seen, depth=0):
    """Every value the request at `node` can give the version field: ints, or 'dynamic'."""
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            for k, v in zip(sub.keys, sub.values):
                if isinstance(k, ast.Constant) and k.value == KEY:
                    found.add(v.value if isinstance(v, ast.Constant) else "dynamic")
        elif isinstance(sub, ast.Call):
            for kw in sub.keywords:
                if kw.arg == KEY:
                    found.add(kw.value.value if isinstance(kw.value, ast.Constant) else "dynamic")
        elif (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and depth < 3
              and sub.id not in seen):
            seen = seen | {sub.id}
            scope = parents.get(node)
            while scope is not None and not isinstance(
                    scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                scope = parents.get(scope)
            for owner in (scope, tree):
                if owner is None:
                    continue
                for value in _definitions(owner, sub.id):
                    found |= _versions(value, parents, tree, seen, depth + 1)
    return found


def _request(node, parents):
    """The request a method literal names: the call it is a positional argument of, or the JSON-RPC
    body whose "method" it is, or a (method, params) batch member. None for any other use (`method == "getTransaction"`, a set of
    method names), which is not a request."""
    parent = parents.get(node)
    if isinstance(parent, ast.Call) and node in parent.args:
        return parent
    if (isinstance(parent, (ast.Tuple, ast.List)) and len(parent.elts) == 2
            and parent.elts[0] is node and not isinstance(parent.elts[1], ast.Constant)):
        return parent                    # a batch member: ("getTransaction", params(signature))
    if isinstance(parent, ast.Dict) and node in parent.values:
        key = parent.keys[parent.values.index(node)]
        if isinstance(key, ast.Constant) and key.value == "method":
            return parent
    return None


def _signatures_only(request):
    """A history page that asks for signatures only carries no transaction to refuse: a dict
    literal or a dict(...) call naming transactionDetails "signatures"."""
    def signatures(k, v):
        return k == "transactionDetails" and isinstance(v, ast.Constant) and v.value == "signatures"
    for d in ast.walk(request):
        if isinstance(d, ast.Dict) and any(
                isinstance(k, ast.Constant) and signatures(k.value, v) for k, v in zip(d.keys, d.values)):
            return True
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dict"
                and any(signatures(kw.arg, kw.value) for kw in d.keywords)):
            return True
    return False


def requests_without_version_1(paths, root=ROOT):
    bad = []
    for path in paths:
        text = path.read_text(errors="replace")
        if not any(m in text for m in METHODS):
            continue
        tree = ast.parse(text)
        parents = _scopes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in METHODS:
                request = _request(node, parents)
                if request is None or (node.value == "getTransactionsForAddress"
                                       and _signatures_only(request)):
                    continue
                versions = _versions(request, parents, tree, frozenset())
                if versions != {1}:
                    bad.append("%s:%d %s %s" % (path.relative_to(root), node.lineno, node.value,
                                                sorted(map(str, versions)) or "missing"))
    return bad


def _sources():
    return sorted(list((ROOT / "sniper").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")))


def test_every_transaction_request_asks_version_1():
    assert requests_without_version_1(_sources()) == []


def test_the_scan_sees_a_missing_field_a_zero_and_a_config_built_elsewhere(tmp_path):
    """The guard's own negative control: each of these shapes must be caught."""
    src = tmp_path / "probe.py"
    src.write_text(
        "def a(rpc, s):\n    return rpc('getTransaction', [s, {'encoding': 'json'}])\n"
        "def b(rpc, s):\n    return rpc('getTransaction', [s, {'maxSupportedTransactionVersion': 0}])\n"
        "def c(rpc, s):\n    opts = {'maxSupportedTransactionVersion': 0}\n"
        "    return rpc('getBlock', [s, dict(opts)])\n"
        "def d(rpc, s):\n    def params(x):\n        return [x, {'maxSupportedTransactionVersion': 1}]\n"
        "    return rpc('getTransaction', params(s))\n")
    bad = requests_without_version_1([src], root=tmp_path)
    assert [b.split(" ")[0].rsplit(":", 1)[1] for b in bad] == ["2", "4", "7"]


def test_the_scan_skips_a_signatures_only_constructor_and_a_name_tuple(tmp_path):
    """R10 (Codex review 2026-10-06): two flags were false positives. A signatures-only history page
    built with dict(...) carries no transaction, and a one-name tuple is an allowlist, not a batch
    member. A real (method, params) batch member without the field is still caught."""
    src = tmp_path / "probe.py"
    src.write_text(
        "def a(rpc, m):\n"
        "    return rpc('getTransactionsForAddress', [m, dict(transactionDetails='signatures')])\n"
        "ALLOW = ('getTransactionsForAddress',)\n"
        "NAMES = ('getTransaction', 'getBlock')\n"
        "def b(batch, s):\n    return batch([('getTransaction', [s, {'encoding': 'json'}])])\n"
        "def c(rpc, m):\n"
        "    return rpc('getTransactionsForAddress', [m, dict(transactionDetails='full')])\n")
    bad = requests_without_version_1([src], root=tmp_path)
    assert sorted(b.split(" ")[0].rsplit(":", 1)[1] for b in bad) == ["6", "8"]
