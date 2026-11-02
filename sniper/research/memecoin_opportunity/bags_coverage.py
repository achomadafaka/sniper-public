"""Independent Bags launch-feed coverage audit; never an event trigger."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Iterable


BAGS_LAUNCH_FEED_URL = "https://public-api-v2.bags.fm/api/v1/token-launch/feed"


@dataclass(frozen=True)
class BagsLaunch:
    token_mint: str
    launch_signature: str | None
    pool_key: str | None


@dataclass
class BagsForwardCoverage:
    """Audit only feed launches first observed after this worker starts."""

    baseline_mints: set[str] | None = None
    verified_mints: set[str] | None = None

    def observe(
        self,
        feed: Iterable[BagsLaunch],
        captured_mints: set[str],
    ) -> tuple[tuple[BagsLaunch, ...], dict[str, object]]:
        launches = tuple(feed)
        feed_mints = {launch.token_mint for launch in launches}
        if self.baseline_mints is None:
            self.baseline_mints = set(feed_mints)
            self.verified_mints = set()
            return (), {
                "coverage_status": "warming",
                "feed_launches": len(feed_mints),
                "audited_launches": 0,
                "captured_feed_launches": 0,
                "missing_mints": [],
            }

        audited = tuple(
            launch for launch in launches
            if launch.token_mint not in self.baseline_mints
        )
        audited_mints = {launch.token_mint for launch in audited}
        verified = self.verified_mints if self.verified_mints is not None else set()
        verified.intersection_update(audited_mints)
        verified.update(audited_mints & set(captured_mints))
        self.verified_mints = verified
        detail = reconcile_bags_coverage(
            audited,
            set(captured_mints) | verified,
            configured=True,
        )
        if not audited:
            detail["coverage_status"] = "warming"
        elif detail["coverage_status"] == "verified":
            detail["coverage_status"] = "forward_sample_verified"
        detail["feed_launches"] = len(feed_mints)
        detail["audited_launches"] = len(audited_mints)
        return audited, detail


def parse_bags_launch_feed(payload: object) -> tuple[BagsLaunch, ...]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("BAGS_FEED_FAILED")
    rows = payload.get("response")
    if not isinstance(rows, list):
        raise ValueError("BAGS_FEED_MALFORMED")
    launches = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mint = str(row.get("tokenMint") or "").strip()
        if not mint or mint in seen:
            continue
        seen.add(mint)
        launches.append(
            BagsLaunch(
                mint,
                str(row["launchSignature"]) if row.get("launchSignature") else None,
                str(row["dbcPoolKey"]) if row.get("dbcPoolKey") else None,
            )
        )
    return tuple(launches)


def fetch_bags_launch_feed(api_key: str, *, timeout_s: float = 5.0) -> tuple[BagsLaunch, ...]:
    if not api_key.strip():
        raise ValueError("BAGS_API_KEY_MISSING")
    request = urllib.request.Request(
        BAGS_LAUNCH_FEED_URL,
        headers={
            "Accept": "application/json",
            "x-api-key": api_key.strip(),
            "User-Agent": "Sniper-Opportunity-Radar/2",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.loads(response.read())
    return parse_bags_launch_feed(payload)


def reconcile_bags_coverage(
    feed: Iterable[BagsLaunch],
    captured_mints: set[str],
    *,
    configured: bool,
) -> dict[str, object]:
    if not configured:
        return {
            "coverage_status": "unmeasured",
            "feed_launches": None,
            "captured_feed_launches": None,
            "missing_mints": [],
        }
    launches = tuple(feed)
    feed_mints = {launch.token_mint for launch in launches}
    missing = sorted(feed_mints - set(captured_mints))
    status = "quiet" if not feed_mints else "coverage_gap" if missing else "verified"
    return {
        "coverage_status": status,
        "feed_launches": len(feed_mints),
        "captured_feed_launches": len(feed_mints) - len(missing),
        "missing_mints": missing[:25],
    }
