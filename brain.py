"""
Sniper Brain — persistent memory for the Sniper agent.

Storage strategy:
  - Agent memory (key/value): Supabase `memories` table (already exists)
  - News items + Trade ideas: SQLite sniper_tracker.db (auto-created tables)

Falls back gracefully: if Supabase unavailable, key/value falls back to
in-process dict (non-persistent); SQLite always available.

Usage:
    from brain import Brain
    b = Brain()
    b.write('last_run', '2026-03-16')
    val = b.read('last_run')
    ideas = b.get_open_trade_ideas()
"""

import json
import logging
import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sniper.markets.content_normalization import normalize_memory_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase KV access (PostgREST over urllib — no SDK)
# ---------------------------------------------------------------------------



def _sb_available() -> bool:
    """True when Supabase REST credentials are present.

    Transport is urllib via bot.supabase_helper. The `supabase` SDK is deliberately
    NOT a dependency: it was installed in no interpreter on this host, so the old
    _get_sb() returned None unconditionally and every Brain KV path dark-failed
    (/api/finance-context, trade-idea act/reject). Two other modules in the Intel
    path already speak PostgREST over urllib and work; this one now matches them.
    """
    return bool(os.getenv("SUPABASE_URL")
                and (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")))


def _sb_request(method: str, path: str, body=None):
    """Execute one PostgREST call. Raises on failure — callers degrade."""
    from bot.supabase_helper import _request
    return _request(method, path, body)


def _sb_key(value: str) -> str:
    """URL-encode a memory name for a PostgREST filter.

    Memory keys contain '/' and ':' (markets/state, world_event_run:last_summary),
    both of which must be escaped or the filter silently addresses the wrong row.
    """
    return urllib.parse.quote(str(value), safe="")


# ---------------------------------------------------------------------------
# SQLite helpers for news_items + trade_ideas
# ---------------------------------------------------------------------------

def _db_path() -> str:
    return os.getenv("DB_PATH", os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "sniper_tracker.db"
    ))


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create news_items and trade_ideas tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            title       TEXT NOT NULL,
            description TEXT,
            url         TEXT,
            published_at TEXT,
            fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
            topic_tags  TEXT,
            sentiment   TEXT,
            processed   INTEGER NOT NULL DEFAULT 0,
            UNIQUE(source, title)
        );
        CREATE INDEX IF NOT EXISTS idx_ni_fetched ON news_items(fetched_at DESC);
        CREATE INDEX IF NOT EXISTS idx_ni_processed ON news_items(processed);

        CREATE TABLE IF NOT EXISTS trade_ideas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT NOT NULL,
            market_title TEXT NOT NULL,
            outcome      TEXT NOT NULL,
            direction    TEXT NOT NULL,
            current_price REAL,
            target_price  REAL,
            confidence   TEXT NOT NULL DEFAULT 'low',
            reasoning    TEXT NOT NULL,
            news_trigger TEXT,
            news_source  TEXT,
            status       TEXT NOT NULL DEFAULT 'open',
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at   TEXT,
            acted_at     TEXT,
            alerted_at   TEXT,
            notes        TEXT,
            driving_event TEXT,
            resolution_outcome TEXT,
            resolution_price REAL,
            resolved_at  TEXT,
            brier_score  REAL,
            source_agent TEXT,
            adversarial_score INTEGER,
            adversarial_reasoning TEXT,
            adversarial_approved INTEGER DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ti_status ON trade_ideas(status);
        CREATE INDEX IF NOT EXISTS idx_ti_created ON trade_ideas(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_ti_review_queue
            ON trade_ideas(status, adversarial_approved, created_at DESC);
    """)
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_ideas_dedup
            ON trade_ideas(condition_id, direction)
        """)
    except Exception:
        # Index creation can fail if pre-existing duplicate rows exist.
        # Clean duplicates first, then retry.
        conn.execute("""
            DELETE FROM trade_ideas WHERE id NOT IN (
                SELECT MIN(id) FROM trade_ideas GROUP BY condition_id, direction
            )
        """)
        conn.commit()
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_ideas_dedup
            ON trade_ideas(condition_id, direction)
        """)
    # Migration: add columns if missing (existing DBs)
    _brain_migrations = [
        ("alerted_at", "TEXT"),
        ("driving_event", "TEXT"),
        ("resolution_outcome", "TEXT"),
        ("resolution_price", "REAL"),
        ("resolved_at", "TEXT"),
        ("brier_score", "REAL"),
        ("source_agent", "TEXT"),
        ("adversarial_score", "INTEGER"),
        ("adversarial_reasoning", "TEXT"),
        ("adversarial_approved", "INTEGER DEFAULT NULL"),
        # Measurement columns. Without these a candidate is an opinion; with them it
        # is a testable claim — which market (token_id), when the event happened
        # (event_ts), what the market knew then (price_at_event) and what we would
        # have paid (price_at_decision). reject_reason makes "no candidate" a
        # first-class recorded output rather than a silent skip.
        ("token_id", "TEXT"),
        ("event_ts", "TEXT"),
        ("price_at_event", "REAL"),
        ("price_at_decision", "REAL"),
        ("reject_reason", "TEXT"),
    ]
    for _col, _def in _brain_migrations:
        try:
            conn.execute(f"ALTER TABLE trade_ideas ADD COLUMN {_col} {_def}")
            conn.commit()
        except Exception:
            pass  # column already exists
    conn.commit()


def ensure_trade_ideas_schema(conn: sqlite3.Connection) -> None:
    """Ensure the trade_ideas table and review columns exist on an arbitrary connection."""
    _ensure_tables(conn)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn)
    return conn


def get_trade_idea_review_gate(conn: sqlite3.Connection, idea_id: int) -> tuple[bool, str]:
    """Return whether a trade idea is allowed through the adversarial gate."""
    ensure_trade_ideas_schema(conn)
    row = conn.execute(
        """SELECT adversarial_approved, adversarial_score, adversarial_reasoning
           FROM trade_ideas
           WHERE id = ?""",
        (idea_id,),
    ).fetchone()
    if row is None:
        return False, f"trade idea {idea_id} not found"

    approved = row["adversarial_approved"]
    score = row["adversarial_score"]
    reasoning = str(row["adversarial_reasoning"] or "").strip()
    if approved is None:
        return False, f"trade idea {idea_id} is pending adversarial review"
    if int(approved):
        return True, "approved"

    details: list[str] = []
    if score is not None:
        details.append(f"score={int(score)}")
    if reasoning:
        details.append(reasoning[:180])
    suffix = f" ({'; '.join(details)})" if details else ""
    return False, f"trade idea {idea_id} was rejected by adversarial review{suffix}"


# ---------------------------------------------------------------------------
# Brain class
# ---------------------------------------------------------------------------

class Brain:
    """Persistent memory interface: Supabase for KV, SQLite for news/ideas."""

    def __init__(self, db_path: Optional[str] = None):
        self.sb_available = _sb_available()
        self.available = True  # SQLite always available
        self._fallback: Dict[str, str] = {}  # in-proc fallback if Supabase down
        self._db_path = db_path  # override path for testing
        # For :memory: or explicit path overrides, keep one persistent connection
        # so that all operations share the same database state.
        self._conn: Optional[sqlite3.Connection] = None
        if db_path is not None:
            self._conn = sqlite3.connect(db_path, timeout=10)
            self._conn.row_factory = sqlite3.Row
            _ensure_tables(self._conn)

    def _get_conn(self) -> sqlite3.Connection:
        """Return a DB connection, using instance-level override if set."""
        if self._conn is not None:
            return self._conn
        return _get_conn()

    # -------------------------------------------------------------------------
    # Agent Memory — key/value (Supabase memories table, type='reference')
    # -------------------------------------------------------------------------

    def read_entry(self, key: str) -> Optional[Dict[str, Any]]:
        """Read a memory entry by key, preferring the freshest knowledge/reference row."""
        if self.sb_available:
            try:
                res = _sb_request(
                    "GET",
                    f"memories?name=eq.{_sb_key(key)}&status=eq.active"
                    f"&select=content,type,updated_at&limit=10",
                )
                rows = [
                    row for row in (res or [])
                    if str(row.get("type") or "") in {"reference", "knowledge"}
                ]
                if rows:
                    best = max(rows, key=lambda row: str(row.get("updated_at") or ""))
                    best_type = str(best.get("type") or "")
                    return {
                        "content": normalize_memory_content(best_type, key, str(best.get("content") or "")),
                        "type": best_type,
                        "updated_at": best.get("updated_at"),
                    }
            except Exception as e:
                logger.debug("Brain.read_entry(%s) Supabase failed: %s", key, e)
        if key in self._fallback:
            return {
                "content": self._fallback[key],
                "type": "fallback",
                "updated_at": None,
            }
        return None

    def read(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read a memory value by key."""
        entry = self.read_entry(key)
        if entry is not None:
            return entry.get("content")
        return default

    def write(self, key: str, value: str, category: str = "general") -> bool:
        """Write or update a memory value."""
        normalized_value = normalize_memory_content("reference", key, str(value))
        self._fallback[key] = normalized_value
        if not self.sb_available:
            return False
        try:
            # Check if key exists first, then insert or update
            existing = _sb_request(
                "GET",
                f"memories?type=eq.reference&name=eq.{_sb_key(key)}&select=id&limit=1",
            ) or []
            if existing:
                _sb_request(
                    "PATCH",
                    f"memories?id=eq.{_sb_key(existing[0]['id'])}",
                    {"content": normalized_value, "description": category},
                )
            else:
                _sb_request("POST", "memories", {
                    "type": "reference",
                    "name": key,
                    "description": category,
                    "content": normalized_value,
                    "source": "vps",
                })
            return True
        except Exception as e:
            logger.debug("Brain.write(%s) Supabase failed: %s", key, e)
            return False

    def read_all(self, category: Optional[str] = None) -> Dict[str, str]:
        """Read all memory entries, optionally filtered by category."""
        if not self.sb_available:
            return dict(self._fallback)
        try:
            path = ("memories?type=eq.reference&status=eq.active"
                    "&select=name,content,description")
            if category:
                path += f"&description=eq.{_sb_key(category)}"
            res = _sb_request("GET", path)
            return {r["name"]: r["content"] for r in (res or [])}
        except Exception as e:
            logger.debug("Brain.read_all() failed: %s", e)
            return dict(self._fallback)

    # -------------------------------------------------------------------------
    # News Items — SQLite
    # -------------------------------------------------------------------------

    def store_news(self, items: List[Dict[str, Any]]) -> int:
        """Store news items; returns count of new items inserted."""
        if not items:
            return 0
        stored = 0
        try:
            conn = self._get_conn()
            for item in items:
                tags = item.get("topic_tags")
                tags_str = json.dumps(tags) if isinstance(tags, list) else (tags or "[]")
                try:
                    conn.execute(
                        """INSERT INTO news_items
                           (source, title, description, url, published_at, fetched_at,
                            topic_tags, sentiment, processed)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(source, title) DO UPDATE SET
                               sentiment=excluded.sentiment,
                               topic_tags=excluded.topic_tags,
                               processed=excluded.processed""",
                        (
                            item.get("source", ""),
                            item.get("title", ""),
                            item.get("description"),
                            item.get("url"),
                            item.get("published_at"),
                            item.get("fetched_at") or _now_dt(),
                            tags_str,
                            item.get("sentiment"),
                            1 if item.get("processed") else 0,
                        )
                    )
                    stored += 1
                except Exception as e:
                    logger.debug("Brain.store_news item failed: %s", e)
            conn.commit()
            if self._conn is None:
                conn.close()
        except Exception as e:
            logger.warning("Brain.store_news failed: %s", e)
        return stored

    def get_recent_news(self, limit: int = 30, unprocessed_only: bool = False) -> List[Dict]:
        """Fetch recent news items."""
        try:
            conn = self._get_conn()
            q = "SELECT * FROM news_items"
            if unprocessed_only:
                q += " WHERE processed = 0"
            q += " ORDER BY fetched_at DESC LIMIT ?"
            rows = conn.execute(q, (limit,)).fetchall()
            if self._conn is None:
                conn.close()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("topic_tags"):
                    try:
                        d["topic_tags"] = json.loads(d["topic_tags"])
                    except Exception:
                        pass
                d["processed"] = bool(d.get("processed"))
                result.append(d)
            return result
        except Exception as e:
            logger.debug("Brain.get_recent_news failed: %s", e)
            return []

    def mark_news_processed(self, news_id: int) -> None:
        try:
            conn = self._get_conn()
            conn.execute("UPDATE news_items SET processed=1 WHERE id=?", (news_id,))
            conn.commit()
            if self._conn is None:
                conn.close()
        except Exception as e:
            logger.debug("Brain.mark_news_processed failed: %s", e)

    # -------------------------------------------------------------------------
    # Trade Ideas — SQLite
    # -------------------------------------------------------------------------

    def add_trade_idea(
        self,
        condition_id: str,
        market_title: str,
        outcome: str,
        direction: str,
        reasoning: str,
        current_price: Optional[float] = None,
        target_price: Optional[float] = None,
        confidence: str = "low",
        news_trigger: Optional[str] = None,
        news_source: Optional[str] = None,
        expires_hours: int = 48,
        end_date_iso: Optional[str] = None,
        driving_event: Optional[str] = None,
        source_agent: Optional[str] = None,
        token_id: Optional[str] = None,
        event_ts: Optional[str] = None,
        price_at_event: Optional[float] = None,
        price_at_decision: Optional[float] = None,
        reject_reason: Optional[str] = None,
    ) -> Optional[int]:
        """Insert a new trade idea. Returns the new row id, or None if duplicate.

        Before inserting, expires any open idea on the SAME market with the OPPOSITE
        direction — Claude cannot simultaneously recommend buy_yes AND buy_no on the
        same condition. The latest signal always supersedes.

        Passing ``reject_reason`` records a NON-candidate: status='not_executable'.
        "We looked at this market and declined" is a first-class output — the
        difference between having found nothing and not having looked.
        """
        try:
            # Normalise at storage time so all comparisons in DB are lowercase
            direction_norm = direction.lower().strip()   # "buy_yes" | "buy_no"
            confidence_norm = confidence.lower().strip() # "high" | "medium" | "low"
            outcome_norm = outcome.upper().strip()       # "YES" | "NO"

            # Smart expiry: tied to market resolution date, not a fixed 48h clock
            if end_date_iso and expires_hours == 48:  # only override if caller uses the default
                expires_hours = _smart_expiry_hours(end_date_iso)

            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=expires_hours)
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn = self._get_conn()
            status = "not_executable" if reject_reason else "open"

            # A stored rejection must never shadow a later real candidate: the dedup
            # index is UNIQUE(condition_id, direction) and the insert below is
            # INSERT OR IGNORE, so a lingering not_executable row would silently
            # swallow every future idea for that market/direction. A rejection is a
            # transient observation — the newest row wins.
            conn.execute(
                """DELETE FROM trade_ideas
                   WHERE condition_id=? AND direction=? AND status='not_executable'""",
                (condition_id, direction_norm),
            )

            # A rejection supersedes nothing — only real candidates expire the
            # opposing side.
            if not reject_reason:
                opposing = "buy_no" if direction_norm == "buy_yes" else "buy_yes"
                conn.execute(
                    """UPDATE trade_ideas
                       SET status='expired', notes='superseded by opposing signal'
                       WHERE condition_id=? AND direction=? AND status='open'""",
                    (condition_id, opposing),
                )

            # Per-world-event cap: max 2 open ideas per driving_event slug
            # When a 3rd arrives, expire the oldest open idea for that event
            if driving_event and not reject_reason:
                existing = conn.execute(
                    "SELECT id FROM trade_ideas WHERE driving_event=? AND status='open' ORDER BY created_at ASC",
                    (driving_event,)
                ).fetchall()
                if len(existing) >= 2:
                    # Expire the oldest to make room for the new one
                    oldest_id = existing[0]["id"]
                    conn.execute(
                        "UPDATE trade_ideas SET status='expired', notes='replaced by newer signal for same event' WHERE id=?",
                        (oldest_id,)
                    )

            cursor = conn.execute(
                """INSERT OR IGNORE INTO trade_ideas
                   (condition_id, market_title, outcome, direction, reasoning,
                    current_price, target_price, confidence, news_trigger,
                    news_source, expires_at, status, driving_event, source_agent,
                    token_id, event_ts, price_at_event, price_at_decision, reject_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    condition_id, market_title, outcome_norm, direction_norm, reasoning,
                    current_price, target_price, confidence_norm,
                    news_trigger, news_source, expires_at, status, driving_event, source_agent,
                    token_id, event_ts, price_at_event, price_at_decision, reject_reason,
                )
            )
            conn.commit()
            if cursor.rowcount == 0:
                # INSERT OR IGNORE silently skipped — same (condition_id, direction) already open
                if self._conn is None:
                    conn.close()
                return None
            new_id = cursor.lastrowid
            if self._conn is None:
                conn.close()
            return new_id
        except Exception:
            logger.exception("Brain.add_trade_idea failed (DB error, not duplicate)")
            return -1  # distinguishable from None (duplicate) and valid int (success)

    def get_open_trade_ideas(self, limit: int = 20) -> List[Dict]:
        """Get open (non-expired, non-acted) trade ideas."""
        try:
            now = _now_dt()
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT * FROM trade_ideas
                   WHERE status='open'
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (now, limit)
            ).fetchall()
            if self._conn is None:
                conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("Brain.get_open_trade_ideas failed: %s", e)
            return []

    def get_all_trade_ideas(self, limit: int = 50) -> List[Dict]:
        """Get all trade ideas (any status), newest first."""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM trade_ideas ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            if self._conn is None:
                conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("Brain.get_all_trade_ideas failed: %s", e)
            return []

    def get_pending_adversarial_reviews(self, limit: int = 20) -> List[Dict]:
        """Return open, unexpired trade ideas that have not yet passed adversarial review."""
        try:
            now = _now_dt()
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT * FROM trade_ideas
                   WHERE status='open'
                     AND (expires_at IS NULL OR expires_at > ?)
                     AND adversarial_approved IS NULL
                   ORDER BY
                     CASE LOWER(confidence)
                       WHEN 'high' THEN 0
                       WHEN 'medium' THEN 1
                       ELSE 2
                     END,
                     created_at ASC
                   LIMIT ?""",
                (now, limit),
            ).fetchall()
            if self._conn is None:
                conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("Brain.get_pending_adversarial_reviews failed: %s", e)
            return []

    def get_unreviewed_papered_ideas(self, limit: int = 20) -> List[Dict]:
        """Unreviewed ideas that already have an open intel paper position.

        `get_pending_adversarial_reviews` filters on `status='open'`, but a papered idea
        can leave that status before the reviewer's next run — `mark_idea_acted` fires
        from the strategy runner and the operator API, and an idea created with a
        `reject_reason` starts as `not_executable` yet can still be papered. Those ideas
        would never be reviewed, so their positions would sit at `reviewer_verdict
        'pending'` forever and drop out of the approved-vs-rejected comparison.

        Expiry is deliberately NOT filtered here: the position is open regardless of
        whether the idea's advertising window lapsed, and an open position needs a
        verdict.
        """
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT i.* FROM trade_ideas i
                   JOIN intel_paper_positions p ON p.idea_id = i.id
                   WHERE p.status = 'open'
                     AND (p.reviewer_verdict IS NULL OR p.reviewer_verdict = 'pending')
                     AND i.adversarial_approved IS NULL
                   ORDER BY i.created_at ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            if self._conn is None:
                conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.debug("Brain.get_unreviewed_papered_ideas failed: %s", e)
            return []

    def record_adversarial_review(
        self,
        idea_id: int,
        *,
        adversarial_score: int,
        adversarial_reasoning: str,
        adversarial_approved: bool,
    ) -> None:
        """Persist an adversarial review decision for a trade idea."""
        try:
            conn = self._get_conn()
            conn.execute(
                """UPDATE trade_ideas
                   SET adversarial_score=?,
                       adversarial_reasoning=?,
                       adversarial_approved=?
                   WHERE id=?""",
                (
                    int(adversarial_score),
                    str(adversarial_reasoning or "").strip(),
                    1 if adversarial_approved else 0,
                    idea_id,
                ),
            )
            conn.commit()
            if self._conn is None:
                conn.close()
        except Exception as e:
            logger.debug("Brain.record_adversarial_review failed: %s", e)

    def mark_idea_acted(self, idea_id: int, notes: Optional[str] = None) -> None:
        try:
            conn = self._get_conn()
            conn.execute(
                "UPDATE trade_ideas SET status='acted', acted_at=? WHERE id=?",
                (_now_dt(), idea_id)
            )
            if notes:
                conn.execute(
                    "UPDATE trade_ideas SET notes=? WHERE id=?",
                    (notes, idea_id)
                )
            conn.commit()
            if self._conn is None:
                conn.close()
        except Exception as e:
            logger.debug("Brain.mark_idea_acted failed: %s", e)

    def reject_idea(self, idea_id: int, notes: Optional[str] = None) -> None:
        try:
            conn = self._get_conn()
            conn.execute(
                "UPDATE trade_ideas SET status='rejected' WHERE id=?",
                (idea_id,)
            )
            if notes:
                conn.execute(
                    "UPDATE trade_ideas SET notes=? WHERE id=?",
                    (notes, idea_id)
                )
            conn.commit()
            if self._conn is None:
                conn.close()
        except Exception as e:
            logger.debug("Brain.reject_idea failed: %s", e)

    def expire_stale_ideas(self) -> int:
        """Mark expired open ideas as expired. Returns count."""
        try:
            now = _now_dt()
            conn = self._get_conn()
            cur = conn.execute(
                "UPDATE trade_ideas SET status='expired' WHERE status='open' AND expires_at < ?",
                (now,)
            )
            count = cur.rowcount
            conn.commit()
            if self._conn is None:
                conn.close()
            return count
        except Exception as e:
            logger.debug("Brain.expire_stale_ideas failed: %s", e)
            return 0

    def record_idea_resolution(self, idea_id: int, outcome: str, resolution_price: float) -> None:
        """Record the market resolution outcome for a trade idea.

        outcome: 'win' if the predicted direction was correct, 'loss' otherwise.
        This creates the feedback loop: Claude's causal chains are evaluated against reality.
        """
        try:
            conn = self._get_conn()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """UPDATE trade_ideas
                   SET resolution_outcome=?, resolution_price=?, resolved_at=?, status='resolved'
                   WHERE id=?""",
                (outcome, resolution_price, now, idea_id)
            )
            conn.commit()
        except Exception as e:
            logger.warning("record_idea_resolution failed for idea %d: %s", idea_id, e)

    # -------------------------------------------------------------------------
    # Outcome Feedback Loop
    # -------------------------------------------------------------------------

    def maybe_update_brain(self, strategy_type, market_category, outcome, pnl, closed_count):
        """Fire-and-forget brain update on position close.

        Only writes to Supabase every 10 closed trades per strategy type.
        Never blocks the calling service.
        """
        try:
            if closed_count % 10 != 0 or closed_count == 0:
                return
            self._do_brain_update(strategy_type, market_category, outcome, pnl, closed_count)
        except Exception:
            import logging
            logging.getLogger(__name__).debug("Brain update failed (non-blocking)", exc_info=True)

    def _do_brain_update(self, strategy_type, market_category, outcome, pnl, closed_count):
        """Write strategy performance knowledge to Supabase."""
        try:
            import os
            from bot import supabase_helper as sb
            from datetime import datetime, timezone

            name = f"strategy:{strategy_type}-performance"
            content = (
                f"## Finding\n"
                f"{strategy_type} has {closed_count} closed trades.\n"
                f"Latest close: {outcome}, PnL=${pnl:.2f}, category={market_category}\n\n"
                f"## Evidence\n"
                f"Auto-generated from trade close event at {closed_count} trades.\n\n"
                f"## Source\nAutomated outcome feedback loop\n\n"
                f"## Confidence\nmedium\n\n"
                f"## Last Verified\n{datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
                f"## Applies To\n{strategy_type} strategy decisions"
            )
            sb.upsert_memory(
                type="project",
                name=name,
                description=f"Rolling performance for {strategy_type}",
                content=content,
                source="vps",
            )
        except Exception:
            import logging
            logging.getLogger(__name__).debug("Supabase brain write failed (non-blocking)", exc_info=True)


def _now_dt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _smart_expiry_hours(end_date_iso: Optional[str]) -> int:
    """Compute idea expiry hours based on market resolution date."""
    from sniper.filters import days_to_resolution
    days = days_to_resolution(end_date_iso)
    if days is None:
        return 48
    if days < 1:
        return 12
    if days < 7:
        return max(12, min(24, int(days * 12)))
    if days <= 30:
        return 48
    return 72
