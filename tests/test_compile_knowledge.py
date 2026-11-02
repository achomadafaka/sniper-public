"""Tests for scripts/compile_knowledge.py.

Highest-risk behavior is the HUMAN block preservation — destroying operator
notes is unacceptable. That is covered first and most thoroughly.
"""
from __future__ import annotations

import sqlite3
import sys
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db import init_db
import compile_knowledge as ck  # noqa: E402


# ---------------------------------------------------------------------------
# HUMAN block preservation (highest-risk)
# ---------------------------------------------------------------------------


class TestExtractHumanBlock(unittest.TestCase):
    def test_extracts_simple_block(self):
        content = textwrap.dedent("""\
            <!-- HUMAN:START -->
            Operator note.
            <!-- HUMAN:END -->

            # Strategy page
            machine-owned body
        """)
        block = ck.extract_human_block(content)
        self.assertIn("Operator note.", block)

    def test_returns_empty_when_markers_missing(self):
        self.assertEqual(ck.extract_human_block("# no markers here"), "")
        self.assertEqual(ck.extract_human_block(""), "")

    def test_returns_empty_when_only_start_marker(self):
        content = "<!-- HUMAN:START -->\nNote\n"
        self.assertEqual(ck.extract_human_block(content), "")

    def test_ignores_content_outside_markers(self):
        content = textwrap.dedent("""\
            Before the block.
            <!-- HUMAN:START -->
            Inside only.
            <!-- HUMAN:END -->
            After the block.
        """)
        block = ck.extract_human_block(content)
        self.assertIn("Inside only.", block)
        self.assertNotIn("Before the block.", block)
        self.assertNotIn("After the block.", block)

    def test_preserves_multiline_content(self):
        content = textwrap.dedent("""\
            <!-- HUMAN:START -->
            Line one.

            Line three with blank line above.
            - Bullet
            - Another
            <!-- HUMAN:END -->
        """)
        block = ck.extract_human_block(content)
        self.assertIn("Line one.", block)
        self.assertIn("Line three with blank line above.", block)
        self.assertIn("- Bullet", block)

    def test_tolerates_whitespace_in_markers(self):
        content = "<!--   HUMAN:START   -->\ninner\n<!--  HUMAN:END -->\n"
        block = ck.extract_human_block(content)
        self.assertIn("inner", block)


class TestSpliceHumanBlock(unittest.TestCase):
    def test_roundtrip_preserves_operator_note(self):
        original = textwrap.dedent("""\
            <!-- HUMAN:START -->
            My operator note that must survive.
            <!-- HUMAN:END -->

            # Old machine body
            stale content
        """)
        new_body = "# New machine body\nfresh content\n"
        result = ck.splice_human_block(original=original, new_body=new_body)
        self.assertIn("My operator note that must survive.", result)
        self.assertIn("# New machine body", result)
        self.assertNotIn("stale content", result)

    def test_new_page_when_no_prior_file(self):
        result = ck.splice_human_block(original="", new_body="# Body\n")
        self.assertIn("<!-- HUMAN:START -->", result)
        self.assertIn("<!-- HUMAN:END -->", result)
        self.assertIn("# Body", result)

    def test_refuses_to_overwrite_when_markers_malformed(self):
        """Existing file has only the START marker — treat as corruption, don't
        destroy content, caller should skip this page."""
        original = "<!-- HUMAN:START -->\nhalf-written note\n# body\n"
        with self.assertRaises(ck.HumanBlockCorruptedError):
            ck.splice_human_block(original=original, new_body="# new\n")


# ---------------------------------------------------------------------------
# Strategy page compilation
# ---------------------------------------------------------------------------


def _seed_strategy_db(
    conn: sqlite3.Connection,
    *,
    strategy_id: int = 21,
    strategy_type: str = "political_no_value_v1",
    status: str = "active",
    label: str = "Political NO Value",
    pnl_usdc: float = 89.0,
    trades_triggered: int = 20,
    lifecycle_stage: str = "paper",
    pause_reason: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                  pnl_usdc, trades_triggered, instance_tag,
                                  lifecycle_stage, pause_reason, paused_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            strategy_id,
            "0xwallet",
            label,
            strategy_type,
            status,
            pnl_usdc,
            trades_triggered,
            "default-local",
            lifecycle_stage,
            pause_reason,
            "2026-04-17 10:00:00" if pause_reason else None,
        ),
    )
    conn.execute(
        """INSERT INTO strategy_research (slug, title, hypothesis, edge_summary,
                                          entry_logic, exit_logic, risk_notes,
                                          evidence, sample_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            strategy_type,
            f"Research: {label}",
            "The hypothesis.",
            "Edge: +20pp over breakeven.",
            "Enter when NO is mispriced.",
            "Exit at resolution or stop.",
            "Watch for category concentration.",
            "Backtest on 2024-2025 data.",
            123,
        ),
    )
    now = int(time.time())
    conn.execute(
        """INSERT INTO strategy_positions (position_id, wallet, condition_id, outcome,
                                            strategy_id, state, updated_ts, realized_pnl,
                                            entry_price, size_usdc, pnl_usdc, opened_ts, closed_ts)
           VALUES (1, '0xwallet', 'cond_a', 'NO', ?, 'closed', ?, 15.0, 0.55, 10.0, 15.0, ?, ?)""",
        (str(strategy_id), now, now - 3600, now),
    )
    conn.execute(
        """INSERT INTO strategy_positions (position_id, wallet, condition_id, outcome,
                                            strategy_id, state, updated_ts, realized_pnl,
                                            entry_price, size_usdc, pnl_usdc, opened_ts, closed_ts)
           VALUES (2, '0xwallet', 'cond_b', 'NO', ?, 'open', ?, 0.0, 0.60, 10.0, 0.0, ?, 0)""",
        (str(strategy_id), now, now - 1800),
    )
    conn.commit()


class TestStrategyPageCompilation(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        return conn

    def test_active_strategy_page_contains_core_fields(self):
        conn = self._conn()
        _seed_strategy_db(conn)
        rendered = ck.render_strategy_page(conn, strategy_id=21)
        self.assertIn("Strategy #21", rendered)
        self.assertIn("political_no_value_v1", rendered)
        self.assertIn("Political NO Value", rendered)
        self.assertIn("active", rendered.lower())
        self.assertIn("The hypothesis.", rendered)
        self.assertIn("+89.00", rendered)  # pnl_usdc formatted
        self.assertIn("<!-- HUMAN:START -->", rendered)
        self.assertIn("<!-- HUMAN:END -->", rendered)

    def test_missing_research_renders_stub(self):
        conn = self._conn()
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, lifecycle_stage)
               VALUES (42, '0xw', 'Orphan', 'no_such_research_v1', 'active', 0.0, 0, 'paper')""",
        )
        conn.commit()
        rendered = ck.render_strategy_page(conn, strategy_id=42)
        self.assertIn("(no research entry yet)", rendered)

    def test_retired_gate_requires_runtime_history(self):
        conn = self._conn()
        # Strategy with status=paused but no closed trades, no pnl, no pause_reason
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, lifecycle_stage, pause_reason)
               VALUES (99, '0xw', 'Abandoned', 'abandoned_v1', 'stopped', 0.0, 0, 'paper', '')""",
        )
        conn.commit()
        self.assertFalse(ck.is_retired_eligible(conn, strategy_id=99))

        # Same strategy but with pause_reason set — eligible
        conn.execute("UPDATE strategies SET pause_reason = ? WHERE id = 99",
                     ("Negative EV",))
        conn.commit()
        self.assertTrue(ck.is_retired_eligible(conn, strategy_id=99))

    def test_retired_eligible_via_pnl(self):
        conn = self._conn()
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, lifecycle_stage, pause_reason)
               VALUES (100, '0xw', 'HadPnL', 'hadpnl_v1', 'stopped', -15.0, 3, 'paper', '')""",
        )
        conn.commit()
        self.assertTrue(ck.is_retired_eligible(conn, strategy_id=100))


# ---------------------------------------------------------------------------
# Wallet page compilation
# ---------------------------------------------------------------------------


def _seed_wallet_db(conn: sqlite3.Connection, wallet: str = "0xabc123") -> None:
    now_ts = int(time.time())
    conn.execute(
        """INSERT INTO followed_wallets (wallet, label, added_at, added_reason,
                                         status, total_positions, total_pnl_usdc,
                                         last_copied_at)
           VALUES (?, 'Theo4', '2026-03-01', 'High lb_pnl', 'active', 5, 42.5, '2026-04-17')""",
        (wallet,),
    )
    conn.execute(
        """INSERT INTO user_analysis (wallet, analyzed_ts, position_count, pnl,
                                      max_position_value, total_open_value, trade_count,
                                      insider_score, speculator_score, classification, tier,
                                      strategy_group, win_rate, wins, losses, closed_sample_size,
                                      roi_percent, qualifying_pnl, matches_criteria, lb_pnl)
           VALUES (?, ?, 10, 22000.0, 500.0, 1500.0, 100, 78.5, 20.0,
                   'high_winrate_review', 'follow', 'informed_edge', 0.65, 13, 7, 20,
                   15.0, 5000.0, 1, 22000.0)""",
        (wallet, now_ts),
    )
    conn.execute(
        """INSERT INTO wallet_profiles (wallet, username, pseudonym, bio,
                                        profile_prediction_count, profile_joined_ts)
           VALUES (?, 'theo4', 'Theo', 'Crypto speculator.', 234, 1700000000)""",
        (wallet,),
    )
    conn.commit()


class TestWalletPageCompilation(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        return conn

    def test_wallet_page_contains_core_fields(self):
        conn = self._conn()
        _seed_wallet_db(conn, "0xabc123")
        rendered = ck.render_wallet_page(conn, wallet="0xabc123")
        self.assertIn("0xabc123", rendered)
        self.assertIn("Theo4", rendered)
        self.assertIn("high_winrate_review", rendered)
        self.assertIn("78.5", rendered)  # insider_score
        self.assertIn("<!-- HUMAN:START -->", rendered)

    def test_wallet_without_analysis_still_renders(self):
        conn = self._conn()
        conn.execute(
            """INSERT INTO followed_wallets (wallet, label, added_reason, status)
               VALUES ('0xnew', 'Fresh', 'just-added', 'active')""",
        )
        conn.commit()
        rendered = ck.render_wallet_page(conn, wallet="0xnew")
        self.assertIn("0xnew", rendered)
        self.assertIn("(not yet analyzed)", rendered)


# ---------------------------------------------------------------------------
# Curated wallet list (followed ∪ watchlist, dedup, cap)
# ---------------------------------------------------------------------------


class TestCuratedWalletList(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        return conn

    def test_followed_before_watchlist(self):
        conn = self._conn()
        conn.execute("INSERT INTO followed_wallets (wallet, label) VALUES ('0xfollow', 'F')")
        conn.execute(
            "INSERT INTO watchlist_wallets (wallet, label, added_ts) VALUES ('0xwatch', 'W', 1)"
        )
        conn.commit()
        wallets = ck.curated_wallets(conn, cap=100)
        self.assertEqual(wallets[0], "0xfollow")
        self.assertIn("0xwatch", wallets)

    def test_dedup_when_wallet_in_both(self):
        conn = self._conn()
        conn.execute("INSERT INTO followed_wallets (wallet, label) VALUES ('0xdup', 'F')")
        conn.execute(
            "INSERT INTO watchlist_wallets (wallet, label, added_ts) VALUES ('0xdup', 'W', 1)"
        )
        conn.commit()
        wallets = ck.curated_wallets(conn, cap=100)
        self.assertEqual(wallets.count("0xdup"), 1)

    def test_hard_cap_truncates(self):
        conn = self._conn()
        for i in range(150):
            conn.execute(
                "INSERT INTO watchlist_wallets (wallet, label, added_ts) VALUES (?, '', ?)",
                (f"0x{i:040x}", i),
            )
        conn.commit()
        wallets = ck.curated_wallets(conn, cap=100)
        self.assertEqual(len(wallets), 100)

    def test_watchlist_ordered_by_added_ts_desc(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO watchlist_wallets (wallet, label, added_ts) VALUES ('0xold', '', 100)"
        )
        conn.execute(
            "INSERT INTO watchlist_wallets (wallet, label, added_ts) VALUES ('0xnew', '', 200)"
        )
        conn.commit()
        wallets = ck.curated_wallets(conn, cap=100)
        self.assertLess(wallets.index("0xnew"), wallets.index("0xold"))


# ---------------------------------------------------------------------------
# Atomic write + skip-if-unchanged
# ---------------------------------------------------------------------------


class TestAtomicWrite(unittest.TestCase):
    def test_does_not_touch_file_when_content_unchanged(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "page.md"
            p.write_text("hello\n")
            mtime_before = p.stat().st_mtime_ns
            time.sleep(0.01)
            changed = ck.write_if_changed(p, "hello\n")
            self.assertFalse(changed)
            self.assertEqual(p.stat().st_mtime_ns, mtime_before)

    def test_writes_when_content_differs(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "page.md"
            p.write_text("old\n")
            changed = ck.write_if_changed(p, "new\n")
            self.assertTrue(changed)
            self.assertEqual(p.read_text(), "new\n")


class TestRenderIdempotence(unittest.TestCase):
    """Regression: rendered bodies must be byte-stable across consecutive runs
    (no wall-clock timestamps in the body). write_if_changed depends on this."""

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        return conn

    def test_strategy_page_byte_stable(self):
        conn = self._conn()
        _seed_strategy_db(conn)
        first = ck.render_strategy_page(conn, strategy_id=21)
        second = ck.render_strategy_page(conn, strategy_id=21, existing_content=first)
        self.assertEqual(first, second)

    def test_wallet_page_byte_stable(self):
        conn = self._conn()
        _seed_wallet_db(conn, "0xabc123")
        first = ck.render_wallet_page(conn, wallet="0xabc123")
        second = ck.render_wallet_page(conn, wallet="0xabc123", existing_content=first)
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# C1 regression — HUMAN block survives nested/quoted marker in notes
# ---------------------------------------------------------------------------


class TestHumanBlockNestedMarkers(unittest.TestCase):
    """Operator notes may quote the marker format (e.g. inside a fenced code
    block referencing this spec). Depth-based parser must take the
    outermost span; the round-trip must preserve all operator content."""

    def test_roundtrip_with_inner_marker_literal_in_notes(self):
        operator_note = textwrap.dedent("""\
            My note.

            I am quoting the marker format for reference:

            ```
            <!-- HUMAN:START -->
            inner example (inside a code block)
            <!-- HUMAN:END -->
            ```

            The text after the quoted end-marker must still be preserved.
        """).rstrip()
        original = (
            f"{ck.HUMAN_START}\n{operator_note}\n{ck.HUMAN_END}\n\n"
            "# old machine body\n"
        )
        new_body = "# new machine body\nfresh\n"
        result = ck.splice_human_block(original=original, new_body=new_body)
        for line in operator_note.splitlines():
            if line.strip():
                self.assertIn(line, result)
        self.assertIn("The text after the quoted end-marker must still be preserved.", result)
        self.assertIn("# new machine body", result)
        self.assertNotIn("# old machine body", result)


class TestHumanBlockCorruptionGuards(unittest.TestCase):
    """Structural checks beyond simple marker balance. Previous implementation
    passed the balance guard on corrupt files with two separate complete
    blocks and silently pulled machine content into the preserved span."""

    def test_refuses_multiple_top_level_blocks(self):
        """Two separate complete HUMAN blocks with machine content between them
        is corruption — refuse, do not splice."""
        original = (
            "<!-- HUMAN:START -->\nnote 1\n<!-- HUMAN:END -->\n\n"
            "# machine body that must not leak\n\n"
            "<!-- HUMAN:START -->\nnote 2\n<!-- HUMAN:END -->\n\n"
            "# more machine\n"
        )
        with self.assertRaises(ck.HumanBlockCorruptedError):
            ck.splice_human_block(original=original, new_body="# new\n")

    def test_refuses_three_or_more_top_level_blocks(self):
        original = (
            "<!-- HUMAN:START -->\na\n<!-- HUMAN:END -->\n"
            "<!-- HUMAN:START -->\nb\n<!-- HUMAN:END -->\n"
            "<!-- HUMAN:START -->\nc\n<!-- HUMAN:END -->\n"
        )
        with self.assertRaises(ck.HumanBlockCorruptedError):
            ck.splice_human_block(original=original, new_body="# new\n")

    def test_refuses_end_without_start(self):
        original = "# body\n<!-- HUMAN:END -->\nrest\n"
        with self.assertRaises(ck.HumanBlockCorruptedError):
            ck.splice_human_block(original=original, new_body="# new\n")

    def test_refuses_start_without_end(self):
        original = "<!-- HUMAN:START -->\nhalf-written\n# body\n"
        with self.assertRaises(ck.HumanBlockCorruptedError):
            ck.splice_human_block(original=original, new_body="# new\n")

    def test_nested_markers_inside_operator_note_are_allowed(self):
        """Sanity: the new parser still accepts the C1 scenario."""
        original = (
            "<!-- HUMAN:START -->\n"
            "note with nested:\n"
            "<!-- HUMAN:START -->\ninner\n<!-- HUMAN:END -->\n"
            "trailing.\n"
            "<!-- HUMAN:END -->\n\n"
            "# machine body\n"
        )
        result = ck.splice_human_block(original=original, new_body="# new\n")
        self.assertIn("trailing.", result)
        self.assertIn("# new", result)
        self.assertNotIn("# machine body", result)


class TestHasOperatorNotes(unittest.TestCase):
    def test_scaffold_only_is_not_operator_content(self):
        content = (
            f"{ck.HUMAN_START}\n"
            f"{ck.HUMAN_SCAFFOLD_COMMENT}\n"
            f"{ck.HUMAN_END}\n\n# body\n"
        )
        self.assertFalse(ck.has_operator_notes(content))

    def test_empty_human_block_is_not_operator_content(self):
        content = f"{ck.HUMAN_START}\n\n{ck.HUMAN_END}\n\n# body\n"
        self.assertFalse(ck.has_operator_notes(content))

    def test_real_notes_are_operator_content(self):
        content = (
            f"{ck.HUMAN_START}\n"
            "My lesson: don't tune insider threshold below 90.\n"
            f"{ck.HUMAN_END}\n\n# body\n"
        )
        self.assertTrue(ck.has_operator_notes(content))

    def test_no_markers_is_not_operator_content(self):
        self.assertFalse(ck.has_operator_notes("# just machine body\n"))

    def test_corrupt_file_is_treated_as_having_notes(self):
        """Safety default: a corrupt file MIGHT contain notes, so the
        destructive path should refuse to touch it."""
        content = (
            "<!-- HUMAN:START -->\na\n<!-- HUMAN:END -->\n"
            "<!-- HUMAN:START -->\nb\n<!-- HUMAN:END -->\n"
        )
        self.assertTrue(ck.has_operator_notes(content))


# ---------------------------------------------------------------------------
# Orchestration — compile_strategies + compile_wallets end-to-end
# ---------------------------------------------------------------------------


class TestCompileStrategies(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        return conn

    def test_active_strategy_writes_active_page(self):
        import tempfile
        conn = self._conn()
        _seed_strategy_db(conn, strategy_id=21, status="active")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            changed, skipped = ck.compile_strategies(conn, root)
            page = root / "strategies" / "active" / "021-political_no_value_v1.md"
            self.assertTrue(page.exists())
            self.assertFalse((root / "strategies" / "retired" / "021-political_no_value_v1.md").exists())
            self.assertEqual(changed, 1)

    def test_retired_eligible_writes_retired_page_and_deletes_active(self):
        import tempfile
        conn = self._conn()
        # Seed as active first, compile, then flip to stopped with pause_reason.
        _seed_strategy_db(conn, strategy_id=21, status="active")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck.compile_strategies(conn, root)
            active_path = root / "strategies" / "active" / "021-political_no_value_v1.md"
            retired_path = root / "strategies" / "retired" / "021-political_no_value_v1.md"
            self.assertTrue(active_path.exists())

            conn.execute(
                "UPDATE strategies SET status = 'stopped', pause_reason = 'EV negative' WHERE id = 21"
            )
            conn.commit()
            changed, skipped = ck.compile_strategies(conn, root)

            self.assertFalse(active_path.exists(), "active page must be deleted on retired transition")
            self.assertTrue(retired_path.exists(), "retired page must be written")
            retired_content = retired_path.read_text()
            self.assertIn("Frozen snapshot", retired_content)

    def test_retired_transition_preserves_human_block(self):
        import tempfile
        conn = self._conn()
        _seed_strategy_db(conn, strategy_id=21, status="active")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck.compile_strategies(conn, root)
            active_path = root / "strategies" / "active" / "021-political_no_value_v1.md"
            # Inject operator note
            content = active_path.read_text()
            content = content.replace(
                ck.HUMAN_SCAFFOLD_COMMENT,
                "PRECIOUS OPERATOR LESSON — must survive retirement.",
            )
            active_path.write_text(content)

            conn.execute(
                "UPDATE strategies SET status = 'stopped', pause_reason = 'EV negative' WHERE id = 21"
            )
            conn.commit()
            ck.compile_strategies(conn, root)

            retired = (root / "strategies" / "retired" / "021-political_no_value_v1.md").read_text()
            self.assertIn("PRECIOUS OPERATOR LESSON", retired)

    def test_retired_page_frozen_on_second_compile(self):
        import tempfile
        conn = self._conn()
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, pause_reason, lifecycle_stage)
               VALUES (50, '0xw', 'Retired', 'retired_v1', 'stopped', -10.0, 5, 'EV neg', 'paper')""",
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck.compile_strategies(conn, root)
            retired_path = root / "strategies" / "retired" / "050-retired_v1.md"
            mtime_before = retired_path.stat().st_mtime_ns
            time.sleep(0.01)
            changed, skipped = ck.compile_strategies(conn, root)
            self.assertEqual(changed, 0)
            self.assertEqual(skipped, 1)
            self.assertEqual(retired_path.stat().st_mtime_ns, mtime_before)

    def test_retired_page_deletes_stale_active_twin(self):
        import tempfile
        conn = self._conn()
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, pause_reason, lifecycle_stage)
               VALUES (51, '0xw', 'Retired', 'retired_twin_v1', 'stopped', -10.0, 5, 'EV neg', 'paper')""",
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck.compile_strategies(conn, root)
            retired_path = root / "strategies" / "retired" / "051-retired_twin_v1.md"
            active_path = root / "strategies" / "active" / "051-retired_twin_v1.md"
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text("stale active twin\n")

            changed, skipped = ck.compile_strategies(conn, root)

            self.assertTrue(retired_path.exists())
            self.assertFalse(active_path.exists(), "stale active twin must be removed")
            self.assertEqual(changed, 1)
            self.assertEqual(skipped, 0)

    def test_stale_active_twin_with_operator_notes_is_preserved(self):
        """Safety gate consistency: stale-twin cleanup must honour operator
        notes the same way abandoned-experiment and wallet-prune paths do."""
        import contextlib
        import io
        import tempfile

        conn = self._conn()
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, pause_reason, lifecycle_stage)
               VALUES (52, '0xw', 'RetiredWithTwinNotes', 'retired_twin_notes_v1',
                       'stopped', -10.0, 5, 'EV neg', 'paper')""",
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # First compile: retired page gets written, active page never existed.
            ck.compile_strategies(conn, root)
            active_path = root / "strategies" / "active" / "052-retired_twin_notes_v1.md"
            # Plant a stale active twin with operator notes.
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text(
                f"{ck.HUMAN_START}\n"
                "OPERATOR NOTE the retired page never saw.\n"
                f"{ck.HUMAN_END}\n\n# stale body\n"
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                changed, skipped = ck.compile_strategies(conn, root)

            self.assertTrue(
                active_path.exists(),
                "stale twin with operator notes must be preserved",
            )
            self.assertIn("OPERATOR NOTE", active_path.read_text())
            self.assertIn("refusing to delete", stderr.getvalue().lower())
            self.assertGreaterEqual(skipped, 1)

    def test_abandoned_experiment_deletes_active_page(self):
        import tempfile
        conn = self._conn()
        # Seed active, compile, then stop with NO history -> abandoned
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, lifecycle_stage)
               VALUES (77, '0xw', 'Abandoned', 'abandoned_v1', 'active', 0.0, 0, 'paper')""",
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck.compile_strategies(conn, root)
            active_path = root / "strategies" / "active" / "077-abandoned_v1.md"
            self.assertTrue(active_path.exists())
            conn.execute("UPDATE strategies SET status = 'stopped' WHERE id = 77")
            conn.commit()
            ck.compile_strategies(conn, root)
            self.assertFalse(active_path.exists())
            retired_path = root / "strategies" / "retired" / "077-abandoned_v1.md"
            self.assertFalse(retired_path.exists(), "no retired page for abandoned")

    def test_abandoned_experiment_with_operator_notes_is_preserved(self):
        """Safety gate: never silently delete a page whose HUMAN block has
        operator content — even when the strategy is abandoned."""
        import tempfile
        conn = self._conn()
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, lifecycle_stage)
               VALUES (78, '0xw', 'AbandonedWithNotes', 'abn_notes_v1', 'active', 0.0, 0, 'paper')""",
        )
        conn.commit()
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck.compile_strategies(conn, root)
            active_path = root / "strategies" / "active" / "078-abn_notes_v1.md"
            # Inject an operator note
            content = active_path.read_text()
            content = content.replace(
                ck.HUMAN_SCAFFOLD_COMMENT,
                "Lesson learned: this entry filter was too loose.",
            )
            active_path.write_text(content)
            # Flip to stopped/abandoned
            conn.execute("UPDATE strategies SET status = 'stopped' WHERE id = 78")
            conn.commit()

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                changed, skipped = ck.compile_strategies(conn, root)
            err_out = stderr.getvalue()

            self.assertTrue(
                active_path.exists(),
                "page with operator notes must survive abandoned-cleanup",
            )
            self.assertIn("Lesson learned", active_path.read_text())
            self.assertIn("refusing to delete", err_out.lower())
            self.assertGreaterEqual(skipped, 1, "safety skip must be counted")

    def test_corrupt_active_page_does_not_abort_run(self):
        import tempfile
        conn = self._conn()
        _seed_strategy_db(conn, strategy_id=21, status="active")
        conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, status,
                                      pnl_usdc, trades_triggered, lifecycle_stage)
               VALUES (22, '0xw', 'Healthy', 'healthy_v1', 'active', 0.0, 0, 'paper')""",
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            active_dir = root / "strategies" / "active"
            active_dir.mkdir(parents=True)
            # Corrupt the page for strategy 21: only a START marker
            corrupt = active_dir / "021-political_no_value_v1.md"
            corrupt.write_text("<!-- HUMAN:START -->\nhalf-written\n# old body\n")
            changed, skipped = ck.compile_strategies(conn, root)
            # Healthy strategy 22 still compiled
            self.assertTrue((active_dir / "022-healthy_v1.md").exists())
            # Corrupt page was NOT overwritten
            self.assertIn("half-written", corrupt.read_text())
            self.assertGreaterEqual(skipped, 1)


class TestCompileWallets(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        return conn

    def test_wallets_written_and_dropped_wallet_is_pruned(self):
        import tempfile
        conn = self._conn()
        _seed_wallet_db(conn, "0xabc123")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck.compile_wallets(conn, root)
            page = root / "wallets" / "0xabc123.md"
            self.assertTrue(page.exists())
            # Drop the wallet from both lists
            conn.execute("DELETE FROM followed_wallets WHERE wallet = '0xabc123'")
            conn.commit()
            ck.compile_wallets(conn, root)
            self.assertFalse(page.exists(), "dropped wallet must be pruned")

    def test_dropped_wallet_with_operator_notes_is_preserved(self):
        """Safety gate: never silently delete a wallet page whose HUMAN block
        has operator content."""
        import tempfile
        import contextlib
        import io

        conn = self._conn()
        _seed_wallet_db(conn, "0xabc123")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ck.compile_wallets(conn, root)
            page = root / "wallets" / "0xabc123.md"
            content = page.read_text()
            content = content.replace(
                ck.HUMAN_SCAFFOLD_COMMENT,
                "Important: this wallet shadows another. Verify before copying.",
            )
            page.write_text(content)
            conn.execute("DELETE FROM followed_wallets WHERE wallet = '0xabc123'")
            conn.commit()

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                changed, skipped = ck.compile_wallets(conn, root)
            err_out = stderr.getvalue()

            self.assertTrue(
                page.exists(),
                "page with operator notes must survive de-curation",
            )
            self.assertIn("Important: this wallet shadows another", page.read_text())
            self.assertIn("refusing to delete", err_out.lower())
            self.assertGreaterEqual(skipped, 1, "safety skip must be counted")

    def test_prune_does_not_touch_non_wallet_files(self):
        """Sentinel files (README, INDEX, operator drops) in wallets/ must survive."""
        import tempfile
        conn = self._conn()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wallets_dir = root / "wallets"
            wallets_dir.mkdir(parents=True)
            readme = wallets_dir / "README.md"
            readme.write_text("# Operator notes about this layer\n")
            notes = wallets_dir / "_NOTES.md"
            notes.write_text("# Operator notes\n")
            ck.compile_wallets(conn, root)  # no curated wallets
            self.assertTrue(readme.exists())
            self.assertTrue(notes.exists())

    def test_dry_run_writes_nothing(self):
        import tempfile
        conn = self._conn()
        _seed_wallet_db(conn, "0xabc123")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            changed, skipped = ck.compile_wallets(conn, root, dry_run=True)
            self.assertFalse((root / "wallets" / "0xabc123.md").exists())
            self.assertFalse((root / "wallets").exists(), "dry-run must not create directories")
            self.assertGreaterEqual(changed, 1)


if __name__ == "__main__":
    unittest.main()
