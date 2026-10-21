"""Task age comes from audited status changes, never from unrelated saves."""
from tests.test_hub_api import BASE, COMMIT, agent_headers, claim_candidate, hub, human_headers, new_task


def overview_task(client, task_id):
    response = client.get(BASE + "/overview", headers=agent_headers("hermes"))
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["tasks"] if item["id"] == task_id)


def test_state_since_tracks_status_changes_only(hub):
    client, app, settings = hub
    created = new_task(client, human_headers(client, app, settings))
    assert overview_task(client, created["id"])["state_since"] == created["updated_at"]

    in_review = claim_candidate(client, created)
    view = overview_task(client, created["id"])
    assert view["status"] == "in_review"
    assert view["state_since"] == in_review["updated_at"]

    # Run bookkeeping saves the task without changing its status.
    repo = app.state.repository
    with repo._transaction() as db:
        task = repo._get(db, created["id"], "task")
        bumped = repo._save(db, "codex", "task.run_failed", "task", task)
    view = overview_task(client, created["id"])
    assert view["updated_at"] == bumped["updated_at"] != in_review["updated_at"]
    assert view["state_since"] == in_review["updated_at"]

    response = client.post(f"{BASE}/tasks/{created['id']}/review", headers=agent_headers("hermes"),
                           json={"expected_revision": view["revision"], "commit": COMMIT, "verdict": "approve"})
    assert response.status_code == 200, response.text
    verified = overview_task(client, created["id"])
    assert verified["status"] == "verified"
    assert verified["state_since"] == response.json()["updated_at"]

    listed = client.get(BASE + "/tasks", headers=agent_headers()).json()["items"]
    assert [item["state_since"] for item in listed] == [verified["state_since"]]


def test_state_since_is_derived_not_stored(hub):
    client, app, settings = hub
    created = new_task(client, human_headers(client, app, settings))
    assert overview_task(client, created["id"])["state_since"] is not None
    stored = app.state.repository.list_tasks()
    assert stored and all("state_since" not in task for task in stored)
    # A task with no recorded status action reports unknown age instead of a guess.
    with app.state.repository._transaction() as db:
        db.execute("DELETE FROM hub_audit WHERE entity_id=?", (created["id"],))
    assert overview_task(client, created["id"])["state_since"] is None
