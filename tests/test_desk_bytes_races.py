"""SQLite sidecars may disappear between file classification and size read."""
from pathlib import Path

import pytest

from test_memecoin_desk_pumpswap_worker import W


@pytest.mark.parametrize('direct', [False, True])
def test_disappearing_file_does_not_abort_counting_survivors(tmp_path, monkeypatch, direct):
    gone = tmp_path / 'tape.db-shm'
    gone.write_bytes(b'gone')
    live = tmp_path / 'tape.db'
    live.write_bytes(b'live bytes')
    stat = Path.stat
    calls = []

    def disappearing_stat(path, *args, **kwargs):
        if path == gone:
            calls.append(path)
            if len(calls) == 2:
                path.unlink()
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', disappearing_stat)
    assert W.desk_bytes(gone if direct else tmp_path, extra=(live,) if direct else ()) == len(b'live bytes')
    assert len(calls) == 2
    assert not gone.exists()


@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize('error', [PermissionError, OSError])
def test_metadata_errors_other_than_disappearance_propagate(tmp_path, monkeypatch, direct, error):
    target = tmp_path / 'tape.db-shm'
    target.write_bytes(b'x')
    stat = Path.stat
    calls = []

    def denied_stat(path, *args, **kwargs):
        if path == target:
            calls.append(path)
            if len(calls) == 2:
                raise error('metadata unavailable')
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', denied_stat)
    with pytest.raises(error, match='metadata unavailable'):
        W.desk_bytes(target if direct else tmp_path)


def test_regular_files_exclusions_and_symlinks_keep_existing_counts(tmp_path):
    nested = tmp_path / 'nested'
    nested.mkdir()
    live = nested / 'tape.db'
    live.write_bytes(b'abc')
    link = tmp_path / 'linked.db'
    link.symlink_to(live)
    (tmp_path / 'broken.db').symlink_to(tmp_path / 'absent')
    (tmp_path / 'linked-dir').symlink_to(nested, target_is_directory=True)
    assert W.desk_bytes(tmp_path) == 6
    assert W.desk_bytes(tmp_path, exclude=(nested,)) == 3
    assert W.desk_bytes(link) == 3
