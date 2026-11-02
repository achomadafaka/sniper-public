"""Audit Sackmann data coverage for target years, ATP + WTA main tour and challenger/ITF.

Reports:
  - Total match count per year per tour
  - Field completeness: winner_name, loser_name, score, minutes, surface, tourney_level
  - Per tourney_level counts
  - Per surface counts
  - Per-year presence of both main-tour and qual-chall / qual-itf files
"""
import csv
import os
import sys
from collections import Counter

ROOT = "data/feasibility_cache"
YEARS = [2024, 2025, 2026]
FIELDS = ["winner_name", "loser_name", "score", "minutes", "surface", "tourney_level", "tourney_date"]

TOURS = [
    # (code, dir, main-tour filename pattern, secondary filename pattern)
    ("atp", "tennis_atp", "atp_matches_{year}.csv", "atp_matches_qual_chall_{year}.csv"),
    ("wta", "tennis_wta", "wta_matches_{year}.csv", "wta_matches_qual_itf_{year}.csv"),
]


def audit_file(path):
    if not os.path.exists(path):
        return None
    total = 0
    field_nonempty = Counter()
    by_level = Counter()
    by_surface = Counter()
    with open(path) as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            total += 1
            for fld in FIELDS:
                if row.get(fld, "").strip():
                    field_nonempty[fld] += 1
            by_level[row.get("tourney_level", "?") or "?"] += 1
            by_surface[row.get("surface", "?") or "?"] += 1
    return {
        "path": path,
        "rows": total,
        "field_pct": {fld: (field_nonempty[fld] / total * 100 if total else 0.0) for fld in FIELDS},
        "by_level": dict(by_level),
        "by_surface": dict(by_surface),
    }


def main():
    for code, dirname, main_pat, sec_pat in TOURS:
        print(f"\n== {code.upper()} ==")
        for year in YEARS:
            main_path = os.path.join(ROOT, dirname, main_pat.format(year=year))
            sec_path = os.path.join(ROOT, dirname, sec_pat.format(year=year))
            for label, path in [("main", main_path), ("qual/chall/ITF", sec_path)]:
                res = audit_file(path)
                if res is None:
                    print(f"  {year} {label}: MISSING ({path})")
                    continue
                print(f"  {year} {label}: {res['rows']} matches")
                print(f"    field_pct: " + "  ".join(f"{k}={v:.1f}%" for k, v in res["field_pct"].items()))
                print(f"    by_level: " + ", ".join(f"{k}={v}" for k, v in sorted(res["by_level"].items(), key=lambda x: -x[1])))
                print(f"    by_surface: " + ", ".join(f"{k}={v}" for k, v in sorted(res["by_surface"].items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
