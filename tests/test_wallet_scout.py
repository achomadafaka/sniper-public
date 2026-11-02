import sqlite3
import pytest

@pytest.fixture
def scout_db(tmp_path):
    p = str(tmp_path / "test.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE user_analysis (
            wallet TEXT PRIMARY KEY, insider_score REAL, speculator_score REAL,
            win_rate REAL, trade_count INTEGER, closed_sample_size INTEGER,
            classification TEXT, analyzed_ts INTEGER
        );
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT, label TEXT,
            strategy_type TEXT, parameters TEXT DEFAULT '{}', status TEXT DEFAULT 'active',
            pause_reason TEXT, paused_at TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE position_signals (
            wallet TEXT, condition_id TEXT, outcome TEXT, current_price REAL,
            is_open INTEGER, position_insider_score REAL,
            position_speculator_score REAL, captured_at TEXT
        );
        CREATE TABLE score_history (
            wallet TEXT, insider_score REAL, classification TEXT, analyzed_ts INTEGER
        );
        CREATE TABLE external_leaderboard_entries (
            wallet TEXT, category TEXT, time_period TEXT, order_by TEXT,
            rank INTEGER, official_pnl REAL, official_volume REAL, username TEXT
        );
    """)
    return p, c

def test_finds_dead_strategy(scout_db):
    """Strategy with wallet not in user_analysis should be detected."""
    p, c = scout_db
    c.execute("INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (1,'0xdead','Dead','insider_copy_v1','active','2026-03-20','2026-03-20')")
    c.commit()
    rows = c.execute("""
        SELECT s.id, s.label, s.status, s.wallet
        FROM strategies s
        WHERE s.strategy_type = 'insider_copy_v1' AND s.status IN ('active','paused')
          AND s.wallet NOT IN (SELECT wallet FROM user_analysis)
    """).fetchall()
    assert len(rows) == 1
    assert rows[0][3] == "0xdead"

def test_finds_high_score_candidates(scout_db):
    """Wallets with high insider scores not already copied should be found."""
    p, c = scout_db
    c.execute("INSERT INTO user_analysis VALUES ('0xgood',85,10,0.80,50,45,'high_winrate_review',1773960000)")
    c.execute("INSERT INTO user_analysis VALUES ('0xbot',85,75,0.60,100,80,'speculator_bot',1773960000)")
    c.commit()
    rows = c.execute("""
        SELECT wallet, insider_score, speculator_score, win_rate, closed_sample_size, classification
        FROM user_analysis
        WHERE insider_score >= 70
          AND speculator_score < 65
          AND closed_sample_size >= 15
          AND classification NOT IN ('speculator_bot','low_signal')
          AND wallet NOT IN (SELECT wallet FROM strategies WHERE status != 'stopped')
        ORDER BY insider_score DESC
        LIMIT 10
    """).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0xgood"

def test_filters_already_copied(scout_db):
    """Wallets with existing active strategy should be excluded."""
    p, c = scout_db
    c.execute("INSERT INTO user_analysis VALUES ('0xcopied',90,5,0.85,60,55,'insider_candidate',1773960000)")
    c.execute("INSERT INTO strategies (wallet, label, strategy_type, status, created_at, updated_at) VALUES ('0xcopied','Copy','insider_copy_v1','active','2026-03-20','2026-03-20')")
    c.commit()
    rows = c.execute("""
        SELECT wallet FROM user_analysis
        WHERE insider_score >= 70 AND speculator_score < 65 AND closed_sample_size >= 15
          AND classification NOT IN ('speculator_bot','low_signal')
          AND wallet NOT IN (SELECT wallet FROM strategies WHERE status != 'stopped')
    """).fetchall()
    assert len(rows) == 0

def test_checks_open_signals(scout_db):
    """Candidate must have open signals with good position-level scores and prices."""
    p, c = scout_db
    c.execute("INSERT INTO position_signals VALUES ('0xgood','0xcond1','Yes',0.45,1,72,10,'2026-03-20')")
    c.execute("INSERT INTO position_signals VALUES ('0xgood','0xcond2','No',0.60,1,68,15,'2026-03-20')")
    c.execute("INSERT INTO position_signals VALUES ('0xbad','0xcond3','Yes',0.95,1,20,5,'2026-03-20')")
    c.commit()
    rows = c.execute("""
        SELECT wallet, COUNT(*) as open_sigs,
            SUM(CASE WHEN position_insider_score >= 55 AND current_price <= 0.70 THEN 1 ELSE 0 END) as qualifying
        FROM position_signals
        WHERE is_open = 1
        GROUP BY wallet
        HAVING qualifying >= 1
    """).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0xgood"

def test_score_stability_check(scout_db):
    """Score drop > 15 points should be detectable."""
    p, c = scout_db
    c.executemany("INSERT INTO score_history VALUES (?,?,?,?)", [
        ("0xstable", 80, "insider_candidate", 1773950000),
        ("0xstable", 78, "insider_candidate", 1773955000),
        ("0xstable", 82, "insider_candidate", 1773960000),
        ("0xdropper", 85, "insider_candidate", 1773950000),
        ("0xdropper", 70, "high_winrate_review", 1773955000),
        ("0xdropper", 65, "high_winrate_review", 1773960000),
    ])
    c.commit()
    rows = c.execute("""
        SELECT wallet, MAX(insider_score) - MIN(insider_score) as score_range
        FROM (
            SELECT wallet, insider_score
            FROM score_history
            WHERE wallet = '0xdropper'
            ORDER BY analyzed_ts DESC LIMIT 5
        )
    """).fetchall()
    assert rows[0][1] == 20  # 85 - 65 = 20 > 15 threshold
