import pytest
from sniper.office.launcher_diagnostics import stderr_tail

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
