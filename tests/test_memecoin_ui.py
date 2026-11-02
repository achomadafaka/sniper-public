from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_real_position_cards_surface_funded_contract_decision_fields():
    js = (ROOT / "static" / "app.js").read_text()
    for label in ("TYPE", "STATE", "WHY", "EXPECT", "INVALID", "SIZE", "POLICY"):
        assert label in js
    assert "entry_contract" in js
    assert "effective_exit_policy" in js
    assert "legacy policy" in js
    assert "unknown" in js
