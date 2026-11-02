"""The grader joins exact retained source transactions and reads the configured state root."""
import sqlite3

from scripts.research.rh_state_stale_grade import slow_path

MINT, TX = "0x" + "aa" * 20, "0x" + "bb" * 32


def _ledger(rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE memecoin_entry_attempts (network_id TEXT, mint TEXT, origin_event_id TEXT, "
               "source_user_id TEXT, delivery_lane TEXT, decision_started_at TEXT, terminal_status TEXT, "
               "terminal_reason TEXT)")
    db.executemany("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?)", rows)
    return db


def _row(uid="u1"):
    return dict(tx_hash=TX, mint=MINT, decided_at="2026-10-08T03:40:59.380000+00:00", source_user_id=uid)


def test_an_opaque_same_wallet_j7_attempt_is_censored():
    db = _ledger([("4663", MINT, "swap:x", "u1", "j7_wallets", "2026-10-08T03:40:59.780000+00:00",
                   "refused", "mcap $1,170,012 over ceiling $250,000 (over_ceiling)")])
    assert slow_path(db, _row()) == "source_identity_unknown"


def test_another_transaction_cannot_replace_the_same_source_verdict():
    wallet = "0x" + "dd" * 20
    db = _ledger([
        ("4663", MINT, f"swap:raybot:0x{'cc' * 32}:{wallet}:{MINT}", "u1", "j7_wallets",
         "2026-10-08T03:40:59.780000+00:00", "filled", "filled"),
        ("4663", MINT, f"swap:raybot:{TX}:{wallet}:{MINT}", "u1", "j7_wallets",
         "2026-10-08T03:41:00.780000+00:00", "refused", "copy price exceeds cap"),
    ])
    assert slow_path(db, _row()) == "refused_chase"


def test_a_different_exact_source_in_the_window_is_not_a_match():
    wallet = "0x" + "dd" * 20
    db = _ledger([("4663", MINT, f"swap:raybot:0x{'cc' * 32}:{wallet}:{MINT}", "u1", "j7_wallets",
                   "2026-10-08T03:40:59.780000+00:00", "filled", "filled")])
    assert slow_path(db, _row()) == "no_attempt"


def test_default_and_explicit_ledger_paths_are_read_only(monkeypatch, tmp_path):
    from scripts.research import rh_state_stale_grade as grade
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    paths = []

    class Empty:
        def execute(self, *_a, **_kw):
            return []

    def connect(path, **kwargs):
        paths.append(path)
        assert kwargs == {"uri": True} and path.endswith("?mode=ro")
        return Empty()

    monkeypatch.setattr(grade.sqlite3, "connect", connect)
    grade.main(["--since", "2026-10-07T00:00:00+00:00"])
    assert paths[1] == f"file:{tmp_path / 'sniper_tracker.db'}?mode=ro"
    override = tmp_path / "other.db"
    grade.main(["--since", "2026-10-07T00:00:00+00:00", "--ledger", str(override)])
    assert paths[3] == f"file:{override}?mode=ro"


def test_another_wallets_j7_attempt_or_one_outside_the_window_is_not():
    db = _ledger([("4663", MINT, "swap:x", "u2", "j7_wallets", "2026-10-08T03:40:59.780000+00:00",
                   "filled", "filled"),
                  ("4663", MINT, "swap:y", "u1", "j7_wallets", "2026-10-08T03:43:30.000000+00:00",
                   "filled", "filled")])
    assert slow_path(db, _row()) == "no_attempt"


def test_the_onchain_attempt_on_the_tx_wins_and_copy_price_is_a_price_cap():
    db = _ledger([("4663", MINT, "onchain:4663:%s:%s" % (TX, MINT), "u1", None, "2026-10-08T03:41:00+00:00",
                   "refused", "copy price minimum is +31.2% above source fill (cap 30.0%)")])
    assert slow_path(db, _row()) == "refused_chase"
