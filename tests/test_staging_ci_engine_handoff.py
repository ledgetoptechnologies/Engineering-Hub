"""Engine stop veto under an enrolled durable CI handoff; engine-free.

Every engine effect (inspect/unit_state/run) is faked and counted. The handoff
journal is real and written by the handoff host with test-only authorities.
"""
import json
import os
import shutil
import sqlite3
import sys

import pytest

from engineering_hub.development_ci_handoff import HANDOFF_MARKER, HandoffRefused
from tests.test_development_ci_handoff import (  # noqa: F401  (fixture import)
    host_rows, load_script, make_host, release_of, rig, start_request)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux-only rootless helper and flock')


def engine_rig(rig, monkeypatch):
    engine = load_script('staging_ci_engine')
    uid = os.getuid()
    layout = engine.Layout(uid, rig.tmp, rig.engine, rig.tmp / 'engine-private')
    monkeypatch.setattr(engine, 'directories', lambda *_a, **_k: None)
    monkeypatch.setattr(engine, '_directory', lambda *_a, **_k: None)
    ready = {'schema': 'engineering-hub.development-ci-engine.v1', 'state': 'ready', 'unit': engine.UNIT,
             'engine_id': 'engine-identity-123', 'invocation_id': 'a' * 32}
    unit = dict(LoadState='loaded', ActiveState='active', SubState='running', InvocationID='a' * 32,
        MainPID='2345', ControlGroup=f'/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/' + engine.UNIT,
        CPUQuotaPerSecUSec='1s', MemoryMax=str(engine.MEMORY), MemorySwapMax='0', TasksMax='384',
        Delegate='yes', Restart='no', SendSIGKILL='no', TimeoutStopUSec='20min', Transient='yes', Type='exec')
    effects = []
    def effect(name, value):
        def fake(*args, **kwargs):
            effects.append(name)
            return value
        return fake
    monkeypatch.setattr(engine, 'inspect', effect('inspect', ready))
    monkeypatch.setattr(engine, 'unit_state', effect('unit_state', unit))
    monkeypatch.setattr(engine, '_pid_start', lambda _: '1234')
    monkeypatch.setattr(engine, '_terminal', lambda *_a: True)
    monkeypatch.setattr(engine, 'run', lambda _value, argv, **_k: effects.append(tuple(argv)) or '')
    stop = lambda anchor=rig.external: engine.stop(layout, engine_id='engine-identity-123',
                                                   invocation_id='a' * 32, external_anchor=anchor)
    return engine, layout, stop, effects


def finalized_attempt(rig):
    assert rig.host.start(*start_request(rig, 1))[0]['state'] == 'terminal_held'
    row = host_rows(rig)[0]
    request = {'attempt_id': row['attempt_id'], 'generation': row['generation'], 'fence': 1,
               'receipt_digest': row['receipt_digest'], 'hub_ack': 'b' * 64}
    rig.host.finalize(request, rig.auth.sign('finalize', request))


def test_unenrolled_stop_is_unchanged_and_never_reads_a_journal(rig, monkeypatch):
    (rig.engine / HANDOFF_MARKER).unlink()
    engine, _layout, stop, effects = engine_rig(rig, monkeypatch)
    monkeypatch.setattr(engine.sqlite3, 'connect', lambda *_a, **_k: pytest.fail('journal opened'))
    result = stop()
    assert result['state'] == 'stopped' and ('/usr/bin/systemctl', '--user', 'stop', '--no-block', engine.UNIT) in effects
    (rig.engine / 'HELD.json').write_text('retained')
    effects.clear()
    with pytest.raises(engine.EngineError):
        stop()
    assert effects == []


def test_enrolled_all_finalized_reserved_allows_stop(rig, monkeypatch):
    finalized_attempt(rig)
    engine, _layout, stop, effects = engine_rig(rig, monkeypatch)
    assert stop()['state'] == 'stopped'
    assert ('/usr/bin/systemctl', '--user', 'stop', '--no-block', engine.UNIT) in effects


@pytest.mark.parametrize('stage', ['running', 'terminal_held'])
def test_enrolled_unfinalized_attempt_vetoes_stop_even_after_restart(rig, monkeypatch, stage):
    if stage == 'running':
        rig.runner.fail = True
        with pytest.raises(RuntimeError):
            rig.host.start(*start_request(rig, 1))
    else:
        rig.host.start(*start_request(rig, 1))
    assert [row['state'] for row in host_rows(rig)] == [stage]
    engine, _layout, stop, effects = engine_rig(rig, monkeypatch)
    with pytest.raises(engine.EngineError):
        stop()
    assert effects == [] and not (rig.engine / 'STOPPING.json').exists()
    # Host and engine-helper process restart: reopened from disk, still vetoed.
    make_host(rig)
    engine, _layout, stop, effects = engine_rig(rig, monkeypatch)
    with pytest.raises(engine.EngineError):
        stop()
    assert effects == []


def test_stop_cannot_interleave_with_start_and_start_cannot_begin_during_stop(rig, monkeypatch):
    engine, layout, stop, effects = engine_rig(rig, monkeypatch)
    observed = []
    def try_stop():
        try:
            stop()
        except BaseException as error:  # lock contention, never a stop
            observed.append(type(error).__name__)
    rig.runner.during = try_stop
    rig.host.start(*start_request(rig, 1))
    assert observed == ['BlockingIOError'] and effects == []
    with engine.admission_lock(layout):
        with pytest.raises(HandoffRefused, match='handoff_host_busy'):
            rig.host.start(*start_request(rig, 2, prior=None))
    assert len(rig.runner.calls) == 1 and len(host_rows(rig)) == 1


def _rewrite_marker(rig, **changes):
    marker = rig.engine / HANDOFF_MARKER
    value = json.loads(marker.read_text())
    value.update(changes)
    marker.write_text(json.dumps(value))


@pytest.mark.parametrize('damage', ['missing_journal', 'corrupt_journal', 'other_journal', 'journal_id',
                                    'missing_anchor', 'corrupt_marker', 'restored'])
def test_ambiguous_or_restored_enrollment_refuses_stop_without_effects(rig, monkeypatch, damage):
    finalized_attempt(rig)
    if damage == 'missing_journal':
        rig.journal.unlink()
    elif damage == 'corrupt_journal':
        rig.journal.write_bytes(b'not a sqlite database' * 64)
    elif damage == 'other_journal':
        other = rig.tmp / 'other.sqlite3'
        shutil.copy(rig.tmp / 'journal-backup.sqlite3', other)
        _rewrite_marker(rig, journal=str(other))
    elif damage == 'journal_id':
        _rewrite_marker(rig, journal_id='0' * 32)
    elif damage == 'missing_anchor':
        rig.anchor.unlink()
    elif damage == 'corrupt_marker':
        (rig.engine / HANDOFF_MARKER).write_text('{')
    else:
        shutil.copy(rig.tmp / 'journal-backup.sqlite3', rig.journal)  # anchor still says fence 1
    engine, _layout, stop, effects = engine_rig(rig, monkeypatch)
    with pytest.raises(engine.EngineError):
        stop()
    assert effects == [] and not (rig.engine / 'STOPPING.json').exists()


def test_enrolled_stop_refuses_without_external_anchor_reader(rig, monkeypatch):
    finalized_attempt(rig)
    engine, _layout, stop, effects = engine_rig(rig, monkeypatch)
    with pytest.raises(engine.EngineError):
        stop(None)
    assert effects == []


def test_coordinated_journal_and_anchor_rollback_refuses_stop(rig, monkeypatch):
    finalized_attempt(rig)
    assert rig.external.highest_fence(rig.generation) == 1
    shutil.copy(rig.tmp / 'journal-backup.sqlite3', rig.journal)
    shutil.copy(rig.tmp / 'anchor-backup.json', rig.anchor)
    engine, _layout, stop, effects = engine_rig(rig, monkeypatch)
    for reader in (None, rig.external):  # default refuses; external fence 1 > journal 0
        with pytest.raises(engine.EngineError):
            stop(reader)
    assert effects == []
