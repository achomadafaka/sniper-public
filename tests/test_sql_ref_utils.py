"""Tests for sql_ref_utils — shared SQL/string extraction helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_sql_string_literals_extracts_strings():
    from sql_ref_utils import sql_string_literals

    code = '''
x = "SELECT * FROM foo"
conn.execute("INSERT INTO bar (a, b) VALUES (?, ?)")
'''
    result = sql_string_literals(code)
    texts = [text for text, _lineno in result]
    assert "SELECT * FROM foo" in texts
    assert "INSERT INTO bar (a, b) VALUES (?, ?)" in texts


def test_sql_string_literals_skips_docstrings():
    from sql_ref_utils import sql_string_literals

    code = '''
def foo():
    """INSERT INTO fake_table (col) VALUES (1)"""
    return "SELECT * FROM real_table"
'''
    result = sql_string_literals(code)
    texts = [text for text, _lineno in result]
    assert "SELECT * FROM real_table" in texts
    assert not any("fake_table" in t for t in texts)


def test_sql_string_literals_includes_line_numbers():
    from sql_ref_utils import sql_string_literals

    code = 'x = "hello"\ny = "world"\n'
    result = sql_string_literals(code)
    assert len(result) >= 2
    for text, lineno in result:
        assert isinstance(lineno, int)
        assert lineno >= 1


def test_sql_string_literals_handles_fstrings():
    from sql_ref_utils import sql_string_literals

    code = 'x = f"SELECT {col} FROM my_table WHERE id = {val}"\n'
    result = sql_string_literals(code)
    texts = [text for text, _lineno in result]
    assert any("my_table" in t for t in texts)


def test_sql_string_literals_handles_syntax_error():
    from sql_ref_utils import sql_string_literals

    result = sql_string_literals("def broken(:\n")
    assert result == []


def test_extract_markdown_code_blocks_python():
    from sql_ref_utils import extract_markdown_code_blocks

    md = '''# Heading

Some text.

```python
conn.execute("INSERT INTO foo (a) VALUES (?)")
```

More text.

```javascript
console.log("ignored");
```
'''
    blocks = extract_markdown_code_blocks(md)
    assert len(blocks) == 1
    assert blocks[0][0] == "python"
    assert "INSERT INTO foo" in blocks[0][1]


def test_extract_markdown_code_blocks_sql():
    from sql_ref_utils import extract_markdown_code_blocks

    md = '''```sql
INSERT INTO bar (x, y) VALUES (1, 2);
```
'''
    blocks = extract_markdown_code_blocks(md)
    assert len(blocks) == 1
    assert blocks[0][0] == "sql"
    assert "INSERT INTO bar" in blocks[0][1]


def test_extract_markdown_code_blocks_with_line_offset():
    from sql_ref_utils import extract_markdown_code_blocks

    md = '''line1
line2
```python
code here
```
'''
    blocks = extract_markdown_code_blocks(md)
    assert len(blocks) == 1
    lang, content, start_line = blocks[0]
    assert start_line == 4  # code starts after the ``` line


def test_looks_like_sql_positive():
    from sql_ref_utils import looks_like_sql

    assert looks_like_sql("INSERT INTO foo (a) VALUES (1)")
    assert looks_like_sql("SELECT x FROM bar")
    assert looks_like_sql("UPDATE baz SET x = 1")
    assert looks_like_sql("delete from qux where id = 1")


def test_looks_like_sql_negative():
    from sql_ref_utils import looks_like_sql

    assert not looks_like_sql("just a regular string")
    assert not looks_like_sql("x = 42")
    assert not looks_like_sql("")
