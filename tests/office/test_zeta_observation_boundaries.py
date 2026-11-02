import json
import pytest
from office.test_runtime_observations import db, context, envelope
from sniper.office import models as M, runtime_observations as O


def test_provider_error_model_echo_cannot_persist_raw_credentials(db):
    result=M.call('claude','claude-opus-5-5','s','u',deadline_s=10,clock=lambda:1000.,
        transport=lambda *a,**k:{'call_error':'provider_error','model_echo':'Authorization: Bearer short-secret'})
    buffer=O.Buffer(db,clock=lambda:1003.)
    buffer.capture_call(context(),result,call_id='secret-echo');buffer.flush()
    assert 'short-secret' not in json.dumps(dict(db.execute('SELECT * FROM model_calls').fetchone()))

def test_provider_usage_over_sqlite_integer_range_is_unknown_not_a_flush_crash(db):
    result=M.call('claude','claude-opus-5-5','s','u',deadline_s=10,clock=lambda:1000.,
        transport=lambda *a,**k:{'text':'{}','model_echo':'claude-opus-5-5','usage':{'input_tokens':2**80}})
    buffer=O.Buffer(db,clock=lambda:1003.)
    buffer.capture_call(context(),result,call_id='big-usage')
    assert buffer.flush()['last_error'] is None
    row=db.execute('SELECT input_tokens,invalid_metadata_n FROM model_calls').fetchone()
    assert tuple(row)==(None,1)


def test_unrepresentable_provider_cost_is_unknown_not_a_model_call_crash():
    result=M.call('claude','claude-opus-5-5','s','u',deadline_s=10,clock=lambda:1000.,
        transport=lambda *a,**k:{'text':'{}','model_echo':'claude-opus-5-5','cost_usd':2**2048,'cost_basis':'billed_receipt'})
    assert result.cost_usd is None and result.invalid_metadata_n==1


def test_out_of_range_integer_cost_is_unknown_and_counted(db):
    result=M.call('claude','claude-opus-5-5','s','u',deadline_s=10,clock=lambda:1000.,
        transport=lambda *a,**k:{'text':'{}','model_echo':'claude-opus-5-5','cost_usd':2**80,'cost_basis':'billed_receipt'})
    buffer=O.Buffer(db,clock=lambda:1003.)
    buffer.capture_call(context(),result,call_id='big-cost')
    assert buffer.flush()['last_error'] is None
    assert tuple(db.execute('SELECT cost_usd,invalid_metadata_n FROM model_calls').fetchone())==(None,1)


def test_known_zero_and_safe_mismatch_echo_are_preserved(db):
    result=M.call('claude','claude-opus-5-5','s','u',deadline_s=10,clock=lambda:1000.,
        transport=lambda *a,**k:{'call_error':'provider_error','model_echo':'claude-sonnet-5-5',
            'usage':{'input_tokens':0},'cost_usd':0,'cost_basis':'billed_receipt'})
    buffer=O.Buffer(db,clock=lambda:1003.)
    buffer.capture_call(context(),result,call_id='zero');buffer.flush()
    row=dict(db.execute('SELECT * FROM model_calls').fetchone())
    assert row['input_tokens']==0 and row['cost_usd']==0 and row['invalid_metadata_n']==0
    assert row['served_model']=='claude-sonnet-5-5'


def test_existing_rows_gain_unknown_metadata_flag_without_rewriting(db):
    buffer=O.Buffer(db,clock=lambda:1003.)
    buffer.capture_call(context(),envelope(),call_id='legacy');buffer.flush()
    db.execute('ALTER TABLE model_calls DROP COLUMN invalid_metadata_n')
    O.ensure_schema(db)
    assert db.execute('SELECT invalid_metadata_n FROM model_calls').fetchone()[0] is None
