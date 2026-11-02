"""Bounded operator stderr evidence; never publish raw provider output or credentials."""
import os
from pathlib import Path
import re

TAIL_BYTES = 4096
CAPTURE_BYTES = 16384
_SECRET = r'[a-z0-9_-]*(?:authorization|(?:x[-_ \t]*)?api[-_ \t]*key|access[-_ \t]*token|refresh[-_ \t]*token|id[-_ \t]*token|oauth[-_ \t]*token|client[-_ \t]*secret|password|token|cookie|set-cookie)'
_VALUES = re.compile(r'''(?im)(\b'''+_SECRET+r'''["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*(?:"|\Z)|'(?:\\.|[^'\\])*(?:'|\Z)|[^\s,}\]]+)''')


def redact_text(text):
    """Shared credential redaction; callers own capture, prompt and output bounds."""
    text=re.sub(r'(?s)-----BEGIN[^\n]*PRIVATE KEY-----.*?(?:-----END[^\n]*PRIVATE KEY-----|\Z)','[REDACTED]',text)
    text=re.sub(r'''(?im)(\b[a-z0-9_-]*(?:authorization|cookie|set-cookie)["']?[ \t]*[:=][ \t]*)[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*''',lambda m:m[1]+'[REDACTED]',text)
    text=_VALUES.sub(lambda m:m[1]+'[REDACTED]',text)
    text=re.sub(r'(?i)\bBearer\s+[^\s,;]+','Bearer [REDACTED]',text)
    text=re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@',r'\1[REDACTED]@',text)
    text=re.sub(r'\b(?:sk-|rk-|sess-|ghp_|gho_|github_pat_|xox[baprs]-|eyJ)[A-Za-z0-9_.-]{8,}','[REDACTED]',text)
    text=re.sub(r'\b[A-Za-z0-9_+/=-]{32,}\b','[REDACTED]',text)
    return text


def stderr_tail(raw, *, truncated=False):
    if isinstance(raw,str):raw=raw.encode('utf-8',errors='replace')
    if not isinstance(raw,(bytes,bytearray)):return '[stderr unavailable]'
    truncated=truncated or len(raw)>CAPTURE_BYTES
    raw=bytes(raw[-CAPTURE_BYTES:])
    # A cut may begin inside a secret; discard the incomplete first line before redaction.
    if truncated:raw=raw.partition(b'\n')[2]
    text=raw.decode('utf-8',errors='replace')
    text=redact_text(text)
    if truncated:text='[stderr tail truncated]\n'+text
    return text.encode('utf-8')[-TAIL_BYTES:].decode('utf-8',errors='ignore')


def persist_stderr(root, request_id, raw, *, truncated=False):
    if not isinstance(request_id,str) or not request_id or Path(request_id).name!=request_id or request_id in ('.','..'):
        raise ValueError('diagnostic_request_id')
    data=stderr_tail(raw,truncated=truncated).encode('utf-8')
    fd=os.open(Path(root)/(request_id+'.stderr.txt'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(data)
