"""Gamma note 2 (2026-10-29): at nominee freeze the evaluated book policy's caps must fit inside the pods totals and
its clip must be the nominee clip; a mismatch refuses without spending alpha."""
import copy
import hashlib
import json

import pytest

from sniper.office import roster as R, store as S
from .test_epoch_freeze import env  # noqa: F401  (fixture)


def _rewrite(args, key, mutate):
    args = dict(args, nominee=copy.deepcopy(args['nominee']), nominee_refs=copy.deepcopy(args['nominee_refs']))
    path = args['config_root']/args['nominee_refs'][key]['path']
    doc = json.loads(path.read_bytes())
    mutate(doc)
    raw = S.canonical_json(doc)
    path.chmod(0o644)
    path.write_bytes(raw)
    path.chmod(0o444)
    digest = hashlib.sha256(raw).hexdigest()
    args['nominee_refs'][key]['sha256'] = digest
    if key == 'pods_rule':
        args['nominee']['pods_rule_sha256'] = digest
    return args


def test_the_fixture_nominee_with_fitting_caps_opens(env):
    conn, args = env
    assert R._open_evaluated(conn, {}, **args)


@pytest.mark.parametrize('key,mutate', [
    ('book_policy', lambda d: d['policy'].update(max_concurrent=7)),          # pods total 6
    ('book_policy', lambda d: d['policy'].update(daily_loss_usd=100.01)),     # pods total 100
    ('book_policy', lambda d: d['policy'].update(clip_usd=30.0)),             # nominee clip 25
    ('book_policy', lambda d: d['policy'].update(max_concurrent=True)),
    ('pods_rule', lambda d: d.update(max_concurrent_total=2)),               # policy 3 > 2
    ('pods_rule', lambda d: d.pop('daily_loss_usd_total')),
])
def test_book_policy_beyond_the_pods_totals_refuses_without_spending(env, key, mutate):
    conn, args = env
    args = _rewrite(args, key, mutate)
    with pytest.raises(R.OrgRefused, match='nominee_book_policy|pods_rule'):
        R._open_evaluated(conn, {}, **args)
    assert conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0] == 0


def test_a_nominee_without_a_book_policy_refuses(env):
    conn, args = env
    args = dict(args, nominee_refs={k: v for k, v in args['nominee_refs'].items() if k != 'book_policy'})
    with pytest.raises(R.OrgRefused):
        R._open_evaluated(conn, {}, **args)
