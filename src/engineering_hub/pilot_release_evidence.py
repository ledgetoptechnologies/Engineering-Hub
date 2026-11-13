"""Read-only, explicitly qualified container-pilot evidence for human review."""
from .container_pilot import ContainerPilotWorkflow
from .container_pilot_client import ContainerPilotRequest, ContainerPilotResponse
from .dto import PilotReleaseEvidenceDTO
from .repository import HubError


_ACTIONS = (
    'deploy_container', 'smoke_container',
    'rollback_container', 'smoke_container_rollback',
    'cleanup_candidate', 'cleanup_rollback',
)


def release_evidence_owner_allowed(principal, auth_config, owner_actor):
    """Fail closed unless the authenticated human is the configured Hub owner."""
    if not isinstance(principal, str):
        return False
    if auth_config.human_auth_mode == 'github':
        owner_id = auth_config.beau_github_id
        return owner_id is not None and principal == f'human:{owner_id}'
    if auth_config.human_auth_mode in {'cloudflare_access', 'github_cloudflare'}:
        return (isinstance(owner_actor, str) and owner_actor.startswith('human:cf:')
                and principal == owner_actor)
    return False


def _command_evidence(repo, workflow, run, target, action):
    rollback = action in {'rollback_container', 'smoke_container_rollback', 'cleanup_rollback'}
    expected_image = target.rollback_image_config_id if rollback else target.artifact_digest
    expected = workflow._request(run, target, action)
    result = dict(
        action=action,
        command_id=expected.command_id,
        state='not_started',
        expected_image_config_id=expected_image,
        reported_image_config_id=None,
        reported_health=None,
        worker_state=None,
        worker_evidence_verified=None,
        protocol_verified=False,
        journal_verified=False,
        reason=None,
    )
    with repo._connection() as db:
        row = db.execute(
            'SELECT payload,state,response_json FROM hub_container_pilot_commands '
            'WHERE run_id=? AND action=?', (run['id'], action),
        ).fetchone()
    if row is None:
        return result

    allowed_states = {'unknown', 'held', 'succeeded', 'failed', 'ack_pending'}
    if row['state'] not in allowed_states:
        result['state'] = 'inconsistent'
        return result
    result['state'] = row['state']
    try:
        saved_request = ContainerPilotRequest.model_validate_json(row['payload'], strict=True)
    except Exception:
        result['state'] = 'inconsistent'
        return result
    if saved_request != expected:
        result['state'] = 'inconsistent'
        return result

    if row['response_json'] is None:
        # A missing receipt is never reconstructed from the run's phase or status.
        if row['state'] in {'succeeded', 'failed', 'ack_pending'}:
            result['state'] = 'inconsistent'
        return result
    try:
        response = ContainerPilotResponse.model_validate_json(row['response_json'], strict=True)
    except Exception:
        result['state'] = 'inconsistent'
        return result

    matches = all(getattr(response, name) == value
                  for name, value in expected.model_dump().items())
    if not matches:
        result['state'] = 'inconsistent'
        return result
    result['protocol_verified'] = True
    result['worker_state'] = response.state
    result['worker_evidence_verified'] = response.evidence.evidence_verified
    result['reported_image_config_id'] = response.evidence.image_config_id
    result['reported_health'] = response.evidence.health
    result['reason'] = response.reason
    verified_state = workflow._verified_state(response, expected, target)
    # ack_pending means that the response was retained but the ACK outcome is
    # not established. Never treat response persistence as an ACK receipt.
    ack_eligible = (verified_state == 'succeeded' or (verified_state == 'failed'
        and response.reason == 'health_failed' and response.evidence.health == 'unhealthy'
        and response.evidence.evidence_verified is True))
    result['journal_verified'] = (ack_eligible if row['state'] == 'ack_pending'
                                  else verified_state == row['state'])
    if not result['journal_verified']:
        result['state'] = 'inconsistent'
    return result


def pilot_release_evidence(repo, workflow, run_id):
    """Return only the exact task, manifest and worker receipt fields needed for review.

    The worker's ``evidence_verified`` field is an authenticated adapter assertion,
    not an independent Hub verification of Docker state. This DTO says so explicitly.
    """
    if type(workflow) is not ContainerPilotWorkflow or workflow.repo is not repo:
        raise HubError('container_pilot_not_enabled', 423)
    workflow._assert_pilot_owner()
    run = repo.get_run(run_id)
    target = workflow._historical_target(run)
    disposable = target.validated_manifest().disposable_final_cleanup is True
    details = {action: (_command_evidence(repo, workflow, run, target, action)
                        if disposable or not action.startswith('cleanup_') else None)
               for action in _ACTIONS}
    return PilotReleaseEvidenceDTO(
        run_id=run['id'],
        task_id=run['task_id'],
        project=run['project'],
        source_commit=run['commit'],
        review=run['review'],
        run_status=run['status'],
        candidate_image_config_id=target.artifact_digest,
        pilot_manifest_digest=target.manifest_digest,
        rollback_image_config_id=target.rollback_image_config_id,
        candidate_deploy=details['deploy_container'],
        candidate_health_check=details['smoke_container'],
        rollback_deploy=details['rollback_container'],
        rollback_health_check=details['smoke_container_rollback'],
        candidate_cleanup=details['cleanup_candidate'],
        rollback_cleanup=details['cleanup_rollback'],
        evidence_basis='authenticated_worker_report',
        independently_verified=False,
    )
