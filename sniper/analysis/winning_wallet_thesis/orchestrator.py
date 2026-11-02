"""Orchestrator — fuse the two engines + archetype into one immutable read, and the worker
core that drains the queue.

`analyze_one` is the seam: it takes a wallet's `ThesisProfile` + `PositionBook` + archetype
(+ injected CLV / funding feeds), runs the fresh-anomaly engine, maps the thesis into a
`ThesisView`, applies the online-FDR gate, synthesizes the read, and appends it immutably.
The live worker (scripts/analyze_wallets_worker.py) supplies the fetched inputs; tests inject
them. Fail-loud: a GeoBlockedError surfaces and leaves NO read — never a false
`insufficient_evidence`.
"""
from __future__ import annotations

import hashlib
import re
import time

from . import online_fdr as FDR
from .fresh_anomaly import assess_fresh_anomaly
from .synthesis import ThesisView, ArchetypeView, QualityView, synthesize_read
from .types import PositionBook

# Thesis cluster labels include non-domain noise (hour-of-day buckets, generic tokens, case dupes).
# Clean them for the human-facing read so "established edge in sports" doesn't read as
# "sports, Sports, Games, held, underdog".
_NOISE_LABELS = {"held", "recurring", "games", "game", "unknown", "other"}
_HOUR_RE = re.compile(r"^\d{1,2}h$")
_DOMAIN_AXES = {"category", "subcat", "tag"}


def _clean_labels(labels, limit: int = 4) -> list:
    out, seen = [], set()
    for x in labels:
        s = str(x or "").strip()
        low = s.lower()
        if not s or low in _NOISE_LABELS or _HOUR_RE.match(low) or low in seen:
            continue
        seen.add(low)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _domain_labels(clusters, limit: int = 4) -> list:
    return _clean_labels(
        getattr(x, "label", "") for x in (clusters or [])
        if str(getattr(x, "axis", "") or "").lower() in _DOMAIN_AXES
    )[:limit]

THESIS_DEFAULTS = {
    "deep_min": 150, "thin_max": 40, "assessable_min": 10, "min_indep": 30,
    "p_sig": 0.05, "e_sig": 20.0, "recent_window_days": 30,
}


class GeoBlockedError(Exception):
    """Raised when an outbound fetch is refused for region/auth reasons. Must surface — the
    worker marks the queue row failed and writes NO read (never a false insufficient_evidence)."""


class RateLimiter:
    """Minimal shared limiter: spaces outbound calls to <= rps. Injectable clock/sleep."""

    def __init__(self, rps: float = 8.0):
        self.min_interval = 1.0 / float(rps) if rps > 0 else 0.0
        self._last = 0.0

    def acquire(self, *, now_fn=time.monotonic, sleep=time.sleep) -> None:
        now = now_fn()
        wait = self._last + self.min_interval - now
        if wait > 0:
            sleep(wait)
        self._last = now_fn()


def _archetype_view(archetype) -> ArchetypeView:
    a = archetype or {}
    return ArchetypeView(archetype=a.get("archetype", "unknown"), intent=a.get("intent", ""),
                         structural_share_high=bool(a.get("structural_share_high", False)),
                         structural_share=float(a.get("structural_share", 0.0)))


def build_thesis_view(profile, book: PositionBook, fresh, *, recent_fresh=None, cfg=None) -> ThesisView:
    """Map a ThesisProfile + book + fresh assessment into the synthesis ThesisView.
    'established' requires moderate/deep INDEPENDENT history + significance + positive PnL,
    so a thin lucky wallet can never satisfy it."""
    c = {**THESIS_DEFAULTS, **(cfg or {})}
    total_resolved = len(book.resolved_with_pnl())
    history_depth = ("deep" if total_resolved >= c["deep_min"]
                     else "thin" if total_resolved <= c["thin_max"] else "moderate")
    realized = round(sum(e.realized_pnl or 0.0 for e in book.resolved_directional()), 2)
    sb = getattr(profile, "shuffle_baseline", None)
    oe = float(sb.obs_over_expected) if sb else 1.0
    pv = float(sb.p_value) if sb else 1.0
    n_indep = int(getattr(fresh, "n_independent_events", 0))
    # An alternative explanation (esp. public_truth_stale = buying AFTER the outcome is public) means
    # the wallet's apparent win-surprise is an ARTIFACT, not edge. It must NOT count toward
    # significance — otherwise a stale-buyer's e_value (which explodes over many trials) reads as
    # "established edge" (the favorite-scraper false positive). The shuffle p-value is also discounted
    # when the artifact is the dominant explanation.
    alt = getattr(fresh, "alt_explanation", None)
    artifact = alt == "public_truth_stale"
    # Established (deep-history) significance comes from the SHUFFLE permutation test — the right tool
    # for deep wallets. The fresh e-process is for THIN-history surprise only (the fresh_high_surprise
    # lane); over huge n a marginal calibration excess (e.g. the favorite-longshot bias from using raw
    # entry price as the null) compounds the e-value astronomically without being directional edge, so
    # it must NOT establish a deep wallet. (A favorite-scraper with oe~1.2 and shuffle p=0.085 was
    # reading as 'established' purely on a 9e45 e-value — fixed here.)
    significant = (pv < c["p_sig"]) and not artifact
    assessable = total_resolved >= c["assessable_min"]
    established = (history_depth in ("moderate", "deep") and significant
                  and realized > 0 and n_indep >= c["min_indep"])
    emerging = (not established) and assessable and realized > 0 and oe > 1.0 and not artifact
    negative = assessable and realized < 0
    edge_clusters = getattr(profile, "edge_clusters", []) or []
    loss_clusters = getattr(profile, "loss_centers", []) or []
    recent_level = getattr(recent_fresh, "ex_post_level", "none") if recent_fresh else "none"
    return ThesisView(
        assessable=assessable, established_edge_supported=bool(established),
        emerging_positive=bool(emerging), negative_evidence=bool(negative),
        directional_residual_supported=bool(edge_clusters), obs_over_expected=oe, p_value=pv,
        e_value=getattr(fresh, "e_value", 1.0), history_depth=history_depth,
        n_independent_events=n_indep,
        edge_domains=_domain_labels(edge_clusters),
        loss_centers=_domain_labels(loss_clusters),
        entry_shape={"obs_over_expected": getattr(fresh, "obs_over_expected", 0.0)},
        recent_anomaly_level=recent_level,
        regime_shift_candidate=bool(recent_level == "strong" and established),
        realized_pnl=realized)


def analyze_one(conn, wallet: str, *, thesis_profile, book: PositionBook, archetype,
                clv_fn=None, funding=None, now_ts: int, data_as_of: int | None = None,
                analyzer_version: int = 1, rule_version: int = 1, test_index: int = 1, cfg=None):
    """Run both engines + synthesis for one wallet and append an immutable read. Returns it.

    `now_ts` is the analysis clock (recent-window, funding age). `data_as_of` is the data-snapshot
    identity for idempotency — pass the latest data timestamp so identical data => one read; defaults
    to now_ts when unset (tests)."""
    from sniper.persistence import db_wallet_reads as RD

    fresh = assess_fresh_anomaly(book, clv_fn=clv_fn, funding=funding, now_ts=now_ts, cfg=cfg)

    window = THESIS_DEFAULTS["recent_window_days"] * 86400
    recent_eps = [e for e in book.episodes if int(getattr(e, "last_ts", 0) or 0) >= now_ts - window]
    recent_fresh = (assess_fresh_anomaly(PositionBook(wallet=wallet, episodes=recent_eps),
                                         clv_fn=clv_fn, now_ts=now_ts, cfg=cfg) if recent_eps else None)

    thesis_view = build_thesis_view(thesis_profile, book, fresh, recent_fresh=recent_fresh, cfg=cfg)
    arch_view = _archetype_view(archetype)
    fdr_pass = FDR.is_flag_eligible(getattr(fresh, "e_value", 1.0), int(test_index))
    da = int(data_as_of if data_as_of is not None else now_ts)
    snap = hashlib.sha1(f"{wallet}:{da}:{len(book.episodes)}".encode()).hexdigest()[:16]
    quality = QualityView(data_fresh=True, analyzer_version=analyzer_version, rule_version=rule_version,
                          data_as_of=da, source_snapshot_hash=snap, fdr_pass=fdr_pass)

    read = synthesize_read(thesis_view, fresh, arch_view, quality)
    RD.append_read(conn, wallet, read, now_ts=int(now_ts))
    return read


def process_queue_row(conn, qrow, analyze, *, now: int):
    """Worker step: run `analyze(wallet)`, then complete; on GeoBlockedError surface + fail
    with NO read; on any other error fail with backoff. Returns the read or None."""
    import logging
    from sniper.persistence import db_wallet_analysis_queue as Q
    wallet = qrow["wallet"]            # sqlite3.Row and dict both support subscripting
    qid = qrow["id"]
    try:
        read = analyze(wallet)
        Q.complete(conn, qid, now=now, analyzer_version=getattr(read, "provenance", {}).get("analyzer_version", 0)
                   if hasattr(read, "provenance") else 0)
        return read
    except GeoBlockedError as e:
        logging.getLogger("analyzer.worker").error("GEO-BLOCKED wallet=%s: %s (surfaced, no read)", wallet, e)
        Q.fail(conn, qid, f"geo-blocked: {e}", now=now, backoff_s=3600, max_attempts=8)
        return None
    except Exception as e:  # noqa: BLE001 — surface + fail with backoff, never a false read
        logging.getLogger("analyzer.worker").exception("analyze failed wallet=%s", wallet)
        Q.fail(conn, qid, str(e), now=now, backoff_s=300)
        return None
