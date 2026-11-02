"""Office Task 10 (Lambda): org core. PURE seat expansion, limit validation and change classification; hire/fire/epochs persistence is wired onto the store once Task 1 is merged.
default_org.json (pods x per-pod template + shared seats) and limits.json are crew-owned and pinned by sha256. Risk and Postmortem are fixed seats (restaffable, never removable); the org size has a
ceiling from limits.json; judges across the org must cover both model families and every pod's judges both. The Head can apply persona/prompt/org changes but never tools, risk floors, guards, scoring,
evidence, fill model, metric registry or live authority. Every material change opens an epoch; a change kind that is not in the config is REFUSED, never treated as immaterial."""
import hashlib
import json


class ConfigMismatch(Exception):
    pass


class BadOrg(ValueError):
    pass


class UnknownChange(ValueError):
    pass


def _load(path, sha):
    raw = open(path, "rb").read()
    got = hashlib.sha256(raw).hexdigest()
    if got != sha:
        raise ConfigMismatch("%s sha256 %s != pinned %s" % (path, got, sha))
    return json.loads(raw)


def load_org(path, sha):
    return _load(path, sha)


def load_limits(path, sha):
    return _load(path, sha)


def expand(org, limits=None):
    """-> list of seat dicts {role, pod, family, ...}. Scout lanes and analyst lenses are spread round-robin across pods so every lane exists; families alternate claude/gpt."""
    n = org["pods"]
    if limits is not None and not limits["min_pods"] <= n <= limits["max_pods"]:
        raise BadOrg("pod count %d outside [%d, %d]" % (n, limits["min_pods"], limits["max_pods"]))
    seats = []
    for s in org["shared_seats"]:
        for _ in range(s["count"]):
            seats.append({"role": s["role"], "pod": None, "family": s["family"], **({"tier": s["tier"]} if "tier" in s else {})})
    t = org["pod_template"]
    fam = ["claude", "gpt"]
    for i in range(n):
        pod = "pod%d" % (i + 1)
        for j in range(t["scouts_per_pod"]):
            seats.append({"role": "Scout", "pod": pod, "family": fam[(i + j) % 2], "lane": t["scouts"][(i * t["scouts_per_pod"] + j) % len(t["scouts"])]["lane"]})
        for j in range(t["analysts_per_pod"]):
            seats.append({"role": "Analyst", "pod": pod, "family": fam[(i + j) % 2], "lens": t["analysts"][(i * t["analysts_per_pod"] + j) % len(t["analysts"])]["lens"]})
        for j in range(t["judges_per_pod"]):
            seats.append({"role": "Judge", "pod": pod, "family": t["judge_families"][(i + j) % len(t["judge_families"])]})
    return seats


def validate(seats, limits, deterministic=()):
    """`deterministic` = seats that exist as deterministic services, not agents (the pilot's Risk/Trader/Postmortem): they satisfy a fixed-seat requirement, they are never counted as agents."""
    problems = []
    roles = {s["role"] for s in seats} | set(deterministic)
    for r in limits["fixed_seats"]:
        if r not in roles:
            problems.append("missing_fixed_seat:%s" % r)
    if len(seats) > limits["org_size_ceiling"]:
        problems.append("org_size_over_ceiling")
    judges = [s for s in seats if s["role"] == "Judge"]
    if len(judges) < limits["judges_min_total"]:
        problems.append("too_few_judges")
    for f in limits["judge_families_required"]:
        if not any(j["family"] == f for j in judges):
            problems.append("judges_missing_family:%s" % f)
    return problems


def head_may_apply(limits, change):
    return change["kind"] not in limits["head_forbidden_changes"]


def opens_epoch(limits, change):
    k = change.get("kind")
    if k in limits["material_changes"] or k in limits["head_forbidden_changes"]:
        return True                                   # crew-only changes (tools, guards, scoring, ...) open an epoch too, each time
    if k in limits["immaterial_changes"]:
        return False
    raise UnknownChange("change kind %r is not classified" % (k,))
