import json
import sqlite3

from scripts.tennis_inverse_paper_daemon import (
    InverseCandidate,
    candidate_from_pair,
    insert_paper_position,
    run_once,
    should_enter_inverse,
)


class FlakyConnection:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.row_factory = inner.row_factory

    def execute(self, sql, params=()):
        if sql.strip().startswith("INSERT INTO strategy_positions") and self.calls == 0:
            self.calls += 1
            raise sqlite3.OperationalError("database is locked")
        self.calls += 1
        return self.inner.execute(sql, params)

    def commit(self):
        return self.inner.commit()

    def rollback(self):
        return self.inner.rollback()


def test_candidate_from_pair_selects_favorite_and_underdog():
    pair = {
        "event": {"title": "Wimbledon: Alice Smith vs Bob Jones"},
        "market": {
            "conditionId": "cid",
            "outcomes": '["Alice Smith", "Bob Jones"]',
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "outcomePrices": '["0.72", "0.28"]',
        },
    }

    candidate = candidate_from_pair(pair, min_ref=0.60, max_ref=0.90)

    assert candidate is not None
    assert candidate.favorite_label == "Alice Smith"
    assert candidate.underdog_label == "Bob Jones"
    assert candidate.ref_price == 0.72


def test_candidate_from_pair_uses_saved_original_favorite_after_flip():
    pair = {
        "event": {"title": "Wimbledon: Alice Smith vs Bob Jones"},
        "market": {
            "conditionId": "cid",
            "outcomes": '["Alice Smith", "Bob Jones"]',
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "outcomePrices": '["0.49", "0.51"]',
        },
    }
    saved = {
        "ref_price": 0.72,
        "favorite_label": "Alice Smith",
        "favorite_token_id": "tok_yes",
        "underdog_label": "Bob Jones",
        "underdog_token_id": "tok_no",
    }

    candidate = candidate_from_pair(pair, min_ref=0.60, max_ref=0.90, saved=saved)

    assert candidate is not None
    assert candidate.favorite_label == "Alice Smith"
    assert candidate.favorite_token_id == "tok_yes"
    assert candidate.favorite_price == 0.49
    assert candidate.underdog_label == "Bob Jones"
    assert candidate.underdog_token_id == "tok_no"
    assert candidate.underdog_price == 0.51
    assert candidate.ref_price == 0.72


def test_candidate_from_pair_backfills_legacy_saved_state_without_underdog():
    pair = {
        "event": {"title": "Wimbledon: Alice Smith vs Bob Jones"},
        "market": {
            "conditionId": "cid",
            "outcomes": '["Alice Smith", "Bob Jones"]',
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "outcomePrices": '["0.49", "0.51"]',
        },
    }
    saved = {
        "ref_price": 0.72,
        "favorite_label": "Alice Smith",
        "favorite_token_id": "tok_yes",
    }

    candidate = candidate_from_pair(pair, min_ref=0.60, max_ref=0.90, saved=saved)

    assert candidate is not None
    assert candidate.favorite_token_id == "tok_yes"
    assert candidate.underdog_token_id == "tok_no"
    assert candidate.underdog_label == "Bob Jones"


def test_should_enter_inverse_on_favorite_drop():
    candidate = InverseCandidate(
        condition_id="cid",
        title="A vs B",
        favorite_label="A",
        favorite_token_id="tok_a",
        favorite_price=0.49,
        underdog_label="B",
        underdog_token_id="tok_b",
        underdog_price=0.51,
        ref_price=0.72,
    )

    decision = should_enter_inverse(candidate, mode="favorite_drop_fade", drop_pct=0.30, max_entry_price=0.98)

    assert decision.enter
    assert decision.reason == "favorite_drop_fade_entry"
    assert decision.entry_token_id == "tok_b"


def test_should_enter_inverse_on_favorite_flip():
    candidate = InverseCandidate(
        condition_id="cid",
        title="A vs B",
        favorite_label="A",
        favorite_token_id="tok_a",
        favorite_price=0.49,
        underdog_label="B",
        underdog_token_id="tok_b",
        underdog_price=0.51,
        ref_price=0.72,
    )

    decision = should_enter_inverse(candidate, mode="favorite_flip_fade", drop_pct=0.30, max_entry_price=0.98)

    assert decision.enter
    assert decision.reason == "favorite_flip_fade_entry"


def test_insert_paper_position_is_idempotent_for_active_position():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            state TEXT NOT NULL,
            token_id TEXT NOT NULL DEFAULT '',
            entry_price REAL NOT NULL DEFAULT 0,
            avg_entry_price REAL NOT NULL DEFAULT 0,
            entry_shares REAL NOT NULL DEFAULT 0,
            filled_entry_shares REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            current_value REAL NOT NULL DEFAULT 0,
            size_usdc REAL NOT NULL DEFAULT 0,
            opened_ts INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL DEFAULT 0,
            instance_tag TEXT,
            version INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX uq_strategy_positions_active_owner
            ON strategy_positions(wallet, condition_id, outcome)
            WHERE state IN ('pending_entry', 'open', 'pending_exit');
        """
    )
    candidate = InverseCandidate(
        condition_id="cid",
        title="A vs B",
        favorite_label="A",
        favorite_token_id="tok_a",
        favorite_price=0.49,
        underdog_label="B",
        underdog_token_id="tok_b",
        underdog_price=0.51,
        ref_price=0.72,
    )

    first = insert_paper_position(conn, strategy_id=42, candidate=candidate, entry_price=0.54, size_usdc=25)
    second = insert_paper_position(conn, strategy_id=42, candidate=candidate, entry_price=0.54, size_usdc=25)

    assert first is not None
    assert second is None
    row = conn.execute("SELECT * FROM strategy_positions").fetchone()
    assert row["wallet"] == "paper:tennis-inverse:42"
    assert row["outcome"] == "B"
    assert row["entry_shares"] == 25 / 0.54


def test_insert_paper_position_retries_transient_db_lock(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            state TEXT NOT NULL,
            token_id TEXT NOT NULL DEFAULT '',
            entry_price REAL NOT NULL DEFAULT 0,
            avg_entry_price REAL NOT NULL DEFAULT 0,
            entry_shares REAL NOT NULL DEFAULT 0,
            filled_entry_shares REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            current_value REAL NOT NULL DEFAULT 0,
            size_usdc REAL NOT NULL DEFAULT 0,
            opened_ts INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL DEFAULT 0,
            instance_tag TEXT,
            version INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    monkeypatch.setattr("scripts.tennis_inverse_paper_daemon.time.sleep", lambda _seconds: None)
    candidate = InverseCandidate(
        condition_id="cid",
        title="A vs B",
        favorite_label="A",
        favorite_token_id="tok_a",
        favorite_price=0.49,
        underdog_label="B",
        underdog_token_id="tok_b",
        underdog_price=0.51,
        ref_price=0.72,
    )

    position_id = insert_paper_position(
        FlakyConnection(conn),
        strategy_id=42,
        candidate=candidate,
        entry_price=0.54,
        size_usdc=25,
        max_attempts=2,
    )

    assert position_id == 1


def test_run_once_enters_when_saved_original_favorite_drops_after_flip(tmp_path, monkeypatch):
    db = tmp_path / "paper.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            state TEXT NOT NULL,
            token_id TEXT NOT NULL DEFAULT '',
            entry_price REAL NOT NULL DEFAULT 0,
            avg_entry_price REAL NOT NULL DEFAULT 0,
            entry_shares REAL NOT NULL DEFAULT 0,
            filled_entry_shares REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            current_value REAL NOT NULL DEFAULT 0,
            size_usdc REAL NOT NULL DEFAULT 0,
            opened_ts INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL DEFAULT 0,
            instance_tag TEXT,
            version INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX uq_strategy_positions_active_owner
            ON strategy_positions(wallet, condition_id, outcome)
            WHERE state IN ('pending_entry', 'open', 'pending_exit');
        """
    )
    conn.close()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "cid": {
                    "ref_price": 0.72,
                    "favorite_label": "Alice Smith",
                    "favorite_token_id": "tok_yes",
                    "underdog_label": "Bob Jones",
                    "underdog_token_id": "tok_no",
                }
            }
        )
    )
    pair = {
        "event": {"title": "Wimbledon: Alice Smith vs Bob Jones"},
        "market": {
            "conditionId": "cid",
            "outcomes": '["Alice Smith", "Bob Jones"]',
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "outcomePrices": '["0.49", "0.51"]',
        },
    }
    monkeypatch.setattr("scripts.tennis_inverse_paper_daemon._fetch_gamma_tennis_h2h", lambda session: [pair])

    out = run_once(
        db_path=db,
        strategy_id=44,
        state_path=state_path,
        mode="favorite_drop_fade",
        drop_pct=0.30,
        min_ref=0.60,
        max_ref=0.90,
        slippage=0.03,
        max_entry_price=0.98,
        position_size_usdc=25,
    )

    assert out["opened"][0]["outcome"] == "Bob Jones"
    assert out["opened"][0]["entry_price"] == 0.54


def test_run_once_dry_run_reports_would_open_without_insert(tmp_path, monkeypatch):
    db = tmp_path / "paper.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            state TEXT NOT NULL,
            token_id TEXT NOT NULL DEFAULT '',
            entry_price REAL NOT NULL DEFAULT 0,
            avg_entry_price REAL NOT NULL DEFAULT 0,
            entry_shares REAL NOT NULL DEFAULT 0,
            filled_entry_shares REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            current_value REAL NOT NULL DEFAULT 0,
            size_usdc REAL NOT NULL DEFAULT 0,
            opened_ts INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL DEFAULT 0,
            instance_tag TEXT,
            version INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.close()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "cid": {
                    "ref_price": 0.72,
                    "favorite_label": "Alice Smith",
                    "favorite_token_id": "tok_yes",
                    "underdog_label": "Bob Jones",
                    "underdog_token_id": "tok_no",
                }
            }
        )
    )
    pair = {
        "event": {"title": "Wimbledon: Alice Smith vs Bob Jones"},
        "market": {
            "conditionId": "cid",
            "outcomes": '["Alice Smith", "Bob Jones"]',
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "outcomePrices": '["0.49", "0.51"]',
        },
    }
    monkeypatch.setattr("scripts.tennis_inverse_paper_daemon._fetch_gamma_tennis_h2h", lambda session: [pair])

    out = run_once(
        db_path=db,
        strategy_id=44,
        state_path=state_path,
        mode="favorite_drop_fade",
        drop_pct=0.30,
        min_ref=0.60,
        max_ref=0.90,
        slippage=0.03,
        max_entry_price=0.98,
        position_size_usdc=25,
        dry_run=True,
    )

    assert out["would_open"][0]["outcome"] == "Bob Jones"
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM strategy_positions").fetchone()[0]
    conn.close()
    assert count == 0
