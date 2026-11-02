import io

import pytest

from sniper.office import seat_search as S


@pytest.fixture(autouse=True)
def ddg_reader_controls(monkeypatch):
    # These are DDG reader controls; another engine must not consume their pages.
    monkeypatch.setattr(S,'ENGINES',tuple(engine for engine in S.ENGINES if engine[0]=='duckduckgo'))


def test_blocked_page_with_no_results_words_is_not_empty_evidence():
    result=S.parse('<html><title>No results.</title><body>Please verify you are human.</body></html>')
    assert result==dict(state='unknown',reason='result_markup_absent')


def test_oversized_partial_empty_page_is_unknown(monkeypatch):
    body=b'<div class="no-results">No results.</div>'+b' '*(S.MAX_BYTES+1)
    monkeypatch.setattr(S.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(body))
    assert S.search('query')==dict(state='unknown',reason='all_engines_failed',failed={'duckduckgo':'response_too_large'})


def test_complete_empty_page_and_real_result_remain_distinct(monkeypatch):
    bodies=[b'<div class="no-results">No results.</div>',b'<a class="result__a" href="https://example.test/">Title</a>']
    monkeypatch.setattr(S.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(bodies.pop(0)))
    assert S.search('empty')==dict(state='known',results=[],engine='duckduckgo',engine_prior='untested',failed={})
    assert S.search('matched')==dict(state='known',results=[dict(title='Title',url='https://example.test/',snippet='')],engine='duckduckgo',engine_prior='untested',failed={})


@pytest.mark.parametrize('body',[b'<html>Please verify you are human.</html>',b'<html>Changed markup</html>'])
def test_incomplete_or_blocked_page_does_not_become_explicit_empty(monkeypatch,body):
    monkeypatch.setattr(S.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(body))
    assert S.search('query')==dict(state='unknown',reason='all_engines_failed',failed={'duckduckgo':'result_markup_absent'})
