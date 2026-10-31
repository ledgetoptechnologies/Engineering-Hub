"""Coordinator issuer role, synthetic measured consumer and real peer UDS."""
import os
from types import SimpleNamespace

import pytest
pytest.importorskip('fcntl')

from hub_container_executor import launch_supervisor as launch
from hub_container_executor import runtime_identity as identity
from hub_container_executor.launch_transport import CoordinatorUnixLaunchReceiptTransport, UnixLaunchReceiptTransport
from tests.test_hub_container_executor_launch_supervisor import synthetic_issuer, short_root, root_journal, serve_fixture


def coordinator(authority, role='supervised-local-coordinator-v2'):
    authority.profile = SimpleNamespace(role=role)
    authority.release = identity.CoordinatorReleaseEnrollment(**{
        name: getattr(authority.release, name) for name in identity.CoordinatorReleaseEnrollment.__dataclass_fields__})


@pytest.mark.parametrize('role', ['supervised-local-coordinator-v2', 'supervised-local-coordinator-v3'])
def test_coordinator_issuer_signs_role_specific_receipt_and_never_serves_executor_observations(synthetic_issuer, role, monkeypatch):
    authority, path, checks = synthetic_issuer
    coordinator(authority, role)
    if role=='supervised-local-coordinator-v3':
        # Protocol test only; retained-owner authorization is tested separately.
        monkeypatch.setattr(authority,'_check_retained_pair',lambda _:None)
    thread, result = serve_fixture(authority)
    transport = CoordinatorUnixLaunchReceiptTransport(socket_path=str(path),
        expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    verifier = None
    try:
        verifier = identity.CoordinatorLaunchReceiptIdentityVerifier(enrollment=authority.release,
            issuer_transport=transport, max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
        assert verifier.implementation_commit() == authority.release.implementation_commit
        thread.join(3); assert not thread.is_alive() and len(result) == 1 and isinstance(result[0], str)
        assert authority.wait_for_coordinator_stop(timeout_ms=20, stop_requested=lambda: False) is False
        with pytest.raises(launch.LaunchSupervisorError):
            authority.wait_for_stop_with_observations(timeout_ms=20, stop_requested=lambda: False)
        assert checks
    finally:
        if verifier is not None: verifier.close()
        transport.close()
        thread.join(3); assert not thread.is_alive()


def test_v3_issuer_rejects_missing_retained_owner(synthetic_issuer):
    authority,_,_=synthetic_issuer
    coordinator(authority,'supervised-local-coordinator-v3')
    with pytest.raises(launch.LaunchSupervisorError): authority._check_retained_pair(None)


def test_v3_issuer_accepts_only_exact_installed_retained_owner(synthetic_issuer,monkeypatch):
    from hub_container_executor.retained_oauth_pair import RetainedOAuthPair
    authority,_,_=synthetic_issuer
    coordinator(authority,'supervised-local-coordinator-v3')
    owned=object.__new__(RetainedOAuthPair);owned._coordinator=authority
    calls=[];monkeypatch.setattr(RetainedOAuthPair,'_check_authority',lambda self:calls.append(self))
    authority._retained_pair=owned
    authority._check_retained_pair(owned);assert calls==[owned]
    with pytest.raises(launch.LaunchSupervisorError):authority._check_retained_pair(object())


def test_v3_pair_hook_consuming_receipt_deadline_denies_reservation(synthetic_issuer,monkeypatch):
    import time as real_time
    authority,path,_=synthetic_issuer
    coordinator(authority,'supervised-local-coordinator-v3')
    clock=SimpleNamespace(offset=0)
    monkeypatch.setattr(launch,'time',SimpleNamespace(monotonic=lambda:real_time.monotonic()+clock.offset,
                                                     time=real_time.time,sleep=real_time.sleep))
    monkeypatch.setattr(authority,'_check_retained_pair',lambda _:setattr(clock,'offset',5))
    thread,result=serve_fixture(authority)
    transport=CoordinatorUnixLaunchReceiptTransport(socket_path=str(path),
        expected_issuer_uid=os.geteuid(),expected_issuer_gid=os.getegid())
    try:
        with pytest.raises(identity.RuntimeIdentityError):transport.issue_receipt(nonce='a'*64,timeout_ms=1000)
        thread.join(3);assert not thread.is_alive() and isinstance(result[0],launch.LaunchSupervisorError)
        with authority._journal.transaction() as db:
            assert db.execute('SELECT count(*) FROM issuance').fetchone()==(0,)
    finally:
        transport.close();thread.join(3);assert not thread.is_alive()


def test_coordinator_issuer_rejects_executor_request(synthetic_issuer):
    authority, path, _ = synthetic_issuer
    coordinator(authority)
    thread, result = serve_fixture(authority)
    transport = UnixLaunchReceiptTransport(socket_path=str(path),
        expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    try:
        with pytest.raises(identity.RuntimeIdentityError):
            transport.issue_receipt(nonce='a'*64, timeout_ms=1000)
        thread.join(3); assert not thread.is_alive() and isinstance(result[0], launch.LaunchSupervisorError)
        assert authority._held
    finally:
        transport.close()
        thread.join(3); assert not thread.is_alive()


@pytest.mark.parametrize('role', ['executor', 'coordinator'])
def test_issuer_rejects_release_type_from_other_role(synthetic_issuer, role):
    authority, _, _ = synthetic_issuer
    if role == 'executor':
        authority.release = identity.CoordinatorReleaseEnrollment(**{
            name: getattr(authority.release, name) for name in identity.CoordinatorReleaseEnrollment.__dataclass_fields__})
    else:
        authority.profile = SimpleNamespace(role='supervised-local-coordinator-v2')
    with pytest.raises(launch.LaunchSupervisorError): authority.serve_one_receipt()
    assert authority._held
