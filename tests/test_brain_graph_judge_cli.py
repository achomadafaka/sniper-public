from unittest.mock import Mock
import pytest
from bot.brain_graph.store import BrainRefused
from test_brain_graph_cli import _cli


@pytest.mark.parametrize('role', ['gamma', 'codex'])
def test_judge_cli_passes_exact_pair_versions(capsys, role):
    cli=_cli(); store=Mock()
    # judge resolves short ids first (brain_cli.py resolve_fact_id); a bare Mock returned Mock ids
    store.resolve_fact_id.side_effect={'new':'new-full','old':'old-full'}.get
    store.get.side_effect=lambda table, ident: dict(id=ident,version=7 if ident=='new-full' else 4)
    store._rpc.return_value={'verdict':'unrelated','replayed':False}
    assert cli.main(['judge','new','old','unrelated','--role',role],store=store)==0
    store._rpc.assert_called_once_with('brain_judge_pair',dict(p_newer='new-full',p_older='old-full',p_newer_version=7,p_older_version=4,p_verdict='unrelated',p_author=role))
    assert 'unrelated' in capsys.readouterr().out


@pytest.mark.parametrize('role', ['cron', 'import', 'unknown-role', ''])
def test_judge_cli_refuses_automation_and_unknown_roles_before_rpc(role):
    cli=_cli(); store=Mock()
    assert cli.main(['judge','new','old','supersedes','--role',role],store=store)==2
    store.get.assert_not_called()
    store._rpc.assert_not_called()
