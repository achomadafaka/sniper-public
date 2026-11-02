from scripts.scrape_atp_wta_results import (
    _column_index,
    excel_serial_to_yyyymmdd,
    parse_tennis_data_rows,
    tennis_data_url,
)


def test_tennis_data_url_uses_http_xlsx_paths():
    assert tennis_data_url(2025, "atp_main") == "http://www.tennis-data.co.uk/2025/2025.xlsx"
    assert tennis_data_url(2025, "wta_main") == "http://www.tennis-data.co.uk/2025w/2025.xlsx"


def test_excel_serial_to_yyyymmdd():
    assert excel_serial_to_yyyymmdd("45658") == "20250101"


def test_column_index_handles_excel_refs():
    assert _column_index("A1") == 0
    assert _column_index("Z1") == 25
    assert _column_index("AA1") == 26


def test_parse_tennis_data_rows_to_sackmann_matches():
    rows = [
        [
            "ATP", "Location", "Tournament", "Date", "Series", "Court", "Surface",
            "Round", "Best of", "Winner", "Loser", "W1", "L1", "W2", "L2",
            "Wsets", "Lsets", "Comment",
        ],
        [
            "1", "Brisbane", "Brisbane International", "45658", "ATP250", "Outdoor",
            "Hard", "1st Round", "3", "Vukic A.", "Goffin D.", "6", "2",
            "6", "3", "2", "0", "Completed",
        ],
    ]
    matches = parse_tennis_data_rows(rows, tour="atp_main", source="tennis-data.co.uk")
    assert len(matches) == 1
    match = matches[0]
    assert match.tour == "atp_main"
    assert match.winner_name == "Vukic A."
    assert match.loser_name == "Goffin D."
    assert match.tourney_date == "20250101"
    assert match.tourney_name == "Brisbane International"
    assert match.surface == "Hard"
    assert match.tourney_level == "A"
    assert match.score == "6-2 6-3"
    assert match.source == "tennis-data.co.uk"


def test_parse_tennis_data_rows_skips_unfinished_rows():
    rows = [
        ["Tournament", "Date", "Winner", "Loser", "Comment"],
        ["Brisbane", "45655", "A", "B", "Scheduled"],
    ]
    assert parse_tennis_data_rows(rows, tour="atp_main", source="x") == []
