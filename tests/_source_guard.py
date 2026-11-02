"""Helper for source-scanning guards. See AGENTS.md "Source-scanning guards" (925d8d77).

A guard that reads source text to forbid or require a pattern must scan CODE ONLY. Prose that
explains a rule necessarily contains the rule, so a guard reading raw text can match the comment
describing the thing instead of the thing. Proved on 2026-08-27 against a real guard: deleting the
chase gate and leaving a comment describing it kept that guard GREEN over code that no longer
existed.

TWO KINDS OF PROSE, AND ONLY ONE VANISHES FOR FREE. `ast.parse` drops `#` comments because they
are not nodes — but a DOCSTRING IS a node (`Expr(Constant(str))`) and `ast.unparse` re-emits it
verbatim. The rule's own first example is a test that matched `datetime('now'` in its own
docstring, so dropping comments alone would not have caught it. `code_of` strips both.

`ast.unparse` also re-emits one statement per line, which removes the line-vs-statement trap where
a wrapped condition puts the matched token and the thing it binds on different lines.

    from _source_guard import code_of
    src = code_of(some_module)          # or code_of(pathlib.Path("sniper/x.py"))
    assert re.search(r"...", src)

Conversion costs one call. 42 test files pull a whole module with `inspect.getsource` and 3 strip
prose; the rest are each a judgement call for their owner, because normalised source moves
indentation and line wrapping and so breaks exact-literal assertions.
"""
from __future__ import annotations

import ast
import inspect
import pathlib


class _DropDocstrings(ast.NodeTransformer):
    """Remove the leading string expression from every module, class and function body."""

    def _strip(self, node):
        self.generic_visit(node)
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = _strip
    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def code_of(target) -> str:
    """Comment-free, docstring-free, one-statement-per-line source.

    `target` is a module, class, function, or a path to a .py file.
    """
    if isinstance(target, (str, pathlib.Path)):
        text = pathlib.Path(target).read_text()
    else:
        text = inspect.getsource(target)
    return ast.unparse(_DropDocstrings().visit(ast.parse(text)))
