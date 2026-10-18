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
