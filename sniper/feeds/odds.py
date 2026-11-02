"""Sports odds feed adapter (The Odds API).

Polls The Odds API v4 for head-to-head odds on configurable sports and stores
normalized snapshots in the ``odds_snapshots`` table.

Supports **multiple free API keys** via ``ODDS_API_KEYS`` (comma-separated).
When a key hits ``OUT_OF_USAGE_CREDITS``, the adapter rotates to the next key
automatically.  Cooldown only engages when *all* keys are exhausted.

Falls back to single ``ODDS_API_KEY`` for backward compatibility.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import FeedAdapter
from sniper.settings.web import INSTANCE_TAG

logger = logging.getLogger(__name__)

DEFAULT_SPORT_KEYS = (
    "basketball_nba",
    "basketball_ncaab",
)

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
DEFAULT_ERROR_BACKOFF_SECONDS = 1800


# ---------------------------------------------------------------------------
# Key ring — manages multiple API keys with automatic rotation
# ---------------------------------------------------------------------------

class _KeyRing:
    """Rotates through a list of API keys, skipping exhausted ones."""

    def __init__(self) -> None:
        self._keys: List[str] = []
        self._exhausted: set = set()  # keys that hit OUT_OF_USAGE_CREDITS
        self._current_idx: int = 0

    def load(self) -> None:
        """Load keys from file, env CSV, or single env var (checked in that order).

        Priority:
        1. ``ODDS_API_KEYS_FILE`` — path to a text file, one key per line.
           Lines starting with ``#`` and blank lines are ignored.
        2. ``ODDS_API_KEYS`` — comma-separated keys in env.
        3. ``ODDS_API_KEY`` — single key fallback.

        The file is re-read every tick, so you can append keys at any time
        and they'll be picked up without a restart.
        """
        keys: List[str] = []

        # 1. File source (highest priority)
        key_file = os.environ.get("ODDS_API_KEYS_FILE", "").strip()
        if key_file and os.path.isfile(key_file):
            try:
                with open(key_file, "r") as fh:
                    for line in fh:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            keys.append(line)
                if keys:
                    new_count = len(set(keys) - set(self._keys))
                    if new_count and self._keys:
                        logger.info("odds keyring: loaded %d key(s) from file (%d new)", len(keys), new_count)
            except OSError:
                logger.warning("odds keyring: could not read %s", key_file)

        # 2. Env CSV
        if not keys:
            raw_multi = os.environ.get("ODDS_API_KEYS", "").strip()
            if raw_multi:
                keys = [k.strip() for k in raw_multi.split(",") if k.strip()]

        # 3. Single key fallback
        if not keys:
            raw_single = os.environ.get("ODDS_API_KEY", "").strip()
            if raw_single:
                keys = [raw_single]

        self._keys = keys
        # Prune exhausted set to only contain keys still in the list
        self._exhausted = self._exhausted & set(keys)
        if self._current_idx >= len(keys):
            self._current_idx = 0

    @property
    def has_keys(self) -> bool:
        return len(self._keys) > 0

    @property
    def all_exhausted(self) -> bool:
        return len(self._keys) > 0 and self._exhausted >= set(self._keys)

    @property
    def active_count(self) -> int:
        return len(self._keys) - len(self._exhausted)

    def current_key(self) -> Optional[str]:
        """Return the next available (non-exhausted) key, or None."""
        if not self._keys:
            return None
        # Try from current index, wrapping around
        for _ in range(len(self._keys)):
            key = self._keys[self._current_idx]
            if key not in self._exhausted:
                return key
            self._current_idx = (self._current_idx + 1) % len(self._keys)
        return None  # all exhausted

    def mark_exhausted(self, key: str) -> None:
        """Mark a key as exhausted and advance to the next."""
        self._exhausted.add(key)
        self._current_idx = (self._current_idx + 1) % max(len(self._keys), 1)
        remaining = self.active_count
        logger.warning(
            "odds key exhausted: %s...%s — %d key(s) remaining",
            key[:4], key[-4:], remaining,
        )

    def reset_exhausted(self) -> None:
        """Clear all exhaustion flags (e.g., after cooldown period)."""
        self._exhausted.clear()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def canonical_event_hash(home_team: str, away_team: str, commence_time: str) -> str:
    """Deterministic, case-insensitive SHA256[:16] hash for an event.

    Strips whitespace and lowercases team names so that "Lakers" and "lakers"
    produce the same hash.
    """
    raw = f"{home_team.lower().strip()}|{away_team.lower().strip()}|{commence_time}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize_h2h_outcomes(raw_outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert The Odds API decimal-format outcomes to normalized dicts.

    Each input dict is expected to have ``name`` (str) and ``price`` (float,
    decimal odds >= 1.01).  Entries with price <= 1.0 are skipped as invalid.

    For totals/spreads, the ``point`` field (e.g. 226.5) is preserved in the
    returned dict so callers can access the line number.

    Returns list of ``{name, price_decimal, implied_prob_raw, point?}``.
    """
    results: List[Dict[str, Any]] = []
    for o in raw_outcomes:
        price = float(o.get("price", 0))
        if price <= 1.0:
            continue  # invalid / American odds guard-rail
        entry: Dict[str, Any] = {
            "name": str(o.get("name", "")),
            "price_decimal": price,
            "implied_prob_raw": 1.0 / price,
        }
        # Preserve point (total line or spread number) when present
        if "point" in o and o["point"] is not None:
            entry["point"] = float(o["point"])
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class _OddsFeedCooldownError(RuntimeError):
    def __init__(self, reason: str, *, backoff_seconds: int = DEFAULT_ERROR_BACKOFF_SECONDS):
        super().__init__(reason)
        self.reason = reason
        self.backoff_seconds = int(backoff_seconds)


class OddsFeedAdapter(FeedAdapter):
    """The Odds API feed adapter."""

    feed_id = "odds"
    display_name = "Sports Odds"
    default_poll_seconds = 1800  # 30 minutes — free tier is 500 req/month/key
    mode = "poll"

    def __init__(self) -> None:
        self._cooldown_until_ts = 0.0
        self._cooldown_reason = ""
        self._keyring = _KeyRing()

    def validate_config(self) -> bool:
        """Return True if at least one API key is configured."""
        self._keyring.load()
        return self._keyring.has_keys

    def warmup(self, conn) -> None:
        from db import ensure_odds_snapshots_table
        ensure_odds_snapshots_table(conn)

    def tick(self, conn) -> None:
        from db import insert_odds_snapshot

        now = time.time()
        if self._cooldown_until_ts > now:
            return
        # After cooldown expires, reset exhausted keys so they can be retried
        if self._cooldown_until_ts > 0:
            self._keyring.reset_exhausted()
            self._cooldown_until_ts = 0.0
            self._cooldown_reason = ""

        # Reload keys each tick (allows hot-swap via env/file without restart)
        self._keyring.load()

        sport_keys = self._get_sport_keys()
        bookmaker_filter = self._get_bookmaker_filter()

        total_stored = 0
        for sport_key in sport_keys:
            # Retry through ALL available keys before giving up on a sport.
            # The single-retry pattern caused each tick to burn 2 keys/sport
            # and exhaust the full keyring in ~20 minutes after restart.
            events = None
            last_exc = None
            while True:
                try:
                    events = self._fetch_odds(sport_key)
                    break  # success
                except _OddsFeedCooldownError as exc:
                    last_exc = exc
                    if exc.reason == "out_of_usage_credits" and not self._keyring.all_exhausted:
                        # Key was marked exhausted in _fetch_odds; try next key
                        continue
                    # All keys exhausted or non-quota error — stop retrying
                    break
                except Exception:
                    logger.exception("odds tick: failed to fetch %s", sport_key)
                    break

            if events is None:
                if last_exc is not None:
                    self._cooldown_until_ts = time.time() + last_exc.backoff_seconds
                    if self._keyring.all_exhausted:
                        self._cooldown_reason = "all_keys_exhausted"
                        logger.warning(
                            "odds tick: all %d keys exhausted — backoff %ds",
                            len(self._keyring._keys), last_exc.backoff_seconds,
                        )
                    else:
                        self._cooldown_reason = last_exc.reason
                        logger.warning(
                            "odds tick: entering backoff for %ss (%s)",
                            last_exc.backoff_seconds,
                            last_exc.reason,
                        )
                    return
                continue  # fetch failed for other reason, try next sport

            for event in events:
                home_team = str(event.get("home_team", ""))
                away_team = str(event.get("away_team", ""))
                commence_time = str(event.get("commence_time", ""))
                event_id = str(event.get("id", ""))
                sport_title = str(event.get("sport_title", ""))

                if not (home_team and away_team and commence_time):
                    continue

                ev_hash = canonical_event_hash(home_team, away_team, commence_time)

                for bookmaker in event.get("bookmakers", []):
                    bk_key = str(bookmaker.get("key", ""))
                    bk_title = str(bookmaker.get("title", ""))

                    if bookmaker_filter and bk_key not in bookmaker_filter:
                        continue

                    market_ts = str(
                        bookmaker.get("last_update", "")
                        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    )

                    for market in bookmaker.get("markets", []):
                        market_key = str(market.get("key", ""))
                        if market_key not in ("h2h", "totals", "spreads"):
                            continue

                        mkt_ts = str(market.get("last_update", "")) or market_ts
                        raw_outcomes = market.get("outcomes", [])
                        normalized = normalize_h2h_outcomes(raw_outcomes)

                        for outcome in normalized:
                            try:
                                # For totals/spreads, include the point in
                                # outcome_name so queries can distinguish lines.
                                o_name = outcome["name"]
                                point = outcome.get("point")
                                if point is not None and market_key in ("totals", "spreads"):
                                    o_name = f"{o_name} {point}"

                                payload = {"raw_price": outcome["price_decimal"]}
                                if point is not None:
                                    payload["point"] = point

                                row_id = insert_odds_snapshot(
                                    conn,
                                    instance_tag=INSTANCE_TAG,
                                    provider="the_odds_api",
                                    sport_key=sport_key,
                                    sport_title=sport_title,
                                    event_id=event_id,
                                    home_team=home_team,
                                    away_team=away_team,
                                    commence_time=commence_time,
                                    bookmaker_key=bk_key,
                                    bookmaker_title=bk_title,
                                    market_key=market_key,
                                    outcome_name=o_name,
                                    price_decimal=outcome["price_decimal"],
                                    implied_prob_raw=outcome["implied_prob_raw"],
                                    market_ts=mkt_ts,
                                    canonical_event_hash=ev_hash,
                                    payload_json=json.dumps(payload),
                                )
                                if row_id is not None:
                                    total_stored += 1
                            except Exception:
                                logger.exception(
                                    "odds tick: failed to store outcome %s for %s",
                                    outcome.get("name"),
                                    event_id,
                                )

        if total_stored:
            logger.info("odds snapshots stored: %d outcomes", total_stored)
        else:
            logger.debug("odds tick: no new snapshots")

    # -- internal helpers ---------------------------------------------------

    def _get_sport_keys(self) -> List[str]:
        env_keys = os.environ.get("ODDS_SPORT_KEYS", "").strip()
        if env_keys:
            return [k.strip() for k in env_keys.split(",") if k.strip()]
        return list(DEFAULT_SPORT_KEYS)

    def _get_bookmaker_filter(self) -> Optional[set]:
        """Return set of bookmaker keys to include, or None for all."""
        env_bks = os.environ.get("ODDS_BOOKMAKERS", "").strip()
        if env_bks:
            return {b.strip().lower() for b in env_bks.split(",") if b.strip()}
        return None

    def _fetch_odds(self, sport_key: str) -> List[Dict[str, Any]]:
        """GET from The Odds API v4 for a single sport.

        Uses the key ring for automatic rotation on quota errors.
        Returns list of event dicts.  Raises on HTTP error.
        """
        api_key = self._keyring.current_key()
        if not api_key:
            reason = "all_keys_exhausted" if self._keyring.has_keys else "missing_api_key"
            raise _OddsFeedCooldownError(reason)

        url = (
            f"{ODDS_API_BASE}/{sport_key}/odds/"
            f"?apiKey={api_key}&regions=us&markets=h2h,totals,spreads&oddsFormat=decimal"
        )

        req = urllib.request.Request(url, headers={"User-Agent": "Sniper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self._cooldown_until_ts = 0.0
                self._cooldown_reason = ""
                return data if isinstance(data, list) else []
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            error_code = ""
            try:
                payload = json.loads(body)
                error_code = str(payload.get("error_code", "")).strip()
            except Exception:
                pass
            if error_code == "OUT_OF_USAGE_CREDITS":
                self._keyring.mark_exhausted(api_key)
                raise _OddsFeedCooldownError(error_code.lower()) from exc
            if error_code in {"MISSING_KEY", "INVALID_KEY"}:
                self._keyring.mark_exhausted(api_key)
                raise _OddsFeedCooldownError(error_code.lower()) from exc
            raise
