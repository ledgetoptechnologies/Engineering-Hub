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


def test_status_time_is_bounded_by_returned_revision_and_ordered_by_revision(hub):
    client, app, settings = hub
    created = new_task(client, human_headers(client, app, settings))
    response = client.post(f"{BASE}/tasks/{created['id']}/claim", headers=agent_headers(),
                           json={"expected_revision": created["revision"]})
    claimed = response.json()
    repo = app.state.repository
    with repo._transaction() as db:
        # A status action for a revision this snapshot's task does not have yet
        # (as if a concurrent claim/review committed between two reads).
        db.execute("INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)",
                   ("codex", "task.candidate", created["id"], claimed["revision"] + 1, "2099-01-01T00:00:00+00:00"))
    assert overview_task(client, created["id"])["state_since"] == claimed["updated_at"]
    with repo._transaction() as db:
        # Clock moved backwards: the current revision's action has an earlier
        # wall time than an older one. Revision order must win.
        db.execute("UPDATE hub_audit SET created_at=? WHERE entity_id=? AND revision=?",
                   ("2000-01-01T00:00:00+00:00", created["id"], claimed["revision"]))
    assert overview_task(client, created["id"])["state_since"] == "2000-01-01T00:00:00+00:00"


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
