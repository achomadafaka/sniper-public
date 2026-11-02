"""Tests for data/sackmann.py — CSV parser + name normalization."""
from pathlib import Path

from data.sackmann import (
    SackmannMatch,
    normalize_name,
    parse_sackmann_csv,
    player_sides_from_title,
    side_name_candidates,
    surnames_from_title,
    title_looks_like_doubles,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
ATP_FIXTURE = FIXTURE_DIR / "sackmann_atp_sample.csv"


def test_parse_sackmann_csv_returns_list_of_dataclass():
    matches = parse_sackmann_csv(ATP_FIXTURE, tour="atp_main")
    assert len(matches) == 11, f"expected 11 rows, got {len(matches)}"
    m = matches[0]
    assert isinstance(m, SackmannMatch)
    assert m.tour == "atp_main"
    assert m.winner_name
    assert m.loser_name
    assert m.tourney_date.isdigit()
    assert len(m.tourney_date) == 8  # YYYYMMDD
    assert m.surface in {"Hard", "Clay", "Grass", "Carpet", None, ""}


def test_parse_sackmann_csv_handles_missing_file():
    matches = parse_sackmann_csv(Path("/nonexistent/path.csv"), tour="atp_main")
    assert matches == []


def test_parse_sackmann_csv_parses_minutes_as_int_or_none():
    matches = parse_sackmann_csv(ATP_FIXTURE, tour="atp_main")
    for m in matches:
        assert m.minutes is None or isinstance(m.minutes, int)


def test_parse_sackmann_csv_rejects_bad_tour():
    import pytest
    with pytest.raises(ValueError):
        parse_sackmann_csv(ATP_FIXTURE, tour="not_a_tour")


def test_normalize_name_strips_diacritics():
    assert normalize_name("Iga Świątek") == "iga swiatek"
    assert normalize_name("Karolína Muchová") == "karolina muchova"
    assert normalize_name("Djokovic") == "djokovic"


def test_normalize_name_lowercases_and_strips_punct():
    assert normalize_name("  O'Connell  ") == "oconnell"
    assert normalize_name("J.P. Morgan") == "jp morgan"
    assert normalize_name("Carlos Alcaraz-Garfia") == "carlos alcarazgarfia"


def test_surnames_from_title_simple():
    n1, n2 = surnames_from_title("Madrid Open: Tatjana Maria vs Laura Samson")
    assert n1 == "Maria"
    assert n2 == "Samson"


def test_surnames_from_title_with_period():
    n1, n2 = surnames_from_title("US Open Final: Sinner vs. Fritz")
    assert n1 == "Sinner"
    assert n2 == "Fritz"


def test_surnames_from_title_without_prefix():
    n1, n2 = surnames_from_title("Alcaraz vs Nadal")
    assert n1 == "Alcaraz"
    assert n2 == "Nadal"


def test_surnames_from_title_bad_format():
    n1, n2 = surnames_from_title("French Open Winner")
    assert n1 is None and n2 is None


def test_player_sides_from_title_handles_market_suffix():
    n1, n2 = player_sides_from_title("Collignon vs. Berrettini: Match O/U 21.5")
    assert n1 == "Collignon"
    assert n2 == "Berrettini"


def test_side_name_candidates_handles_abbreviated_mpetshi_alias():
    assert "mpetshi" in side_name_candidates("Mpetshi G.")
    assert "perricard" in side_name_candidates("Giovanni Mpetshi Perricard")


def test_title_looks_like_doubles_from_slash_side():
    assert title_looks_like_doubles("Madrid Open: Arevalo/Pavic vs Bolelli/Vavassori") is True
    assert title_looks_like_doubles("Madrid Open: Alex de Minaur vs Karen Khachanov") is False


# ---------------------------------------------------------------------------
# Alignment tests (Task 6)
# ---------------------------------------------------------------------------

from data.sackmann import (
    AlignedMatch,
    align_event_to_sackmann,
    alignment_success_rate,
)


def _sack(tour, w, l, date, tname="Test Cup", surface="Hard", level="A", minutes=100):
    return SackmannMatch(
        tour=tour, winner_name=w, loser_name=l, tourney_date=date,
        tourney_name=tname, surface=surface, tourney_level=level, minutes=minutes,
    )


def test_align_event_to_sackmann_exact_date_hit():
    sack = [_sack("atp_main", "Jannik Sinner", "Taylor Fritz", "20240908", tname="Us Open", level="G")]
    m = align_event_to_sackmann(
        title="US Open Final: Sinner vs. Fritz",
        end_date_iso="2024-09-08T00:00:00Z",
        sackmann_matches=sack,
        window_days=21,
    )
    assert m is not None
    assert m.sackmann.winner_name == "Jannik Sinner"
    assert m.sackmann.loser_name == "Taylor Fritz"
    assert m.date_delta_days == 0
    assert m.alignment_confidence == 1.0
    # AlignedMatch property shortcuts work
    assert m.winner_name == "Jannik Sinner"
    assert m.loser_name == "Taylor Fritz"


def test_align_event_to_sackmann_within_window():
    # 7 days delta, window 21 -> confidence = 1 - 7/21 = 0.6666...
    sack = [_sack("atp_main", "Jannik Sinner", "Taylor Fritz", "20240901")]
    m = align_event_to_sackmann(
        title="US Open: Sinner vs Fritz",
        end_date_iso="2024-09-08T00:00:00Z",
        sackmann_matches=sack,
        window_days=21,
    )
    assert m is not None
    assert m.date_delta_days == 7
    assert 0.66 < m.alignment_confidence < 0.68


def test_align_event_rejects_outside_window():
    sack = [_sack("atp_main", "Jannik Sinner", "Taylor Fritz", "20230908")]
    m = align_event_to_sackmann(
        title="US Open Final: Sinner vs. Fritz",
        end_date_iso="2024-09-08T00:00:00Z",
        sackmann_matches=sack,
        window_days=21,
    )
    assert m is None


def test_align_event_handles_unicode_surnames():
    # Polymarket title uses ASCII "Swiatek"; Sackmann stores Unicode "Świątek".
    # Alignment must succeed despite diacritic difference.
    sack = [_sack("wta_main", "Iga Świątek", "Coco Gauff", "20240128", tname="Australian Open", level="G")]
    m = align_event_to_sackmann(
        title="Australian Open: Swiatek vs. Gauff",
        end_date_iso="2024-01-28T00:00:00Z",
        sackmann_matches=sack,
    )
    assert m is not None
    assert m.sackmann.winner_name == "Iga Świątek"
    assert m.sackmann.loser_name == "Coco Gauff"


def test_align_event_picks_closest_date_when_multiple_candidates():
    # Two Sackmann rows with same surnames but different dates; closest wins.
    sack = [
        _sack("atp_main", "Jannik Sinner", "Taylor Fritz", "20240101"),  # 8-month delta, too far
        _sack("atp_main", "Jannik Sinner", "Taylor Fritz", "20240910"),  # 2 days
        _sack("atp_main", "Jannik Sinner", "Taylor Fritz", "20240915"),  # 7 days
    ]
    m = align_event_to_sackmann(
        title="US Open Final: Sinner vs. Fritz",
        end_date_iso="2024-09-08T00:00:00Z",
        sackmann_matches=sack,
        window_days=21,
    )
    assert m is not None
    assert m.sackmann.tourney_date == "20240910"


def test_align_event_bad_title_returns_none():
    sack = [_sack("atp_main", "A", "B", "20240101")]
    m = align_event_to_sackmann(
        title="French Open Winner",
        end_date_iso="2024-01-01T00:00:00Z",
        sackmann_matches=sack,
    )
    assert m is None


def test_align_event_bad_date_returns_none():
    sack = [_sack("atp_main", "A Smith", "B Jones", "20240101")]
    m = align_event_to_sackmann(
        title="Cup: Smith vs Jones",
        end_date_iso="not-a-date",
        sackmann_matches=sack,
    )
    assert m is None


def test_alignment_success_rate_reports_hits_and_rows():
    events = [
        {"condition_id": "c1", "title": "US Open: Sinner vs Fritz", "end_date": "2024-09-08T00:00:00Z"},
        {"condition_id": "c2", "title": "US Open: Ghost vs Phantom", "end_date": "2024-09-08T00:00:00Z"},
    ]
    sack = [_sack("atp_main", "Jannik Sinner", "Taylor Fritz", "20240908")]
    hits, total, rows = alignment_success_rate(events, sack, window_days=21)
    assert hits == 1
    assert total == 2
    assert len(rows) == 2
    # Each row has a diagnostic shape
    r_hit = next(r for r in rows if r["condition_id"] == "c1")
    r_miss = next(r for r in rows if r["condition_id"] == "c2")
    assert r_hit["matched"] is True
    assert r_hit["winner"] == "Jannik Sinner"
    assert r_miss["matched"] is False
