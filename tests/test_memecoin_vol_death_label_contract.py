"""The vol_death audit label is a PREFIX CONTRACT, and this guards it.

`memecoin_exit_trigger_audit` has no witness column. The covered/uncovered split the operator
registered is therefore computed by reading the LABEL out of `detail`, a free-text display field:

    detail = 'measured_no_state_witness silence=60.1s'

An analysis keyed on prose breaks silently. If the format string is ever reordered, every reader
that splits on the first token starts finding no label, and the failure surfaces as a CHANGE IN THE
COVERED RATE rather than as a parsing error — a number that moved for a reason nobody can see.

The durable fix is a column. That is deliberately deferred: this contract plus this test gives the
same durability without churning a 9,101-row funded-ledger table mid-day. Until then the contract
is: **the label is the first whitespace-delimited token of `detail`, always.**

This scans CODE, never text — see `_source_guard`. A comment describing the format cannot satisfy
it, and neither can a docstring.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from _source_guard import code_of

RUNNER = pathlib.Path("sniper/services/memecoin_real_runner.py")
WITNESS = pathlib.Path("sniper/services/memecoin_volume_death.py")

# The witness outcomes, split by ERA because the audit table holds both and a reader must parse
# both. `state_witness` returns moving/still/uncovered/reader_dark; the caller maps them onto these.
CURRENT_LABELS = {"state_moving", "state_reader_dark",
                  "state_uncovered_frozen", "measured_state_still",
                  # 4663 names the read that went dark (2026-09-16, position 58831).
                  "state_reader_dark:no_head", "state_reader_dark:no_confirm_fn",
                  "state_reader_dark:getlogs_failed"}
# RETIRED 2026-08-29, still present in the ledger. `measured_no_state_witness` was the verdict when
# an uncovered pool FIRED on swap silence alone; that branch now freezes and is labelled
# `state_uncovered_frozen`. Twenty-four audit rows carry the old token, so it must stay parseable
# forever even though nothing writes it. Retiring a label from the SOURCE check and from the
# PARSING check are two different decisions, and conflating them either breaks history or lets a
# dead label mask a live one.
RETIRED_LABELS = {"measured_no_state_witness"}
WITNESS_LABELS = CURRENT_LABELS | RETIRED_LABELS


def _vol_death_audit_details(tree):
    """Every `detail=` expression on a record_exit_trigger_audit call whose reason is vol_death."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "record_exit_trigger_audit":
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        reason = kw.get("reason")
        if isinstance(reason, ast.Constant) and reason.value == "vol_death":
            out.append(kw.get("detail"))
    return out


def test_the_vol_death_audit_label_is_the_FIRST_TOKEN_of_detail():
    tree = ast.parse(RUNNER.read_text())
    details = _vol_death_audit_details(tree)
    assert details, "no vol_death record_exit_trigger_audit call found — the write site moved"

    for detail in details:
        assert detail is not None, "a vol_death audit call writes no detail at all"
        # The contract is satisfied by any expression whose FIRST emitted characters are the label
        # and nothing else. Only two shapes can be checked statically, and both are accepted.
        if isinstance(detail, ast.BinOp) and isinstance(detail.op, ast.Mod):
            fmt = detail.left
            assert isinstance(fmt, ast.Constant) and isinstance(fmt.value, str), \
                "vol_death detail is a %-format whose left side is not a literal"
            assert fmt.value.startswith("%s"), (
                "the label must be the FIRST token of detail; format string starts %r"
                % fmt.value[:24])
            assert not fmt.value.startswith("%s%"), \
                "the label must be followed by a separator, not another substitution"
        elif isinstance(detail, ast.JoinedStr):           # f-string
            first = detail.values[0]
            assert isinstance(first, ast.FormattedValue), (
                "the label must be the FIRST token of detail; the f-string opens with literal text")
        else:
            pytest.fail("vol_death detail is %s, which this contract cannot verify statically; "
                        "keep it a %%-format or an f-string opening with the label"
                        % type(detail).__name__)


def test_every_witness_label_is_a_single_token():
    """`first token` is only well defined if no label contains whitespace.

    Applies to RETIRED labels too: a historical audit row is parsed by the same split.
    """
    for label in sorted(WITNESS_LABELS):
        assert " " not in label and "\t" not in label, \
            "label %r contains whitespace, so a first-token split cannot recover it" % label


def test_every_CURRENT_label_is_actually_produced():
    """A label the witness no longer returns belongs in RETIRED, not in the live set — otherwise
    the contract vouches for a verdict nothing can emit."""
    src = code_of(WITNESS)
    for label in sorted(CURRENT_LABELS):
        assert label in src, "label %r is no longer produced by the witness" % label


def test_a_retired_label_is_NOT_still_produced():
    """The other direction. If a retired token reappears in the source, the split is wrong and a
    reader keying on eras would silently mix them."""
    src = code_of(WITNESS)
    for label in sorted(RETIRED_LABELS):
        assert label not in src, (
            "%r is marked retired but the witness still returns it" % label)


def test_the_guard_itself_can_fail():
    """Reorder the format string and the contract must go red. A guard proved only in the green
    direction is a guard nobody has tested."""
    bad = ast.parse('m.record_exit_trigger_audit(conn, reason="vol_death",\n'
                    '    detail="silence=%.1fs %s" % (s, why))\n')
    details = _vol_death_audit_details(bad)
    assert len(details) == 1
    fmt = details[0].left
    assert not fmt.value.startswith("%s"), "the mutation should violate the contract"

    good = ast.parse('m.record_exit_trigger_audit(conn, reason="vol_death",\n'
                     '    detail="%s silence=%.1fs" % (why, s))\n')
    assert _vol_death_audit_details(good)[0].left.value.startswith("%s")
