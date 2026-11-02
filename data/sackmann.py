"""Sackmann tennis match data parser + name helpers.

Sackmann's public repos (https://github.com/JeffSackmann/tennis_atp and _wta)
publish per-year CSVs. We parse them into SackmannMatch records and expose
diacritic-aware name helpers used by the Polymarket alignment module.

Alignment helpers (align_event_to_sackmann, alignment_success_rate,
AlignedMatch) are added in Task 6.
"""
from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

LOG = logging.getLogger(__name__)

VALID_TOURS = {"atp_main", "wta_main", "atp_chal", "wta_itf"}


@dataclass
class SackmannMatch:
    tour: str  # one of VALID_TOURS
    winner_name: str
    loser_name: str
    tourney_date: str  # YYYYMMDD
    tourney_name: str = ""
    surface: Optional[str] = None
    tourney_level: Optional[str] = None
    minutes: Optional[int] = None
    score: Optional[str] = None
    source: str = "sackmann"  # 'sackmann' | 'scraped' (Task 9)


def parse_sackmann_csv(path: Path, tour: str) -> list[SackmannMatch]:
    """Parse a single Sackmann CSV into SackmannMatch records.

    Unknown tours raise ValueError. Missing files return [].
    """
    if tour not in VALID_TOURS:
        raise ValueError(f"tour must be in {VALID_TOURS}, got {tour!r}")
    path = Path(path)
    if not path.exists():
        LOG.info("Sackmann CSV missing: %s", path)
        return []
    out = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            minutes_raw = (row.get("minutes") or "").strip()
            try:
                minutes = int(minutes_raw) if minutes_raw else None
            except ValueError:
                minutes = None
            out.append(SackmannMatch(
                tour=tour,
                winner_name=row.get("winner_name", "").strip(),
                loser_name=row.get("loser_name", "").strip(),
                tourney_date=(row.get("tourney_date") or "").strip(),
                tourney_name=row.get("tourney_name", "").strip(),
                surface=(row.get("surface") or "").strip() or None,
                tourney_level=(row.get("tourney_level") or "").strip() or None,
                minutes=minutes,
                score=(row.get("score") or "").strip() or None,
                source=(row.get("source") or "sackmann").strip() or "sackmann",
            ))
    return out


def load_scraped(cache_root: Path, years: Iterable[int]) -> list[SackmannMatch]:
    """Load scraper-produced CSVs from data/feasibility_cache/scraped.

    Files use the same columns as sackmann_matches plus a source column and are
    named `{tour}_{year}.csv`, for example `atp_main_2025.csv`.
    """
    cache_root = Path(cache_root)
    matches = []
    for year in years:
        for tour in ("atp_main", "wta_main"):
            matches.extend(parse_sackmann_csv(cache_root / "scraped" / f"{tour}_{year}.csv", tour=tour))
    return matches


def load_all(cache_root: Path, years: Iterable[int]) -> list[SackmannMatch]:
    """Load Sackmann ATP + WTA main + qual/chall/ITF CSVs for given years.

    Skips missing files silently (logs at INFO). Returns concatenated list.
    """
    cache_root = Path(cache_root)
    plans = [
        (cache_root / "tennis_atp" / "atp_matches_{y}.csv", "atp_main"),
        (cache_root / "tennis_atp" / "atp_matches_qual_chall_{y}.csv", "atp_chal"),
        (cache_root / "tennis_wta" / "wta_matches_{y}.csv", "wta_main"),
        (cache_root / "tennis_wta" / "wta_matches_qual_itf_{y}.csv", "wta_itf"),
    ]
    matches = []
    for year in years:
        for pat, tour in plans:
            p = Path(str(pat).format(y=year))
            matches.extend(parse_sackmann_csv(p, tour=tour))
    matches.extend(load_scraped(cache_root, years))
    return matches


def normalize_name(name: str) -> str:
    """Strip diacritics, lowercase, keep [a-z ] + collapse whitespace.

    Used for tolerant surname matching between Polymarket event titles
    (ASCII-ish) and Sackmann winner/loser_name fields (Unicode).
    """
    if not name:
        return ""
    nfd = unicodedata.normalize("NFD", name)
    ascii_only = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    # lowercase, keep only [a-z ], collapse spaces
    lowered = ascii_only.lower()
    filtered = re.sub(r"[^a-z ]+", "", lowered)
    collapsed = re.sub(r"\s+", " ", filtered).strip()
    return collapsed


def player_sides_from_title(title: str) -> tuple[Optional[str], Optional[str]]:
    """Extract raw player sides from a Polymarket event or market title.

    Accepts 'Prefix: Player A vs Player B' or 'Player A vs. Player B'.
    Returns (None, None) if the title doesn't parse.
    """
    if not title:
        return (None, None)
    normalized = re.sub(r"\s+", " ", title).strip()
    colon_parts = [part.strip() for part in normalized.split(":") if part.strip()]
    core = next((part for part in reversed(colon_parts) if re.search(r"\bvs?\.?\b", part, re.IGNORECASE)), normalized)
    parts = re.split(r"\s+v(?:s)?\.?\s+", core, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return (None, None)
    n1 = re.split(r"\s+[-–—]\s+|\s+\(|,", parts[0].strip())[0].strip()
    n2 = re.split(r"\s+[-–—]\s+|\s+\(|,", parts[1].strip())[0].strip()
    n2 = n2.split(":")[0].strip()
    return (n1, n2)


def side_name_candidates(name: str) -> set[str]:
    """Return compact surname candidates for full or abbreviated player sides."""
    normalized = normalize_name(name)
    tokens = normalized.split()
    if not tokens:
        return set()
    candidates = {tokens[-1]}
    non_initials = [token for token in tokens if len(token) > 1]
    if non_initials:
        candidates.add(non_initials[-1])
        candidates.add("".join(non_initials))
        if len(tokens[-1]) == 1:
            candidates.update(non_initials)
    return {candidate.replace(" ", "") for candidate in candidates if candidate}


def title_player_candidate_sets(title: str) -> tuple[set[str], set[str]]:
    side1, side2 = player_sides_from_title(title)
    if not side1 or not side2:
        return (set(), set())
    return (side_name_candidates(side1), side_name_candidates(side2))


def title_looks_like_doubles(title: str) -> bool:
    """Return True for doubles-style titles unsupported by the singles result set."""
    side1, side2 = player_sides_from_title(title)
    if not side1 or not side2:
        return False
    if re.search(r"\bdoubles?\b", title, re.IGNORECASE):
        return True
    return any(re.search(r"\s[/&]\s|/|&|\band\b", side, re.IGNORECASE) for side in (side1, side2))


def surnames_from_title(title: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (surname1, surname2) from a Polymarket event title."""
    side1, side2 = player_sides_from_title(title)
    n1 = side1.split()[-1] if side1 else None
    n2 = side2.split()[-1] if side2 else None
    return (n1, n2)


# ---------------------------------------------------------------------------
# Polymarket <-> Sackmann alignment
# ---------------------------------------------------------------------------


@dataclass
class AlignedMatch:
    sackmann: SackmannMatch
    date_delta_days: int
    alignment_confidence: float  # 1.0 = exact date hit; declines linearly with delta

    @property
    def winner_name(self) -> str:
        return self.sackmann.winner_name

    @property
    def loser_name(self) -> str:
        return self.sackmann.loser_name


def align_event_to_sackmann(
    title: str,
    end_date_iso: str,
    sackmann_matches: list[SackmannMatch],
    window_days: int = 21,
) -> Optional[AlignedMatch]:
    """Match a Polymarket event to the closest Sackmann row by surname + date.

    Matching rule: normalize (strip diacritics, lowercase, alphabetic-only) both
    surnames extracted from title, require each to appear in the Sackmann row's
    combined (winner_name + loser_name). Date delta must be within window_days.
    Among multiple candidates, pick the one with the smallest delta (highest
    confidence).

    Returns AlignedMatch or None if no candidate passes.
    """
    n1, n2 = surnames_from_title(title)
    if not n1 or not n2:
        return None
    nn1 = normalize_name(n1)
    nn2 = normalize_name(n2)
    if not nn1 or not nn2:
        return None
    try:
        target = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    best: Optional[AlignedMatch] = None
    for m in sackmann_matches:
        try:
            mdate = datetime.strptime(m.tourney_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        delta = abs((mdate - target).days)
        if delta > window_days:
            continue
        combined = f"{normalize_name(m.winner_name)} {normalize_name(m.loser_name)}"
        if nn1 in combined and nn2 in combined:
            conf = max(0.0, 1.0 - delta / window_days)
            if best is None or conf > best.alignment_confidence:
                best = AlignedMatch(sackmann=m, date_delta_days=delta, alignment_confidence=conf)
    return best


def alignment_success_rate(
    events: list[dict],
    sackmann_matches: list[SackmannMatch],
    window_days: int = 21,
) -> tuple[int, int, list[dict]]:
    """Return (hits, total, diagnostics). Each event gets one diagnostic row.

    Diagnostic row shape: {condition_id, title, end_date, matched, winner, confidence}.
    """
    hits = 0
    rows = []
    for e in events:
        m = align_event_to_sackmann(
            title=e.get("title", ""),
            end_date_iso=e.get("end_date", ""),
            sackmann_matches=sackmann_matches,
            window_days=window_days,
        )
        rows.append({
            "condition_id": e.get("condition_id"),
            "title": e.get("title"),
            "end_date": e.get("end_date"),
            "matched": m is not None,
            "winner": m.winner_name if m else None,
            "confidence": m.alignment_confidence if m else 0.0,
        })
        if m is not None:
            hits += 1
    return (hits, len(events), rows)
