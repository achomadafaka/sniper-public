from __future__ import annotations

from pathlib import Path

from agents import brain_hygiene_agent as bha


def test_scan_text_flags_legacy_insider_score_without_applying():
    proposals = bha._scan_text(
        "memory",
        "knowledge:old-wallet-model",
        "This finding says high insider_score wallets are a copy edge.",
        "active",
    )

    assert len(proposals) == 1
    assert proposals[0].proposed_status == "superseded"
    assert proposals[0].reason_code == "legacy_wallet_insider_score"
    assert "archetype" in (proposals[0].superseded_by or "")


# ---------------------------------------------------------------------------
# Recurring false-positive: slug substring match flags active strategies.
#
# `_retired_strategy_slugs()` collects slugs from knowledge/strategies/retired/
# (e.g. 002-insider_copy_v1.md → slugs {"002-insider_copy_v1", "insider_copy_v1"}).
# The scanner then flags any file whose stem contains any retired slug as a
# substring. That fires on `037-insider_copy_v1.md` (Strategy #37 Scout: cashy,
# legitimately ACTIVE) because `insider_copy_v1` is also a plugin TYPE shared
# across many strategies. Two hygiene passes have caught and rejected this
# false-positive; fix the heuristic so it self-resolves.
#
# Fix: skip the retired-slug match when the file declares itself active —
# either by directory location (`knowledge/strategies/active/`) or by a
# `**Status:** active` line in the body. Files in `knowledge/strategies/retired/`
# are also skipped because they're already retired (no proposal needed).
# ---------------------------------------------------------------------------


def test_strategy_file_in_active_dir_self_declares_active():
    """Path-based signal: a file under knowledge/strategies/active/ is
    authoritatively active by location."""
    assert bha._strategy_file_self_declares_active(
        Path("knowledge/strategies/active/037-insider_copy_v1.md"),
        "irrelevant content",
    )


def test_strategy_file_in_retired_dir_treated_as_already_retired():
    """Already-retired files must not be re-flagged: they're literally
    in retired/ already, so a `retired_or_deleted_artifact` proposal
    against them is noise."""
    assert bha._strategy_file_self_declares_active(
        Path("knowledge/strategies/retired/002-insider_copy_v1.md"),
        "irrelevant content",
    )


def test_strategy_file_status_active_field_overrides_slug_match():
    """Content signal: a body containing `**Status:** active` (markdown
    bold or plain) suppresses the retired-slug match even when the
    path doesn't help."""
    body = (
        "# Strategy #37 — Scout: cashy\n\n"
        "**Type:** `insider_copy_v1`\n"
        "**Status:** active  •  **Lifecycle:** paper\n"
    )
    assert bha._strategy_file_self_declares_active(
        Path("knowledge/research/some/unrelated/path.md"),
        body,
    )


def test_strategy_file_with_no_active_signal_does_not_self_declare():
    """Regression: a file with no path signal and no Status: active body
    must NOT short-circuit the retired-slug check. The substring
    heuristic remains useful for catching renamed/retired artifacts."""
    body = "Some doc that happens to mention insider_copy_v1 in passing.\n"
    assert not bha._strategy_file_self_declares_active(
        Path("docs/audits/2026-03-15-some-audit.md"),
        body,
    )


def test_status_retired_field_does_not_count_as_active():
    """A `**Status:** retired` line must not be misread as active just
    because the regex hits the word `status`."""
    body = (
        "**Type:** `wallet_scout`\n"
        "**Status:** retired  •  reason: dead-ended\n"
    )
    assert not bha._strategy_file_self_declares_active(
        Path("knowledge/strategies/some/path.md"),
        body,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_scan_files_skips_active_strategy_even_if_slug_matches_retired(monkeypatch, tmp_path):
    """End-to-end: an active-dir file with a slug-substring match
    against a retired strategy must not appear in the proposal list.
    This pins the actual scanner output, not just the helper."""
    monkeypatch.setattr(bha, "ROOT", tmp_path)
    monkeypatch.setattr(
        bha, "SCAN_DIRS",
        (tmp_path / "knowledge", tmp_path / "docs" / "research", tmp_path / "docs" / "audits"),
    )
    monkeypatch.setattr(bha, "REFERENCE_FILES", ())

    _write(
        tmp_path / "knowledge" / "strategies" / "retired" / "002-insider_copy_v1.md",
        "# Strategy #2 — retired insider_copy_v1\n**Status:** retired\n",
    )
    _write(
        tmp_path / "knowledge" / "strategies" / "active" / "037-insider_copy_v1.md",
        "# Strategy #37 — Scout: cashy\n**Type:** `insider_copy_v1`\n**Status:** active\n",
    )

    retired_slugs = bha._retired_strategy_slugs()
    proposals, _ = bha._scan_files(retired_slugs, deleted=set())

    bad = [
        p for p in proposals
        if p.reason_code == "retired_or_deleted_artifact"
        and "active/037-insider_copy_v1.md" in p.identifier
    ]
    assert not bad, (
        f"active strategy with matching plugin-type slug must not be "
        f"flagged retired; got: {bad}"
    )


def test_scan_files_skips_files_already_in_retired_dir(monkeypatch, tmp_path):
    """A file literally located in `knowledge/strategies/retired/` is
    already retired; the scanner must not propose retired status for
    it. Same Pattern B noise the human caught in earlier passes."""
    monkeypatch.setattr(bha, "ROOT", tmp_path)
    monkeypatch.setattr(
        bha, "SCAN_DIRS",
        (tmp_path / "knowledge", tmp_path / "docs" / "research", tmp_path / "docs" / "audits"),
    )
    monkeypatch.setattr(bha, "REFERENCE_FILES", ())

    _write(
        tmp_path / "knowledge" / "strategies" / "retired" / "002-insider_copy_v1.md",
        "# Strategy #2\n**Status:** retired\n",
    )

    retired_slugs = bha._retired_strategy_slugs()
    proposals, _ = bha._scan_files(retired_slugs, deleted=set())

    bad = [
        p for p in proposals
        if p.reason_code == "retired_or_deleted_artifact"
        and "retired/002-insider_copy_v1.md" in p.identifier
    ]
    assert not bad, (
        f"files already in retired/ must not get a retired_or_deleted_artifact "
        f"proposal; got: {bad}"
    )


def test_scan_files_still_flags_renamed_retired_artifact_without_status(monkeypatch, tmp_path):
    """Regression: the slug-substring heuristic still does useful work.
    A file with a retired-slug substring in its stem, no `Status:` line,
    and outside the strategies/active subtree must still be flagged."""
    monkeypatch.setattr(bha, "ROOT", tmp_path)
    monkeypatch.setattr(
        bha, "SCAN_DIRS",
        (tmp_path / "knowledge", tmp_path / "docs" / "research", tmp_path / "docs" / "audits"),
    )
    monkeypatch.setattr(bha, "REFERENCE_FILES", ())

    _write(
        tmp_path / "knowledge" / "strategies" / "retired" / "wallet-scout-v1.md",
        "# retired wallet_scout_v1\n",
    )
    _write(
        tmp_path / "docs" / "research" / "old-wallet-scout-v1-notes.md",
        "Notes referencing wallet-scout-v1 with no status declaration.\n",
    )

    retired_slugs = bha._retired_strategy_slugs()
    proposals, _ = bha._scan_files(retired_slugs, deleted=set())

    hits = [
        p for p in proposals
        if p.reason_code == "retired_or_deleted_artifact"
        and "old-wallet-scout-v1-notes.md" in p.identifier
    ]
    assert hits, (
        f"slug-substring heuristic must still flag renamed retired artifacts "
        f"that lack an active self-declaration; got: {proposals}"
    )


def test_run_is_proposal_only(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bha,
        "_scan_memories",
        lambda: (
            [
                bha.Proposal(
                    target_kind="memory",
                    identifier="knowledge:legacy",
                    current_status="active",
                    proposed_status="retired",
                    reason_code="hl_liquidation_retired",
                    reason="retired direction",
                    evidence="HL liquidation",
                )
            ],
            1,
        ),
    )
    monkeypatch.setattr(bha, "_scan_files", lambda retired, deleted: ([], 0))
    monkeypatch.setattr(bha, "_retired_strategy_slugs", lambda: set())
    monkeypatch.setattr(bha, "_deleted_files", lambda: set())
    monkeypatch.setattr(bha, "_load_reference_summary", lambda: [])
    monkeypatch.setattr(bha, "_current_commit", lambda: "abc123")

    writes = []
    monkeypatch.setattr(bha, "_write_finding", lambda proposals, output_path: writes.append((proposals, output_path)))

    out = tmp_path / "brain_hygiene.json"
    report = bha.run(write_finding=True, output_path=out)

    assert report["mode"] == "proposal_only"
    assert report["proposals"][0]["proposed_status"] == "retired"
    assert out.exists()
    assert writes and writes[0][0][0].identifier == "knowledge:legacy"
