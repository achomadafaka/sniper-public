"""Local nightly health only. Never contacts a provider or changes money-health warnings."""
import json
import math
from pathlib import Path

STALE_H = 36.0


def summary(path, now_ts):
    try:
        payload = json.loads(Path(path).read_text())
        if not isinstance(payload, dict) or not isinstance(payload.get('metrics'), dict):
            raise ValueError('invalid health shape')
        written = float(payload['written_ts'])
        now = float(now_ts)
        if not math.isfinite(written) or not math.isfinite(now) or written > now:
            raise ValueError('invalid timestamp')
    except FileNotFoundError:
        return {'state': 'missing'}
    except (OSError, ValueError, TypeError, KeyError):
        return {'state': 'error', 'reason': 'unreadable'}
    metrics = payload['metrics']
    age_h = (now - written) / 3600
    statuses = metrics.get('by_status')
    state = 'error' if payload.get('error') else 'stale' if age_h > STALE_H else 'ok'
    return dict(state=state, age_h=round(age_h, 1), facts_live=metrics.get('facts_live'),
                share_verified=metrics.get('share_verified'), disputed=metrics.get('disputed'),
                evidence_broken=statuses.get('evidence_broken', 0) if isinstance(statuses, dict) else None,
                queue=metrics.get('queue'), error=payload.get('error'))
