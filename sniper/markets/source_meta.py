"""
Source perspective metadata for news bias and alignment scoring.

Each source entry has:
  - lean: ideological lean (left, center-left, center, center-right, right, state/XX)
  - region: country/region of editorial perspective
  - lang: primary language of the feed
  - reliability: factual reliability (high, medium, low)

Alignment scoring:
  A source saying something AGAINST its expected lean = counter-narrative = higher signal weight
  A source saying something ALIGNED with its lean = expected = lower signal weight
"""

from typing import Dict, Optional, Tuple

SOURCE_META: Dict[str, Dict] = {
    # --- English / Western ---
    "bbc_world":       {"lean": "center",       "region": "UK",            "lang": "en", "reliability": "high"},
    "bbc_politics":    {"lean": "center",       "region": "UK",            "lang": "en", "reliability": "high"},
    "bbc_tech":        {"lean": "center",       "region": "UK",            "lang": "en", "reliability": "high"},
    "guardian_world":  {"lean": "center-left",  "region": "UK",            "lang": "en", "reliability": "high"},
    "guardian_us":     {"lean": "center-left",  "region": "UK/US",         "lang": "en", "reliability": "high"},
    "guardian_tech":   {"lean": "center-left",  "region": "UK",            "lang": "en", "reliability": "high"},
    "npr_world":       {"lean": "center-left",  "region": "US",            "lang": "en", "reliability": "high"},
    "foreignpolicy":   {"lean": "center",       "region": "US",            "lang": "en", "reliability": "high"},
    "the_hill":        {"lean": "center",       "region": "US",            "lang": "en", "reliability": "medium"},
    "politico":        {"lean": "center",       "region": "US",            "lang": "en", "reliability": "high"},
    "axios":           {"lean": "center",       "region": "US",            "lang": "en", "reliability": "high"},
    "france24":        {"lean": "center",       "region": "France",        "lang": "en", "reliability": "high"},
    "dw_world":        {"lean": "center",       "region": "Germany",       "lang": "en", "reliability": "high"},
    "aljazeera":       {"lean": "center-left",  "region": "Qatar/MENA",    "lang": "en", "reliability": "high"},  # Qatar state-funded; pro-Palestinian lean on conflict coverage
    "middleeasteye":   {"lean": "center-left",  "region": "UK/MENA",       "lang": "en", "reliability": "medium"},
    "marketwatch":     {"lean": "center",       "region": "US",            "lang": "en", "reliability": "high"},
    "cnbc_economy":    {"lean": "center-right", "region": "US",            "lang": "en", "reliability": "high"},
    # --- Crypto ---
    "coindesk":        {"lean": "center",       "region": "US",            "lang": "en", "reliability": "high"},
    "cointelegraph":   {"lean": "center",       "region": "US",            "lang": "en", "reliability": "medium"},
    "theblock":        {"lean": "center",       "region": "US",            "lang": "en", "reliability": "high"},
    "decrypt":         {"lean": "center",       "region": "US",            "lang": "en", "reliability": "medium"},
    "blockworks":      {"lean": "center",       "region": "US",            "lang": "en", "reliability": "medium"},
    # --- New multilingual sources ---
    "scmp":            {"lean": "center",       "region": "HK/China",      "lang": "en", "reliability": "high"},
    "nikkei_asia":     {"lean": "center-right", "region": "Japan",         "lang": "en", "reliability": "high"},
    "the_hindu":       {"lean": "center-left",  "region": "India",         "lang": "en", "reliability": "high"},
    "ndtv":            {"lean": "center",       "region": "India",         "lang": "en", "reliability": "medium"},
    "dw_arabic":       {"lean": "center",       "region": "Germany/MENA",  "lang": "ar", "reliability": "high"},
    "rfi_french":      {"lean": "center",       "region": "France",        "lang": "fr", "reliability": "high"},
    "rfi_arabic":      {"lean": "center",       "region": "France/MENA",   "lang": "ar", "reliability": "high"},
    "euractiv":        {"lean": "center",       "region": "EU",            "lang": "en", "reliability": "high"},
    "kyiv_independent":{"lean": "center",        "region": "Ukraine",       "lang": "en", "reliability": "medium"},  # Dead feed — replaced by ukr_pravda_en
    "ukr_pravda_en":   {"lean": "center",        "region": "Ukraine",       "lang": "en", "reliability": "high"},   # Ukrainska Pravda (English) — Ukrainian investigative; counter-narrative when reporting Russian gains
    "meduza":          {"lean": "center-left",  "region": "Russia (exile)","lang": "en", "reliability": "high"},
    "global_times":    {"lean": "state/cn",     "region": "China",         "lang": "en", "reliability": "low"},
    "tass_english":    {"lean": "state/ru",     "region": "Russia",        "lang": "en", "reliability": "low"},
    "asharq_awsat":    {"lean": "center-right", "region": "Saudi Arabia",  "lang": "en", "reliability": "medium"},
    "mercopress":      {"lean": "center",       "region": "South America", "lang": "en", "reliability": "medium"},
    "elpais_english":  {"lean": "center-left",  "region": "Spain",         "lang": "en", "reliability": "high"},
    "almonitor":       {"lean": "center",       "region": "US/MENA",       "lang": "en", "reliability": "high"},
    "nhk_world":       {"lean": "center",       "region": "Japan",         "lang": "en", "reliability": "high"},
    "times_india":     {"lean": "center-right", "region": "India",         "lang": "en", "reliability": "medium"},
    "rfi_english":     {"lean": "center",       "region": "France",        "lang": "en", "reliability": "high"},
}

# Political lean ordering for diversity calculation
LEAN_ORDER = ["state/ru", "state/cn", "right", "center-right", "center", "center-left", "left"]

def get_source_meta(source_key: str) -> Dict:
    """Return perspective metadata for a source key, with safe defaults."""
    return SOURCE_META.get(source_key, {"lean": "unknown", "region": "unknown", "lang": "en", "reliability": "medium"})


def compute_perspective_diversity(source_keys: list) -> Tuple[float, str]:
    """
    Compute a perspective diversity score for a set of sources covering the same topic.

    Returns (score, label) where:
      score: 0.0 (echo chamber) to 1.0 (fully diverse)
      label: human-readable description

    Algorithm:
      - Count unique (lean, region_group) pairs
      - Penalise state media heavily (single-source state coverage = low diversity)
      - Reward geographic spread
    """
    if not source_keys:
        return 0.0, "no sources"

    metas = [get_source_meta(sk) for sk in source_keys]

    unique_leans = set(m["lean"] for m in metas)
    unique_regions = set(_region_group(m["region"]) for m in metas)

    # State media alone = minimum diversity
    state_only = all(m["lean"].startswith("state/") for m in metas)
    if state_only:
        return 0.1, "state media only"

    lean_score = min(len(unique_leans) / 4.0, 1.0)
    region_score = min(len(unique_regions) / 3.0, 1.0)
    score = (lean_score * 0.6 + region_score * 0.4)

    if score >= 0.8:
        label = "high diversity"
    elif score >= 0.5:
        label = "moderate diversity"
    elif score >= 0.3:
        label = "limited diversity"
    else:
        label = "echo chamber"

    return round(score, 2), label


def compute_surprise_weight(source_key: str, signal_direction: str, cluster_slug: str) -> float:
    """
    Return a weight multiplier (0.5-2.0) for a source's signal based on how
    surprising it is given the source's known lean.

    Counter-intuitive signals from biased sources carry MORE weight.
    State media saying something against their government's interest is very notable.

    signal_direction: "bullish_yes" | "bullish_no" | "neutral"
    cluster_slug: entity cluster this news item relates to
    """
    meta = get_source_meta(source_key)
    lean = meta["lean"]

    # State media specifically: surprising if they're covering a story about their own gov negatively
    if lean == "state/cn" and cluster_slug in ("taiwan_strait", "south_china_sea", "us_china_trade"):
        # Chinese state media being alarmed about these = elevated signal
        if signal_direction == "bullish_yes":
            return 1.8  # China state saying conflict escalates = big signal
        return 0.7

    if lean == "state/ru" and cluster_slug == "russia_ukraine":
        if signal_direction == "bullish_no":
            return 1.8  # Russian state media softening = peace signal
        return 0.6

    return 1.0  # neutral weight for unclassified combinations


def _region_group(region: str) -> str:
    """Collapse granular region strings to broad groups for diversity calculation."""
    region_lower = region.lower()
    for group, keywords in {
        "western": ["us", "uk", "germany", "france", "spain", "eu", "canada", "australia"],
        "mena": ["qatar", "mena", "saudi", "israel", "iran", "iraq", "turkey"],
        "east_asia": ["china", "japan", "hk", "korea", "taiwan"],
        "south_asia": ["india", "pakistan"],
        "russia_cis": ["russia", "ukraine"],
        "africa": ["africa", "sahel", "kenya", "nigeria"],
        "latam": ["latin", "south america", "brazil", "spain"],
    }.items():
        if any(k in region_lower for k in keywords):
            return group
    return "other"


# ---------------------------------------------------------------------------
# Propaganda detection — SemEval-2020 Task 11 taxonomy (keyword heuristics)
# ---------------------------------------------------------------------------
# 18-technique taxonomy; we cover 10 via keyword matching.
# Use as a screening signal, not ground truth. A trained classifier would be
# more accurate, but these heuristics catch the most common manipulation patterns.

PROPAGANDA_TECHNIQUES: dict[str, list[str]] = {
    # 1. Loaded Language — emotionally charged labels
    "loaded_language": [
        "terrorist", "regime", "thug", "radical", "extremist", "freedom fighter",
        "patriot", "traitor", "occupation", "colonizer", "puppet state",
        "imperialist", "genocide", "ethnic cleansing",
    ],
    # 2. Name-Calling / Labeling
    "name_calling": [
        "fake news", "puppet", "stooge", "fascist", "globalist", "warmonger",
        "deep state", "establishment elite", "soros", "propaganda machine",
    ],
    # 3. Appeal to Fear / Prejudice
    "appeal_to_fear": [
        "existential threat", "imminent danger", "catastrophic", "devastating",
        "alarm", "dire warning", "crisis point", "point of no return",
        "disaster", "collapse", "annihilation",
    ],
    # 4. Black-and-White Fallacy
    "black_white_fallacy": [
        "either you are with us", "only option", "no other choice",
        "inevitably", "must choose between", "simple choice",
    ],
    # 5. Whataboutism
    "whataboutism": [
        "what about", "but they also", "and yet when", "double standard",
        "hypocritical", "why don't you condemn",
    ],
    # 6. Appeal to Authority (vague sourcing)
    "appeal_to_authority": [
        "experts say", "scientists agree", "officials confirm",
        "unnamed officials", "sources close to", "insiders say",
        "intelligence officials", "anonymous government",
    ],
    # 7. Simplification / Causal Oversimplification
    "simplification": [
        "obviously", "clearly", "everyone knows", "it's simple",
        "undeniably", "without question", "self-evident",
    ],
    # 8. False Equivalence
    "false_equivalence": [
        "both sides", "no different from", "just as bad as",
        "equivalent to", "same as", "both equally",
    ],
    # 9. Straw Man
    "straw_man": [
        "so you're saying", "which means you believe", "which would lead to",
        "so according to you",
    ],
    # 10. Repetition (partial — repetition of specific charged phrases)
    "repetition": [
        # These are commonly repeated propaganda slogans across state media
        "special military operation", "denazification", "liberation operation",
        "unprovoked aggression", "rules-based order",
    ],
}


def score_propaganda_likelihood(text: str) -> dict:
    """
    Score article text for propaganda technique signals using SemEval-2020 taxonomy.

    Returns:
      {
        "total_signals": int,
        "techniques_detected": [str, ...],
        "tier": "factual" | "speculative" | "propaganda",
        "flag_reason": str | None,
      }

    Tier rules:
      - 0 signals → "factual" (no heuristic flags)
      - 1 signal  → "speculative" (possible manipulation, verify)
      - 2+ signals → "propaganda" (likely manipulative framing)

    NOTE: This is a keyword heuristic, not a trained classifier. It has significant
    false-positive rates for legitimate analysis that quotes propaganda. Use as a
    screening signal — always verify with source reliability (source_meta) before
    downgrading confidence.
    """
    text_lower = text.lower()
    detected: list[str] = []
    total = 0

    for technique, keywords in PROPAGANDA_TECHNIQUES.items():
        for kw in keywords:
            if kw in text_lower:
                if technique not in detected:
                    detected.append(technique)
                total += 1
                break  # count max one hit per technique to avoid gaming

    if total >= 2 or len(detected) >= 2:
        tier = "propaganda"
        flag_reason = f"Detected techniques: {', '.join(detected[:3])}"
    elif total == 1:
        tier = "speculative"
        flag_reason = f"Possible technique: {detected[0]}"
    else:
        tier = "factual"
        flag_reason = None

    return {
        "total_signals": total,
        "techniques_detected": detected,
        "tier": tier,
        "flag_reason": flag_reason,
    }


def classify_source_credibility_tier(source_key: str) -> str:
    """
    Classify a source into a credibility tier based on its metadata.

    Returns: "factual" | "speculative" | "opinion" | "propaganda"

    Rules:
      - state/ru or state/cn lean → "propaganda" (state-controlled narrative)
      - reliability=low → "propaganda"
      - reliability=medium → "speculative"
      - reliability=high → "factual"
    """
    meta = get_source_meta(source_key)
    lean = meta.get("lean", "unknown")
    reliability = meta.get("reliability", "medium")

    if lean.startswith("state/") or reliability == "low":
        return "propaganda"
    if reliability == "medium":
        return "speculative"
    return "factual"
