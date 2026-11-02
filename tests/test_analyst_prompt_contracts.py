from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FINANCE = ROOT / "sniper" / "services" / "finance_analyst.py"
WORLD = ROOT / "scripts" / "world_analyst.py"


def test_finance_prompt_defines_ui_facing_event_title_contract():
    src = FINANCE.read_text()
    assert "EVENT TITLE CONTRACT:" in src
    assert 'Good: "Fed Hold Risk", "OPEC Supply Cut", "ETF Flows Slow"' in src
    assert 'Bad: "Federal Reserve signals rates may stay higher for longer"' in src


def test_finance_prompt_defines_canonical_report_headings():
    src = FINANCE.read_text()
    assert "Short heading labels only — the UI parses these headings directly" in src
    assert "## EXECUTIVE SUMMARY: What changed" in src
    assert "## MARKET IMPLICATIONS: Position impact" in src
    assert "## SOURCE CREDIBILITY: Source audit" in src
    assert "Write for dashboard delivery, not essay completeness" in src


def test_finance_prefetch_reads_report_delivery_contract():
    src = FINANCE.read_text()
    assert '("intel/report_delivery_contract","reference", 1200)' in src


def test_finance_prompt_requires_temp_report_and_exact_calendar_verify_times():
    src = FINANCE.read_text()
    assert "/tmp/markets_state_report.txt" in src
    assert '"source": "finance_analyst"' in src
    assert "--verify-at" in src


def test_world_prompt_defines_compact_event_title_contract():
    src = WORLD.read_text()
    assert "EVENT TITLE CONTRACT:" in src
    assert 'Prefer "Iran-US Talks", "Russia-Ukraine", "US Tariffs", "China Deploys"' in src
    assert 'Avoid "Iran-US War Day 28: +1" or "Oil Hits 12.57 Brent"' in src


def test_world_prompt_defines_briefing_headings_for_world_narrative():
    src = WORLD.read_text()
    assert "Short heading labels only — keep them stable for downstream readers" in src
    assert "## EXECUTIVE SUMMARY: What changed" in src
    assert "## MARKET IMPLICATIONS: Position impact" in src
    assert "## DIVERSITY: Coverage balance" in src
    assert "Write for briefing delivery, not essay completeness" in src


def test_world_prefetch_reads_report_delivery_contract():
    src = WORLD.read_text()
    assert '("intel/report_delivery_contract", "reference", 1200)' in src


def test_world_prompt_requires_temp_recovery_files_and_exact_calendar_verify_times():
    src = WORLD.read_text()
    assert "/tmp/world_state_report.json" in src
    assert "/tmp/world_narrative_report.md" in src
    assert "--verify-at" in src


def test_world_prompt_uses_touch_reviewed_refresh_flow():
    src = WORLD.read_text()
    assert "--older-than-hours 48" in src
    assert "--exclude-prefix fin:" in src
    assert "--touch-reviewed" in src
    assert "Do not re-run" in src


def test_finance_prompt_uses_touch_reviewed_refresh_flow():
    src = FINANCE.read_text()
    assert "STEP 3.5: REFRESH STALE FINANCE EVENTS" in src
    assert "--slug-prefix fin:" in src
    assert "--touch-reviewed" in src
    assert "Do not re-run" in src
