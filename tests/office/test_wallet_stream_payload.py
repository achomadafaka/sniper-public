"""Lossless provider bytes; legacy compatibility and bounded decompression."""
import base64
import json
import zlib

import pytest


def test_compressed_payload_roundtrips_exact_provider_bytes_and_legacy_remains_readable():
    from sniper.office.wallet_stream_proof import encode_payload,decode_payload
    raw=bytes(range(256))*200
    packed=encode_payload(raw)
    assert len(packed)<len(raw)
    assert decode_payload(packed)==raw
    assert decode_payload(json.dumps(dict(raw_update_b64=base64.b64encode(raw).decode())))==raw


@pytest.mark.parametrize('bad',['bomb','truncated','trailing','wrong_size','bool_size','wrong_codec','bad_base64','ambiguous'])
def test_bad_compressed_payload_never_escapes_raw_frame_bound(bad):
    from sniper.office.wallet_stream_proof import encode_payload,decode_payload
    payload=json.loads(encode_payload(b'provider-bytes'*20))
    if bad=='bomb':payload.update(data=base64.b64encode(zlib.compress(b'x'*(128*1024+1))).decode(),raw_size=128*1024)
    elif bad=='truncated':payload['data']=base64.b64encode(base64.b64decode(payload['data'])[:-2]).decode()
    elif bad=='trailing':payload['data']=base64.b64encode(base64.b64decode(payload['data'])+b'tail').decode()
    elif bad=='wrong_size':payload['raw_size']+=1
    elif bad=='bool_size':payload['raw_size']=True
    elif bad=='wrong_codec':payload['encoding']='guess'
    elif bad=='bad_base64':payload['data']='!'
    else:payload['raw_update_b64']='AA=='
    with pytest.raises(ValueError):decode_payload(json.dumps(payload))


def test_frame_bounds_are_not_relaxed_by_payload_compression():
    from sniper.office.wallet_stream_proof import encode_payload,decode_payload
    with pytest.raises(ValueError):encode_payload(b'x'*(128*1024+1))
    with pytest.raises(ValueError):decode_payload(json.dumps(dict(raw_update_b64=base64.b64encode(b'x'*(128*1024+1)).decode())))


def test_archive_reference_uses_shared_reader_and_rejects_nested_archive(tmp_path,monkeypatch):
    import sys
    from types import SimpleNamespace
    from sniper.office.wallet_stream_proof import encode_payload,decode_payload
    ref=dict(file='packed',flow_id=7);calls=[]
    def read_payload(value,directory):
        calls.append((value,directory));return encode_payload(b'exact-provider-frame')
    monkeypatch.setitem(sys.modules,'sniper.office.wallet_flow_archive',SimpleNamespace(read_payload=read_payload))
    marker=json.dumps(dict(archive_payload_v1=ref))
    assert decode_payload(marker,archive_dir=tmp_path)==b'exact-provider-frame'
    assert calls==[(ref,tmp_path)]
    monkeypatch.setitem(sys.modules,'sniper.office.wallet_flow_archive',SimpleNamespace(read_payload=lambda *a:marker))
    with pytest.raises(ValueError):decode_payload(marker,archive_dir=tmp_path)
    with pytest.raises(ValueError):decode_payload(marker)
