from pathlib import Path

from data.backtest.schema import open_backtest_db
from data.sackmann import SackmannMatch
from scripts.backfill_stink_bid_backtest import (
    _align_event_indexed,
    _build_match_index,
    _load_gamma_events_jsonl,
    step_alignment,
    step_gamma_events,
    step_prices_history,
    step_sackmann_load,
    step_trades,
    trade_row_hash,
)


class FakeClient:
    def __init__(self):
        self.price_calls = []
        self.trade_calls = []
        self.gamma_calls = 0

    def get_resolved_tennis_markets(self, since_ts, until_ts):
        self.gamma_calls += 1
        return [
            {
                "condition_id": "cid1",
                "event_id": "ev1",
                "title": "US Open: Sinner vs Fritz",
                "event_slug": "atp-sinner-fritz",
                "tour_prefix": "atp",
                "token_id_yes": "tok_yes",
                "token_id_no": "tok_no",
                "start_date": "2024-09-08T12:00:00Z",
                "end_date": "2024-09-08T00:00:00Z",
            }
        ]

    def get_prices_series(self, token_id, start_ts, end_ts, fidelity=1):
        self.price_calls.append((token_id, start_ts, end_ts, fidelity))
        return [
            {"timestamp": 1725800000, "price": 0.55},
            {"timestamp": 1725800060, "price": 0.57},
        ]

    def get_trades_window(self, condition_id, start_ts, end_ts):
        self.trade_calls.append((condition_id, start_ts, end_ts))
        return [
            {
                "_ts_sec": 1725800010,
                "transactionHash": "0xtx",
                "asset": "tok_yes",
                "outcome": "YES",
                "price": "0.50",
                "size": "12.5",
                "side": "SELL",
            }
        ]

    def get_trades_window_with_meta(self, condition_id, start_ts, end_ts, page_size=200, max_pages=50):
        trades = self.get_trades_window(condition_id, start_ts, end_ts)
        return {
            "trades": trades,
            "pages_fetched": 1,
            "hit_page_cap": False,
            "first_trade_ts": min(t["_ts_sec"] for t in trades) if trades else None,
            "last_trade_ts": max(t["_ts_sec"] for t in trades) if trades else None,
        }


def _con(tmp_path):
    return open_backtest_db(str(tmp_path / "backtest.db"))


def test_backfill_gamma_events_inserts_rows(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    assert step_gamma_events(con, client, 2024, 2024) == 1
    row = con.execute("SELECT condition_id, title, token_id_yes, token_id_no FROM gamma_events").fetchone()
    assert row == ("cid1", "US Open: Sinner vs Fritz", "tok_yes", "tok_no")


def test_backfill_gamma_events_is_idempotent(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    step_gamma_events(con, client, 2024, 2024)
    assert con.execute("SELECT COUNT(*) FROM gamma_events").fetchone()[0] == 1


def test_gamma_jsonl_fallback_extracts_h2h_market(tmp_path):
    path = tmp_path / "events.jsonl"
    event = {
        "id": "ev1",
        "title": "Madrid: A vs B",
        "slug": "atp-a-b",
        "startDate": "2024-05-01T00:00:00Z",
        "endDate": "2024-05-02T00:00:00Z",
        "tags": [{"slug": "tennis"}],
        "markets": [
            {"question": "Set 1 Winner: A vs B", "conditionId": "set", "clobTokenIds": '["s1","s2"]'},
            {"question": "Madrid: A vs B", "conditionId": "cid", "clobTokenIds": '["t1","t2"]'},
        ],
    }
    path.write_text(__import__("json").dumps(event) + "\n")
    rows = _load_gamma_events_jsonl(path, since_ts=1704067200, until_ts=1735689599)
    assert rows == [
        {
            "condition_id": "cid",
            "event_id": "ev1",
            "title": "Madrid: A vs B",
            "event_slug": "atp-a-b",
            "tour_prefix": "atp",
            "token_id_yes": "t1",
            "token_id_no": "t2",
            "start_date": "2024-05-01T00:00:00Z",
            "end_date": "2024-05-02T00:00:00Z",
        }
    ]


def test_gamma_events_offline_mode_skips_live_client(tmp_path):
    path = tmp_path / "events.jsonl"
    event = {
        "id": "ev1",
        "title": "Madrid: A vs B",
        "slug": "atp-a-b",
        "startDate": "2024-05-01T00:00:00Z",
        "endDate": "2024-05-02T00:00:00Z",
        "tags": [{"slug": "tennis"}],
        "markets": [{"question": "Madrid: A vs B", "conditionId": "cid", "clobTokenIds": '["t1","t2"]'}],
    }
    path.write_text(__import__("json").dumps(event) + "\n")
    con = _con(tmp_path)
    client = FakeClient()
    assert step_gamma_events(con, client, 2024, 2024, gamma_jsonl=path, offline_gamma=True) == 1
    assert client.gamma_calls == 0


def test_gamma_jsonl_fallback_prefers_moneyline_when_question_differs(tmp_path):
    path = tmp_path / "events.jsonl"
    event = {
        "id": "ev1",
        "title": "Madrid Open: Raphael Collignon vs Matteo Berrettini",
        "slug": "atp-collign-berrett-2026-04-22",
        "startDate": "2026-04-22T00:00:00Z",
        "endDate": "2026-04-29T00:00:00Z",
        "tags": [{"slug": "tennis"}],
        "markets": [
            {
                "question": "Set 1 Winner: Collignon vs Berrettini",
                "conditionId": "set",
                "sportsMarketType": "tennis_first_set_winner",
                "clobTokenIds": '["s1","s2"]',
            },
            {
                "question": "Collignon vs. Berrettini",
                "conditionId": "cid",
                "sportsMarketType": "moneyline",
                "clobTokenIds": '["t1","t2"]',
            },
        ],
    }
    path.write_text(__import__("json").dumps(event) + "\n")
    rows = _load_gamma_events_jsonl(path, since_ts=1767225600, until_ts=1798761599)
    assert len(rows) == 1
    assert rows[0]["condition_id"] == "cid"
    assert rows[0]["title"] == "Madrid Open: Raphael Collignon vs Matteo Berrettini"


def test_gamma_jsonl_fallback_skips_doubles_markets(tmp_path):
    path = tmp_path / "events.jsonl"
    event = {
        "id": "ev1",
        "title": "Madrid Open Doubles: Arevalo/Pavic vs Bolelli/Vavassori",
        "slug": "atp-arevalo-pavic-bolelli-vavassori",
        "startDate": "2026-04-22T00:00:00Z",
        "endDate": "2026-04-29T00:00:00Z",
        "tags": [{"slug": "tennis"}],
        "markets": [
            {
                "question": "Madrid Open Doubles: Arevalo/Pavic vs Bolelli/Vavassori",
                "conditionId": "cid",
                "sportsMarketType": "moneyline",
                "clobTokenIds": '["t1","t2"]',
            }
        ],
    }
    path.write_text(__import__("json").dumps(event) + "\n")
    assert _load_gamma_events_jsonl(path, since_ts=1767225600, until_ts=1798761599) == []


def test_backfill_prices_history_skips_already_cached_tokens(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    assert step_prices_history(con, client, sleep_s=0) == 4
    assert step_prices_history(con, client, sleep_s=0) == 0
    assert len(client.price_calls) == 2
    assert con.execute("SELECT COUNT(*) FROM prices_history").fetchone()[0] == 4


def test_prices_history_aligned_only_skips_unaligned_events(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid2', 'ev2', 'Ghost Cup: A vs B', 'atp-a-b', 'atp', 'tok2_yes', 'tok2_no',
                '2024-09-08T12:00:00Z', '2024-09-08T00:00:00Z')
        """
    )
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES ('cid1', 'atp_main', '20240908', 'Jannik Sinner', 'Taylor Fritz', 0, 1.0)
        """
    )
    assert step_prices_history(con, client, sleep_s=0, aligned_only=True) == 4
    assert {call[0] for call in client.price_calls} == {"tok_yes", "tok_no"}


def test_prices_history_year_filter_uses_alignment_year(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid2', 'ev2', 'Future Cup: A vs B', 'atp-a-b', 'atp', 'tok2_yes', 'tok2_no',
                '2026-01-01T12:00:00Z', '2026-01-01T00:00:00Z')
        """
    )
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES
            ('cid1', 'atp_main', '20240908', 'Jannik Sinner', 'Taylor Fritz', 0, 1.0),
            ('cid2', 'atp_main', '20260101', 'A', 'B', 0, 1.0)
        """
    )
    assert step_prices_history(con, client, sleep_s=0, aligned_only=True, since_year=2026, until_year=2026) == 4
    assert {call[0] for call in client.price_calls} == {"tok2_yes", "tok2_no"}


def test_backfill_trades_inserts_and_skips_cached_market(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    assert step_trades(con, client, sleep_s=0) == 1
    assert step_trades(con, client, sleep_s=0) == 0
    row = con.execute(
        "SELECT condition_id, transaction_hash, asset, timestamp_sec, price, size, side FROM trades"
    ).fetchone()
    assert row == ("cid1", "0xtx", "tok_yes", 1725800010, 0.50, 12.5, "SELL")


def test_trades_aligned_only_skips_unaligned_events(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid2', 'ev2', 'Ghost Cup: A vs B', 'atp-a-b', 'atp', 'tok2_yes', 'tok2_no',
                '2024-09-08T12:00:00Z', '2024-09-08T00:00:00Z')
        """
    )
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES ('cid1', 'atp_main', '20240908', 'Jannik Sinner', 'Taylor Fritz', 0, 1.0)
        """
    )
    assert step_trades(con, client, sleep_s=0, aligned_only=True) == 1
    assert [call[0] for call in client.trade_calls] == ["cid1"]


def test_trades_year_filter_uses_alignment_year(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    con.execute(
        """
        INSERT INTO gamma_events
        (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid2', 'ev2', 'Future Cup: A vs B', 'atp-a-b', 'atp', 'tok2_yes', 'tok2_no',
                '2026-01-01T12:00:00Z', '2026-01-01T00:00:00Z')
        """
    )
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES
            ('cid1', 'atp_main', '20240908', 'Jannik Sinner', 'Taylor Fritz', 0, 1.0),
            ('cid2', 'atp_main', '20260101', 'A', 'B', 0, 1.0)
        """
    )
    assert step_trades(con, client, sleep_s=0, aligned_only=True, since_year=2026, until_year=2026) == 1
    assert [call[0] for call in client.trade_calls] == ["cid2"]


def test_trade_row_hash_distinguishes_duplicate_transaction_fills():
    base = {"transactionHash": "0xtx", "asset": "tok", "price": 0.5, "size": 1, "side": "BUY", "outcome": "YES"}
    other = dict(base, size=2)
    assert trade_row_hash("cid", base) != trade_row_hash("cid", other)
    assert trade_row_hash("cid", base) == trade_row_hash("cid", dict(base))


def test_sackmann_load_and_alignment(tmp_path):
    cache = tmp_path / "cache"
    atp = cache / "tennis_atp"
    atp.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "sackmann_atp_sample.csv"
    (atp / "atp_matches_2024.csv").write_text(fixture.read_text())

    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    assert step_sackmann_load(con, cache, years=[2024]) > 0
    con.execute(
        """
        INSERT OR REPLACE INTO sackmann_matches
        (tour, tourney_date, winner_name, loser_name, tourney_name, surface, tourney_level, minutes, score, source)
        VALUES ('atp_main', '20240908', 'Jannik Sinner', 'Taylor Fritz', 'US Open', 'Hard', 'G', 120, '6-3 6-4', 'sackmann')
        """
    )
    assert step_alignment(con, window_days=21) == 1
    row = con.execute("SELECT condition_id, sack_winner, sack_loser, confidence FROM event_alignment").fetchone()
    assert row[0] == "cid1"
    assert row[1] == "Jannik Sinner"
    assert row[2] == "Taylor Fritz"
    assert row[3] == 1.0


def test_alignment_removes_stale_rows_for_current_gamma_events(tmp_path):
    con = _con(tmp_path)
    client = FakeClient()
    step_gamma_events(con, client, 2024, 2024)
    con.execute(
        """
        INSERT INTO event_alignment
        (condition_id, sack_tour, sack_date, sack_winner, sack_loser, date_delta_days, confidence)
        VALUES ('cid1', 'atp_main', '20240101', 'Old Winner', 'Old Loser', 0, 1.0)
        """
    )
    assert step_alignment(con, window_days=21) == 0
    assert con.execute("SELECT COUNT(*) FROM event_alignment").fetchone()[0] == 0


def test_indexed_alignment_handles_tennis_data_abbreviated_names():
    matches = [
        SackmannMatch(
            tour="atp_main",
            winner_name="Sinner J.",
            loser_name="Alcaraz C.",
            tourney_date="20251116",
            tourney_name="Masters Cup",
        )
    ]
    aligned = _align_event_indexed(
        title="Masters Cup: Jannik Sinner vs Carlos Alcaraz",
        end_date_iso="2025-11-16T12:00:00Z",
        match_index=_build_match_index(matches),
        window_days=21,
    )
    assert aligned is not None
    assert aligned.winner_name == "Sinner J."


def test_indexed_alignment_handles_multiword_tennis_data_surnames():
    matches = [
        SackmannMatch(
            tour="atp_main",
            winner_name="Bautista Agut R.",
            loser_name="Mensik J.",
            tourney_date="20250618",
            tourney_name="HSBC Championships",
        )
    ]
    aligned = _align_event_indexed(
        title="HSBC Championships: Bautista Agut vs. Mensik",
        end_date_iso="2025-06-18T00:00:00Z",
        match_index=_build_match_index(matches),
        window_days=21,
    )
    assert aligned is not None
    assert aligned.winner_name == "Bautista Agut R."


def test_indexed_alignment_handles_abbreviated_mpetshi_title_alias():
    matches = [
        SackmannMatch(
            tour="atp_main",
            winner_name="Mpetshi Perricard G.",
            loser_name="Shapovalov D.",
            tourney_date="20250425",
            tourney_name="Madrid Open",
        )
    ]
    aligned = _align_event_indexed(
        title="Madrid Open: Mpetshi G. vs Denis Shapovalov",
        end_date_iso="2025-04-25T00:00:00Z",
        match_index=_build_match_index(matches),
        window_days=21,
    )
    assert aligned is not None
    assert aligned.winner_name == "Mpetshi Perricard G."
