"""One harmless schema probe, never retries, fallback, trades or evaluation evidence."""
from sniper.office import models


SCHEMA = {'type':'object','properties':{'ok':{'const':True}},'required':['ok'],'additionalProperties':False}


def run(provider, model, *, transport, clock, deadline_s):
    if not callable(transport):
        raise ValueError('launcher_transport_required')
    envelope = models.call(provider, model, 'Return only the requested JSON. No tools or actions.',
                           'Return {"ok":true}.', deadline_s=deadline_s, schema_obj=SCHEMA,
                           clock=clock, transport=transport)
    reason = envelope.error_reason or envelope.outcome
    passed = envelope.outcome == 'success' and envelope.served_model is not None
    if envelope.served_model is None and (envelope.outcome == 'success' or envelope.error_reason == 'model_mismatch'):
        reason = 'served_model_unknown'
    return {'version':'office-auth-canary-v1','state':'passed' if passed else 'refused',
            'provider':provider,'requested_model':model,'served_model':envelope.served_model,
            'attempt_id':envelope.attempt_id,'started_at':envelope.started_at,'ended_at':envelope.ended_at,
            'reason':None if passed else reason,'counts_as_evidence':False}
