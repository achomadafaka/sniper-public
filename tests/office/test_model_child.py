import json
from pathlib import Path
import pytest
from sniper.office import model_child as C


@pytest.fixture(autouse=True)
def compatible_vendor(monkeypatch):
    monkeypatch.setattr(C.claude_compat,'startup_refusal',lambda:None)


def request(**kw):
    return dict(system='s',user='u',model='gpt-6-luna',deadline_s=2.,schema={'type':'object'},**kw)


@pytest.mark.parametrize('kind',['model_unavailable','schema','local_setup','other','Bearer SECRET'])
def test_codex_preserves_only_typed_provider_error_category(monkeypatch,kind):
    monkeypatch.setattr(C.transport,'_codex_json_locked',lambda *a,**kw:
        (None,'provider_error',1.,{'provider_error_kind':kind}))
    result=C.codex(request())
    assert result['provider_error_kind']==(kind if kind!='Bearer SECRET' else None)
    assert 'SECRET' not in json.dumps(result)


def test_claude_error_retains_bounded_safe_detail_without_prompt_or_credentials(monkeypatch):
    import claude_agent_sdk as sdk
    from types import SimpleNamespace
    secret='private prompt do not persist'
    error=('invalid_json_schema: additionalProperties missing; '+secret+
           ' Authorization: Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 '+
           'access_token="actual-private-token" https://example.com/?api_key=very-secret '+
           '/home/bigpapi/.claude/private.json '+('x'*1500))
    async def query(**kw):
        yield SimpleNamespace(result=error,structured_output=None,is_error=True,
            session_id='session',usage={},uuid='result')
    monkeypatch.setattr(sdk,'ResultMessage',SimpleNamespace)
    monkeypatch.setattr(sdk,'query',query)
    doc=request();doc['user']=secret
    result=C.claude(doc)
    assert result['call_error']=='provider_error'
    detail=result['provider_error_detail']
    assert 'invalid_json_schema' in detail and 'additionalProperties' in detail
    assert len(detail.encode())<=512
    for value in (secret,'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789','actual-private-token','very-secret','/home/bigpapi'):
        assert value not in detail
        assert value not in json.dumps(result)


@pytest.mark.parametrize('raw,secret',[
    ('schema rejected; Authorization: Basic dTpw','dTpw'),
    ('schema rejected; Cookie: one=abc; two=secret','two=secret'),
    ('schema rejected; Set-Cookie: session=abc; other=secret','other=secret'),
])
def test_short_authorization_and_entire_cookie_header_are_redacted(raw,secret):
    result=C.diagnostics.detail(raw)
    assert 'schema rejected' in result and secret not in result


def test_truncated_echo_of_large_prompt_is_not_retained():
    prompt='sensitive words '*1000
    result=C.diagnostics.detail('provider error: '+prompt,prompts=(prompt,))
    assert 'sensitive words' not in result and 'provider error' in result


@pytest.mark.parametrize('bad',[{'deadline_s':float('inf')},{'deadline_s':True},{'schema':[]},{'argv':['sh']},{'system':None}])
def test_invalid_request_never_dispatches(bad):
    doc=request();doc.update(bad);calls=[]
    result=C.execute('codex_exec',doc,dispatch=lambda *a:calls.append(a))
    assert result['call_error']=='invalid_request' and calls==[]


def test_direct_codex_has_no_nested_isolation_and_keeps_unknown_identity(monkeypatch):
    seen={}
    def call(w,prompt,**kw):
        seen['jail']=w.isolation('tmp','codex','home');seen['prompt']=prompt
        return '{}',None,12.,dict(usage={'input_tokens':3},model_echo=None)
    monkeypatch.setattr(C.transport,'_codex_json_locked',call)
    result=C.codex(request())
    assert seen['jail']==[] and result['model_echo'] is None and result['text']=='{}'
    assert result['usage']=={'input_tokens':3}


def test_execute_does_not_turn_transport_exception_into_success():
    def fail(*a):raise RuntimeError('secret')
    result=C.execute('claude_sdk',request(),dispatch=fail)
    assert result['call_error']=='transport_error' and 'secret' not in json.dumps(result)


def test_child_requires_jail_and_no_bypass_flag():
    assert C.sandbox_refusal('NoNewPrivs:\t0\nCapEff:\t0000\n')=='sandbox_required'
    assert C.sandbox_refusal('NoNewPrivs:\t1\nCapEff:\t0000\n') is None
    assert C.sandbox_refusal('NoNewPrivs:\t1\nCapEff:\t0001\n')=='sandbox_required'


def test_sdk_options_disable_tools_and_external_settings():
    options=C.claude_options(request())
    assert options.tools==[] and options.setting_sources==[] and options.mcp_servers=={}
    assert options.max_turns==1 and 'no-session-persistence' in options.extra_args


def test_codex_error_receipt_preserves_refusal_and_never_invents_echo(monkeypatch):
    monkeypatch.setattr(C.transport,'_codex_json_locked',lambda *a,**kw:(None,'timeout',2000.,{}))
    result=C.codex(request())
    assert result['call_error']=='timeout' and result['text'] is None and result['model_echo'] is None


def test_entrypoint_refuses_host_invocation_before_file_or_provider_access(monkeypatch):
    monkeypatch.setattr(C,'sandbox_refusal',lambda *a:'sandbox_required')
    assert C.main(['--adapter','codex_exec','--request','/opt/scratch/request.json','--output','/opt/scratch/result.json'])==2


def test_sdk_tool_event_refuses_without_returning_result(monkeypatch):
    import claude_agent_sdk as sdk
    async def query(**kw):
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id='t',name='Bash',input={})],model='claude-opus-5-5')
    monkeypatch.setattr(sdk,'query',query)
    result=C.claude(request())
    assert result=={'call_error':'tool_event'}


def test_claude_receives_schema_and_keeps_structured_only_result(monkeypatch):
    import claude_agent_sdk as sdk
    from types import SimpleNamespace
    seen={}
    async def query(**kw):
        seen['format']=kw['options'].output_format
        yield SimpleNamespace(result=None,structured_output={'unique_field':7},is_error=False,
                              session_id='s',usage={},uuid='r')
    monkeypatch.setattr(sdk,'ResultMessage',SimpleNamespace)
    monkeypatch.setattr(sdk,'query',query)
    doc=request();doc['schema']={'type':'object','properties':{'unique_field':{'type':'integer'}},'required':['unique_field']}
    result=C.claude(doc)
    assert seen['format']=={'type':'json_schema','schema':doc['schema']}
    assert json.loads(result['text'])=={'unique_field':7} and result['call_error'] is None


def test_codex_requested_basis_requires_private_locked_config(monkeypatch,tmp_path):
    home=tmp_path/'codex-home';home.mkdir()
    monkeypatch.setattr(C.checker,'prepare_codex_home',lambda *a,**k:(home,home/'auth.json'))
    def call(wrapper,prompt,**kw):
        actual,_=wrapper.prepare_codex_home(tmp_path)
        config=actual/'config.toml'
        assert config.read_text()=='model = "gpt-6-luna"\n'
        assert config.stat().st_mode & 0o222==0
        assert kw['model']=='gpt-6-luna'
        return '{}',None,1.,dict(model_echo=None)
    monkeypatch.setattr(C.transport,'_codex_json_locked',call)
    result=C.codex(request())
    assert result['model_echo'] is None
    assert result['served_model_basis']=='requested_pinned'


def test_codex_never_claims_pin_without_home_preparation(monkeypatch):
    monkeypatch.setattr(C.transport,'_codex_json_locked',lambda *a,**k:('{}',None,1.,dict(model_echo=None)))
    assert C.codex(request())['served_model_basis']=='unknown'


def test_sdk_structured_output_tool_is_data_not_authority(monkeypatch):
    import claude_agent_sdk as sdk
    from types import SimpleNamespace
    async def query(**kw):
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id='t',name='StructuredOutput',input={'ignored':'tool-input'})],model='claude-opus-5-5')
        yield SimpleNamespace(result='ignored text',structured_output={'validated':'final-object'},is_error=False,session_id='s',usage={},uuid='r')
    monkeypatch.setattr(sdk,'ResultMessage',SimpleNamespace);monkeypatch.setattr(sdk,'query',query)
    result=C.claude(request())
    assert result['call_error'] is None
    assert json.loads(result['text'])=={'validated':'final-object'}
    assert result['model_echo']=='claude-opus-5-5'


def test_structured_output_tool_without_validated_final_result_cannot_succeed(monkeypatch):
    import claude_agent_sdk as sdk
    from types import SimpleNamespace
    async def query(**kw):
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id='t',name='StructuredOutput',input={})],model='claude-opus-5-5')
        yield SimpleNamespace(result='{}',structured_output=None,is_error=False,session_id='s',usage={},uuid='r')
    monkeypatch.setattr(sdk,'ResultMessage',SimpleNamespace);monkeypatch.setattr(sdk,'query',query)
    result=C.claude(request())
    assert result['call_error']=='structured_output_missing' and result['text'] is None


@pytest.mark.parametrize('name',['structuredoutput','StructuredOutputBash','Bash',None])
def test_only_exact_sdk_structured_output_tool_is_allowed(monkeypatch,name):
    import claude_agent_sdk as sdk
    async def query(**kw):
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id='t',name=name,input={})],model='claude-opus-5-5')
        raise AssertionError('must stop before continuing an arbitrary tool stream')
    monkeypatch.setattr(sdk,'query',query)
    assert C.claude(request())=={'call_error':'tool_event'}


def test_recorded_scout_native_broker_call_is_never_executed(monkeypatch):
    import claude_agent_sdk as sdk
    async def query(**kw):
        # Isolated replay of c20027fc: Sonnet used a host-broker name natively.
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(
            id='recorded',name='desk.near_pass_list',input={'args':{}})],
            model='claude-sonnet-5-5')
        raise AssertionError('native broker call must stop the stream')
    monkeypatch.setattr(sdk,'query',query)
    assert C.claude(request())=={'call_error':'tool_event'}


@pytest.mark.parametrize('model',['claude-sonnet-5-5','claude-opus-5-5'])
def test_all_claude_seats_receive_json_transport_envelope(monkeypatch,model):
    import claude_agent_sdk as sdk
    from types import SimpleNamespace
    doc=request();doc['model']=model
    line=('Return only a JSON object matching the supplied schema. Do not use tools. '
          'Represent host broker requests only in the JSON requests array; never invoke them as native tools.')
    async def query(**kw):
        assert kw['prompt']==doc['user']+'\n\n'+line
        assert kw['options'].system_prompt==doc['system']
        yield SimpleNamespace(structured_output={},is_error=False,session_id='s',usage={},uuid='r')
    monkeypatch.setattr(sdk,'ResultMessage',SimpleNamespace)
    monkeypatch.setattr(sdk,'query',query)
    assert C.claude(doc)['call_error'] is None
