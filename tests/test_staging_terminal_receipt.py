"""Real temporary-file journal primitives; not actual ENOSPC/CoW acceptance."""
import errno
import hashlib
import os
from pathlib import Path
from unittest.mock import Mock
import uuid

import pytest

from hub_staging_service.core import Rejected
from hub_staging_service import terminal_receipt as tr


def identity():
    return dict(command_id=str(uuid.uuid4()), job_id=str(uuid.uuid4()),
                operation_identity='a'*64, worker_id=str(uuid.uuid4()),
                journal_identity=[4, 18], lease_identity='b'*64)


@pytest.fixture
def receipt(tmp_path):
    if os.name != 'posix' or not hasattr(os, 'pwrite'):
        pytest.skip('POSIX private-file/fsync primitives')
    root = tmp_path/'private'; root.mkdir(mode=0o700); root.chmod(0o700)
    return tr.TerminalReceipt.prepare(root, str(uuid.uuid4()))


def test_frame_codec_is_fixed_bounded_and_detects_torn_payload():
    raw = tr._frame({'sequence': 1, 'value': 'notes'})
    assert len(raw) == tr.FRAME_BYTES and tr._decode(raw) == {'sequence': 1, 'value': 'notes'}
    for offset in (0, 100, tr.FRAME_BYTES-1):
        torn=bytearray(raw); torn[offset] ^= 1
        with pytest.raises(Rejected):tr._decode(bytes(torn))
    assert tr._decode(tr._BLANK*tr.FRAME_BYTES) is None


@pytest.mark.parametrize('value', [{'s':'x'*tr.MAX_JSON_BYTES}, {'s':'\u2603'*(tr.MAX_JSON_BYTES//2)},
                                  {'invalid':float('nan')}, {'too_many':list(range(257))}])
def test_frame_rejects_unbounded_or_noncanonical_metadata(value):
    with pytest.raises(Rejected):tr._frame(value)


def test_repeated_unicode_references_are_rejected_before_serialization(monkeypatch):
    repeated='\U0001f333'*tr.MAX_JSON_BYTES
    serializer=Mock(side_effect=AssertionError('Giant JSON allocation must not occur'))
    monkeypatch.setattr(tr.json,'dumps',serializer)
    with pytest.raises(Rejected,match='receipt_metadata_too_large'):
        tr._json({'values':[repeated]*256})
    serializer.assert_not_called()


def test_exact_escaped_json_budget_and_one_over_are_checked_before_dumps(monkeypatch):
    assert len(tr._json({'s':'x'*(tr.MAX_JSON_BYTES-8)}))==tr.MAX_JSON_BYTES
    serializer=Mock(side_effect=AssertionError('Over-budget serialization'))
    monkeypatch.setattr(tr.json,'dumps',serializer)
    with pytest.raises(Rejected):tr._json({'s':'x'*(tr.MAX_JSON_BYTES-7)})
    serializer.assert_not_called()


def test_json_budget_matches_standard_escaping_for_all_metadata_scalars():
    value={'s':'"\\\b\f\n\r\t\x00\x1f\x7f\u0080\u2603\U0001f333',
           'n':[-2**63,2**63-1,True,False,None], 'empty':{}, 'tuple':()}
    assert tr._json(value)==tr.json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')


def test_preparation_allocates_full_slot_and_bind_terminal_reopen_are_durable(receipt,monkeypatch):
    path=receipt.root/receipt.name
    assert path.stat().st_size == tr.SLOT_BYTES and path.stat().st_blocks*512 >= tr.SLOT_BYTES
    assert path.read_bytes()==tr._BLANK*tr.SLOT_BYTES
    original=identity(); original['lease_identity']=None
    original_pwrite=os.pwrite; offsets=[]
    def write(fd,raw,offset):offsets.append(offset);return original_pwrite(fd,raw,offset)
    monkeypatch.setattr(os,'pwrite',write)
    receipt.bind(original)
    assert receipt.evidence(original)=={'state':'uncertain','reason':'terminal_observation_missing'}
    receipt.record_terminal(original,outcome='failed',result={'error':'stage_test_failed'})
    reopened=tr.TerminalReceipt(receipt.root,receipt.slot_id,expected_capture=receipt.capture())
    result=reopened.evidence(original)
    assert result['authority']=='evidence_only' and result['terminal']['runtime'] is None
    assert result['terminal']['outcome']=='failed' and path.stat().st_size==tr.SLOT_BYTES
    assert offsets==[0,tr.FRAME_BYTES-tr._FOOTER_BYTES,tr.FRAME_BYTES,2*tr.FRAME_BYTES-tr._FOOTER_BYTES]
    # No lifecycle methods can authorize cleanup or replay.
    assert not hasattr(receipt,'release') and not hasattr(receipt,'replay')


def test_identical_calls_idempotent_conflicts_deny_and_no_runtime_is_invented(receipt):
    original=identity();receipt.bind(original);receipt.bind(original)
    runtime={'engine_id':'engine-A','boot_id':str(uuid.uuid4()),'invocation_id':str(uuid.uuid4())}
    receipt.record_terminal(original,outcome='succeeded',result={'stages':{}},runtime=runtime)
    snapshot=(receipt.root/receipt.name).read_bytes()
    receipt.record_terminal(original,outcome='succeeded',result={'stages':{}},runtime=runtime)
    with pytest.raises(Rejected):receipt.record_terminal(original,outcome='failed',result={})
    for key in original:
        changed=dict(original)
        changed[key] = [9,19] if key=='journal_identity' else ('c'*64 if key.endswith('identity') else str(uuid.uuid4()))
        with pytest.raises(Rejected):receipt.evidence(changed)
    assert (receipt.root/receipt.name).read_bytes()==snapshot


def test_terminal_requires_durable_original_binding(receipt):
    with pytest.raises(Rejected):receipt.record_terminal(identity(),outcome='succeeded',result={})
    original=identity();receipt.bind(original)
    with pytest.raises(Rejected):receipt.record_terminal(original,outcome='succeeded',result={},runtime={})
    assert receipt.evidence(original)['state']=='uncertain'


def test_short_writes_are_completed_and_zero_progress_is_uncertain(receipt,monkeypatch):
    original=identity();write=os.pwrite
    monkeypatch.setattr(os,'pwrite',lambda fd,raw,offset:write(fd,raw[:97],offset))
    receipt.bind(original)
    monkeypatch.setattr(os,'pwrite',lambda *args:0)
    with pytest.raises(Rejected,match='receipt_short_write'):
        receipt.record_terminal(original,outcome='held',result={})
    assert receipt.evidence(original)['state']=='uncertain'


@pytest.mark.parametrize('failure_call',[1,2])
def test_fsync_failures_never_return_success_even_if_bytes_look_complete(receipt,monkeypatch,failure_call):
    original=identity();receipt.bind(original);actual=os.fsync;calls=[0]
    def sync(fd):
        calls[0]+=1
        if calls[0]==failure_call:raise OSError(errno.ENOSPC,'injected metadata exhaustion')
        return actual(fd)
    monkeypatch.setattr(os,'fsync',sync)
    with pytest.raises(OSError):receipt.record_terminal(original,outcome='succeeded',result={})
    # Valid bytes alone cannot establish successful fsync after an I/O error.
    # The integration must latch the persistence failure and keep holds.
    assert calls[0]==failure_call
    assert receipt.evidence(original)=={'state':'uncertain','reason':'receipt_persistence_failed'}
    with pytest.raises(Rejected,match='receipt_persistence_uncertain'):
        receipt.record_terminal(original,outcome='succeeded',result={})


def test_partial_terminal_write_is_not_repaired_or_reused(receipt,monkeypatch):
    original=identity();receipt.bind(original);write=os.pwrite;calls=[0]
    def fail(fd,raw,offset):
        calls[0]+=1
        if calls[0]==1:return write(fd,raw[:37],offset)
        raise OSError(errno.ENOSPC,'injected full filesystem')
    monkeypatch.setattr(os,'pwrite',fail)
    with pytest.raises(OSError):receipt.record_terminal(original,outcome='succeeded',result={})
    monkeypatch.setattr(os,'pwrite',write)
    snapshot=(receipt.root/receipt.name).read_bytes()
    assert receipt.evidence(original)=={'state':'uncertain','reason':'receipt_persistence_failed'}
    reopened=tr.TerminalReceipt(receipt.root,receipt.slot_id,expected_capture=receipt.capture())
    with pytest.raises(Rejected):reopened.evidence(original)
    with pytest.raises(Rejected):receipt.record_terminal(original,outcome='succeeded',result={})
    assert (receipt.root/receipt.name).read_bytes()==snapshot


@pytest.mark.parametrize('replacement',['symlink','hardlink','new_inode','mode','root_replaced'])
def test_replacement_link_or_private_mode_drift_denies_without_writes(receipt,replacement):
    original=identity();receipt.bind(original);path=receipt.root/receipt.name
    if replacement=='symlink':
        moved=receipt.root/'old';path.rename(moved);path.symlink_to(moved)
    elif replacement=='hardlink':os.link(path,receipt.root/'second')
    elif replacement=='new_inode':
        raw=path.read_bytes();path.rename(receipt.root/'old');path.write_bytes(raw);path.chmod(0o600)
    elif replacement=='mode':path.chmod(0o644)
    else:
        root=receipt.root;root.rename(root.with_name('old-private'));root.mkdir(mode=0o700)
        path.write_bytes(tr._BLANK*tr.SLOT_BYTES);path.chmod(0o600)
    with pytest.raises((Rejected,OSError)):
        receipt.record_terminal(original,outcome='succeeded',result={})


def test_reopening_capture_detects_copied_receipt_inode(receipt):
    original=identity();receipt.bind(original);capture=receipt.capture();path=receipt.root/receipt.name
    raw=path.read_bytes();path.rename(receipt.root/'old');path.write_bytes(raw);path.chmod(0o600)
    with pytest.raises(Rejected,match='receipt_replaced'):
        tr.TerminalReceipt(receipt.root,receipt.slot_id,expected_capture=capture)


def test_preparation_failure_leaves_unusable_owned_slot_and_never_overwrites(receipt,monkeypatch):
    new=str(uuid.uuid4());actual=os.pwrite;calls=[0]
    def fail(fd,raw,offset):
        calls[0]+=1
        if calls[0]==1:return actual(fd,raw[:41],offset)
        raise OSError(errno.ENOSPC,'injected')
    monkeypatch.setattr(os,'pwrite',fail)
    with pytest.raises(OSError):tr.TerminalReceipt.prepare(receipt.root,new)
    with pytest.raises(Rejected):tr.TerminalReceipt(receipt.root,new)
    with pytest.raises(FileExistsError):tr.TerminalReceipt.prepare(receipt.root,new)


def test_terminal_writes_need_no_new_file_and_use_body_then_commit_fsync(receipt,monkeypatch):
    original=identity();receipt.bind(original)
    entries=sorted(p.name for p in receipt.root.iterdir());events=[];write=os.pwrite;sync=os.fsync
    def written(fd,raw,offset):events.append(('write',offset,len(raw)));return write(fd,raw,offset)
    def synced(fd):events.append(('fsync',));return sync(fd)
    monkeypatch.setattr(os,'pwrite',written);monkeypatch.setattr(os,'fsync',synced)
    receipt.record_terminal(original,outcome='held',result={'error':'execution_uncertain'})
    assert events==[('write',tr.FRAME_BYTES,tr.FRAME_BYTES-tr._FOOTER_BYTES),('fsync',),
                   ('write',2*tr.FRAME_BYTES-tr._FOOTER_BYTES,tr._FOOTER_BYTES),('fsync',)]
    assert sorted(p.name for p in receipt.root.iterdir())==entries


def test_valid_checksum_cannot_cross_bind_or_change_frame_sequence(receipt):
    original=identity();receipt.bind(original)
    receipt.record_terminal(original,outcome='held',result={})
    path=receipt.root/receipt.name;raw=path.read_bytes()
    second=tr._decode(raw[tr.FRAME_BYTES:]);second['sequence']=True
    with path.open('r+b') as stream:stream.seek(tr.FRAME_BYTES);stream.write(tr._frame(second));stream.flush();os.fsync(stream.fileno())
    with pytest.raises(Rejected):receipt.evidence(original)
