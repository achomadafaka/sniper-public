"""No test module imports `conftest` by its bare name (2026-10-15, Gamma's suite list).

Every conftest.py in one pytest process is imported as the module `conftest`, so a bare
`from conftest import X` gets whichever conftest was registered last. tests/ink imported its
constants that way: collecting tests/ink/test_accounting.py, any tests/wallet_protocol test, then
tests/ink/test_op_fees.py failed with ImportError against tests/wallet_protocol/conftest.py (the
same class as tests/test_operator_env_does_not_reach_tests.py, 2026-10-07). Shared test values go
in a uniquely named module (tests/ink/ink_constants.py)."""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def bare_conftest_imports(source):
    """Line numbers of `import conftest` / `from conftest import ...` in one module's source."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "conftest" and not node.level:
            lines.append(node.lineno)
        elif isinstance(node, ast.Import) and any(a.name == "conftest" for a in node.names):
            lines.append(node.lineno)
    return lines


def test_the_scan_sees_both_forms():
    assert bare_conftest_imports("from conftest import INK_TOKEN\nimport conftest\n") == [1, 2]
    assert bare_conftest_imports("from .conftest import X\nfrom ink_constants import Y\n") == []


def test_no_test_module_imports_conftest_by_its_bare_name():
    found = {str(p.relative_to(TESTS)): bare_conftest_imports(p.read_text())
             for p in TESTS.rglob("*.py") if p.name != "conftest.py"}
    assert {k: v for k, v in found.items() if v} == {}
