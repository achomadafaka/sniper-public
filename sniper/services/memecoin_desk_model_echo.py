"""The one model-echo rule for every desk model seat (Gamma, Alpha 2026-10-21): JUDGE, crawler,
post-mortem, RISK and scout import this, never their own copy.

An echo names the seat's model when it equals MODEL exactly, or MODEL plus one dated-snapshot suffix,
"-" and exactly 8 digits (claude-haiku-5-5-20261015). Another model, MODEL-anything, MODEL plus
characters or a prefix of MODEL is a mismatch. A missing echo (None: the SDK omitted it) is allowed,
the same in every seat. Each seat stores the raw echo as evidence beside its proof; the proof's
model field stays MODEL. The first operator-gated live call records the actual echo string."""
import re

REPR_CAP = 200          # chars of the raw repr kept in the marker; an unbounded container must not bloat rows (Alpha)
TYPE_CAP = 60           # chars of a type name (a generated class name can be long too, Gamma)
MARKER_MAX = len("nonstr::<repr failed: >") + 2 * REPR_CAP + 3   # the longest echo_text of a non-str


def echo_ok(echo, model):
    """Only None or a str can match; a container, bytes or an object that prints as MODEL is a mismatch.
    The snapshot digits are ASCII [0-9]: \\d also takes Arabic-Indic and fullwidth digits (Gamma LOW)."""
    return echo is None or (isinstance(echo, str) and re.fullmatch(re.escape(model) + r"(-[0-9]{8})?", echo) is not None)


def _bindable(text):
    try:
        text.encode("utf-8")                 # a lone surrogate (JSON "\\ud800") makes the sqlite3 bind raise
        return True
    except UnicodeEncodeError:
        return False


def _cut(text, cap):
    return text if len(text) <= cap else text[:cap] + "..."


def echo_text(echo):
    """The echo as the SDK adapter hands it to callers, always None or a bindable, bounded str.
    A valid str is unchanged. A str that is not UTF-8 encodable or longer than REPR_CAP becomes
    'badstr:<ascii(echo) cut>'; anything else 'nonstr:<type>:<repr>' (type cut at TYPE_CAP, repr at
    REPR_CAP plus '...'). The raw value survives in the existing model_echo column (no schema change),
    and neither prefix can match MODEL, so echo_ok on a marker is a mismatch. Never None for a bad
    echo: None is allowed, so a bad echo would pass."""
    if echo is None:
        return None
    if isinstance(echo, str):
        if len(echo) <= REPR_CAP and _bindable(echo):
            return echo
        return "badstr:" + _cut(ascii(echo[:REPR_CAP + 1]), REPR_CAP)
    kind = type(echo).__name__[:TYPE_CAP]
    try:                                     # repr and slice together: a broken __repr__ still yields a marker
        raw = _cut(repr(echo), REPR_CAP)
    except Exception as exc:
        raw = "<repr failed: %s>" % type(exc).__name__[:TYPE_CAP]
    kind, raw = (x if _bindable(x) else ascii(x)[:REPR_CAP] for x in (kind, raw))
    return "nonstr:%s:%s" % (kind, raw)
