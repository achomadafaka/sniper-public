"""Claude seat auth evidence and access-only snapshots. Never refreshes a token."""
import hashlib
import json
import math
import os
from pathlib import Path

from .launcher_client import read_regular

ACCESS_FIELDS=('accessToken','expiresAt','scopes','subscriptionType','rateLimitTier')


class Refused(ValueError):
    def __init__(self,reason,mark):
        super().__init__(reason);self.reason,self.mark=reason,mark


def read(path, *, now):
    mark=None
    try:
        raw=read_regular(path);mark=hashlib.sha256(raw).hexdigest()
        oauth=json.loads(raw)['claudeAiOauth'];expiry=oauth['expiresAt'];token=oauth['accessToken']
        if not isinstance(token,str) or not token or type(expiry) not in (int,float) or not math.isfinite(expiry):raise ValueError()
        if expiry/1000<=now+60:return mark,None,'claude_auth_expired'
        return mark,dict(claudeAiOauth={k:oauth[k] for k in ACCESS_FIELDS if k in oauth}),None
    except (OSError,ValueError,KeyError,TypeError):return mark,None,'claude_auth_unknown'


def exception_reason(exc):
    if getattr(exc,'status_code',None) in (401,403):return 'claude_auth_failed'
    text=getattr(exc,'stderr','')
    text=text[:8192].lower() if isinstance(text,str) else ''
    if any(s in text for s in ('oauth token has expired','oauth token expired','authentication_error','refresh token has already been used','invalid_grant','authentication_failed')):
        return 'claude_auth_failed'
    return None


def quota_page(cfg,doc,*,page):
    """The single Claude account poller persists a delivery latch; transport failure never clears auth."""
    name=hashlib.sha256(cfg['account_alias'].encode()).hexdigest()[:16]
    path=Path(cfg['quota_db']).with_name('claude-auth-page-'+name+'.json')
    try:saved=json.loads(path.read_text());alerted=saved.get('alerted') is True
    except (OSError,ValueError,AttributeError):alerted=False
    if doc.get('reason')=='auth':
        if alerted:return
        if page('Office Claude quota authentication failed: '+cfg['account_alias']) is False:raise ValueError('page_not_delivered')
        alerted=True
    elif doc.get('state') in ('known','partial'):alerted=False
    else:return
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(dict(alerted=alerted))+'\n');os.replace(tmp,path)


def message_reason(message):
    if getattr(message,'error',None)=='authentication_failed':return 'claude_auth_failed'
    if getattr(message,'is_error',False):
        if getattr(message,'api_error_status',None) in (401,403):return 'claude_auth_failed'
        from types import SimpleNamespace
        for text in (getattr(message,'result',None),*(getattr(message,'errors',None) or ())):
            if exception_reason(SimpleNamespace(stderr=text)):return 'claude_auth_failed'
    return None
