"""Lossless display projection only; never feeds admission, receipts or sheet hashing."""
from copy import deepcopy
import json


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),default=str)


def project(result):
    out=deepcopy({k:result.get(k) for k in ('state','reason','sheet','lenses')})
    if 'ps_pending_gaps' in result:out['ps_pending_gaps']=deepcopy(result['ps_pending_gaps'])
    body=out.get('sheet')
    if out.get('state')!='known' or not isinstance(body,dict):return out
    out['display_format']='sheet-display-v1'
    out['display_note']='Exact repeated blocks use $ref JSON pointers within this reply; stored admission sheet is unchanged.'
    # Keep the authoritative nested blocks intact; only their exact top-level aliases become references.
    for name in ('ps_facts','d_blocks'):
        source=body.get(name)
        if not isinstance(source,dict):continue
        for key,value in source.items():
            if (key in body and key not in ('ps_facts','d_blocks') and isinstance(value,(dict,list))
                    and value and _json(value)==_json(body[key])):
                token=key.replace('~','~0').replace('/','~1')
                body[key]={'$ref':'#/sheet/'+name+'/'+token}
    safety=body.get('safety');proof=body.get('safety_proof')
    safety=safety if isinstance(safety,dict) else {}
    proof=proof if isinstance(proof,dict) else {}
    at=proof.get('known_at')
    if type(at) not in (int,float) or not 0<=at<1e15:at=None
    out['holder_evidence']={key:dict(value=safety.get(key),as_of=at,observation_at=None)
                            for key in ('top10_pct','creator_pct')}
    out['holder_evidence'].update(clock_basis='as_recorded_unix_seconds',source='safety_proof.known_at',
        note='Values are frozen admission evidence known_at, not refreshed. Holder observation time is unavailable; this is not a freshness claim. as_of is copied without clock conversion.')
    return out
