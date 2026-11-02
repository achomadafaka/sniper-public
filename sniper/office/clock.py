"""Office clocks fail closed when the shared correction sample is unknown."""
import json
import math
from pathlib import Path


def corrected_now(path, *, host_now):
    try:
        doc = json.loads(Path(path).read_text())
        offset, sampled_at = doc['offset_s'], doc['at']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (host_now, offset, sampled_at)) or not 0 <= host_now - sampled_at <= 3600.:
        return None
    now = host_now - offset
    return now if math.isfinite(now) else None
