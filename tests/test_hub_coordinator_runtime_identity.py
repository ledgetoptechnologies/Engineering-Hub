"""Distinct coordinator receipt over synthetic direct UDS; no host custody claim."""
import base64
import json
import os
import struct
import uuid

import pytest
pytest.importorskip('fcntl')
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from engineering_hub.container_pilot_manifest import Platform
from hub_container_executor import runtime_identity as identity
from hub_container_executor.launch_transport import CoordinatorUnixLaunchReceiptTransport, COORDINATOR_REQUEST_PROTOCOL
from tests.test_hub_container_executor_launch_transport import issuer, read_exact, frame, canonical
from tests.test_hub_container_executor_runtime_identity import enrolled, clock


def test_coordinator_direct_signed_receipt_and_single_use():
    key=Ed25519PrivateKey.generate()
    enrollment=identity.CoordinatorReleaseEnrollment(issuer_id='coordinator-issuer',
        public_key=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),implementation_commit='a'*40,
        runnable_digest='sha256:'+'1'*64,platform_manifest_digest='sha256:'+'2'*64,image_config_id='sha256:'+'3'*64,
        platform=Platform(os='linux',architecture='amd64',variant=None),launch_profile_digest='sha256:'+'4'*64)
    def handle(peer,state):
        size=struct.unpack('!I',read_exact(peer,4))[0]; raw=read_exact(peer,size);value=json.loads(raw)
        assert raw==canonical(value) and set(value)=={'protocol','nonce'} and value['protocol']==COORDINATOR_REQUEST_PROTOCOL
        assert peer.recv(1)==b''
        import time
        now=int(time.time()*1000)
        payload={'protocol':identity.COORDINATOR_PROTOCOL,'audience':identity.COORDINATOR_AUDIENCE,'nonce':value['nonce'],
            'receipt_id':str(uuid.uuid4()),'launch_id':str(uuid.uuid4()),'issuer_id':enrollment.issuer_id,'boot_id':str(uuid.uuid4()),
            'authenticated_host_pid':123,'process_start_ticks':456,'container_id':'b'*64,'runnable_digest':enrollment.runnable_digest,
            'platform_manifest_digest':enrollment.platform_manifest_digest,'image_config_id':enrollment.image_config_id,
            'platform':enrollment.platform.model_dump(mode='json'),'implementation_commit':enrollment.implementation_commit,
            'launch_profile_digest':enrollment.launch_profile_digest,'issued_at_ms':now,'expires_at_ms':now+30000}
        signature=base64.urlsafe_b64encode(key.sign(identity.COORDINATOR_SIGNATURE_DOMAIN+canonical(payload))).decode().rstrip('=')
        peer.sendall(frame(canonical({'payload':payload,'signature':signature})));state['payload']=payload
    with issuer(handle) as (path,state):
        transport=CoordinatorUnixLaunchReceiptTransport(socket_path=path,expected_issuer_uid=os.geteuid(),expected_issuer_gid=os.getegid())
        try:
            verifier=identity.CoordinatorLaunchReceiptIdentityVerifier(enrollment=enrollment,issuer_transport=transport,
                max_receipt_lifetime_seconds=60,exchange_timeout_ms=1000)
            assert verifier.implementation_commit()==enrollment.implementation_commit
            assert verifier.launch_binding()[4]=='b'*64
            with pytest.raises(identity.RuntimeIdentityError):transport.issue_receipt(nonce='a'*64,timeout_ms=1000)
            verifier.close()
        finally: transport.close()
    assert state['payload']['nonce']


def test_executor_enrollment_cannot_be_reused_for_coordinator(enrolled,clock):
    # Reuse existing real-key synthetic fixture: the denial occurs before I/O.
    from tests.test_hub_container_executor_runtime_identity import Issuer
    executor,key=enrolled; transport=Issuer(executor,key,clock)
    with pytest.raises(identity.RuntimeIdentityError):
        identity.CoordinatorLaunchReceiptIdentityVerifier(enrollment=executor,issuer_transport=transport,
            max_receipt_lifetime_seconds=60,exchange_timeout_ms=1000)
    assert not transport.calls


@pytest.mark.parametrize('change',['executor-protocol','executor-audience','executor-domain','wrong-pin'])
def test_correct_signature_cannot_cross_roles_or_pins(enrolled,clock,change):
    from tests.test_hub_container_executor_runtime_identity import Issuer
    executor,key=enrolled
    coordinator=identity.CoordinatorReleaseEnrollment(**{name:getattr(executor,name) for name in executor.__dataclass_fields__})
    class Signer(Issuer):
        def issue_receipt(self,*,nonce,timeout_ms):
            original=json.loads(super().issue_receipt(nonce=nonce,timeout_ms=timeout_ms)); p=original['payload']
            p['protocol']=identity.COORDINATOR_PROTOCOL; p['audience']=identity.COORDINATOR_AUDIENCE
            if change=='executor-protocol':p['protocol']=identity.PROTOCOL
            if change=='executor-audience':p['audience']=identity.AUDIENCE
            if change=='wrong-pin':p['launch_profile_digest']='sha256:'+'f'*64
            domain=identity.SIGNATURE_DOMAIN if change=='executor-domain' else identity.COORDINATOR_SIGNATURE_DOMAIN
            original['signature']=base64.urlsafe_b64encode(key.sign(domain+canonical(p))).decode().rstrip('=')
            return canonical(original)
    with pytest.raises(identity.RuntimeIdentityError):
        identity.CoordinatorLaunchReceiptIdentityVerifier(enrollment=coordinator,issuer_transport=Signer(executor,key,clock),
            max_receipt_lifetime_seconds=60,exchange_timeout_ms=1000)
