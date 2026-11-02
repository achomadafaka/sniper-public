#!/usr/bin/env python3
"""Brain redesign, migration DRY RUN: split today's brain into candidate typed facts. Read-only.

Question (Alpha, 2026-09-28, for docs/superpowers/specs/2026-09-28-brain-knowledge-graph-design.md):
if HANDOFF.md, the handover skill's trap list and the Supabase `memories` rows were split into
atomic facts, how many of each type would there be, which entities do they name, and how many of
the active memories already fail an automatic evidence check (the staleness baseline)?

SOURCES (all read, nothing written anywhere):
  handoff   ~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/HANDOFF.md, one EPISODE per
            top-level block (a `## ` heading or a column-0 `- ` bullet plus its continuation lines)
  trap      .claude/skills/handover/SKILL.md, the items between "NAMING TRAPS" and "SETTLED"
  memory    Supabase `memories`, status=active, fetched by GET (or --snapshot JSON). The two
            session/HANDOFF* mirrors are skipped: they are copies of the handoff source.

A block is split into candidate facts at sub-bullets and sentence ends, and each candidate is typed
by the first matching rule in RULES. The typing is a regex heuristic: the counts size the migration,
they are not the migration.

STALENESS (active memories only). Each check has its own denominator (memories that carry at least
one reference of that kind); a memory with no checkable reference is counted apart, never as fresh.
  sha      a hex token that resolves to a commit object which origin/main does not contain.
           A hex token that is not a commit (an attempt id, a key hash) is UNRESOLVED, not a fail.
  path     a repo-relative path that neither origin/main's tree nor the disk (runtime files under
           data/ and logs/) holds. A path whose file name now lives under archive/ is counted too
           (moved), and reported apart.
  knob     an ENV-style name whose first segment is a .env key prefix, found in no source file at
           origin/main (per-chain numeric suffixes are stripped first). A knob that only .env
           names is "env_only", reported apart from unknown.

    python3 scripts/research/brain_migration_dryrun.py                      # live fetch
    python3 scripts/research/brain_migration_dryrun.py --snapshot mem.json  # reuse a fetch
    python3 scripts/research/brain_migration_dryrun.py --selftest
"""
import argparse
import collections
import contextlib
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HANDOFF = Path.home() / ".claude/projects/-home-bigpapi-Desktop-Sniper/memory/HANDOFF.md"
SKILL = REPO / ".claude/skills/handover/SKILL.md"

RULES = [  # (type, pattern) first match wins; order is the priority
    ("retraction", r"\bRETRACT|\bretract(ed|ion)\b|\bCORRECT(ED|ION)\b|\bcorrected\b|\bwas wrong\b|\bwithdrawn\b"),
    ("decision", r"\bOPERATOR\b|\boperator (said|chose|ordered|decided|\"|picks|order)|\bDECISION\b|"
                 r"\bDECIDED\b|\bSHELVED\b|\bAPPROVED\b|\bGO\b|\boperator's call\b|\bsettled\b|\bSETTLED\b"),
    ("rule", r"\bTRAP\b|\bCost on\b|\bHow to apply\b|\bNEVER\b|\bnever\b|\balways\b|\bmust\b|\bMUST\b|"
             r"\bdo not\b|\bDo not\b|\bdon't\b|\bstanding rule\b"),
    ("change", r"\b(pushed|merged|MERGED|committed|built|BUILT|squash|reverted)\b|\bcommit [0-9a-f]{7}|"
               r"\bon branch\b|\bbranch [a-z]+/"),
    ("state", r"\bDEPLOYED\b|\bdeployed\b|\bLIVE\b|\blive (since|on|at)\b|\brunning\b|\bpid \d+|"
              r"\.env\b|\brestarted\b|\breleases?/|\barmed\b|\bis (on|off)\b|\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\s*=\s*\S"),
    ("measurement", r"\bn\s*=\s*\d|\bp[159]0\b|\bmedian\b|\bCI\b|\[-?\d|\d+ of \d+|\d+/\d+\b|"
                    r"\d(\.\d+)?\s*%|\$\s?\d|\b\d+(\.\d+)?\s*(ms|s)\b"),
    ("task", r"\b(next|Next|NEXT|TODO|queue[sd]?|waits?|WAITS|HELD|held|pending|owe[sd]?|due|watch(es|ing)?|"
             r"will|to do|OPEN)\b"),
]
RULES = [(t, re.compile(p)) for t, p in RULES]

HEX = re.compile(r"(?<![0-9A-Za-z_/.:-])([0-9a-f]{7,40})(?![0-9A-Za-z_])")
KNOB = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,})\b")
PATH = re.compile(r"(?<![\w./-])((?:scripts|sniper|bot|agents|tests|docs|static|templates|index|knowledge|"
                  r"data|logs|mcp_tools|archive|\.claude|\.github)/[\w./@+-]*\.[A-Za-z0-9]{1,6})\b")
CREW = re.compile(r"\b(Alpha|Beta|Gamma|Lambda|Codex|OPERATOR|[Oo]perator)\b")
CHAIN = re.compile(r"\b(solana|Solana|4663|8453|5042|chain 56|BSC|Base|Robinhood|Arc)\b")
EVM_ADDR = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
SOL_ADDR = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])([1-9A-HJ-NP-Za-km-z]{32,44})(?![1-9A-HJ-NP-Za-km-z])")
POSITION = re.compile(r"\b(?:position|pos|positions)\s+#?(\d{4,6})\b|\b(6[0-9]{4})\b")
SENT = re.compile(r"(?<=[.;])\s+(?=[A-Z(\"'])")


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                          check=True).stdout


# ---------------------------------------------------------------- parsing (pure)
def handoff_blocks(text):
    """[(line_no, header, body_lines)] for top-level blocks."""
    blocks, cur = [], None
    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("## ") or line.startswith("- "):
            cur = [i, line, []]
            blocks.append(cur)
        elif cur is not None and line.strip():
            cur[2].append(line)
    return [tuple(b) for b in blocks]


def trap_items(text):
    """Trap list items between the NAMING TRAPS and SETTLED markers."""
    lines = text.splitlines()
    lo = next(i for i, l in enumerate(lines) if "NAMING TRAPS" in l)
    hi = next(i for i, l in enumerate(lines) if i > lo and l.startswith("SETTLED"))
    items, cur = [], None
    for line in lines[lo + 1:hi]:
        if line.startswith("- "):
            cur = [line[2:].strip()]
            items.append(cur)
        elif cur is not None and line.strip():
            cur.append(line.strip())
    return [" ".join(i) for i in items]


def strip_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def split_candidates(header, body_lines):
    """Atomic candidates: every sub-bullet is one; prose is split at sentence ends."""
    chunks, para = [], [header.lstrip("#- ").strip()]
    for line in body_lines:
        s = line.strip()
        if s.startswith(("- ", "* ", "| ")) or re.match(r"^\d+[.)] ", s):
            chunks.append(" ".join(para))
            para = [s.lstrip("-*| ").strip()]
        elif s.startswith("```"):
            continue
        else:
            para.append(s)
    chunks.append(" ".join(para))
    out = []
    for c in chunks:
        out += [p.strip() for p in SENT.split(c) if len(p.strip()) >= 12]
    return out


def classify(text):
    for t, rx in RULES:
        if rx.search(text):
            return t
    return "narrative"


def entities(text, handles=frozenset()):
    """Entity candidates by kind. `handles` is a set; a handle counts only as a whole token."""
    ent = collections.defaultdict(list)
    for m in HEX.finditer(text):
        tok = m.group(1)
        if re.search(r"[a-f]", tok) and re.search(r"\d", tok):
            ent["hex"].append(tok)
    for m in KNOB.finditer(text):
        ent["knob_like"].append(m.group(1))
    for m in PATH.finditer(text):
        ent["path"].append(m.group(1).rstrip("."))
    ent["crew"] += [c.upper() if c.lower() == "operator" else c for c in CREW.findall(text)]
    ent["chain"] += CHAIN.findall(text)
    ent["evm_address"] += EVM_ADDR.findall(text)
    ent["sol_address"] += [a for a in SOL_ADDR.findall(text)
                           if re.search(r"\d", a) and re.search(r"[a-z]", a) and re.search(r"[A-Z]", a)
                           and not re.fullmatch(r"x?[0-9a-fA-F]+", a)]
    for m in POSITION.finditer(text):
        ent["position"].append(m.group(1) or m.group(2))
    words = set(re.findall(r"[\w.-]+", text)) | set(re.findall(r"\w+", text))
    ent["wallet_handle"] += [h for h in handles if h in words]
    return ent


def check_memory(ent, *, commits, main_commits, tree, tree_names_archive, disk_exists,
                 code_tokens, env_names, env_prefixes):
    """{check: [(token, verdict)]} for one memory's entities. Pure given the injected sets."""
    out = collections.defaultdict(list)
    for tok in set(ent.get("hex", ())):
        full = commits.get(tok)
        if full is None:
            out["sha"].append((tok, "unresolved"))
        elif full in main_commits:
            out["sha"].append((tok, "ok"))
        else:
            out["sha"].append((tok, "off_main"))
    for p in set(ent.get("path", ())):
        if any(ch in p for ch in "<>*{}") or "..." in p or "XX" in p:
            continue
        if p in tree or disk_exists(p):
            out["path"].append((p, "ok"))
        elif Path(p).name in tree_names_archive:
            out["path"].append((p, "moved_to_archive"))
        else:
            out["path"].append((p, "missing"))
    for k in set(ent.get("knob_like", ())):
        if "_".join(k.split("_")[:2]) not in env_prefixes:
            continue
        base = re.sub(r"(_\d+)+$", "", k)
        if k in code_tokens or base in code_tokens:
            out["knob"].append((k, "ok"))
        elif k in env_names or base in env_names:
            out["knob"].append((k, "env_only"))
        else:
            out["knob"].append((k, "unknown"))
    return out


FAIL = {"sha": {"off_main"}, "path": {"missing", "moved_to_archive"}, "knob": {"unknown"}}


# ---------------------------------------------------------------- live inputs
def fetch_memories():
    sys.path.insert(0, str(REPO))
    for line in (REPO / ".env").read_text().splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and not k.startswith("#") and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip()
    import bot.supabase_helper as sb
    rows, off = [], 0
    while True:
        page = sb._request("GET", "memories?select=id,type,name,description,content,source,status,"
                                  "updated_at,valid_as_of_commit&order=id&limit=200&offset=%d" % off,
                           timeout=30)
        rows += page
        if len(page) < 200:
            return rows
        off += 200


def repo_sets():
    commits = {}
    all_shas = git("rev-list", "--all").split()
    main = set(git("rev-list", "origin/main").split())
    for s in all_shas:
        for n in range(7, 13):
            commits.setdefault(s[:n], s)
        commits[s] = s
    tree = set(git("ls-tree", "-r", "--name-only", "origin/main").split("\n"))
    archive_names = {Path(p).name for p in tree if p.startswith("archive/")}
    code = git("grep", "-ohE", r"\b[A-Z][A-Z0-9]*(_[A-Z0-9]+){2,}\b", "origin/main", "--",
               "*.py", "*.sh", "*.js", "*.yml", "*.yaml", "*.html", "*.example", "*.toml", "*.rs",
               ":!archive/", ":!docs/", ":!index/")
    code_tokens = {line.split(":", 1)[-1] for line in code.split("\n") if line}
    env_names = set()
    for line in (REPO / ".env").read_text().splitlines():
        k, sep, _ = line.strip().partition("=")   # key names only, never values
        if sep and not k.startswith("#"):
            env_names.add(k.strip())
    first = {n.split("_")[0] for n in env_names} - {""}
    env_prefixes = {"_".join(n.split("_")[:2]) for n in env_names | code_tokens
                    if n.split("_")[0] in first and n.count("_") >= 1}
    return dict(commits=commits, main_commits=main, tree=tree, tree_names_archive=archive_names,
                disk_exists=lambda p: (REPO / p).exists(), code_tokens=code_tokens,
                env_names=env_names, env_prefixes=env_prefixes)


def wallet_handles():
    sys.path.insert(0, str(REPO))
    from sniper.persistence.db_core import get_readonly_connection
    with contextlib.closing(get_readonly_connection(str(REPO / "sniper_tracker.db"))) as c:
        handles = {r[0] for r in c.execute("SELECT handle FROM memecoin_wallets") if r[0]}
    words = set()
    with contextlib.suppress(OSError):
        words = {w.strip().lower() for w in open("/usr/share/dict/words")}
    keep = {h for h in handles if len(h) >= 4 and h.lower() not in words}
    print("wallet handles: %d in memecoin_wallets, %d kept (dictionary words and < 4 chars dropped)"
          % (len(handles), len(keep)))
    return keep


# ---------------------------------------------------------------- report
def run(memories, handles, sets, sample_n=20, seed=20260928):
    facts = []   # (source, episode, type, text, entities)
    blocks = handoff_blocks(HANDOFF.read_text())
    for ln, header, body in blocks:
        for c in split_candidates(header, body):
            facts.append(("handoff", "HANDOFF.md:%d" % ln, classify(c), c, entities(c, handles)))
    traps = trap_items(SKILL.read_text())
    for i, t in enumerate(traps, 1):
        facts.append(("trap", "SKILL.md trap %d" % i, "rule", t, entities(t, handles)))
    active = [m for m in memories if m.get("status") == "active"]
    split = [m for m in active if not (m["name"] or "").startswith("session/HANDOFF")]
    for m in split:
        body = strip_frontmatter(m.get("content") or "").splitlines()
        head = (m.get("description") or m["name"] or "")
        for c in split_candidates(head, body):
            facts.append(("memory", m["name"], classify(c), c, entities(c, handles)))

    print("EPISODES: handoff blocks %d, trap items %d, active memories %d (split %d; skipped %d "
          "HANDOFF mirrors)" % (len(blocks), len(traps), len(active), len(split),
                                len(active) - len(split)))
    print("CANDIDATE FACTS: %d" % len(facts))
    by = collections.Counter((f[0], f[2]) for f in facts)
    types = [t for t, _ in RULES] + ["narrative"]
    print("  %-12s %8s %8s %8s %8s" % ("type", "handoff", "trap", "memory", "total"))
    for t in types:
        row = [by[(s, t)] for s in ("handoff", "trap", "memory")]
        print("  %-12s %8d %8d %8d %8d" % (t, *row, sum(row)))

    # entities: resolve hex to commits and knobs to known names before counting
    ent_count = collections.defaultdict(collections.Counter)
    for f in facts:
        for kind, toks in f[4].items():
            for tok in toks:
                if kind == "hex":
                    kind = "commit" if tok in sets["commits"] else "hex_other"
                if kind == "knob_like":
                    if "_".join(tok.split("_")[:2]) not in sets["env_prefixes"]:
                        continue
                    kind = "knob"
                ent_count[kind][tok] += 1
    print("ENTITY CANDIDATES (unique / mentions; top 8):")
    for kind in sorted(ent_count):
        c = ent_count[kind]
        print("  %-14s %6d / %7d  %s" % (kind, len(c), sum(c.values()),
                                          ", ".join("%s x%d" % kv for kv in c.most_common(8))))

    # staleness on active memories (HANDOFF mirrors included: they are active rows)
    per_check = collections.defaultdict(collections.Counter)
    stale, checkable, examples = 0, 0, collections.defaultdict(list)
    by_group = collections.defaultdict(collections.Counter)
    for m in active:
        text = (m.get("description") or "") + "\n" + (m.get("content") or "")
        res = check_memory(entities(text, ()), **sets)
        group = "session_mirror" if (m["name"] or "").startswith("session/") else "direct"
        if any(res.values()):
            checkable += 1
        failed = False
        for check, items in res.items():
            verdicts = {v for _, v in items}
            per_check[check]["with_refs"] += 1
            for v in verdicts:
                per_check[check]["has_" + v] += 1
            bad = [tok for tok, v in items if v in FAIL[check]]
            if bad:
                failed = True
                per_check[check]["FAIL"] += 1
                if len(examples[check]) < 6:
                    examples[check].append("%s: %s" % (m["name"], ", ".join(sorted(bad)[:3])))
        by_group[group]["active"] += 1
        by_group[group]["checkable"] += bool(any(res.values()))
        by_group[group]["stale"] += failed
        stale += failed
    print("STALENESS BASELINE (active %d; checkable %d; uncheckable %d):"
          % (len(active), checkable, len(active) - checkable))
    for check in ("sha", "path", "knob"):
        c = per_check[check]
        print("  %-5s with_refs %4d  FAIL %4d  | %s" % (
            check, c["with_refs"], c["FAIL"],
            ", ".join("%s %d" % (k[4:], v) for k, v in sorted(c.items()) if k.startswith("has_"))))
        for e in examples[check]:
            print("        e.g. %s" % e[:150])
    print("  any check FAIL: %d of %d checkable (%.1f%%)" % (stale, checkable,
                                                           100.0 * stale / max(checkable, 1)))
    for g, c in sorted(by_group.items()):
        print("    %-15s active %4d checkable %4d stale %4d" % (g, c["active"], c["checkable"], c["stale"]))
    trap_fail = 0
    for t in traps:
        res = check_memory(entities(t, ()), **sets)
        trap_fail += any(v in FAIL[k] for k, items in res.items() for _, v in items)
    print("  trap items failing any check: %d of %d" % (trap_fail, len(traps)))

    rng = random.Random(seed)
    strata = collections.defaultdict(list)
    for f in facts:
        strata[(f[0], f[2])].append(f)
    keys = sorted(strata)
    picks = []
    while len(picks) < sample_n and keys:
        for k in list(keys):
            if len(picks) >= sample_n:
                break
            pool = strata[k]
            if pool:
                picks.append(pool.pop(rng.randrange(len(pool))))
            else:
                keys.remove(k)
    print("SAMPLE (%d, stratified by source x type, seed %d):" % (len(picks), seed))
    for src, ep, t, text, ent in picks:
        names = sorted({tok for toks in ent.values() for tok in toks})[:4]
        print("  [%s|%s] %s :: %s || ents: %s" % (src, t, ep[:48], text[:170].replace("\n", " "),
                                                  ", ".join(names)))
    return facts


STALE_RX = re.compile(r"\b(STALE|RETRACTED|retracted|CORRECTED|SUPERSEDED|superseded|WRONG|withdrawn|"
                      r"no longer (true|holds|applies))\b")
FRONT_RX = [("memecoin", r"memecoin|fomo|solana|4663|8453|pump|funded|j7|jupiter|evm|bsc"),
            ("crypto5m", r"crypto5m|btc.?5m|5m.?binary"),
            ("hyperliquid", r"hyperl|\bhl[-_ ]|far.?whale|cascade|microstructure"),
            ("wallet-analyzer", r"wallet.?protocol|dossier|archetype|swissmiss|smallfish|survivorship|polymarket"),
            ("sports/markets", r"sports?|soccer|political|conviction"),
            ("brain/infra", r"brain|memor(y|ies)|retrieval|embedding|knowledge|workflow|git|test")]
CUT_DAYS = 7


def front_of(text):
    for label, pat in FRONT_RX:
        if re.search(pat, text or "", re.IGNORECASE):
            return label
    return "other"


def block_dates(blocks):
    """{line_no: 'YYYY-MM-DD'}: the header's own date, else the nearest earlier header's."""
    out, last = {}, None
    for ln, header, _ in blocks:
        m = re.search(r"(20\d\d-\d\d-\d\d)", header)
        last = m.group(1) if m else last
        out[ln] = last
    return out


def apply_rules(content):
    """The 'How to apply' block of a memory: its bullets, else the block as one rule."""
    m = re.search(r"\*\*How to apply:?\*\*:?(.*?)(?=\n\s*\n\s*(?:\*\*|#|Related:|\Z)|\Z)", content, re.S)
    if not m:
        return []
    block = m.group(1).strip()
    bullets = [b.strip()[2:].strip() for b in block.splitlines() if b.strip().startswith(("- ", "* "))]
    return [b for b in bullets if len(b) >= 12] or ([block] if len(block) >= 12 else [])


def words(text):
    return {w for w in re.findall(r"[a-z0-9_]{4,}", (text or "").lower())}


def value_cut(memories, facts, sets, today="2026-09-28", sample_n=20, seed=20260928):
    """Tier the candidates: FACT tiers T1-T5 are kept as facts; everything else stays episode-only.
    Prints counts per tier and per dropped class, and two graded-by-hand samples."""
    from datetime import date
    t0 = date.fromisoformat(today)
    blocks = handoff_blocks(HANDOFF.read_text())
    bdate = block_dates(blocks)
    active = [m for m in memories if m.get("status") == "active"
              and not (m["name"] or "").startswith("session/HANDOFF")]
    # duplicates: a direct row whose suffix equals a session mirror's name
    mirror_names = {m["name"][len("session/"):] for m in active if m["name"].startswith("session/")}
    kept, review, dup_rows = [], [], 0
    for m in active:
        name = m["name"] or ""
        suffix = name.split("/", 1)[-1]
        if not name.startswith("session/") and (suffix in mirror_names or
                                                 suffix.split("/")[-1] in mirror_names):
            dup_rows += 1
            continue
        head = (m.get("description") or name).strip()
        text = head + "\n" + (m.get("content") or "")
        res = check_memory(entities(text), **sets)
        failed = any(v in FAIL[k] for k, items in res.items() for _, v in items)
        stale = bool(STALE_RX.search(name + " " + head))
        front = front_of(name + " " + head)
        if stale or failed:
            review.append((name, "self-declared" if stale else "pointer-fail", front))
            continue
        kept.append(("T2 headline", name, front, head))
        for r in apply_rules(strip_frontmatter(m.get("content") or "")):
            kept.append(("T3 how-to-apply", name, front, r))
    traps = trap_items(SKILL.read_text())
    trap_rows = [("T1 trap", "SKILL.md trap %d" % i, "memecoin", t) for i, t in enumerate(traps, 1)]
    dec, seen = [], set()
    for src, ep, t, text, ent in facts:
        if src != "handoff" or t != "decision":
            continue
        d = bdate.get(int(ep.split(":")[1]))
        if not d or (t0 - date.fromisoformat(d)).days > CUT_DAYS:
            continue
        key = re.sub(r"[^a-z]+", " ", text.lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        dec.append(("T4 recent decision", ep, "memecoin", text))
    links = []
    for src, ep, t, text, ent in facts:
        if t != "retraction":
            continue
        target = [x for k in ("hex", "path", "knob_like", "position") for x in ent.get(k, ())]
        target += re.findall(r"\[\[[^\]]+\]\]|\([\w.-]+\.md\)", text)
        if target:
            links.append(("T5 retraction link", ep, "any", text))
    tiers = trap_rows + kept + dec + links
    # trap vs headline overlap (a trap that restates a memory headline is one fact, two sources)
    heads = [(k[1], words(k[3])) for k in kept if k[0] == "T2 headline"]
    overlap = 0
    for _, _, _, t in trap_rows:
        tw = words(t)
        if any(len(tw & hw) / max(len(tw | hw), 1) >= 0.25 for _, hw in heads if hw):
            overlap += 1
    by = collections.Counter(k[0] for k in tiers)
    print("VALUE CUT (facts kept; everything else stays an episode):")
    for tier in sorted(by):
        print("  %-20s %5d" % (tier, by[tier]))
    print("  %-20s %5d" % ("TOTAL facts", len(tiers)))
    fronts = collections.Counter(k[2] for k in kept if k[0] == "T2 headline")
    print("  headlines by front:", dict(fronts.most_common()))
    print("  held for REVIEW before import (headline not kept): %d  %s" % (
        len(review), dict(collections.Counter(r[1] for r in review))))
    print("  direct rows duplicating a session mirror (dropped): %d" % dup_rows)
    print("  traps sharing >=25%% content words with a kept headline: %d of %d" % (overlap, len(trap_rows)))
    kept_keys = {(k[1], k[3]) for k in tiers}
    dropped = [f for f in facts if (f[1], f[3]) not in kept_keys]
    dcls = collections.Counter("%s/%s" % (f[0], f[2]) for f in dropped)
    print("EPISODE-ONLY candidates: %d" % len(dropped))
    for k, v in sorted(dcls.items(), key=lambda kv: -kv[1]):
        print("  %-22s %6d" % (k, v))
    rng = random.Random(seed)
    for label, pool, strat in (("KEPT", tiers, lambda k: k[0]), ("DROPPED", dropped, lambda f: f[2])):
        strata = collections.defaultdict(list)
        for x in pool:
            strata[strat(x)].append(x)
        picks, keys = [], sorted(strata)
        while len(picks) < sample_n and any(strata[k] for k in keys):
            for k in keys:
                if strata[k] and len(picks) < sample_n:
                    picks.append(strata[k].pop(rng.randrange(len(strata[k]))))
        print("%s SAMPLE (%d, stratified, seed %d):" % (label, len(picks), seed))
        for x in picks:
            if label == "KEPT":
                print("  [%s] %s :: %s" % (x[0], x[1][:44], x[3][:190].replace("\n", " ")))
            else:
                print("  [%s|%s] %s :: %s" % (x[0], x[2], x[1][:44], x[3][:190].replace("\n", " ")))
    return tiers


NEG = re.compile(r"\b(never|not|no|don't|do not|must not|mustn't|avoid|stop|refuse[sd]?|without|"
                 r"forbid(?:den|s)?|instead of|rather than)\b", re.I)
ACTIONS = {  # entity key -> pattern; actions and tools rules argue about
    "git stash": r"git stash|\bstash\b", "git add -A": r"git add -A|add -A\b|git add \.",
    "git commit -a": r"commit -a\b", "git amend": r"--amend|\bamend\b", "git rebase": r"\brebase\b",
    "git push": r"git push|\bpush(?:ed|es)? (?:to )?origin", "subagents": r"\bsubagents?\b|Agent tool|\bagents?\b",
    "pytest -n": r"-n auto|-n \d+|xdist", "full suite": r"full (?:test )?suite|whole suite",
    "pkill -f": r"pkill -f|pkill\b", "pgrep -f": r"pgrep -f|pgrep\b", "backup": r"\bbackups?\b|--with-backup",
    "GeckoTerminal": r"GeckoTerminal|\bGT\b", "mainnet-beta": r"mainnet-beta", "Helius": r"Helius",
    "research key": r"research key", "funded key": r"funded key|custody RPC", "datetime.utcnow": r"utcnow",
    "sqlite :memory:": r":memory:", "get_readonly_connection": r"get_readonly_connection",
    "opened_at": r"\bopened_at\b", "opened_recorded_at": r"opened_recorded_at", "restart": r"\brestart",
    "deploy": r"\bdeploy", ".env": r"\.env\b", "worktree": r"\bworktrees?\b",
}
ACTIONS = {k: re.compile(v, re.I) for k, v in ACTIONS.items()}


def fact_keys(text):
    """{entity key: polarity} for one fact: '-' when a negation precedes the mention in its clause."""
    keys = {}
    spans = [(m.group(0), m.start()) for rx in ACTIONS.values() for m in rx.finditer(text)]
    names = [k for k, rx in ACTIONS.items() for _ in rx.finditer(text)]
    for m in re.finditer(r"`([^`]{3,60})`", text):
        spans.append((m.group(1), m.start()))
        names.append("`" + m.group(1).strip().lower() + "`")
    for m in KNOB.finditer(text):
        spans.append((m.group(1), m.start()))
        names.append(m.group(1))
    for name, (_, pos) in zip(names, spans):
        clause = re.split(r"[.;:!?]\s", text[max(0, pos - 80):pos])[-1]
        pol = "-" if NEG.search(clause) else "+"
        keys.setdefault(name, set()).add(pol)
    return {k: (v.pop() if len(v) == 1 else "mixed") for k, v in keys.items()}


def doctrine_rules():
    """Rule-typed candidates from CLAUDE.md and the handover STANDING RULES (comparison set only)."""
    out = []
    claude = (REPO / "CLAUDE.md").read_text()
    for ln, header, body in handoff_blocks(claude):
        out += [("DOCTRINE CLAUDE.md", "CLAUDE.md:%d" % ln, "doctrine", c)
                for c in split_candidates(header, body) if classify(c) == "rule"]
    lines = SKILL.read_text().splitlines()
    lo = next(i for i, l in enumerate(lines) if "STANDING RULES" in l)
    hi = next(i for i, l in enumerate(lines) if "NAMING TRAPS" in l)
    for c in split_candidates("", lines[lo + 1:hi]):
        out.append(("DOCTRINE standing", "SKILL.md standing", "doctrine", c))
    return out


def contradiction_pass(tiers, taus=(0.70, 0.75, 0.80, 0.85), max_group=60, sample_n=10, seed=20260928):
    """Dry run of a per-entity contradiction pass: pairs that share an entity key with opposite
    polarity, and embedding cosine at or above tau. Prints counts, a known-reversal recall check
    and a sample to grade by hand."""
    facts = [t for t in tiers if t[0].startswith(("T1", "T2", "T3"))] + doctrine_rules()
    keys = [fact_keys(t[3]) for t in facts]
    groups = collections.defaultdict(list)
    for i, k in enumerate(keys):
        for name, pol in k.items():
            if pol in "+-":
                groups[name].append((i, pol))
    big = {n for n, g in groups.items() if len(g) > max_group}
    from fastembed import TextEmbedding
    sys.path.insert(0, str(REPO))
    import bot.embeddings as emb
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    model = TextEmbedding(model_name=emb.MODEL_NAME, cache_dir=emb.CACHE_DIR, threads=4)
    vecs = [v / (sum(x * x for x in v) ** 0.5) for v in model.passage_embed([f[3][:1500] for f in facts])]
    pairs = {}
    for name, g in groups.items():
        if name in big:
            continue
        for a in range(len(g)):
            for b in range(a + 1, len(g)):
                (i, pi), (j, pj) = g[a], g[b]
                if pi != pj and facts[i][1] != facts[j][1]:
                    cos = float(sum(x * y for x, y in zip(vecs[i], vecs[j])))
                    key = (min(i, j), max(i, j))
                    if key not in pairs or pairs[key][1] < cos:
                        pairs[key] = (name, cos)
    print("CONTRADICTION PASS (dry run): %d facts (T1-T3 %d + doctrine %d); entity keys %d; "
          "keys over %d facts skipped: %d (%s)" % (
              len(facts), sum(1 for f in facts if f[0][0] == "T"),
              sum(1 for f in facts if f[0].startswith("DOCTRINE")), len(groups), max_group, len(big),
              ", ".join(sorted(big)[:8])))
    print("  opposite-polarity pairs sharing a key: %d" % len(pairs))
    for tau in taus:
        n = sum(1 for _, c in pairs.values() if c >= tau)
        nd = sum(1 for (i, j), (_, c) in pairs.items() if c >= tau and
                 ("doctrine" in (facts[i][2], facts[j][2])))
        print("  disputed at cos >= %.2f: %5d  (with a doctrine side %d)" % (tau, n, nd))
    for label, rx_a, rx_b in (("stash", r"git stash.*the fix|stash the fix", r"never .*git stash|git stash"),
                              ("agents", r"general-purpose Agent", r"Agent tool|subagents|no agents")):
        hits = [(p, v) for p, v in pairs.items()
                if any(re.search(rx_a, facts[x][3], re.I) for x in p)
                and any(re.search(rx_b, facts[x][3], re.I) and not re.search(rx_a, facts[x][3], re.I) for x in p)]
        best = max((v[1] for _, v in hits), default=None)
        print("  known reversal '%s': %d pairs raised, best cos %s" % (label, len(hits),
                                                                    "%.3f" % best if best else "n/a"))
    rng = random.Random(seed)
    tau = 0.80
    cand = sorted((p, v) for p, v in pairs.items() if v[1] >= tau)
    picks = rng.sample(cand, min(sample_n, len(cand)))
    print("SAMPLE (%d of %d at cos >= %.2f, seed %d):" % (len(picks), len(cand), tau, seed))
    for (i, j), (name, cos) in picks:
        print("  key=%s cos=%.3f" % (name, cos))
        for x in (i, j):
            print("     %s [%s %s] %s" % (keys[x].get(name), facts[x][0], facts[x][1][:40],
                                         facts[x][3][:200].replace("\n", " ")))
    return pairs


def selftest():
    blocks = handoff_blocks("## head\nline a\n- 2026-09-28T13:00Z Beta: first fact here. Second "
                            "fact here.\n  - a sub bullet fact\n")
    assert [b[0] for b in blocks] == [1, 3], blocks
    assert split_candidates(blocks[1][1], blocks[1][2]) == [
        "2026-09-28T13:00Z Beta: first fact here.", "Second fact here.", "a sub bullet fact"], \
        split_candidates(blocks[1][1], blocks[1][2])
    assert classify("Alpha: DEPLOYED 7a29f227 (runner + web)") == "state"
    assert classify("OPERATOR ORDER: set grace to 0") == "decision"
    assert classify("Gamma RETRACTED both claims") == "retraction"
    assert classify("fills p50 5658 ms, n=50") == "measurement"
    assert classify("never copy the ledger into memory") == "rule"
    sets = dict(commits={"abc1234": "abc1234" + "0" * 33, "def5678": "def5678" + "0" * 33},
                main_commits={"abc1234" + "0" * 33}, tree={"scripts/a.py"},
                tree_names_archive={"old.py"}, disk_exists=lambda p: p == "data/x.db",
                code_tokens={"MEMECOIN_KNOWN_KNOB", "MEMECOIN_EVM_GRACE_S"},
                env_names={"MEMECOIN_ENV_ONLY_KNOB"},
                env_prefixes={"MEMECOIN_KNOWN", "MEMECOIN_EVM", "MEMECOIN_ENV", "MEMECOIN_MADE"})
    text = ("abc1234 def5678 ff00aa1 scripts/a.py scripts/gone.py scripts/old.py data/x.db "
            "MEMECOIN_KNOWN_KNOB MEMECOIN_EVM_GRACE_S_4663 MEMECOIN_ENV_ONLY_KNOB MEMECOIN_MADE_UP_KNOB "
            "HTTP_403_ERROR_X")
    res = check_memory(entities(text, ()), **sets)
    got = {k: dict(v) for k, v in res.items()}
    assert got["sha"] == {"abc1234": "ok", "def5678": "off_main", "ff00aa1": "unresolved"}, got
    assert got["path"] == {"scripts/a.py": "ok", "scripts/gone.py": "missing",
                           "scripts/old.py": "moved_to_archive", "data/x.db": "ok"}, got
    assert got["knob"] == {"MEMECOIN_KNOWN_KNOB": "ok", "MEMECOIN_EVM_GRACE_S_4663": "ok",
                           "MEMECOIN_ENV_ONLY_KNOB": "env_only", "MEMECOIN_MADE_UP_KNOB": "unknown"}, got
    # a fixture that holds no bad reference must report no failure
    clean = check_memory(entities("abc1234 scripts/a.py MEMECOIN_KNOWN_KNOB", ()), **sets)
    assert not any(v in FAIL[k] for k, items in clean.items() for _, v in items)
    assert trap_items("x\nNAMING TRAPS -- a:\n- one\n  more\n- two\nSETTLED -- b\n- no\n") == [
        "one more", "two"]
    assert fact_keys("Never use `git stash`; commit instead.")["git stash"] == "-"
    assert fact_keys("`git stash` the fix and run the suite.")["git stash"] == "+"
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--snapshot", help="memories JSON from an earlier fetch")
    ap.add_argument("--dump", help="write every candidate fact as JSONL here")
    ap.add_argument("--cut", action="store_true", help="also print the value cut (fact tiers)")
    ap.add_argument("--contradict", action="store_true",
                    help="also dry-run the per-entity contradiction pass over the auto-import tiers")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    memories = json.load(open(args.snapshot)) if args.snapshot else fetch_memories()
    sets = repo_sets()
    facts = run(memories, wallet_handles(), sets)
    if args.cut or args.contradict:
        tiers = value_cut(memories, facts, sets)
        if args.contradict:
            contradiction_pass(tiers)
    if args.dump:
        with open(args.dump, "w") as fh:
            for src, ep, t, text, ent in facts:
                fh.write(json.dumps({"source": src, "episode": ep, "type": t, "text": text,
                                     "entities": {k: v for k, v in ent.items() if v}}) + "\n")


if __name__ == "__main__":
    main()
