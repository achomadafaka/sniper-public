"""Desk cohort U ranker outcome label (pre-registration 2026-10-21-desk-u-ranker §4). Pure code; the
batch script scripts/research/desk_u_ranker_labels.py owns the tape reads and its own file.

Every candidate, kept or cut, gets the same tape label, so recall is a full count where the U tape
covers the coin. Times are host received_at seconds, the clock the ranker and the emission use.

- t0 = the candidate's band-entry swap (its emission). E = the first swap price in [t0 + 5, t0 + 65] s,
  the window the source scout and the runner study share; none = NULL 'no_tape' (never a loser).
- Y2 = max ln(price / E) over (t_E, t_E + 600 s] >= ln 2.
- D20 = -20 % reached before +20 % (both from E; neither reached = 0).
- Censored (the source scout's rule, Lambda 2240b36cd): a whole-tape silence longer than 10 s inside
  [t0, t_E + 600 s], which is how a worker_start or stream:* (reconnect) gap row shows; any other gap
  cause (ring_overflow, or one not known) censors whenever its time falls in the window, because
  frames were dropped while the tape kept moving and no silence shows it. gap_cause is kept;
  a graduation inside the window (the U tape is the curve only, so the PumpSwap pool is not
  covered) is censored at graduation and counted, never read as the end price.
- Our own signatures (actors) are excluded from the price path.
"""
from __future__ import annotations

import math

LABEL_VERSION = "u-label-v2"   # v2: entry window to t0 + 65 s, gap censoring by cause (Gamma, 6e2bf2748)
ENTRY_DELAY_S = 5.0
ENTRY_WAIT_S = 65.0
WINDOW_S = 600.0
MAX_TAPE_GAP_S = 10.0
LN2 = math.log(2.0)


def silence_cause(cause):
    """A gap row whose loss shows as tape silence (a restart or a reconnect)."""
    return cause == "worker_start" or str(cause).startswith("stream:")


def label(swaps, *, t0, silences, gaps, exclude_actors=()):
    """swaps: the coin's [(received_at, price, actor, real_token)]; silences: whole-tape intervals
    (a, b) with no swap received, longer than MAX_TAPE_GAP_S (tape_silences); gaps: producer gap rows
    [(at, cause)]. Returns {status, y2, d20, t_e, e_price, graduated_at, max_ln}; status is one of
    'labelled' | 'no_tape' | 'censored_gap' | 'censored_graduation'."""
    out = {"label_version": LABEL_VERSION, "y2": None, "d20": None, "t_e": None, "e_price": None,
           "graduated_at": None, "max_ln": None, "gap_cause": None}
    own = set(exclude_actors)
    path = sorted((s for s in swaps if s[0] >= t0 and s[2] not in own and s[1]), key=lambda s: s[0])
    entry = next((s for s in path if t0 + ENTRY_DELAY_S <= s[0] <= t0 + ENTRY_WAIT_S), None)
    if entry is None:
        return {**out, "status": "no_tape"}
    t_e, e = entry[0], entry[1]
    end = t_e + WINDOW_S
    out.update(t_e=t_e, e_price=e)
    lost = next((c for at, c in gaps if t0 <= at <= end and not silence_cause(c)), None)
    if lost is not None:
        return {**out, "status": "censored_gap", "gap_cause": lost or "unknown"}
    if any(a < end and b > t0 for a, b in silences):
        return {**out, "status": "censored_gap", "gap_cause": "tape_silence"}
    grad = next((s[0] for s in swaps if t0 <= s[0] <= end and s[3] == 0), None)
    if grad is not None:
        return {**out, "status": "censored_graduation", "graduated_at": grad}
    window = [s for s in path if t_e < s[0] <= end]
    best, d20 = (max(math.log(s[1] / e) for s in window) if window else 0.0), 0
    for s in window:
        r = s[1] / e
        if r >= 1.2:
            break
        if r <= 0.8:
            d20 = 1
            break
    return {**out, "status": "labelled", "y2": int(best >= LN2), "d20": d20, "max_ln": round(best, 9)}


def tape_silences(received_times, *, start, end):
    """Whole-tape intervals inside [start, end] with no swap received for more than MAX_TAPE_GAP_S,
    the edges included (a tape that stops before `end` is silent to `end`)."""
    times = sorted(t for t in received_times if start <= t <= end)
    edges = [start] + times + [end]
    return [(a, b) for a, b in zip(edges, edges[1:]) if b - a > MAX_TAPE_GAP_S]
