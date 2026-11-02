"""
situation_model.py — Persistent temporal knowledge graph for geopolitical analysis.

Implements lightweight Graphiti-style temporal knowledge (arXiv:2501.13956) using SQLite.
Each claim has valid_from/valid_until timestamps for temporal accuracy, and a source_tier
for epistemological clarity (factual / speculative / opinion / propaganda).

Source credibility tiers:
  factual     — verified by high-reliability sources; confidence-weighted by diversity
  speculative — reasonable inference, medium-reliability, unverified claims
  opinion     — editorial framing, analyst interpretation, not directly verifiable
  propaganda  — one or more SemEval-2020 propaganda techniques detected

Usage:
  from situation_model import SituationModel
  sm = SituationModel()
  sm.upsert_claim(slug="iran-us-2026", subject="Iran",
                  predicate="resumed_talks_with", object_="US",
                  confidence=0.85, source_tier="factual",
                  sources=["bbc_world", "france24"])
  claims = sm.get_active_claims("iran-us-2026")

CLI (for world_analyst.py subprocess calls):
  python3 situation_model.py upsert --slug iran-us-2026 \\
    --subject "Iran" --predicate "resumed_talks_with" --object "US" \\
    --confidence 0.85 --source-tier factual --sources "bbc_world,france24"
  python3 situation_model.py read --slug iran-us-2026 --summary
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional


DEFAULT_DB_PATH = os.getenv("DB_PATH", "sniper_tracker.db")

_CREATE_TABLE_SQL = [
    """CREATE TABLE IF NOT EXISTS situation_model (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT    NOT NULL,
    claim_type      TEXT    NOT NULL DEFAULT 'fact',
    subject         TEXT    NOT NULL,
    predicate       TEXT    NOT NULL,
    object          TEXT,
    confidence      REAL    NOT NULL DEFAULT 0.5,
    source_tier     TEXT    NOT NULL DEFAULT 'factual',
    sources         TEXT,
    diversity_score REAL,
    valid_from      TEXT    NOT NULL,
    valid_until     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
)""",
    "CREATE INDEX IF NOT EXISTS idx_situation_slug    ON situation_model (slug)",
    "CREATE INDEX IF NOT EXISTS idx_situation_subject ON situation_model (subject)",
    "CREATE INDEX IF NOT EXISTS idx_situation_valid   ON situation_model (valid_from, valid_until)",
]


def _get_conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    return conn


class SituationModel:
    """Read/write interface to the situation_model table."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._ensure_table()

    def _ensure_table(self) -> None:
        conn = _get_conn(self.db_path)
        try:
            for stmt in _CREATE_TABLE_SQL:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()

    def upsert_claim(
        self,
        slug: str,
        subject: str,
        predicate: str,
        object_: Optional[str] = None,
        claim_type: str = "fact",
        confidence: float = 0.5,
        source_tier: str = "factual",
        sources: Optional[List[str]] = None,
        diversity_score: Optional[float] = None,
        valid_from: Optional[str] = None,
        valid_until: Optional[str] = None,
    ) -> int:
        """Insert or update a claim. Returns the row id.

        If an active claim with the same (slug, subject, predicate) exists,
        it is closed (valid_until set) and replaced with the new claim.
        This preserves history while keeping the active set clean.
        """
        now = datetime.now(timezone.utc).isoformat()
        valid_from = valid_from or now
        sources_json = json.dumps(sources) if sources else None

        conn = _get_conn(self.db_path)
        try:
            # Close any existing active claim for this (slug, subject, predicate)
            conn.execute(
                """UPDATE situation_model
                   SET valid_until = ?, updated_at = ?
                   WHERE slug = ? AND subject = ? AND predicate = ?
                     AND (valid_until IS NULL OR valid_until > ?)""",
                (now, now, slug, subject, predicate, now),
            )
            # Insert new claim
            cur = conn.execute(
                """INSERT INTO situation_model
                   (slug, claim_type, subject, predicate, object, confidence,
                    source_tier, sources, diversity_score, valid_from, valid_until,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (slug, claim_type, subject, predicate, object_, confidence,
                 source_tier, sources_json, diversity_score, valid_from, valid_until,
                 now, now),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_active_claims(
        self,
        slug: Optional[str] = None,
        subject: Optional[str] = None,
        source_tier: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        """Return currently valid claims, optionally filtered."""
        now = datetime.now(timezone.utc).isoformat()
        where_parts = ["(valid_until IS NULL OR valid_until > ?)"]
        params: list = [now]
        if slug:
            where_parts.append("slug = ?")
            params.append(slug)
        if subject:
            where_parts.append("subject = ?")
            params.append(subject)
        if source_tier:
            where_parts.append("source_tier = ?")
            params.append(source_tier)

        sql = (
            f"SELECT * FROM situation_model WHERE {' AND '.join(where_parts)}"
            " ORDER BY confidence DESC, valid_from DESC LIMIT ?"
        )
        params.append(limit)

        conn = _get_conn(self.db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("sources"):
                    try:
                        d["sources"] = json.loads(d["sources"])
                    except Exception:
                        pass
                result.append(d)
            return result
        finally:
            conn.close()

    def get_summary(self, slug: str) -> str:
        """Return a compact text summary of active claims for a slug."""
        claims = self.get_active_claims(slug=slug)
        if not claims:
            return f"No active claims for '{slug}'"
        lines = [f"Situation model — {slug} ({len(claims)} active claims):"]
        tier_icons = {"factual": "✓", "speculative": "~", "opinion": "◦", "propaganda": "⚠"}
        for c in claims:
            icon = tier_icons.get(c["source_tier"], "?")
            div = f" div={c['diversity_score']:.2f}" if c.get("diversity_score") is not None else ""
            obj_str = f" {c['object']}" if c.get("object") else ""
            lines.append(
                f"  {icon} [{c['source_tier'].upper()}] {c['subject']} {c['predicate']}"
                f"{obj_str} (conf={c['confidence']:.2f}{div})"
            )
        return "\n".join(lines)

    def get_cross_domain_summary(self, prefix: str | None = None, limit: int = 60) -> str:
        """Return compact summary of active claims, optionally filtered by slug prefix.

        prefix: "fin" → returns fin:* claims only
                "geo" → returns geo:* claims only
                None  → returns ALL active claims across domains
        """
        now = datetime.now(timezone.utc).isoformat()
        if prefix:
            # Match slugs starting with prefix: (e.g. "fin:" or "geo:")
            sql = (
                "SELECT * FROM situation_model WHERE "
                "(valid_until IS NULL OR valid_until > ?) AND slug LIKE ? "
                "ORDER BY confidence DESC, valid_from DESC LIMIT ?"
            )
            params = [now, f"{prefix}:%", limit]
        else:
            sql = (
                "SELECT * FROM situation_model WHERE "
                "(valid_until IS NULL OR valid_until > ?) "
                "ORDER BY confidence DESC, valid_from DESC LIMIT ?"
            )
            params = [now, limit]

        conn = _get_conn(self.db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        if not rows:
            label = f"'{prefix}:*'" if prefix else "any domain"
            return f"No active situation model claims for {label}"

        # Group by slug for readability
        by_slug: dict[str, list] = {}
        for r in rows:
            d = dict(r)
            slug = d["slug"]
            by_slug.setdefault(slug, []).append(d)

        tier_icons = {"factual": "✓", "speculative": "~", "opinion": "◦", "propaganda": "⚠"}
        lines = [f"Situation model — {len(rows)} active claims across {len(by_slug)} slugs:"]
        for slug, claims in by_slug.items():
            lines.append(f"\n  [{slug}]")
            for c in claims:
                icon = tier_icons.get(c["source_tier"], "?")
                div = f" div={c['diversity_score']:.2f}" if c.get("diversity_score") is not None else ""
                obj_str = f" {c['object']}" if c.get("object") else ""
                lines.append(
                    f"    {icon} {c['subject']} {c['predicate']}{obj_str}"
                    f" (conf={c['confidence']:.2f}{div})"
                )
        return "\n".join(lines)

    def expire_claim(self, slug: str, subject: str, predicate: str) -> int:
        """Mark a claim as expired. Returns rows affected."""
        now = datetime.now(timezone.utc).isoformat()
        conn = _get_conn(self.db_path)
        try:
            cur = conn.execute(
                """UPDATE situation_model SET valid_until = ?, updated_at = ?
                   WHERE slug = ? AND subject = ? AND predicate = ?
                     AND (valid_until IS NULL OR valid_until > ?)""",
                (now, now, slug, subject, predicate, now),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# CLI — lets world_analyst.py call this as a subprocess
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, os.path.dirname(__file__))

    parser = argparse.ArgumentParser(description="Situation model CLI")
    sub = parser.add_subparsers(dest="cmd")

    # upsert subcommand
    up = sub.add_parser("upsert", help="Upsert a claim")
    up.add_argument("--slug", required=True)
    up.add_argument("--subject", required=True)
    up.add_argument("--predicate", required=True)
    up.add_argument("--object", dest="object_")
    up.add_argument("--claim-type", default="fact")
    up.add_argument("--confidence", type=float, default=0.5)
    up.add_argument("--source-tier", default="factual",
                    choices=["factual", "speculative", "opinion", "propaganda"])
    up.add_argument("--sources", help="Comma-separated source keys")
    up.add_argument("--diversity-score", type=float)
    up.add_argument("--valid-until")

    # read subcommand
    rd = sub.add_parser("read", help="Read active claims")
    rd.add_argument("--slug")
    rd.add_argument("--subject")
    rd.add_argument("--tier", dest="source_tier")
    rd.add_argument("--limit", type=int, default=50)
    rd.add_argument("--summary", action="store_true", help="Print compact text summary")
    rd.add_argument("--all", action="store_true", help="Return all active claims across all domains")
    rd.add_argument("--prefix", help="Filter by slug prefix (e.g. 'fin' returns fin:* slugs)")

    args = parser.parse_args()

    if args.cmd == "upsert":
        sm = SituationModel()
        sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
        row_id = sm.upsert_claim(
            slug=args.slug,
            subject=args.subject,
            predicate=args.predicate,
            object_=args.object_,
            claim_type=args.claim_type,
            confidence=args.confidence,
            source_tier=args.source_tier,
            sources=sources,
            diversity_score=args.diversity_score,
            valid_until=args.valid_until,
        )
        print(f"✅ Upserted claim id={row_id}")

    elif args.cmd == "read":
        sm = SituationModel()
        if args.all or args.prefix:
            print(sm.get_cross_domain_summary(prefix=args.prefix if not args.all else None, limit=args.limit))
        elif args.summary and args.slug:
            print(sm.get_summary(args.slug))
        else:
            claims = sm.get_active_claims(
                slug=args.slug,
                subject=args.subject,
                source_tier=args.source_tier,
                limit=args.limit,
            )
            print(json.dumps(claims, indent=2, default=str))
    else:
        parser.print_help()
