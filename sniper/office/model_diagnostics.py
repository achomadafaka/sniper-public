"""Bounded failure-only diagnostics; no environment, credential files or prompt persistence."""
import re
from .launcher_diagnostics import redact_text

KINDS = frozenset({'model_unavailable','schema','local_setup','other'})
MAX_DETAIL_BYTES = 512


def detail(value, *, prompts=()):
    if not isinstance(value,str) or not value:
        return None
    # Bound work before regexes; a truncated secret value is still matched and removed.
    value=value[:8192]
    for prompt in prompts:
        if isinstance(prompt,str) and len(prompt)>=8:
            value=value.replace(prompt,'[prompt redacted]')
            if len(prompt)>128:
                at=value.find(prompt[:128])
                if at>=0:
                    value=value[:at]+'[prompt redacted]'+value[at+len(prompt):]
    value=redact_text(value)
    value=re.sub(r'https?://[^\s<>"\']+','[url redacted]',value,flags=re.I)
    value=re.sub(r'/(?:home|tmp|run/user|opt)/[^\s<>"\']+','[path redacted]',value)
    value=' '.join(value.split())
    return value.encode('utf-8')[:MAX_DETAIL_BYTES].decode('utf-8',errors='ignore') or None


def kind(value):
    return value if isinstance(value,str) and value in KINDS else None
