import os

from sniper.office import runtime as R


def _reader(tmp_path,monkeypatch,body,mode=0o600):
    monkeypatch.delenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL',raising=False)
    key=tmp_path/'office-research.env'
    if body is not None:key.write_text(body);os.chmod(key,mode)
    return R.pool_reader(tmp_path,key_file=key)


def test_owner_only_key_file_turns_the_exact_read_on(tmp_path,monkeypatch):
    reader=_reader(tmp_path,monkeypatch,'MEMECOIN_RESEARCH_HELIUS_RPC_URL=https://rpc.example/?api-key=k\n')
    assert reader is not None and reader.url=='https://rpc.example/?api-key=k'


def test_absent_loose_empty_or_foreign_file_keeps_the_read_off(tmp_path,monkeypatch):
    assert _reader(tmp_path,monkeypatch,None) is None
    assert _reader(tmp_path,monkeypatch,'MEMECOIN_RESEARCH_HELIUS_RPC_URL=https://x\n',mode=0o644) is None
    assert _reader(tmp_path,monkeypatch,'MEMECOIN_RESEARCH_HELIUS_RPC_URL=\n') is None
    assert _reader(tmp_path,monkeypatch,'OTHER=x\n') is None


def test_default_path_is_the_operator_key_file():
    assert str(R.HELIUS_KEY_FILE).endswith('/.config/sniper/office-research.env')
