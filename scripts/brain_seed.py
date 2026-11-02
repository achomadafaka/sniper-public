#!/usr/bin/env python3
"""Idempotent seed of the brain's sections, role views and actor entities (spec 1.1, 1.3, R4, R17).

    python3 scripts/brain_seed.py --dry-run   # reads the live brain, prints what it would create
    python3 scripts/brain_seed.py             # authorized crew setup; a second run must print all zeros

Reruns converge: a run that died half way is completed by the next one. Two concurrent runs can
collide on a unique slug, view name or actor name; the loser raises and a rerun finishes the seed.
Existing view settings are preserved; only exact retired role-text defaults are refreshed."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECTIONS = [
    ("memecoin", "Memecoin", None),
    ("memecoin/solana", "Solana", "memecoin"),
    ("memecoin/solana/slot-lane", "Slot lane", "memecoin/solana"),
    ("memecoin/solana/j7", "J7 feed", "memecoin/solana"),
    ("memecoin/solana/onchain", "On-chain lane", "memecoin/solana"),
    ("memecoin/4663", "Robinhood 4663", "memecoin"), ("memecoin/56", "BSC 56", "memecoin"),
    ("memecoin/8453", "Base 8453", "memecoin"), ("memecoin/5042", "Arc 5042", "memecoin"),
    ("memecoin/exits", "Exits", "memecoin"), ("memecoin/gates", "Entry gates", "memecoin"),
    ("memecoin/sizing", "Sizing", "memecoin"),
    ("infra", "Infrastructure", None), ("infra/deploy", "Deploy", "infra"),
    ("infra/runtime", "Runtime", "infra"), ("infra/rpc", "RPC providers", "infra"),
    ("infra/db", "Databases", "infra"), ("infra/clock", "Clocks", "infra"),
    ("research", "Research", None), ("research/methods", "Methods", "research"),
    ("research/studies", "Studies", "research"),
    ("crew", "Crew", None), ("crew/alpha", "Alpha", "crew"), ("crew/beta", "Beta", "crew"),
    ("crew/gamma", "Gamma", "crew"), ("crew/lambda", "Lambda", "crew"), ("crew/codex", "Codex", "crew"),
    ("crew/delta", "Delta", "crew"), ("crew/epsilon", "Epsilon", "crew"), ("crew/zeta", "Zeta", "crew"),
    ("brain", "Brain", None), ("brain/doctrine", "Doctrine (CLAUDE.md, AGENTS.md)", "brain"),
    ("brain/selftest", "Selftest", "brain"),
    ("hyperliquid", "Hyperliquid", None), ("crypto5m", "Crypto 5m", None),
    ("wallet-analyzer", "Wallet analyzer", None),
]
_MEME = [s for s, _, _ in SECTIONS if s.startswith("memecoin")]
_BUILD = "builds with TDD in the shared checkout; coordinate exact owned paths and independent review."
ROLE_VIEWS = {
    "alpha": {"sections": ["infra/deploy", "infra/runtime", "crew", "crew/alpha", "brain/doctrine"] + _MEME,
              "role_text": "Alpha leads coordination and releases with Codex; agree release ownership first. "
                           "Use the shared checkout; .env changes require the operator's explicit yes."},
    "beta": {"sections": ["crew/beta", "brain/doctrine", "infra/runtime"] + _MEME, "role_text": "Beta " + _BUILD},
    "lambda": {"sections": ["crew/lambda", "brain/doctrine", "infra/rpc", "infra/runtime"] + _MEME,
               "role_text": "Lambda " + _BUILD},
    "gamma": {"sections": ["crew/gamma", "brain/doctrine", "research", "research/methods", "research/studies",
                           "infra/db", "infra/clock"] + _MEME,
              "role_text": "Gamma measures, read-only: studies, audits, pre-registered reads."},
    "codex": {"sections": ["crew/codex", "brain/doctrine", "infra/deploy"] + _MEME,
              "role_text": "Codex coordinates, integrates and reviews in the shared checkout; may push and deploy "
                           "within operator authority after agreeing release ownership with Alpha."},
    "delta": {"sections": ["crew/delta", "brain/doctrine", "infra/runtime"] + _MEME,
              "role_text": "Delta owns execution and reliability implementation; " + _BUILD},
    "epsilon": {"sections": ["crew/epsilon", "brain/doctrine", "infra/db", "infra/rpc", "research"] + _MEME,
                "role_text": "Epsilon owns desk, data, tooling and research plumbing; " + _BUILD},
    "zeta": {"sections": ["crew/zeta", "brain/doctrine", "research", "research/methods", "research/studies",
                          "infra/db", "infra/clock"] + _MEME,
             "role_text": "Zeta measures and independently reviews read-only; no deployment or money authority."},
}
ACTORS = ("operator", *ROLE_VIEWS)
_RETIRED_TEXT = {
    "alpha": "Alpha leads: the only role that pushes to origin/main and deploys; merges crew branches; owns .env changes on the operator's yes.",
    "beta": "Beta builds: TDD in a private branch; Alpha merges and deploys.",
    "lambda": "Lambda builds: TDD in a private branch; Alpha merges and deploys.",
    "codex": "Codex: an outside session in its own worktree. It reads the brain and never writes it until the operator says otherwise.",
}


class DryRun:
    """Reads through to the real store; an insert is echoed with a placeholder id, never sent."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def patch_view(self, ident, patch):
        return {**self._store.view(ident), **patch}

    def insert(self, table, row):
        return {**row, "id": f"dry:{table}:{row.get('slug') or row.get('name')}"}


def seed(store):
    counts = {"sections_created": 0, "views_created": 0, "actors_created": 0, "views_updated": 0}
    by_slug = {s["slug"]: s for s in store.sections()}
    for slug, title, parent in SECTIONS:
        if slug in by_slug:
            continue
        row = {"slug": slug, "title": title}
        if parent:
            row["parent_id"] = by_slug[parent]["id"]
        by_slug[slug] = store.insert("brain_sections", row)
        counts["sections_created"] += 1
    for role, spec in ROLE_VIEWS.items():
        view = store.view(f"role/{role}")
        if view is None:
            store.insert("brain_views", {"name": f"role/{role}", "description": f"boot view for {role}",
                                         "char_limit": 12000, "query_spec": {**spec, "kinds": None}})
            counts["views_created"] += 1
        elif role in _RETIRED_TEXT and view["query_spec"].get("role_text") == _RETIRED_TEXT[role]:
            store.patch_view(view["name"], {"query_spec": {**view["query_spec"], "role_text": spec["role_text"]}})
            counts["views_updated"] += 1
    crew = by_slug["crew"]["id"]
    for name in ACTORS:
        if store.find_entity("actor", name) is None:
            store.insert("brain_entities", {"type": "actor", "name": name, "section_id": crew})
            counts["actors_created"] += 1
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="read the live brain, write nothing")
    ns = ap.parse_args(argv)
    from bot.brain_graph import live
    from bot.brain_graph.store import RestStore
    from sniper.util.envfiles import load_env_files
    load_env_files(str(live.state_root(ROOT)))
    store = RestStore(timeout=10)
    print(("DRY RUN, nothing written: " if ns.dry_run else "") + str(seed(DryRun(store) if ns.dry_run else store)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
