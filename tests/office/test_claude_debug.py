"""Debug wiring only: fake transports, hermetic installer, no model/auth/services."""
import json
import pytest
from sniper.office import model_child as C, runtime as R
from .test_model_child import request
from .test_runtime import configured
from .test_pilot_activate import env, rewrite_runtime


def test_default_on_and_explicit_off_do_not_change_model_options():
    enabled=C.claude_options(request())
    disabled=C.claude_options(request(debug_stderr=False))
    assert 'debug-to-stderr' in enabled.extra_args and callable(enabled.stderr)
    assert 'debug-to-stderr' not in disabled.extra_args and disabled.stderr is None
    for key in ('model','tools','setting_sources','mcp_servers','max_turns','effort','system_prompt','output_format'):
        assert getattr(enabled,key)==getattr(disabled,key)


def test_callback_is_bounded_redacted_and_keeps_stdout_clean(capsys):
    doc=request();doc['user']='private prompt exact'
    callback=C.claude_options(doc).stderr
    callback('phase: initialize OPENAI_API_KEY="short-secret"')
    callback('private prompt exact /home/operator/private.json https://host.invalid/?token=short-secret')
    for _ in range(1000):callback('phase: wait '+('🔥'*2000))
    output=capsys.readouterr()
    assert output.out=='' and len(output.err.encode())<=4096
    assert 'phase: initialize' in output.err
    for value in ('short-secret','private prompt exact','/home/operator','host.invalid'):assert value not in output.err


@pytest.mark.parametrize('flag',[None,1,'true'])
def test_debug_flag_is_boolean_at_child_boundary(flag):
    calls=[]
    assert C.execute('claude_sdk',request(debug_stderr=flag),dispatch=lambda doc:calls.append(doc))=={'call_error':'invalid_request'}
    assert calls==[]


def test_runtime_accepts_flag_and_forwards_false_to_actual_child_doc(configured,monkeypatch):
    config,args,write=configured;config['claude_debug_stderr']=False;write()
    runtime=R.build(**args)
    try:
        supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture');runtime['initialize'](gen)
        docs=[]
        def call(adapter,doc,**kw):docs.append(doc);return {'call_error':'timeout'}
        monkeypatch.setattr(runtime['transports']['claude'].client,'call',call)
        task=runtime['tasks']()[0]
        supervisor.run_turn(task['agent_id'],gen=gen,deadline_s=30,max_steps=1,turn_id=task['turn_id'])
        assert docs[0]['debug_stderr'] is False
        supervisor.lease.release(gen)
    finally:runtime['close']()


@pytest.mark.parametrize('flag',[True,False])
def test_installer_accepts_optional_debug_flag_in_dry_run(env,flag):
    rewrite_runtime(env,lambda doc:doc.update(claude_debug_stderr=flag))
    result=env['run']()
    assert result.returncode==0,result.stdout+result.stderr
    assert not (env['repo']/'.runtime').exists()


def test_callback_drops_folded_payload_lines_and_marks_truncation(capsys):
    callback=C.claude_options(request()).stderr
    callback('Authorization: Bearer')
    callback(' short-secret')
    for _ in range(100):callback('phase '+('x '*500))
    output=capsys.readouterr().err
    assert 'short-secret' not in output and 'budget exhausted' in output
    assert len(output.encode())<=4096


@pytest.mark.parametrize('value',[None,'true',1])
def test_runtime_refuses_nonboolean_debug_flag_before_state(configured,value):
    config,args,write=configured;config['claude_debug_stderr']=value;write()
    with pytest.raises(R.RuntimeRefused,match='runtime_config_unapproved'):R.build(**args)
    assert not args['store_path'].exists()


def test_installer_refuses_nonboolean_debug_flag(env):
    rewrite_runtime(env,lambda doc:doc.update(claude_debug_stderr='yes'))
    assert env['run']().returncode==2
    assert not (env['repo']/'.runtime').exists()


def test_multiline_prompt_fragments_never_reach_debug_output(capsys):
    emit=C._claude_stderr({'system':'system policy\nPRIVATE_SYSTEM_LINE','user':'user context\nPRIVATE_USER_LINE'})
    emit('PRIVATE_SYSTEM_LINE');emit('PRIVATE_USER_LINE')
    output=capsys.readouterr().err
    assert 'PRIVATE_SYSTEM_LINE' not in output and 'PRIVATE_USER_LINE' not in output
    assert output.count('[prompt line suppressed]')==2


def test_nested_json_and_embedded_value_fragments_are_suppressed(capsys):
    doc={'system':'policy','user':json.dumps({'array':['NESTED_PRIVATE_VALUE'],'data':'START DATA '+json.dumps({'memo':'EMBEDDED_PRIVATE_VALUE'})+' END DATA'})}
    emit=C._claude_stderr(doc)
    emit('prefix NESTED_PRIVATE_VALUE suffix')
    emit('prefix EMBEDDED_PRIVATE_VALUE suffix')
    emit('phase: normal startup')
    output=capsys.readouterr().err
    assert 'PRIVATE_VALUE' not in output and output.count('[prompt line suppressed]')==2
    assert 'normal startup' in output


@pytest.mark.parametrize('render',[lambda s:json.dumps(s)[1:-1],lambda s:json.dumps(json.dumps(s)[1:-1])[1:-1],lambda s:s.swapcase().replace('_',' - ')])
def test_canonical_unicode_escape_and_case_prompt_is_suppressed(capsys,render):
    secret='πPRIVATE_LINE'
    emit=C._claude_stderr({'system':'policy\n'+secret,'user':'public context'})
    emit(render(secret))
    assert '[prompt line suppressed]' in capsys.readouterr().err


def test_canonical_partial_middle_window_is_suppressed(capsys):
    secret='private'+''.join(str(i%10) for i in range(180))+'material'
    emit=C._claude_stderr({'system':'policy','user':json.dumps({'nested':secret})})
    emit('debug: '+secret[47:96].upper())
    assert '[prompt line suppressed]' in capsys.readouterr().err


def test_canonical_nfkc_and_oversized_debug_fail_closed(capsys):
    emit=C._claude_stderr({'system':'privateidentity','user':'ordinary context'})
    emit('ＰＲＩＶＡＴＥＩＤＥＮＴＩＴＹ')
    emit('secret '+('x'*(8*C.MAX_BYTES+1)))
    output=capsys.readouterr().err
    assert output.count('[prompt line suppressed]')==2
    assert 'secret' not in output


def test_canonical_surrogate_pair_and_lone_surrogate_fail_closed(capsys):
    secret='\U0001d504PRIVATE_LINE'
    emit=C._claude_stderr({'system':'policy\n'+secret,'user':'ordinary context'})
    emit(json.dumps(secret)[1:-1])
    emit(r'\ud835LONE_PRIVATE_LINE')
    output=capsys.readouterr().err
    assert output.count('[prompt line suppressed]')==2
    assert 'PRIVATE_LINE' not in output
