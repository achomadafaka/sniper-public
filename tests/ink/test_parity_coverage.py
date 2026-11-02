"""The Ink admission parity table is complete, resolvable, and honest (plan Task 7)."""
import ast
import re
from pathlib import Path

from parity_coverage import COVERAGE, PASS, REFUSE, UNKNOWN
from sniper.settings import memecoin as cfg

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MATRIX = ROOT / "docs/superpowers/research/2026-09-29-ink-filter-parity.md"


def matrix_rows():
    """Row names of the '## Admission matrix' table, in order."""
    text = MATRIX.read_text()
    section = text.split("## Admission matrix", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if (line.startswith("|") and len(cells) >= 3 and not set(cells[0]) <= set("-: ")
                and cells[0] != "Filter / invariant"):
            rows.append(cells[0])
    return rows


def _functions(path):
    return {n.name for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)}


def test_every_matrix_row_is_listed_with_three_outcomes():
    rows = matrix_rows()
    assert len(rows) == 23, rows
    assert set(COVERAGE) == set(rows), (set(rows) - set(COVERAGE), set(COVERAGE) - set(rows))
    for row, outcomes in COVERAGE.items():
        assert set(outcomes) == {PASS, REFUSE, UNKNOWN}, row


def test_every_claimed_node_id_resolves_to_a_test():
    for row, outcomes in COVERAGE.items():
        for outcome, claim in outcomes.items():
            if claim.startswith("gap:"):
                assert len(claim) > len("gap: "), (row, outcome)
                continue
            m = re.fullmatch(r"(test_[a-z0-9_]+\.py)::(test_[a-z0-9_]+)", claim)
            assert m, (row, outcome, claim)
            path = HERE / m.group(1)
            assert path.exists() and m.group(2) in _functions(path), (row, outcome, claim)


def test_ink_stays_blocked_while_any_row_has_a_gap():
    gaps = [(row, outcome) for row, outcomes in COVERAGE.items()
            for outcome, claim in outcomes.items() if claim.startswith("gap:")]
    if gaps:
        assert cfg.ink_capability_reason() is not None, (
            "the pinned manifest says ready while %d parity outcomes are gaps" % len(gaps))
