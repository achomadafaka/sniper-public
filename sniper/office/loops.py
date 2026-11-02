"""Office Task 10 (Lambda): the self-driven loop's scheduling core. PURE. Gives Codex's Supervisor.tasks() STABLE turn ids: turn_id = sha256(agent_id | generation | slot)[:20] with slot = floor(now / role cadence),
so a restart or a retry inside the same slot returns the same id (durable dedupe) and a prompt/persona/model change (generation bump) or the next slot returns a new one. A role with no configured cadence is
REFUSED (no default). A per-agent turns/hour cap names a runaway loop (spec 4.1.26: an alarm and a pause, not a USD cap). floor_rhythm names the Head's morning plan / midday check / end-of-day review on UTC hours.
Cadence config is crew-owned and pinned by sha256."""
import hashlib
import json


class ConfigMismatch(Exception):
    pass


class UnknownRole(KeyError):
    pass


def load(path, expected_sha256):
    raw = open(path, "rb").read()
    got = hashlib.sha256(raw).hexdigest()
    if got != expected_sha256:
        raise ConfigMismatch("cadence sha256 %s != pinned %s" % (got, expected_sha256))
    return json.loads(raw)


def due_turns(cfg, agents, *, now, done_turn_ids):
    out = []
    for a in sorted(agents, key=lambda a: a["agent_id"]):
        if a["state"] != "active":
            continue
        cad = cfg["role_cadence_s"].get(a["role"])
        if not cad:
            raise UnknownRole("no cadence for role %r" % (a["role"],))
        slot = int(now // cad)
        tid = hashlib.sha256(("%s|%d|%d" % (a["agent_id"], a["generation"], slot)).encode()).hexdigest()[:20]
        if tid not in done_turn_ids:
            out.append({"agent_id": a["agent_id"], "turn_id": tid, "generation": a["generation"], "slot": slot})
    return out


def runaway(cfg, calls_by_agent, *, now):
    cap = int(cfg["max_turns_per_agent_per_hour"] * cfg["runaway_pause_factor"])
    out = []
    for aid in sorted(calls_by_agent):
        n = sum(1 for t in calls_by_agent[aid] if now - 3600.0 < t <= now)
        if n > cap:
            out.append({"agent_id": aid, "turns_last_hour": n, "cap": cap})
    return out


def floor_rhythm(cfg, now):
    h = cfg["head_rhythm_utc"]
    hour = int((now % 86400.0) // 3600)
    for name, key in (("morning_plan", "morning_plan_hour"), ("midday_check", "midday_check_hour"), ("end_of_day_review", "end_of_day_review_hour")):
        if hour == h[key]:
            return name
    return None
