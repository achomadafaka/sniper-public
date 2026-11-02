import pytest
from sniper.office.model_diagnostics import detail

def stderr_tail(raw):
    return detail(raw.decode() if isinstance(raw, bytes) else raw)

@pytest.mark.parametrize('raw', [b'OPENAI_API_KEY=short-secret', b'RH_API_KEY=short-secret', b'API key: short-secret', b'client secret: short-secret', b'OPENAI_API_KEY="short-secret"'])
def test_named_secret_assignments_cannot_escape(raw):
    assert 'short-secret' not in stderr_tail(raw)

@pytest.mark.parametrize('raw', [b'HTTP_COOKIE: a=short-secret; b=second-secret', b'HTTP_AUTHORIZATION=Basic short-secret second-secret'])
def test_prefixed_auth_headers_are_redacted_in_full(raw):
    clean=stderr_tail(raw)
    assert 'short-secret' not in clean and 'second-secret' not in clean

@pytest.mark.parametrize('raw', [b'Authorization: Bearer\n short-secret\nError: denied', b'HTTP_COOKIE: a=short-secret;\r\n\tb=second-secret\r\nError: denied'])
def test_folded_secret_headers_are_redacted_without_losing_next_diagnostic(raw):
    clean=stderr_tail(raw)
    assert 'short-secret' not in clean and 'second-secret' not in clean
    assert 'Error: denied' in clean

@pytest.mark.parametrize('raw', ['api_key="prefix\\\"short-secret"', "api_key='prefix\\'short-secret'"])
def test_escaped_quotes_do_not_end_secret_redaction_early(raw):
    assert 'short-secret' not in stderr_tail(raw)

@pytest.mark.parametrize('quote', ['"', "'"])
def test_work_limit_does_not_expose_a_truncated_quoted_secret(quote):
    raw='api_key='+quote+('short-secret '*1000)+quote
    assert 'short-secret' not in stderr_tail(raw)

@pytest.mark.parametrize('text', [
    'OPENAI_API_KEY=short-secret', 'RH_API_KEY=short-secret', 'API key: short-secret',
    'client secret: short-secret', 'OPENAI_API_KEY="short-secret"',
    'HTTP_COOKIE: a=short-secret; b=second-secret', 'HTTP_AUTHORIZATION=Basic short-secret second-secret',
    'Authorization: Bearer\n short-secret\nError: denied',
    'HTTP_COOKIE: a=short-secret;\r\n\tb=second-secret\r\nError: denied',
])
def test_both_paths_use_one_redactor_for_original_witnesses(text):
    from sniper.office import launcher_diagnostics as launch, model_diagnostics as model
    assert model.redact_text is launch.redact_text
    for clean in (launch.stderr_tail(text), model.detail(text)):
        assert 'short-secret' not in clean and 'second-secret' not in clean
