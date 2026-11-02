#!/usr/bin/env python3
"""Fetch 2025+ ATP/WTA tennis results into Sackmann-compatible CSVs.

Source spike, 2026-04-25:
- Jeff Sackmann GitHub raw files for 2025 return 404, matching the Phase 1
  plan's warning that local Sackmann cache stops at 2024.
- tennis-data.co.uk exposes yearly ATP/WTA XLSX files over HTTP, linked from
  http://www.tennis-data.co.uk/alldata.php. HTTPS currently fails TLS
  negotiation from this environment, but HTTP downloads succeed.

The Tennis-Data names are abbreviated ("Sinner J."), not full Sackmann names,
but the alignment layer expands surname candidates for these forms.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.sackmann import SackmannMatch

TENNIS_DATA_BASE = "http://www.tennis-data.co.uk"
SACKMANN_COLUMNS = [
    "tour",
    "tourney_date",
    "winner_name",
    "loser_name",
    "tourney_name",
    "surface",
    "tourney_level",
    "minutes",
    "score",
    "source",
]
XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(idx - 1, 0)


def tennis_data_url(year: int, tour: str) -> str:
    if tour == "atp_main":
        return f"{TENNIS_DATA_BASE}/{year}/{year}.xlsx"
    if tour == "wta_main":
        return f"{TENNIS_DATA_BASE}/{year}w/{year}.xlsx"
    raise ValueError(f"unsupported tour: {tour}")


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    return response.content


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in item.findall(".//a:t", XLSX_NS)) for item in root.findall("a:si", XLSX_NS)]


def read_first_sheet_rows(xlsx_bytes: bytes) -> list[list[str]]:
    """Read the first worksheet from an XLSX using stdlib zip/xml only."""
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
        shared = _shared_strings(zf)
        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        rows = []
        for row in root.findall(".//a:sheetData/a:row", XLSX_NS):
            values = []
            for cell in row.findall("a:c", XLSX_NS):
                ref = cell.get("r") or ""
                if ref:
                    col_idx = _column_index(ref)
                    while len(values) < col_idx:
                        values.append("")
                value_node = cell.find("a:v", XLSX_NS)
                value = "" if value_node is None else value_node.text or ""
                if cell.get("t") == "s" and value:
                    value = shared[int(value)]
                values.append(value)
            rows.append(values)
        return rows


def excel_serial_to_yyyymmdd(value: str) -> str:
    serial = int(float(value))
    dt = datetime(1899, 12, 30) + timedelta(days=serial)
    return dt.strftime("%Y%m%d")


def _score_from_row(row: dict[str, str]) -> str:
    sets = []
    for idx in range(1, 6):
        w = row.get(f"W{idx}", "")
        l = row.get(f"L{idx}", "")
        if w and l:
            sets.append(f"{w}-{l}")
    return " ".join(sets)


def _level(series: str) -> str | None:
    series = (series or "").lower()
    if "grand slam" in series:
        return "G"
    if "masters" in series:
        return "M"
    if "wta1000" in series or "wta 1000" in series:
        return "P"
    if "atp500" in series or "wta500" in series:
        return "A"
    if "atp250" in series or "wta250" in series:
        return "A"
    return series.upper()[:12] if series else None


def parse_tennis_data_rows(rows: list[list[str]], tour: str, source: str) -> list[SackmannMatch]:
    if not rows:
        return []
    header = rows[0]
    matches = []
    for raw in rows[1:]:
        row = {header[i]: raw[i] if i < len(raw) else "" for i in range(len(header))}
        if (row.get("Comment") or "").lower() not in {"completed", "retired", "walkover"}:
            continue
        winner = (row.get("Winner") or "").strip()
        loser = (row.get("Loser") or "").strip()
        date = row.get("Date")
        if not winner or not loser or not date:
            continue
        matches.append(
            SackmannMatch(
                tour=tour,
                winner_name=winner,
                loser_name=loser,
                tourney_date=excel_serial_to_yyyymmdd(date),
                tourney_name=(row.get("Tournament") or "").strip(),
                surface=(row.get("Surface") or "").strip() or None,
                tourney_level=_level(row.get("Series") or ""),
                minutes=None,
                score=_score_from_row(row) or None,
                source=source,
            )
        )
    return matches


def write_sackmann_csv(path: Path, matches: Iterable[SackmannMatch]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(SACKMANN_COLUMNS)
        for match in matches:
            writer.writerow(
                [
                    match.tour,
                    match.tourney_date,
                    match.winner_name,
                    match.loser_name,
                    match.tourney_name,
                    match.surface,
                    match.tourney_level,
                    match.minutes,
                    match.score,
                    match.source,
                ]
            )
            count += 1
    return count


def fetch_tour_year(year: int, tour: str, cache_root: Path, force: bool = False) -> Path:
    out_path = cache_root / "scraped" / f"{tour}_{year}.csv"
    if out_path.exists() and not force:
        return out_path
    url = tennis_data_url(year, tour)
    rows = read_first_sheet_rows(fetch_bytes(url))
    matches = parse_tennis_data_rows(rows, tour=tour, source="tennis-data.co.uk")
    write_sackmann_csv(out_path, matches)
    return out_path


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", nargs="+", type=int, required=True)
    parser.add_argument("--tours", nargs="+", choices=["atp_main", "wta_main"], default=["atp_main", "wta_main"])
    parser.add_argument("--cache-root", default="data/feasibility_cache")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.5)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    cache_root = Path(args.cache_root)
    for year in args.years:
        for tour in args.tours:
            path = fetch_tour_year(year, tour, cache_root=cache_root, force=args.force)
            with open(path, newline="") as f:
                rows = max(sum(1 for _ in f) - 1, 0)
            print(f"{tour} {year}: {rows} rows -> {path}")
            if args.sleep:
                time.sleep(args.sleep)


if __name__ == "__main__":
    main()
