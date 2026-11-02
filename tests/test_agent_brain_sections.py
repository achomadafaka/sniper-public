# tests/test_agent_brain_sections.py
"""Verify agent brain section boundaries — each agent writes to its own
section and doesn't modify others'."""

import json
import sys
import types
import pytest


@pytest.fixture(autouse=True)
def mock_claude_agent_sdk():
    """Mock claude_agent_sdk so agent modules can be imported in test env."""
    mod_name = "claude_agent_sdk"
    if mod_name not in sys.modules:
        mock_mod = types.ModuleType(mod_name)
        mock_mod.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {})
        mock_mod.ResultMessage = type("ResultMessage", (), {})
        mock_mod.SystemMessage = type("SystemMessage", (), {})
        mock_mod.query = lambda *a, **kw: None
        sys.modules[mod_name] = mock_mod
        yield
        # Clean up the agents from sys.modules so the mock doesn't leak
        to_remove = [k for k in sys.modules if k.startswith("agents.") and k != "agents.tools.domain_query"]
        for k in to_remove:
            sys.modules.pop(k, None)
        sys.modules.pop(mod_name, None)
    else:
        yield


class TestDomainMapping:
    """Verify domain_query.py's domain map is complete and correct."""

    def test_geopolitics_domain_exists(self):
        from agents.tools.domain_query import DOMAIN_MAP
        assert "geopolitics" in DOMAIN_MAP
        assert DOMAIN_MAP["geopolitics"]["agent_type"] == "world_analyst"

    def test_finance_domain_exists(self):
        from agents.tools.domain_query import DOMAIN_MAP
        assert "finance" in DOMAIN_MAP
        assert DOMAIN_MAP["finance"]["agent_type"] == "finance_analyst"

    def test_all_domains_have_required_keys(self):
        from agents.tools.domain_query import DOMAIN_MAP
        required = {"brain_section", "agent_type", "obs_strategy"}
        for domain, config in DOMAIN_MAP.items():
            for key in required:
                assert key in config, f"Domain {domain} missing {key}"


class TestAgentPromptBrainMap:
    """Verify each agent's system prompt contains a BRAIN MAP section."""

    def test_wallet_scout_has_brain_map(self):
        from agents.wallet_scout_agent import SYSTEM_PROMPT
        assert "BRAIN MAP" in SYSTEM_PROMPT
        assert "wallets" in SYSTEM_PROMPT.lower()

    def test_wallet_scout_creation_half_is_explicitly_disabled(self):
        from agents.wallet_scout_agent import SYSTEM_PROMPT, build_run_prompt

        assert "Discovery/creation is currently DISABLED" in SYSTEM_PROMPT
        assert "Strategy creation is disabled" in SYSTEM_PROMPT
        assert "Strategy activation is disabled" in SYSTEM_PROMPT
        run_prompt = build_run_prompt()
        assert "no discovery / no creation" in run_prompt
        assert "Do NOT rank candidate wallets" in run_prompt
        assert "--strategy-type insider_copy_v1" not in run_prompt
        assert "action_tool.py activate" not in run_prompt

    def test_insider_copy_has_brain_map(self):
        from agents.insider_copy_agent import SYSTEM_PROMPT
        assert "BRAIN MAP" in SYSTEM_PROMPT
        assert "signals" in SYSTEM_PROMPT.lower()

    def test_insider_copy_prompt_avoids_agent_side_log_run(self):
        from agents.insider_copy_agent import SYSTEM_PROMPT, build_run_prompt

        system_lines = [line.strip() for line in SYSTEM_PROMPT.splitlines()]
        run_lines = [line.strip() for line in build_run_prompt().splitlines()]
        assert "python3 agents/tools/predict_tool.py log-run \\" not in system_lines
        assert "python3 agents/tools/predict_tool.py log-run \\" not in run_lines
        assert "wrapper records finish-run automatically" in SYSTEM_PROMPT
        assert "wrapper finalizes the run automatically via finish-run" in build_run_prompt()

    def test_insider_copy_prompt_requires_literal_run_uuid_not_env_export(self):
        from agents.insider_copy_agent import SYSTEM_PROMPT

        assert "literal run UUID" in SYSTEM_PROMPT
        assert "never pass the literal string \"$RUN_ID\"" in SYSTEM_PROMPT
        assert "exported by the wrapper into your environment" not in SYSTEM_PROMPT

    def test_insider_copy_prediction_example_uses_run_id_and_calibration_note(self):
        from agents.insider_copy_agent import SYSTEM_PROMPT

        anchor = "# Make verifiable predictions during analysis"
        assert anchor in SYSTEM_PROMPT
        snippet = SYSTEM_PROMPT.split(anchor, 1)[1][:600]
        assert "--run-id" in snippet
        assert "--calibration-note" in snippet

    # insider_scoring_analyst archived 2026-07-14 (ADR-006 retired insider_score).
    # Prompt-shape test dropped alongside the source move to archive/legacy/.
    def test_strategy_reviewer_has_brain_map(self):
        from agents.strategy_reviewer_agent import SYSTEM_PROMPT
        assert "BRAIN MAP" in SYSTEM_PROMPT


class TestObservationTypes:
    """Verify obs_tool strategy types match the brain section owners."""

    def test_obs_strategy_types_are_distinct(self):
        """No two agents should write to the same observation strategy_type."""
        strategy_types = [
            "insider_copy_v1",  # insider copy agent
            "wallet_scout",     # wallet scout
            "insider_analyst",  # insider scoring analyst
            "strategy_reviewer", # strategy reviewer
            # world_analyst writes to memories, not observations
        ]
        assert len(strategy_types) == len(set(strategy_types))
