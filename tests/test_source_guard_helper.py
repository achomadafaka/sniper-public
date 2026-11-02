"""The helper must remove BOTH kinds of prose, and must not remove code."""
from __future__ import annotations

import re

from _source_guard import code_of

SAMPLE = '''
"""Module docstring naming FORBIDDEN_TOKEN."""


class C:
    """Class docstring naming FORBIDDEN_TOKEN."""

    def m(self):
        """Method docstring naming FORBIDDEN_TOKEN."""
        # a comment naming FORBIDDEN_TOKEN
        return 1


def f(x):
    """A docstring naming FORBIDDEN_TOKEN."""
    # a comment naming FORBIDDEN_TOKEN
    if (x > 0
            and x < 10):
        return "FORBIDDEN_TOKEN"
    return None
'''


def test_both_comments_and_docstrings_are_stripped(tmp_path):
    """A docstring is an AST node, so `ast.unparse` alone re-emits it — the rule's own first
    example was a guard that matched the forbidden string in its own docstring."""
    p = tmp_path / "s.py"
    p.write_text(SAMPLE)
    code = code_of(p)
    assert code.count("FORBIDDEN_TOKEN") == 1, "only the string LITERAL may survive"
    assert "a comment naming" not in code
    assert "docstring naming" not in code


def test_code_is_not_removed(tmp_path):
    p = tmp_path / "s.py"
    p.write_text(SAMPLE)
    code = code_of(p)
    assert "class C:" in code and "def m(self):" in code and "return 1" in code
    assert "return None" in code


def test_a_wrapped_condition_becomes_one_statement(tmp_path):
    """The line-vs-statement trap: a wrapped `if` must be matchable as one line."""
    p = tmp_path / "s.py"
    p.write_text(SAMPLE)
    assert re.search(r"if x > 0 and x < 10:", code_of(p))


def test_a_docstring_only_function_still_parses(tmp_path):
    """Stripping the docstring must leave a valid body, not an empty one."""
    p = tmp_path / "s.py"
    p.write_text('def f():\n    """only a docstring"""\n')
    assert "pass" in code_of(p)


NESTED = '''
"""module doc: FORBIDDEN"""
class Outer:
    """class doc: FORBIDDEN"""
    def m(self):
        """method doc: FORBIDDEN"""
        def inner():
            """inner doc: FORBIDDEN"""
            class Deep:
                """deep class doc: FORBIDDEN"""
                async def a(self):
                    """async doc: FORBIDDEN"""
                    return "FORBIDDEN"
            return Deep
        return inner
'''


def test_docstrings_are_stripped_at_every_level(tmp_path):
    """Stripping only the LEADING docstring leaves every nested one — the incomplete fix Beta
    found in their own guard. Depth here is module > class > method > function > class > async."""
    p = tmp_path / "n.py"
    p.write_text(NESTED)
    out = code_of(p)
    assert out.count("FORBIDDEN") == 1, "only the string literal may survive, at any depth"
    for kind in ("module doc", "class doc", "method doc", "inner doc", "deep class doc",
                 "async doc"):
        assert kind not in out, f"{kind} survived"
    for kept in ("class Outer:", "def inner():", "class Deep:", "async def a(self):"):
        assert kept in out, f"{kept} was removed"
