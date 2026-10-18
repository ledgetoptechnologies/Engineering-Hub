"""Public hold reasons are bounded evidence, never arbitrary adapter text."""
import pytest
from pydantic import ValidationError

from engineering_hub.dto import RunEventDTO


def event(**changes):
    return dict(event='held', actor='executor:hub-pilot-v1', command_id='fixture-phase',
                fencing_generation=1, reason='rollback_outcome_uncertain') | changes


@pytest.mark.parametrize('reason', ['candidate_outcome_uncertain', 'staging_paused',
    'rollback_failed', 'rollback_outcome_uncertain', 'rollback_smoke_failed',
    'rollback_smoke_outcome_uncertain'])
def test_fixed_hold_reasons_round_trip(reason):
    assert RunEventDTO.model_validate(event(reason=reason)).reason == reason


@pytest.mark.parametrize('changes', [{'event': 'succeeded'}, {'event': 'rolled_back'},
    {'reason': 'private adapter exception'}, {'reason': True}])
def test_reasons_cannot_claim_success_or_contain_private_text(changes):
    with pytest.raises(ValidationError):
        RunEventDTO.model_validate(event(**changes))


def test_legacy_event_without_reason_remains_valid_and_unset():
    legacy = event()
    del legacy['reason']
    assert 'reason' not in RunEventDTO.model_validate(legacy).model_dump(exclude_unset=True)


@pytest.mark.parametrize('identity', ['not-a-uuid', 'ABCDEFAB-1234-1234-1234-123456789ABC', 1])
def test_event_occurrence_identity_is_canonical(identity):
    with pytest.raises(ValidationError):
        RunEventDTO.model_validate(event(event_id=identity))


def test_event_occurrence_round_trip_preserves_original_command_identity():
    identity = '00000000-0000-4000-8000-000000000001'
    result = RunEventDTO.model_validate(event(event_id=identity))
    assert result.event_id == identity and result.command_id == 'fixture-phase'
