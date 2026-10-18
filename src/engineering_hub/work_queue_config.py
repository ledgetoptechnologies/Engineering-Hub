"""Explicit V1 coordination registry; never an execution or approval provider."""
from .repository import AGENTS, PROJECTS
from .work_queue import WorkAction, WorkQueue


def configured_work_queue(repo, *, machine_id="staging-pc"):
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

    return WorkQueue(repo, actions, target_verifier=verified_target)
