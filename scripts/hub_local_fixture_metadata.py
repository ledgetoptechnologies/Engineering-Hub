"""Portable, bounded decoding of synthetic image fixture metadata."""
import base64
import re

from scripts.hub_local_test_oci import SyntheticImageFixture


def decode_fixture(raw):
    if (type(raw) is not dict or set(raw)!={'record','inputs','manifest_digest','config_digest','blobs'}
            or any(type(raw[k]) is not str for k in ('record','inputs','manifest_digest','config_digest'))
            or type(raw['blobs']) is not dict or len(raw['blobs']) != 2
            or any(type(k) is not str or not re.fullmatch('sha256:[a-f0-9]{64}',k) or type(v) is not str
                   for k,v in raw['blobs'].items())):
        raise ValueError('invalid fixture metadata')
    record=base64.b64decode(raw['record'],validate=True); inputs=base64.b64decode(raw['inputs'],validate=True)
    blobs={k:base64.b64decode(v,validate=True) for k,v in raw['blobs'].items()}
    if (not 0<len(record)<=65536 or not 0<len(inputs)<=65536 or
            any(not 0<len(v)<=1024*1024 for v in blobs.values())):
        raise ValueError('fixture metadata exceeds bound')
    return SyntheticImageFixture(record,inputs,raw['manifest_digest'],raw['config_digest'],blobs)
