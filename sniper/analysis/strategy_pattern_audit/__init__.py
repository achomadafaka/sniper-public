"""Strategy Pattern Audit Phase 1 public API."""
from sniper.analysis.strategy_pattern_audit.core import compute_strategy_pattern_audit
from sniper.analysis.strategy_pattern_audit.dossier_adapter import (
    compute_dossier_strategy_pattern_audit,
)
from sniper.analysis.strategy_pattern_audit.rules import RuleEvaluation, ThesisRule
from sniper.analysis.strategy_pattern_audit.seed_rules import default_seed_rules
from sniper.analysis.strategy_pattern_audit.validation_workflow import (
    build_thesis_validation_workflow,
)

__all__ = [
    "RuleEvaluation",
    "ThesisRule",
    "build_thesis_validation_workflow",
    "compute_dossier_strategy_pattern_audit",
    "compute_strategy_pattern_audit",
    "default_seed_rules",
]
