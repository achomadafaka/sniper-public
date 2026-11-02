"""Shared SQL/string extraction helpers for brain index and schema validator.

Used by:
- scripts/brain_index.py (dependency extraction)
- scripts/validate_schema_refs.py (column reference validation)
"""

from __future__ import annotations

import ast
import re


def sql_string_literals(content: str) -> list[tuple[str, int]]:
    """AST-parse Python source, return non-docstring string literals with line numbers.

    Returns list of (literal_text, lineno) tuples.  Skips docstrings at the
    start of modules, classes, and functions.  Handles f-strings by extracting
    the static text portions.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if isinstance(first, ast.Expr):
                value = first.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstring_ids.add(id(value))
                elif isinstance(value, ast.JoinedStr):
                    docstring_ids.add(id(value))

    literals: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if id(node) in docstring_ids:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append((node.value, getattr(node, "lineno", 0)))
        elif isinstance(node, ast.JoinedStr):
            static_text = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if static_text:
                literals.append((static_text, getattr(node, "lineno", 0)))
    return literals


def extract_markdown_code_blocks(content: str) -> list[tuple[str, str, int]]:
    """Extract fenced code blocks from markdown.

    Returns list of (language, code_content, start_line) tuples.
    Only extracts blocks tagged as python, py, or sql.
    start_line is 1-indexed and points to the first line of code
    (the line after the opening fence).
    """
    blocks: list[tuple[str, str, int]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower()
            if lang in ("python", "py", "sql"):
                code_lines: list[str] = []
                code_start = i + 2  # 1-indexed, line after fence
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                blocks.append((lang, "\n".join(code_lines), code_start))
        i += 1
    return blocks


def looks_like_sql(text: str) -> bool:
    """Return True if text contains SQL-like keywords."""
    return bool(
        re.search(
            r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE)\b",
            text,
            re.IGNORECASE,
        )
    )
