"""Agent prompt contract for source-traced numeric claims."""

from pathlib import Path


AGENT_FILES = [
    Path(".claude/agents/feature-research-analyst.md"),
    Path(".claude/agents/wallet-dossier-writer.md"),
]


def test_agents_require_source_trace_columns_for_cited_numbers():
    for path in AGENT_FILES:
        text = path.read_text(encoding="utf-8")
        assert "source_trace" in text
        assert "field name" in text
        assert "row count" in text
        assert "filter" in text
        assert "Every cited number" in text
        for column in (
            "claim",
            "field_name",
            "row_count",
            "tool_or_query",
            "as_of",
        ):
            assert column in text
