"""Pure legacy-source parsing shared by import, catch-up, and nightly doctrine checks."""
import hashlib
import re
from datetime import datetime, timedelta, timezone

_WS = re.compile(r"\s+")
_ISO = re.compile(r"(20\d\d-\d\d-\d\d)(?:T(\d\d:\d\d(?::\d\d(?:\.\d+)?)?)(Z|[+-]\d\d:\d\d)?)?")
IMPORT_SESSION = "import:v1"
_MARKER = re.compile(r"\b(RETRACTED|retracted|WRONG|withdrawn|STALE|SUPERSEDED|superseded|CORRECTED|"
                     r"no longer (?:true|holds|applies))\b")
_ROLE = re.compile(r"^\W*(?:(?:\d{4}-\d\d-\d\d)(?:T[\d:]+Z)?\s+|[\d:]+Z\s+)?(Alpha|Beta|Gamma|Lambda|OPERATOR|Operator|Codex)\b")
_DAYS = {"measurement": 7, "settled": 60, "task": 3}


def normalize(statement):
    return _WS.sub(" ", str(statement or "")).strip()


def episode_key(anchor, body):
    return "%s#%s" % (anchor, hashlib.sha256(body.encode()).hexdigest()[:12])


def _iso(text):
    m = _ISO.search(str(text or ""))
    if not m:
        return None
    when = m.group(1) + "T" + (m.group(2) or "00:00:00")
    if len(when) == 16:
        when += ":00"
    try:
        parsed = datetime.fromisoformat(when + (m.group(3) or "").replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key in ("name", "description", "type", "originSessionId", "modified"):
            out.setdefault(key, value.strip().strip('"').strip("'"))
    out["session"] = out.pop("originSessionId", None)
    return out


def source_date(fm, file_name):
    return _iso(fm.get("modified")) or _iso(file_name)


def memory_source(file_name, text):
    fm = frontmatter(text)
    head = normalize(fm.get("description") or fm.get("name") or file_name)
    mtype = fm.get("type")
    marker = "marked" if _MARKER.search(head) else None      # routes to the judge queue; never closes
    return {"headline": head, "bullets": [normalize(b) for b in apply_rules(strip_frontmatter(text))],
            "kind": "rule" if mtype == "feedback" else "finding", "type": mtype,
            "session": fm.get("session"), "date": source_date(fm, file_name), "marker": marker}


def split_statement(text):
    text = normalize(text)
    if len(text) <= 600:
        return text, None
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sentence) + 1 > 600:
            break
        out = (out + " " + sentence).strip()
    if not out:
        out = text[:597].rsplit(" ", 1)[0] + "..."
    return out, text[:4096]


def _items(lines, lo, hi):
    items, cur = [], None
    for n in range(lo, hi):
        line = lines[n]
        if line.startswith("- "):
            cur = [n + 1, [line[2:].strip()]]
            items.append(cur)
        elif cur is not None and line.strip():
            cur[1].append(line.strip())
    return [(first, normalize(" ".join(parts))) for first, parts in items]


def traps(skill_text):
    lines = skill_text.splitlines()
    lo = next(i for i, l in enumerate(lines) if "NAMING TRAPS" in l)
    hi = next(i for i, l in enumerate(lines) if i > lo and l.startswith("SETTLED"))
    return _items(lines, lo + 1, hi)


def standing_rules(skill_text):
    lines = skill_text.splitlines()
    lo = next(i for i, l in enumerate(lines) if "STANDING RULES" in l)
    hi = next(i for i, l in enumerate(lines) if "NAMING TRAPS" in l)
    return _items(lines, lo + 1, hi)


def doctrine_rules(text, source):
    out = []
    for line_no, header, body in handoff_blocks(text):
        for candidate in split_candidates(header, body):
            if re.search(r"\b(never|always|must|do not|don't|standing rule)\b", candidate, re.I):
                out.append({"statement": normalize(candidate), "line": line_no, "source": source})
    return out


def handoff_author(header):
    m = _ROLE.search(header or "")
    if not m or m.group(1) == "Codex":
        return None
    return "operator" if m.group(1).lower() == "operator" else m.group(1).lower()


def verify_by(kind, valid_from, key):
    start = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
    stagger = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 14
    return (start + timedelta(days=_DAYS.get(kind, 30) + stagger)).isoformat()


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

SENT = re.compile(r"(?<=[.;])\s+(?=[A-Z(\"'])")

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

STALE_RX = re.compile(r"\b(STALE|RETRACTED|retracted|CORRECTED|SUPERSEDED|superseded|WRONG|withdrawn|"
                      r"no longer (true|holds|applies))\b")

FRONT_RX = [("memecoin", r"memecoin|fomo|solana|4663|8453|pump|funded|j7|jupiter|evm|bsc"),
            ("crypto5m", r"crypto5m|btc.?5m|5m.?binary"),
            ("hyperliquid", r"hyperl|\bhl[-_ ]|far.?whale|cascade|microstructure"),
            ("wallet-analyzer", r"wallet.?protocol|dossier|archetype|swissmiss|smallfish|survivorship|polymarket"),
            ("sports/markets", r"sports?|soccer|political|conviction"),
            ("brain/infra", r"brain|memor(y|ies)|retrieval|embedding|knowledge|workflow|git|test")]

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
    # A bullet wrapped over several lines keeps its continuation lines (Gamma trap f865b165: taking
    # only lines that start '- ' cut 116 of 521 imported rules mid-sentence). A blank line closes
    # the open bullet, so a narrative paragraph after it is never glued onto a rule.
    bullets, open_ = [], False
    for line in block.splitlines():
        text = line.strip()
        if text.startswith(("- ", "* ")):
            bullets.append(text[2:].strip())
            open_ = True
        elif not text:
            open_ = False
        elif open_:
            bullets[-1] += " " + text
    return [b for b in bullets if len(b) >= 12] or ([block] if len(block) >= 12 else [])
