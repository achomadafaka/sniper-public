"""The CLI drill-down must preserve the boot's Office exclusion boundary."""
import pytest

from test_brain_graph_cli import _cli
from test_brain_graph_office_filter import seeded
from bot.brain_graph.store import BrainUnavailable


@pytest.mark.parametrize('args',[
    ['entity','actor','alpha'],['queue','--role','alpha'],['queue','--role','alpha','--author','beta'],
])
def test_cli_drill_down_excludes_mixed_office_facts(args,capsys):
    cli=_cli()
    store,clean,dirty=seeded()
    # Queue readers select unverified facts; the same valid mixed-namespace row must be hidden.
    store.patch_fact(dirty['id'],dirty['version'],{'status':'unverified'})
    assert cli.main(args,store=store)==0
    out=capsys.readouterr().out
    assert 'OFFICE_WEB_SENTINEL' not in out
    assert clean['statement'] in out


@pytest.mark.parametrize('args', [['entity','actor','alpha'],['queue','--role','alpha']])
def test_cli_drill_down_fails_closed_when_provenance_lookup_fails(args,capsys):
    cli=_cli()
    store,_,_=seeded()
    def failed(*args,**kwargs):
        raise BrainUnavailable('provenance unavailable')
    store.rows_page=failed
    assert cli.main(args,store=store)==4
    out=capsys.readouterr().out
    assert 'BRAIN UNREACHABLE' in out and 'OFFICE_WEB_SENTINEL' not in out
