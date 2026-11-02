"""Office Task 19 (Lambda): shifts and written handovers (spec 2.6). PURE: no clock, store or scheduler (Codex's T6 hooks call these with the trusted corrected clock).
The floor runs 24/7 in UTC shifts from a crew-owned config pinned by sha256. The floor is never unstaffed: `missing_seats` reports every mandatory seat that is absent OR zero.
A handover note must carry every section and every item an evidence ref. Opportunities inside a handover window stay in the universe; unprocessed ones are scored as SKIP (spec 2.6), while an
unprocessed opportunity OUTSIDE a handover is a stall, never relabelled as a handover SKIP. The null-arm latency is frozen per shift; a missing shift value is refused, never defaulted."""
import hashlib
import json

DAY_S = 86400.0


class ConfigMismatch(Exception):
    pass


class MissingLatency(KeyError):
    pass


def load(path, expected_sha256):
    raw = open(path, "rb").read()
    got = hashlib.sha256(raw).hexdigest()
    if got != expected_sha256:
        raise ConfigMismatch("shifts config sha256 %s != pinned %s" % (got, expected_sha256))
    cfg = json.loads(raw)
    spans = sorted((s["start_hour"], s["end_hour"]) for s in cfg["shifts"])
    if spans[0][0] != 0 or spans[-1][1] != 24 or any(a[1] != b[0] for a, b in zip(spans, spans[1:])):
        raise ConfigMismatch("shifts must tile the UTC day with no gap or overlap")
    return cfg


def _hour(t):
    return (t % DAY_S) / 3600.0


def shift_at(cfg, t):
    h = _hour(t)
    for s in cfg["shifts"]:
        if s["start_hour"] <= h < s["end_hour"]:
            return s
    raise ConfigMismatch("no shift covers hour %s" % h)


def in_handover(cfg, t):
    """True within handover_window_minutes before or after any shift boundary (the midnight boundary wraps)."""
    w = cfg["handover_window_minutes"] / 60.0
    h = _hour(t)
    for b in {s["start_hour"] % 24 for s in cfg["shifts"]}:
        d = abs(h - b)
        if min(d, 24 - d) <= w:
            return True
    return False


def missing_seats(cfg, roster):
    return sorted(seat for seat in cfg["min_staffed_seats"] if not roster.get(seat))


def validate_note(cfg, note):
    problems = []
    for sec in cfg["handover_note_sections"]:
        if sec not in note:
            problems.append("missing_section:%s" % sec)
            continue
        for i, item in enumerate(note[sec]):
            if not item.get("ref"):
                problems.append("no_evidence_ref:%s[%d]" % (sec, i))
    return problems


def classify_opportunity(cfg, t, *, processed):
    if processed:
        return {"in_universe": True, "state": "processed"}
    return {"in_universe": True, "state": "skip_unprocessed_handover" if in_handover(cfg, t) else "unprocessed"}


def null_latency(cfg, table, t):
    sid = shift_at(cfg, t)["id"]
    if sid not in table or table[sid] is None:
        raise MissingLatency("no frozen null latency for shift %s" % sid)
    return table[sid]


def occurrence(cfg, t):
    """The DATED shift occurrence containing t: {shift_id, start_t, end_t}. The same label on another day is another occurrence, so ids derived from it survive restarts and UTC midnight."""
    s = shift_at(cfg, t)
    day0 = t - (t % DAY_S)
    return {"shift_id": s["id"], "start_t": day0 + s["start_hour"] * 3600.0, "end_t": day0 + s["end_hour"] * 3600.0}


def handover_id(cfg, t):
    """Deterministic id of the handover INTO the shift occurrence containing t (previous dated occurrence -> current dated occurrence); identical for any t inside the current occurrence."""
    cur = occurrence(cfg, t)
    prev = occurrence(cfg, cur["start_t"] - 1.0)
    return hashlib.sha256(("handover|%s@%d|%s@%d" % (prev["shift_id"], prev["start_t"], cur["shift_id"], cur["start_t"])).encode()).hexdigest()[:32]
