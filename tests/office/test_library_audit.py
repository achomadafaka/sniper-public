from pathlib import Path
import pytest
from sniper.office.library_audit import audit


def test_regular_libraries_and_non_executable_pth_pass(tmp_path):
    (tmp_path/'module.py').write_text('pass')
    (tmp_path/'data.pth').write_text('# harmless\nrelative-library\n')
    assert audit(tmp_path)['state']=='passed'


@pytest.mark.parametrize('line',['import os','import\tos',' import os','\ufeffimport os','\ufeffimport\tos'])
def test_executable_pth_refuses_without_executing_it(tmp_path,line):
    (tmp_path/'bad.pth').write_text(line)
    with pytest.raises(ValueError,match='executable_pth'):audit(tmp_path)


def test_external_symlink_refuses(tmp_path):
    (tmp_path/'link').symlink_to('/etc/passwd')
    with pytest.raises(ValueError,match='symlink'):audit(tmp_path)


def test_fifo_pth_refuses_without_blocking(tmp_path):
    import os
    os.mkfifo(tmp_path/'bad.pth')
    with pytest.raises(ValueError,match='special_file'):audit(tmp_path)
