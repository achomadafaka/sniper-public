"""An explicit deploy manifest must remain bounded and cannot omit money-path regressions."""
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from test_memecoin_runtime_deploy import SCRIPT, _run, _setup


def _script_core_tests():
    """MEMECOIN_CORE_TESTS as the deploy script declares it (comments dropped). Read from the script so
    a widened core list cannot leave the isolated fixture seeding too few files (2abb8357f added the
    tree-walk tests and this file went red: 'mandatory core test missing')."""
    text = SCRIPT.read_text()
    out = set()
    for line in text[text.index('\nMEMECOIN_CORE_TESTS=(') + 1:].splitlines():
        code = line.split('#', 1)[0].replace('MEMECOIN_CORE_TESTS=(', ' ')
        out.update(code.replace(')', ' ').split())
        if ')' in code:                       # the array ends on its first ')' outside a comment
            return out
    raise AssertionError('MEMECOIN_CORE_TESTS has no closing parenthesis')


CORE = _script_core_tests()


def test_the_core_list_is_read_from_the_script():
    assert {'tests/test_memecoin_real_runner.py', 'tests/test_memecoin_real_rails_v2.py',
            'tests/test_memecoin_exits.py', 'tests/test_memecoin_exit_executor.py',
            'tests/test_memecoin_runner.py', 'tests/test_memecoin_runner_health.py',
            'tests/test_memecoin_risk_context.py', 'tests/test_memecoin_arming.py',
            'tests/test_armed_wallet_link_collector.py'} <= CORE
    assert all(path.startswith('tests/') and path.endswith('.py') for path in CORE), CORE


def test_mandatory_core_files_exist_in_the_real_repository():
    missing = sorted(path for path in CORE if not (SCRIPT.parents[1] / path).is_file())
    assert not missing, 'isolated deploy fixtures hid missing mandatory tests: ' + ', '.join(missing)


def _commit(source, message):
    _run(['git', 'add', '.'], source)
    _run(['git', 'commit', '-qm', message], source)
    _run(['git', 'push', '-q', 'origin', 'main'], source)
    return _run(['git', 'rev-parse', 'HEAD'], source).stdout.strip()


@pytest.fixture
def deployment(tmp_path):
    source, state, runtime, env, _sha = _setup(tmp_path)
    tests = source / 'tests'
    tests.mkdir()
    for path in CORE | {
        'tests/test_requested.py', 'tests/test_changed_existing.py',
        'tests/test_old_name.py', 'tests/test_memecoin_unrelated.py',
        'tests/test_memecoin_imports_changed_module.py',
    }:
        (source / path).write_text('def test_ok():\n    assert True\n')
    (tests / 'test_memecoin_imports_changed_module.py').write_text(
        'from sniper.services import changed_module\n')
    module = source / 'sniper/services/changed_module.py'
    module.write_text('VERSION = 1\n')
    baseline = _commit(source, 'live baseline')
    baseline_dir = runtime / 'releases' / baseline
    baseline_dir.mkdir(parents=True)
    (runtime / 'current').symlink_to(baseline_dir)
    (state / 'data/memecoin_deploy.json').write_text(json.dumps({'sha': baseline}))

    (tests / 'test_changed_existing.py').write_text('def test_ok():\n    assert 1 == 1\n')
    (tests / 'test_new_case.py').write_text('def test_new():\n    assert True\n')
    (tests / 'nested').mkdir()
    (tests / 'nested/test_new_nested.py').write_text('def test_new_nested():\n    assert True\n')
    (tests / 'test_old_name.py').rename(tests / 'test_renamed.py')
    module.write_text('VERSION = 2\n')
    target = _commit(source, 'candidate changed tests and module')

    calls = tmp_path / 'pytest-args.txt'
    checks = tmp_path / 'validation-checks.txt'
    wrapper = tmp_path / 'python-wrapper'
    wrapper.write_text(
        '#!/usr/bin/env bash\nset -eu\n'
        'if [ "${1:-}" = -m ] && [ "${2:-}" = pytest ]; then\n'
        '  printf "%s\\n" "$@" > "$PYTEST_CALLS"; exit 0\nfi\n'
        'case "${1:-}" in\n'
        '  *validate_schema_refs.py|*validate_memecoin_funded_contracts.py|*check_positional_block_times_writes.py)\n'
        '    printf "%s\\n" "$1" >> "$VALIDATION_CHECKS"; exit 0 ;;\nesac\n'
        'if [ "${1:-}" = -m ] && [ "${2:-}" = compileall ]; then\n'
        '  printf "compileall\\n" >> "$VALIDATION_CHECKS"; exit 0\nfi\n'
        'exec "$REAL_PYTHON" "$@"\n')
    wrapper.chmod(0o755)
    for key in ('MEMECOIN_DEPLOY_TEST_FILES_FILE', 'MEMECOIN_DEPLOY_FULL_TESTS'):
        env.pop(key, None)
    env.update(SNIPER_DEPLOY_SKIP_VALIDATION='0', SNIPER_PYTHON=str(wrapper),
               REAL_PYTHON=sys.executable, PYTEST_CALLS=str(calls), VALIDATION_CHECKS=str(checks))
    # The outer real deploy already owns this host lock while running pytest. Only this
    # fixture's nested validation uses a local bounded lock; deploy/watchdog FD locks stay real.
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir()
    flock_wrapper = binary_dir / 'flock'
    flock_wrapper.write_text(
        '#!/usr/bin/env bash\nset -eu\nargs=()\n'
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "$HOST_PYTEST_LOCK" ]; then args+=(-w 1); arg="$FIXTURE_PYTEST_LOCK"; fi\n'
        '  args+=("$arg")\ndone\n'
        'exec "$REAL_FLOCK" "${args[@]}"\n')
    flock_wrapper.chmod(0o755)
    env.update(REAL_FLOCK=shutil.which('flock'),
               HOST_PYTEST_LOCK=str(Path.home() / '.cache/codex-pytest-full.lock'),
               FIXTURE_PYTEST_LOCK=str(tmp_path / 'pytest-fixture.lock'),
               PATH=str(binary_dir) + os.pathsep + env['PATH'])
    manifest = tmp_path / 'selected-tests.txt'
    manifest.write_text('tests/test_requested.py\n')
    return source, state, runtime, env, target, calls, checks, manifest


def _deploy(deployment, *, manifest=True, extra_env=None):
    source, _state, _runtime, env, target, _calls, _checks, path = deployment
    configured = env | (extra_env or {})
    if manifest:
        configured['MEMECOIN_DEPLOY_TEST_FILES_FILE'] = str(path)
    args = ['bash', str(SCRIPT), target, '--dry-run']
    process = subprocess.Popen(args, cwd=source, env=configured, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=3)
        pytest.fail('isolated deploy fixture timed out: ' + stdout + stderr)
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _selected(calls):
    args = calls.read_text().splitlines()
    assert args[:4] == ['-m', 'pytest', '-n', '2']
    assert args[-1] == '-q'
    assert len(args[4:-1]) == len(set(args[4:-1])), 'duplicate test files waste the bounded gate'
    return set(args[4:-1])


@pytest.mark.parametrize('full_flag', ['0', '1'])
def test_explicit_manifest_adds_core_and_changed_tests_without_import_expansion(deployment, full_flag):
    _source, _state, _runtime, _env, _target, calls, checks, manifest = deployment
    manifest.write_text('tests/test_requested.py\ntests/test_requested.py\n')
    result = _deploy(deployment, extra_env={'MEMECOIN_DEPLOY_FULL_TESTS': full_flag})
    assert result.returncode == 0, result.stderr
    assert _selected(calls) == CORE | {
        'tests/test_requested.py', 'tests/test_changed_existing.py', 'tests/test_new_case.py',
        'tests/nested/test_new_nested.py', 'tests/test_renamed.py',
    }
    checked = checks.read_text()
    assert 'validate_schema_refs.py' in checked and 'check_positional_block_times_writes.py' in checked
    assert 'validate_memecoin_funded_contracts.py' in checked and 'compileall' in checked
    assert 'validation explicit' in result.stdout


@pytest.mark.parametrize('explicit', [True, False])
def test_only_explicit_manifest_runs_while_the_shared_suite_lock_is_held(deployment, explicit):
    _source, _state, _runtime, env, _target, calls, _checks, _manifest = deployment
    with Path(env['FIXTURE_PYTEST_LOCK']).open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _deploy(deployment, manifest=explicit)

    if explicit:
        assert result.returncode == 0, result.stderr
        assert _selected(calls) >= CORE
    else:
        assert result.returncode != 0 and 'memecoin suite failed' in result.stderr
        assert not calls.exists(), 'default validation ran despite the held suite lock'


@pytest.mark.parametrize('explicit', [True, False])
def test_validation_children_do_not_inherit_parent_test_manifest(deployment, tmp_path, explicit):
    _source, _state, _runtime, env, _target, calls, _checks, _manifest = deployment
    child_env_log = tmp_path / 'validation-child-env.txt'
    wrapper = Path(env['SNIPER_PYTHON'])
    probe = (
        'record_child_env() {\n'
        '  state=absent; [ "${MEMECOIN_DEPLOY_TEST_FILES_FILE+x}" != x ] || state=present\n'
        '  printf "%s %s\\n" "$1" "$state" >> "$CHILD_ENV_LOG"\n}\n'
        'case "${1:-}" in\n'
        '  *validate_schema_refs.py) record_child_env schema ;;\n'
        '  *check_positional_block_times_writes.py) record_child_env structural ;;\n'
        '  -m) case "${2:-}" in pytest|compileall) record_child_env "$2" ;; esac ;;\n'
        'esac\n'
    )
    wrapper.write_text(wrapper.read_text().replace('set -eu\n', 'set -eu\n' + probe, 1))
    result = _deploy(deployment, manifest=explicit, extra_env={'CHILD_ENV_LOG': str(child_env_log)})
    assert result.returncode == 0, result.stderr
    assert child_env_log.read_text().splitlines() == [
        'schema absent', 'structural absent', 'pytest absent', 'compileall absent',
    ]
    expected = CORE | {
        'tests/test_changed_existing.py', 'tests/test_new_case.py',
        'tests/nested/test_new_nested.py', 'tests/test_renamed.py',
    }
    expected.add('tests/test_requested.py' if explicit else 'tests/test_memecoin_imports_changed_module.py')
    assert _selected(calls) == expected
    assert ('validation explicit' if explicit else 'validation scoped') in result.stdout


@pytest.mark.parametrize('entry', [
    '', '\n', '   \n', '--maxfail=1\n', '/tests/test_requested.py\n',
    'tests/../tests/test_requested.py\n', 'tests/nested/../../tests/test_requested.py\n',
    'tests/test_missing.py\n', 'tests/test_requested.py::test_ok\n',
    'sniper/services/changed_module.py\n', 'tests/test_requested.txt\n',
])
def test_invalid_manifest_refuses_before_materializing_or_switching_runtime(deployment, entry):
    _source, state, runtime, _env, target, calls, _checks, manifest = deployment
    manifest.write_text(entry)
    old_current = os.readlink(runtime / 'current')
    old_deploy = (state / 'data/memecoin_deploy.json').read_bytes()
    result = _deploy(deployment)
    assert result.returncode != 0, 'invalid explicit selection silently became a default suite'
    assert 'deploy refused' in result.stderr
    assert not calls.exists() and not (runtime / 'releases' / target).exists()
    assert os.readlink(runtime / 'current') == old_current
    assert (state / 'data/memecoin_deploy.json').read_bytes() == old_deploy
    assert not (state / 'data/.memecoin_deploy_intent').exists()


def test_missing_manifest_file_is_not_an_implicit_default(deployment):
    manifest = deployment[-1]
    manifest.unlink()
    result = _deploy(deployment)
    assert result.returncode != 0 and not deployment[5].exists()


@pytest.mark.parametrize('baseline', ['missing', 'unknown_commit', 'non_sha'])
def test_explicit_mode_refuses_an_unavailable_live_baseline_without_full_fallback(deployment, baseline):
    _source, _state, runtime, _env, target, calls, _checks, _manifest = deployment
    (runtime / 'current').unlink()
    if baseline != 'missing':
        invalid = runtime / 'releases' / ('f' * 40 if baseline == 'unknown_commit' else 'main')
        invalid.mkdir()
        (runtime / 'current').symlink_to(invalid)
    result = _deploy(deployment)
    assert result.returncode != 0 and 'comparable live SHA' in result.stderr
    assert not calls.exists() and not (runtime / 'releases' / target).exists()


def test_git_diff_failure_cannot_silently_drop_changed_tests(deployment, tmp_path):
    _source, _state, runtime, env, target, calls, _checks, _manifest = deployment
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir(exist_ok=True)
    fake_git = binary_dir / 'git'
    fake_git.write_text('#!/usr/bin/env bash\n'
                        'if [ "${3:-}" = diff ]; then exit 73; fi\n'
                        'exec "$REAL_GIT" "$@"\n')
    fake_git.chmod(0o755)
    result = _deploy(deployment, extra_env={
        'PATH': str(binary_dir) + os.pathsep + env['PATH'], 'REAL_GIT': shutil.which('git')})
    assert result.returncode != 0 and 'diff' in result.stderr
    assert not calls.exists() and not (runtime / 'releases' / target).exists()


def test_explicit_mode_cannot_deselect_a_missing_mandatory_core_test(deployment):
    source, _state, runtime, _env, _target, calls, _checks, _manifest = deployment
    (source / 'tests/test_memecoin_risk_context.py').unlink()
    changed = list(deployment)
    changed[4] = _commit(source, 'remove required core')
    result = _deploy(changed)
    assert result.returncode != 0 and 'tests/test_memecoin_risk_context.py' in result.stderr
    assert not calls.exists() and not (runtime / 'releases' / changed[4]).exists()


def test_default_selection_includes_changed_tests_and_imports(deployment):
    result = _deploy(deployment, manifest=False)
    assert result.returncode == 0, result.stderr
    assert _selected(deployment[5]) == CORE | {
        'tests/test_memecoin_imports_changed_module.py', 'tests/test_changed_existing.py',
        'tests/test_new_case.py', 'tests/nested/test_new_nested.py', 'tests/test_renamed.py',
    }
    assert 'validation scoped' in result.stdout


def test_default_selection_includes_non_memecoin_importing_test(deployment):
    source = deployment[0]
    path = 'tests/test_slot_lane_changed_module.py'
    (source / path).write_text('from sniper.services import changed_module\n')
    changed = list(deployment)
    changed[4] = _commit(source, 'add connected slot regression')
    result = _deploy(changed, manifest=False)
    assert result.returncode == 0, result.stderr
    assert path in _selected(changed[5])


def test_default_diff_failure_refuses_before_materialization(deployment, tmp_path):
    _source, _state, runtime, env, target, calls, _checks, _manifest = deployment
    binary_dir = tmp_path / 'failing-git'
    binary_dir.mkdir()
    fake_git = binary_dir / 'git'
    fake_git.write_text('#!/usr/bin/env bash\n'
                        'if [ "${3:-}" = diff ]; then exit 73; fi\n'
                        'exec "$REAL_GIT" "$@"\n')
    fake_git.chmod(0o755)
    result = _deploy(deployment, manifest=False, extra_env={
        'PATH': str(binary_dir) + os.pathsep + env['PATH'], 'REAL_GIT': shutil.which('git')})
    assert result.returncode != 0 and 'diff' in result.stderr
    assert not calls.exists() and not (runtime / 'releases' / target).exists()


def test_default_missing_core_refuses(deployment):
    source, _state, _runtime, _env, _target, calls, _checks, _manifest = deployment
    (source / 'tests/test_memecoin_risk_context.py').unlink()
    changed = list(deployment)
    changed[4] = _commit(source, 'remove mandatory core')
    result = _deploy(changed, manifest=False)
    assert result.returncode != 0 and 'test_memecoin_risk_context.py' in result.stderr
    assert not calls.exists()
