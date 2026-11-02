"""KSG Phase 6: regression tests for the upsert validator + advisory checks.

These lock the validator's behavior so a future edit to the vocabulary,
required fields, or forbidden phrases does not silently regress.

The upsert *write* path is not exercised here — that requires live
Supabase access and is covered by the audit-log review on first real
production run. These tests cover validate_draft() pure behavior +
the advisory validator's static checks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# --------------------------------------------------------------------
# scripts/knowledge_sync.py — validate_draft
# --------------------------------------------------------------------

@pytest.fixture
def ks_module():
    """Load scripts/knowledge_sync.py as a module."""
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "scripts" / "knowledge_sync.py"
    spec = importlib.util.spec_from_file_location("knowledge_sync", p)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_empty_draft_passes_validation(ks_module):
    errors = ks_module.validate_draft({"entries": [], "supersessions": []})
    assert errors == []


def test_valid_statuses_matches_helper_vocabulary(ks_module):
    """knowledge_sync.VALID_STATUSES and bot.supabase_helper.VALID_STATUSES
    must agree byte-for-byte. Drift here is the bug-class that lets the
    validator accept entries the writer/DB then rejects."""
    import bot.supabase_helper as sb
    assert ks_module.VALID_STATUSES == sb.VALID_STATUSES


def test_validate_draft_accepts_retired_status(ks_module):
    """`retired` was missing from knowledge_sync.VALID_STATUSES; a draft
    promoting an entry to retired would falsely flag as invalid here while
    the writer accepted it. The widened vocabulary fixes this asymmetry."""
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/wallet/example",
            "entry_type": "finding",
            "description": "d",
            "content": "c",
            "status": "retired",
        }],
    })
    assert not any("status" in e for e in errors), errors


def test_validate_draft_rejects_unknown_status(ks_module):
    """Widening must not become anything-goes — unknown statuses still flag."""
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/wallet/example",
            "entry_type": "finding",
            "description": "d",
            "content": "c",
            "status": "completely-made-up",
        }],
    })
    assert any("status" in e for e in errors), errors


def test_missing_required_fields_flagged(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{"name": "finding/foo", "entry_type": "finding"}],
    })
    # Missing description + content
    assert any("description" in e for e in errors)
    assert any("content" in e for e in errors)


def test_invalid_entry_type_flagged(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "INVALID_TYPE",
            "description": "x",
            "content": "y",
        }],
    })
    assert any("entry_type" in e and "vocabulary" in e for e in errors)


def test_all_13_entry_types_accepted(ks_module):
    """Lock the §5.2 vocabulary at exactly 13 values."""
    vocab = {
        "doctrine", "architecture", "workflow", "test_command",
        "bug_lesson", "semantic_fix", "wallet_insight", "strategy_thesis",
        "simulation_candidate", "legacy_retirement", "open_question",
        "research_note", "finding",
    }
    assert ks_module.VALID_ENTRY_TYPES == vocab
    for et in vocab:
        errors = ks_module.validate_draft({
            "entries": [{
                "name": f"finding/test-{et}",
                "entry_type": et,
                "description": "x",
                "content": "y",
            }],
        })
        assert errors == [], f"entry_type {et!r} should be valid but got: {errors}"


def test_active_with_superseded_by_is_invariant_violation(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "status": "active",
            "superseded_by": "finding/bar",
        }],
    })
    assert any("active-supersession invariant" in e for e in errors)


def test_superseded_without_target_is_orphan(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "status": "superseded",
        }],
    })
    assert any("orphan supersession" in e for e in errors)


def test_name_with_spaces_flagged(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding has spaces",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
        }],
    })
    assert any("slug" in e.lower() for e in errors)


def test_uppercase_in_name_flagged(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/Wallet-Foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
        }],
    })
    assert any("slug" in e.lower() or "lowercase" in e.lower() for e in errors)


def test_forbidden_framing_flagged(ks_module):
    for phrase in (
        "copy this wallet immediately",
        "recommended trigger: BUY",
        "expected pnl is +20%",
        "this is a buy signal",
        "this is a sell signal",
        "deployable trigger",
    ):
        errors = ks_module.validate_draft({
            "entries": [{
                "name": "finding/foo",
                "entry_type": "finding",
                "description": "x",
                "content": phrase,
            }],
        })
        assert any("forbidden framing" in e for e in errors), (
            f"phrase {phrase!r} should have been flagged but wasn't; errors={errors}"
        )


def test_forbidden_framing_in_summary_too(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "benign body",
            "summary": "copy this wallet at insider score 90+",
        }],
    })
    assert any("forbidden framing" in e for e in errors)


def test_duplicate_names_in_same_draft_flagged(ks_module):
    errors = ks_module.validate_draft({
        "entries": [
            {"name": "finding/foo", "entry_type": "finding", "description": "x", "content": "a"},
            {"name": "finding/foo", "entry_type": "finding", "description": "x", "content": "b"},
        ],
    })
    assert any("duplicate" in e.lower() for e in errors)


def test_supersession_missing_old_slug_flagged(ks_module):
    errors = ks_module.validate_draft({
        "entries": [],
        "supersessions": [{"new_slug": "finding/new"}],
    })
    assert any("old_slug" in e for e in errors)


def test_supersession_missing_new_slug_flagged(ks_module):
    errors = ks_module.validate_draft({
        "entries": [],
        "supersessions": [{"old_slug": "finding/old"}],
    })
    assert any("new_slug" in e for e in errors)


def test_tags_must_be_list_of_strings(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "tags": "not-a-list",
        }],
    })
    assert any("tags" in e for e in errors)


def test_source_pr_must_be_int(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "source_pr": "42",  # string, should be int
        }],
    })
    assert any("source_pr" in e for e in errors)


def test_good_entry_passes(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/wallet-protocol/test-2026-05-15",
            "entry_type": "finding",
            "description": "Test finding",
            "content": "Detailed body for the finding.",
            "status": "active",
            "tags": ["wallet-protocol", "test"],
            "source_branch": "feat/test",
            "source_pr": 42,
            "confidence": "medium",
        }],
    })
    assert errors == []


def test_status_active_no_superseded_by_passes(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "status": "active",
        }],
    })
    assert errors == []


def test_status_superseded_with_target_passes(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "status": "superseded",
            "superseded_by": "finding/new",
        }],
    })
    assert errors == []


# --------------------------------------------------------------------
# Edge-case audit fixes (added after the 2026-05-15 audit found gaps)
# --------------------------------------------------------------------

def test_tags_empty_string_rejected(ks_module):
    """Empty-string tag entries are noise; reject so upstream doesn't propagate them."""
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "tags": ["wallet-protocol", ""],
        }],
    })
    assert any("empty-string" in e for e in errors), errors


def test_tags_whitespace_only_rejected(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "tags": ["   "],
        }],
    })
    assert any("empty-string" in e for e in errors)


def test_source_pr_negative_rejected(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "source_pr": -1,
        }],
    })
    assert any("positive" in e for e in errors)


def test_source_pr_zero_rejected(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "source_pr": 0,
        }],
    })
    assert any("positive" in e for e in errors)


def test_source_pr_bool_rejected(ks_module):
    """bool is a subclass of int in Python — explicit guard so True doesn't pass as PR #1."""
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/foo",
            "entry_type": "finding",
            "description": "x",
            "content": "y",
            "source_pr": True,
        }],
    })
    assert any("integer" in e for e in errors)


def test_self_supersession_rejected(ks_module):
    errors = ks_module.validate_draft({
        "entries": [],
        "supersessions": [{"old_slug": "finding/foo", "new_slug": "finding/foo"}],
    })
    assert any("self-supersession" in e for e in errors)


def test_duplicate_old_slug_in_supersessions_rejected(ks_module):
    errors = ks_module.validate_draft({
        "entries": [
            {"name": "finding/new1", "entry_type": "finding", "description": "x", "content": "y"},
            {"name": "finding/new2", "entry_type": "finding", "description": "x", "content": "y"},
        ],
        "supersessions": [
            {"old_slug": "finding/old", "new_slug": "finding/new1"},
            {"old_slug": "finding/old", "new_slug": "finding/new2"},
        ],
    })
    assert any("multiple supersessions" in e for e in errors)


def test_chained_supersession_in_same_draft_rejected(ks_module):
    """An entry that's both a new active row AND the old side of a supersession is contradictory."""
    errors = ks_module.validate_draft({
        "entries": [
            {"name": "finding/b", "entry_type": "finding", "description": "x", "content": "y"},
            {"name": "finding/c", "entry_type": "finding", "description": "x", "content": "y"},
        ],
        "supersessions": [
            {"old_slug": "finding/a", "new_slug": "finding/b"},
            {"old_slug": "finding/b", "new_slug": "finding/c"},  # finding/b can't be both active and superseded
        ],
    })
    assert any("chained supersession" in e for e in errors), errors


def test_forbidden_phrase_in_description_now_flagged(ks_module):
    """Pre-audit version only scanned content/summary. Now description is included."""
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/ok",
            "entry_type": "finding",
            "description": "copy this wallet",  # forbidden, only in description
            "content": "benign body",
        }],
    })
    assert any("forbidden framing" in e for e in errors)


# --------------------------------------------------------------------
# Validator (Phase 5) edge-case fixes
# --------------------------------------------------------------------

def test_pr_body_untouched_template_fails(vks_module, monkeypatch):
    """If the operator pasted the template but didn't fill it in, the validator must reject."""
    body = """## Summary
foo

## Knowledge Sync

1. Did this branch change project doctrine? [y/n] evidence:
2. Did it add or retire a module? [y/n] evidence:
3. Did it change behavior? [y/n] evidence:

**Delta:** yes | none
**Reason (if none):** ___

**Attestation:** I ran the 11-question Knowledge Delta Report.
"""
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert not result["ok"]
    assert result["template_untouched"]
    assert result["unfilled_placeholder_count"] >= 3


def test_pr_body_partial_template_still_flagged(vks_module, monkeypatch):
    """Even if operator answered some but left others as [y/n], catch it."""
    body = """## Knowledge Sync
1. [y] evidence: real
2. [y/n] evidence:
3. [y/n] evidence:
4. [y/n] evidence:

**Delta:** yes
**Attestation:** done
"""
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert not result["ok"]


def test_pr_body_well_filled_passes(vks_module, monkeypatch):
    """Fully-answered template passes."""
    body = """## Knowledge Sync
1. [n] evidence: nothing
2. [n] evidence: nothing
3. [n] evidence: nothing

**Delta:** none
**Reason (if none):** test-only

**Attestation:** I ran the 11-question Knowledge Delta Report.
"""
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert result["ok"]


def test_pr_body_unfilled_delta_alone_fails(vks_module, monkeypatch):
    """If 'Delta: yes | none' is left as the literal template, that alone is enough to fail."""
    body = """## Knowledge Sync
1. [y] evidence: real
2. [y] evidence: real

**Delta:** yes | none

**Attestation:** done
"""
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert not result["ok"]
    assert result["unfilled_delta"]


# --------------------------------------------------------------------
# Code-fence stripping — closes D9/F5 limitation
# --------------------------------------------------------------------

@pytest.fixture
def ksg_text_module():
    """Load bot/ksg_text.py."""
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "bot" / "ksg_text.py"
    spec = importlib.util.spec_from_file_location("ksg_text", p)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_strip_fenced_code_basic_backticks(ksg_text_module):
    text = """Outside the fence.
```
inside the fence
also inside
```
Outside again."""
    stripped = ksg_text_module.strip_fenced_code(text)
    assert "Outside the fence." in stripped
    assert "Outside again." in stripped
    assert "inside the fence" not in stripped
    assert "also inside" not in stripped


def test_strip_fenced_code_with_info_string(ksg_text_module):
    text = """before
```python
def copy_this_wallet(): pass
```
after"""
    stripped = ksg_text_module.strip_fenced_code(text)
    assert "before" in stripped
    assert "after" in stripped
    assert "copy_this_wallet" not in stripped


def test_strip_fenced_code_tilde_fence(ksg_text_module):
    text = """outside
~~~
inside tilde
~~~
outside"""
    stripped = ksg_text_module.strip_fenced_code(text)
    assert stripped.count("outside") == 2
    assert "inside tilde" not in stripped


def test_strip_fenced_code_preserves_line_count(ksg_text_module):
    text = "a\nb\n```\nc\nd\n```\ne\n"
    stripped = ksg_text_module.strip_fenced_code(text)
    assert stripped.count("\n") == text.count("\n")


def test_strip_fenced_code_mixed_fence_chars_dont_close(ksg_text_module):
    """A `~~~` does not close a ```-opened fence."""
    text = """before
```
inside
~~~
still inside
```
after"""
    stripped = ksg_text_module.strip_fenced_code(text)
    assert "before" in stripped
    assert "after" in stripped
    assert "inside" not in stripped
    assert "still inside" not in stripped


def test_strip_fenced_code_longer_closing_fence(ksg_text_module):
    """Per CommonMark, closing fence ≥ opening length is valid."""
    text = """before
```
inside
``````
after"""
    stripped = ksg_text_module.strip_fenced_code(text)
    assert "before" in stripped
    assert "after" in stripped
    assert "inside" not in stripped


def test_strip_fenced_code_handles_empty(ksg_text_module):
    assert ksg_text_module.strip_fenced_code("") == ""
    assert ksg_text_module.strip_fenced_code("no fences here") == "no fences here"


def test_pr_body_fenced_marker_does_not_count(vks_module, monkeypatch):
    """A `## Knowledge Sync` heading inside a code fence is documentation, not attestation."""
    body = """## Summary

Here's an example of the KSG template:

```
## Knowledge Sync

**Delta:** yes

**Attestation:** ...
```

But this PR has no real attestation block.
"""
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert not result["ok"]
    assert not result["has_marker"]
    assert result.get("fenced_marker_present_but_not_counted") is True


def test_pr_body_real_marker_outside_fence_passes(vks_module, monkeypatch):
    """A real KSG block outside any fence still passes (no false-negative)."""
    body = """## Summary

For reference, the template is:
```
## Knowledge Sync
```

Now the actual attestation:

## Knowledge Sync

1. doctrine? [n] evidence: -

**Delta:** none

**Attestation:** I ran the report.
"""
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert result["ok"], f"Expected pass; got {result}"
    assert result["has_marker"]


# --------------------------------------------------------------------
# Negation context — closes H2 limitation
# --------------------------------------------------------------------

def test_negation_simple_dont(ksg_text_module):
    text = "don't copy this wallet".lower()
    idx = text.index("copy this wallet")
    assert ksg_text_module.phrase_has_preceding_negation(text, idx) is True


def test_negation_must_not(ksg_text_module):
    text = "you must not copy this wallet now".lower()
    idx = text.index("copy this wallet")
    assert ksg_text_module.phrase_has_preceding_negation(text, idx) is True


def test_negation_never(ksg_text_module):
    text = "we should never copy this wallet".lower()
    idx = text.index("copy this wallet")
    assert ksg_text_module.phrase_has_preceding_negation(text, idx) is True


def test_negation_long_distance_in_same_sentence(ksg_text_module):
    """The AGENTS.md case: negation is 10+ tokens before the match."""
    text = "do not frame analyzer output as copy-trading advice, deployable triggers, expected pnl, sizing, or live-trading recommendations.".lower()
    idx = text.index("expected pnl")
    assert ksg_text_module.phrase_has_preceding_negation(text, idx) is True


def test_negation_does_not_cross_sentence_boundary(ksg_text_module):
    """A negation in the PREVIOUS sentence does not cancel a positive assertion in the next."""
    text = "we do not endorse this. but copy this wallet anyway.".lower()
    idx = text.index("copy this wallet")
    assert ksg_text_module.phrase_has_preceding_negation(text, idx) is False


def test_negation_positive_assertion_is_flagged(ksg_text_module):
    text = "you should copy this wallet immediately".lower()
    idx = text.index("copy this wallet")
    assert ksg_text_module.phrase_has_preceding_negation(text, idx) is False


def test_negation_forbidden_word_in_sentence(ksg_text_module):
    """'forbidden' counts as negation context."""
    text = "the following is forbidden: copy this wallet".lower()
    idx = text.index("copy this wallet")
    assert ksg_text_module.phrase_has_preceding_negation(text, idx) is True


def test_negation_at_start_of_text(ksg_text_module):
    """No preceding context → no negation."""
    text = "copy this wallet right now".lower()
    idx = 0
    assert ksg_text_module.phrase_has_preceding_negation(text, idx) is False


def test_validator_doesnt_flag_negated_phrase_in_entry(ks_module):
    """End-to-end: a memory entry that NEGATES a forbidden phrase should pass."""
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/doctrine",
            "entry_type": "doctrine",
            "description": "Wallet analyzer doctrine",
            "content": (
                "Do not frame the analyzer's output as copy-trading "
                "advice, deployable triggers, expected PnL, or sizing."
            ),
        }],
    })
    assert errors == [], f"Negated forbidden phrases should not be flagged; got: {errors}"


def test_validator_does_flag_positive_assertion_of_forbidden(ks_module):
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/bad",
            "entry_type": "finding",
            "description": "x",
            "content": "We will copy this wallet and the expected pnl is +20%.",
        }],
    })
    assert any("forbidden framing" in e for e in errors)
    # Two distinct forbidden phrases should each be flagged.
    flagged_phrases = [e for e in errors if "forbidden framing" in e]
    assert len(flagged_phrases) >= 2


def test_validator_doesnt_flag_phrase_in_fenced_code(ks_module):
    """A forbidden phrase inside a code fence in entry content is an example, not an assertion."""
    body = (
        "Example of an anti-pattern that violates our doctrine:\n\n"
        "```\n"
        "copy this wallet\n"
        "```\n\n"
        "This is something we explicitly avoid."
    )
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/example",
            "entry_type": "finding",
            "description": "x",
            "content": body,
        }],
    })
    assert errors == [], f"Phrase in code fence shouldn't flag; got: {errors}"


def test_validator_flags_phrase_outside_fence_even_if_fence_present(ks_module):
    """If the phrase is both inside a fence AND outside (as an assertion), still flag."""
    body = (
        "We will copy this wallet immediately.\n\n"
        "```\n"
        "An example of the phrasing: copy this wallet\n"
        "```\n"
    )
    errors = ks_module.validate_draft({
        "entries": [{
            "name": "finding/mixed",
            "entry_type": "finding",
            "description": "x",
            "content": body,
        }],
    })
    assert any("forbidden framing" in e for e in errors)


# --------------------------------------------------------------------
# scripts/validate_knowledge_sync.py — advisory checks
# --------------------------------------------------------------------

@pytest.fixture
def vks_module():
    """Load scripts/validate_knowledge_sync.py as a module."""
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "scripts" / "validate_knowledge_sync.py"
    spec = importlib.util.spec_from_file_location("validate_knowledge_sync", p)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_local_files_exist_check_passes(vks_module):
    result = vks_module.check_local_files_exist()
    assert result["ok"], f"Required local files missing: {result['missing']}"


def test_no_forbidden_phrases_in_main_files(vks_module):
    """If this ever fails, doctrine drift has occurred and must be reviewed."""
    result = vks_module.check_no_forbidden_phrases()
    assert result["ok"], f"Forbidden current-truth phrases found: {result['findings']}"


def test_forbidden_phrases_list_locked_to_spec(vks_module):
    """Lock the §7 phrase list to prevent quiet weakening."""
    expected = {
        "wallet analyzer is a copy bot",
        "copy this wallet",
        "recommended trigger",
        "expected pnl",
        "insider_score is source of truth",
        "low_buy_trades is the primary stream",
    }
    assert set(vks_module.FORBIDDEN_CURRENT_PHRASES) == expected


def test_pr_body_check_with_well_formed_block(vks_module, monkeypatch):
    body = """## Summary
foo

## Knowledge Sync

1. Did this branch change project doctrine? [n] evidence: -

**Delta:** none
**Reason (if none):** test-only

**Attestation:** I ran the 11-question Knowledge Delta Report.
"""
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert result["ok"], f"Expected pass; got: {result}"


def test_pr_body_check_missing_block_fails(vks_module, monkeypatch):
    body = "## Summary\nNo KSG block at all.\n"
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert not result["ok"]
    assert not result["has_marker"]


def test_pr_body_check_block_without_attestation_fails(vks_module, monkeypatch):
    body = """## Summary
foo

## Knowledge Sync

1. Did this branch change project doctrine? [n] evidence: -
"""
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = vks_module.check_pr_body_has_ksg_block()
    assert not result["ok"]
    assert result["has_marker"]
    assert not result["has_attestation"]


def test_schema_validator_runs(vks_module):
    """Delegated check should not crash and should report ok status."""
    result = vks_module.check_schema_validator()
    # Only assert it ran without exception; it's allowed to fail if real
    # violations exist, but those would be unrelated to KSG.
    assert "check" in result
    assert "ok" in result
