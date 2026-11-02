"""Locked initial X roster (2026-07-13). Handles are configuration, not identity-forever;
handle changes require operator review. Tier-1 X handles may differ from the fomo handle
(resolved via the fomo profile `twitter` field). Official-context accounts are captured for
narrative but excluded from source-caller ranking."""
from __future__ import annotations

from sniper.analysis.memecoin_x.normalize import normalize_handle

_TIER1 = [
    ("zinceth", "zinceth"), ("RuneCrypto_", "RuneCrypto_"), ("thokani", "Thokani"),
    ("saivSOL", "saivSOL"), ("PaikCapital", "PaikCapital"), ("Aurelius0121", "Aurelius0121"),
    ("ErisScalp", "Fiboguy"), ("Dprisav", "techquant"), ("0xJiggy", "0xJiggy"),
    ("00CTG", "00CTG"), ("Farmercharts", "Farmercharts"), ("memekingdom", "memekingdom"),
    ("_OHT_", "_OHT_"), ("airtightfish", "airtightfish"), ("badmonft", "badmonft"),
    ("theveeman", "theveeman"), ("real_y22", "real_y22"), ("chimpfone", "chimpfone"),
    ("brezscales", "brezscales"), ("changefomo", "change"), ("astaso1", "asta"),
    ("Binkieeefomo", "Binkieee"), ("shillcapital", "shillcapital"), ("goyimpnl", "goyim"),
    ("careful", "careful"), ("WoahWurdz", "Hiraethh"), ("dddddddddddd100", "jester"),
]
_TIER2 = ["poptyedev", "blknoiz06", "iruletrenches", "ProofMarkets", "Noxa_Fi", "hooddotfun"]
_OFFICIAL = ["vladtenev", "RobinhoodApp"]


def _entry(handle, role, fomo_handle=None):
    return {"handle": normalize_handle(handle), "display_handle": handle,
            "role": role, "fomo_handle": fomo_handle}


INITIAL_ROSTER = (
    [_entry(h, "tier1_trader", f) for h, f in _TIER1]
    + [_entry(h, "tier2_source") for h in _TIER2]
    + [_entry(h, "official_context") for h in _OFFICIAL]
)


def roster_counts() -> dict:
    out = {}
    for e in INITIAL_ROSTER:
        out[e["role"]] = out.get(e["role"], 0) + 1
    return out
