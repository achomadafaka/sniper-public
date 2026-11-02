"""SOL04 (engineering review 2026-10-08): the chase-cap cost study preregistration's per-wallet
missed-row table must reconcile with its own frozen cohort (Appendix A) and headline: 29 rows, 23
missed, 6 late fallbacks. The table said ResellCalendar 11 (total 24); Appendix A holds 10."""
import collections
import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs/superpowers/specs/2026-10-07-memecoin-solana-chase-cap-cost-study-prereg.md"


def _cells(block):
    return [[c.strip() for c in line.split("|")[1:-1]] for line in block.splitlines() if line.startswith("|")]


def test_the_per_wallet_missed_table_reconciles_with_appendix_a():
    text = DOC.read_text()
    appendix = text[text.index("## Appendix A"):text.index("## Appendix B")]
    cohort = [r for r in _cells(appendix) if re.match(r"\d\d-\d\d \d\d:\d\d", r[0])]
    fates = collections.Counter(r[4] for r in cohort)
    assert len(cohort) == 29 and fates == {"missed": 23, "late_fallback": 6}
    missed = collections.Counter(r[2] for r in cohort if r[4] == "missed")
    summary = text[text.index("By wallet, missed rows:"):text.index("**Revision 2 coverage.**")]
    table = {r[0]: int(r[1].split()[0]) for r in _cells(summary) if r and r[0] not in ("Wallet", "---")
             and r[1][:1].isdigit()}
    assert table == dict(missed) and sum(table.values()) == 23
