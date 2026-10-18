"""Explicit V1 coordination registry; never an execution or approval provider."""
from .repository import AGENTS, PROJECTS
from .work_queue import ClosedClaimPolicy, WorkAction, WorkQueue


def configured_work_queue(repo, *, machine_id="staging-pc", claim_verifier=None):
    """Expose coordination; reserve only through trusted fixed-worker admission.

    The optional callback receives (WorkWorker, action_key, environment,
    machine_id, WorkTarget). It must bind the enrolled source/artifact/task and
    actual receipt provider, not merely recognize an agent bearer identity.
    No callback or enrollment can be submitted through a public endpoint.
    """
    # Production here denotes work priority, not access to production hosts.
    # No production approval verifier, command, credential or executor is enabled.
    actions = {project + ".validate": WorkAction(
        machine_id, frozenset({"production", "staging"}), project=project)
        for project in sorted(PROJECTS)}

    def verified_target(actor, action_key, environment, target):
        try:
            task = repo.get_task(target.task_id)
            review = task.get("review")
            return (task["project"] == actions[action_key].project
                    and task["owner"] == actor and task["status"] == "verified"
                    and task["candidate_commit"] == target.source_commit
                    and type(review) is dict and review.get("actor") != actor
                    and review.get("actor") in AGENTS
                    and review.get("verdict") == "approve"
                    and review.get("commit") == target.source_commit)
        except Exception:
            return False

    # Merely enabling endpoints must not let an un-enrolled client reserve a
    # physical machine it cannot execute or reconcile. Only trusted local
    # composition can install an actual worker admission policy.
    return WorkQueue(repo, actions, target_verifier=verified_target,
                     claim_verifier=ClosedClaimPolicy() if claim_verifier is None else claim_verifier)
