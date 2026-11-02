from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_inject_app_brain_documents_wallet_quality_and_schedule():
    text = _read("scripts/inject_app_brain.py")
    assert '"name": "wallet_scoring_system"' in text
    assert "wallet_quality_score" in text
    assert "gate0_status" in text
    assert '"name": "autonomous_agent_schedule"' in text
    assert "fast_ooda" in text
    assert "event_trigger.py" in text
    assert '"name": "wallet_analysis_pipeline"' in text
    assert '"name": "strategy_runner_position_management"' in text


def test_enrich_brain_uses_two_score_wallet_language():
    text = _read("scripts/enrich_brain.py")
    assert 'name="wallet_scoring_system"' in text
    assert "wallet_quality_score" in text
    assert "position_insider_score remains for entry timing" in text
    assert 'name="autonomous_agent_schedule"' in text
    assert 'name="wallet_analysis_pipeline"' in text
    assert 'name="strategy_runner_position_management"' in text
