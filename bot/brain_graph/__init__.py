"""The Supabase brain as a verified knowledge graph (spec 2026-09-28). Never imported by sniper/."""
KINDS = ("rule", "trap", "decision", "settled", "finding", "measurement", "task", "observation")
GROUPS = {"normative": ("rule", "trap", "decision", "settled"),
          "descriptive": ("finding", "measurement"),
          "task": ("task",), "observation": ("observation",)}
STATUSES = ("unverified", "verified", "verified_partial", "disputed", "superseded", "retracted",
            "refuted", "evidence_broken")
READ_STATUSES = ("unverified", "verified", "verified_partial", "disputed", "evidence_broken")
VERDICTS = ("duplicate", "refines", "supersedes", "contradicts", "unrelated")
# These lists mirror the database CHECKs so a refusal is explained before a round trip.
AUTHORS = ("alpha", "beta", "gamma", "lambda", "codex", "delta", "epsilon", "zeta", "operator", "cron", "import")
VERIFIERS = ("alpha", "beta", "gamma", "lambda", "codex", "delta", "epsilon", "zeta", "operator")
ENTITY_TYPES = ("actor", "chain", "commit", "process", "exit_rail", "venue", "path", "provider",
                "wallet", "knob", "lane", "table", "column", "position", "gate", "spec", "audit",
                "task", "release", "mint", "doctrine", "credential_id", "tool", "selftest")


def group_of(kind: str) -> str:
    for name, kinds in GROUPS.items():
        if kind in kinds:
            return name
    raise ValueError(f"unknown kind {kind!r}")
