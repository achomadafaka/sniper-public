import subprocess

import pytest

from sniper.office import seat_publish as P


@pytest.mark.parametrize('name',['.env','a/.env.bak.1','data/solana_keypair.json','x/ca.key','credentials.json','config/office/runtime.json','sniper_tracker.db','a.db-wal'])
def test_forbidden_names_are_refused(tmp_path,name):
    path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('x')
    assert P.refused(tmp_path)


def test_private_key_text_refused_under_any_name(tmp_path):
    (tmp_path/'notes.txt').write_bytes(b'-----BEGIN PRIVATE KEY-----\n'+b'A'*64)
    assert P.refused(tmp_path)==['notes.txt:private_key']


def test_source_naming_the_marker_is_not_a_key(tmp_path):
    (tmp_path/'redact.py').write_text("m='-----BEGIN PRIVATE KEY-----'")
    assert P.refused(tmp_path)==[]


def test_symlink_out_refused_and_clean_tree_passes(tmp_path):
    (tmp_path/'ok.py').write_text('x');assert P.refused(tmp_path)==[]
    (tmp_path/'link').symlink_to('/etc/passwd');assert P.refused(tmp_path)==['link:symlink_out']


def test_publish_release_is_tracked_files_only_and_refuses_a_tracked_secret(tmp_path):
    repo=tmp_path/'r';repo.mkdir();bridge=tmp_path/'b';bridge.mkdir()
    def git(*a):return subprocess.run(['git','-C',str(repo),'-c','user.name=t','-c','user.email=t@t',*a],check=True,capture_output=True,text=True).stdout.strip()
    git('init','-q');(repo/'a.py').write_text('x');(repo/'.env.example').write_text('x');(repo/'untracked.db').write_text('x')
    git('add','a.py','.env.example');git('commit','-qm','1')
    P.publish_release(repo,git('rev-parse','HEAD'),bridge,running=lambda:False)
    assert sorted(p.name for p in (bridge/'release').iterdir())==['PUBLISHED_SHA','a.py']
    (repo/'.env').write_text('x');git('add','-f','.env');git('commit','-qm','2')
    with pytest.raises(ValueError,match='publish_refused'):P.publish_release(repo,git('rev-parse','HEAD'),bridge,running=lambda:False)
    assert (bridge/'release/a.py').exists() and not (bridge/'release.new').exists()


@pytest.mark.parametrize('offset',[2*1024**2,1024**2-20])
def test_key_past_one_mib_or_across_a_chunk_is_found(tmp_path,offset):
    (tmp_path/'big.txt').write_bytes(b'x'*offset+b'-----BEGIN PRIVATE KEY-----\n'+b'A'*64)
    assert P.refused(tmp_path)==['big.txt:private_key']


def test_publish_refused_while_a_seat_runs_and_tree_untouched(tmp_path):
    (tmp_path/'release').mkdir();(tmp_path/'release/a.py').write_text('x')
    with pytest.raises(ValueError,match='publish_seats_running'):P.publish_release(tmp_path,'a'*40,tmp_path,running=lambda:True)
    with pytest.raises(ValueError,match='publish_seats_running'):P.publish_runtime(tmp_path,running=lambda:True)
    assert (tmp_path/'release/a.py').exists()


def test_unreadable_process_table_counts_as_running(monkeypatch):
    def boom(*a,**k):raise OSError()
    monkeypatch.setattr(P.subprocess,'run',boom)
    assert P.seats_running() is True


def test_key_header_survives_whitespace_spanning_multiple_chunks(tmp_path):
    path=tmp_path/'padded.txt'
    path.write_bytes(b'-----BEGIN PRIVATE KEY-----\n'+b' '*(3*1024**2)+b'A'*64)
    assert P.refused(tmp_path)==['padded.txt:private_key']


def _tables(monkeypatch,ps,units):
    def run(argv,**k):
        if argv[0]=='systemctl' and units is None:raise P.subprocess.CalledProcessError(1,argv)
        return type('R',(),{'stdout':ps if argv[0]=='ps' else units})()
    monkeypatch.setattr(P.subprocess,'run',run)


@pytest.mark.parametrize('listing,running',[('systemd\n(sd-pam)\npipewire\ndbus-daemon\n',False),('systemd\nbwrap\n',True)])
def test_only_a_sandbox_counts_as_a_running_seat(monkeypatch,listing,running):
    _tables(monkeypatch,listing,'')
    assert P.seats_running() is running


@pytest.mark.parametrize('units',['office-seat@head.service loaded active running Office v2 head seat host\n','office-paper-watcher-v2.service loaded active running watcher\n',None])
def test_an_idle_seat_host_or_the_watcher_refuses_the_publish(monkeypatch,tmp_path,units):
    _tables(monkeypatch,'systemd\npipewire\n',units)
    (tmp_path/'release').mkdir();(tmp_path/'release/a.py').write_text('x')
    with pytest.raises(ValueError,match='publish_seats_running'):P.publish_release(tmp_path,'a'*40,tmp_path)
    assert (tmp_path/'release/a.py').exists()


def test_stale_pins_reports_behind_and_unknown(monkeypatch):
    def run(argv,**k):return type('R',(),{'stdout':'claude-agent-sdk (9.9.9)\n' if 'claude-agent-sdk' in argv else ''})()
    monkeypatch.setattr(P.subprocess,'run',run)
    behind=P.stale_pins()
    assert behind['claude-agent-sdk']['latest']=='9.9.9' and behind['playwright']['latest']=='unknown'
