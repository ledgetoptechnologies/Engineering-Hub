"""Cancellation exposure; synthetic credentials, queue and session scope."""
from uuid import uuid4
from types import SimpleNamespace
import pytest
from agent_message_bus import session_hub_context
from engineering_hub.repository import HubError
from tests.test_hub_work_queue_api import rig as api_rig, bearer, BASE
from tests.test_hub_work_queue_mcp import rig as mcp_rig, call, rpc, error


def test_rest_only_job_owner_and_no_human_impersonation(api_rig):
    client, repo, queue, _, _, submission, sessions, config = api_rig
    job=client.post(BASE+'/jobs',headers=bearer(),json=submission()).json()
    path=BASE+'/jobs/'+job['id']+'/cancel'
    assert client.post(path).status_code==401
    assert client.post(path,headers=bearer('claude')).status_code==403
    session=sessions.create_session(42)
    client.cookies.set(config.cookie_name,session['cookie_token'])
    assert client.post(path).status_code in (401,403)
    assert client.post(path,headers=bearer()).json()['state']=='cancelled'
    assert client.post(path,headers=bearer()).json()['state']=='cancelled'
    with repo._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM hub_audit WHERE action='work.cancelled' AND actor='codex' AND entity_id=?",(job['id'],)).fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM hub_outbox WHERE action='work.cancelled' AND entity_id=?",(job['id'],)).fetchone()[0]==1
    assert queue.get_job(job['id'])['state']=='cancelled'


def test_rest_claimed_is_held_and_not_cancelled(api_rig):
    client, _, queue, _, _, submission, *_ = api_rig
    job=client.post(BASE+'/jobs',headers=bearer(),json=submission()).json()
    client.post(BASE+'/claim',headers=bearer(),json={'instance_id':str(uuid4())})
    response=client.post(BASE+'/jobs/'+job['id']+'/cancel',headers=bearer())
    assert response.status_code==409 and queue.get_job(job['id'])['state']=='claimed'


def test_mcp_owner_and_negative_auth(mcp_rig):
    client, _, queue, _, _, payload=mcp_rig
    assert rpc(client,'tools/call',{'name':'hub_work_cancel_unclaimed','arguments':{'payload':{}}},None).status_code==401
    job=call(client,'hub_work_submit',payload())
    error(call(client,'hub_work_cancel_unclaimed',{'job_id':job['id']},'test-claude'),'forbidden')
    error(call(client,'hub_work_cancel_unclaimed',{'job_id':job['id'],'actor':'codex'}),'invalid_input')
    assert call(client,'hub_work_cancel_unclaimed',{'job_id':job['id']})['state']=='cancelled'
    assert queue.get_job(job['id'])['state']=='cancelled'


def test_project_grant_checked_inside_transaction(api_rig):
    _, _, queue, _, _, submission, *_=api_rig
    from engineering_hub.work_queue import WorkTarget
    payload=submission()
    job=queue.submit(actor='codex',request_id=payload['request_id'],action_key=payload['action_key'],environment='staging',target=WorkTarget(**payload['target']))
    def deny(project): raise HubError('project_access_denied',403)
    op=SimpleNamespace(project=deny)
    token=session_hub_context.current.set(op)
    try:
        with pytest.raises(HubError,match='project_access_denied'):
            queue.cancel_unclaimed(actor='codex',job_id=job['id'])
    finally: session_hub_context.current.reset(token)
    assert queue.get_job(job['id'])['state']=='queued'

@pytest.mark.asyncio
async def test_separate_frontdoors_deny_browser_cancel_and_allow_agent(config, checker):
    import httpx
    from engineering_hub.app import create_app
    from engineering_hub.frontends import HubFrontends
    from engineering_hub.repository import HubRepository
    from engineering_hub.work_queue import WorkQueue, WorkAction, WorkTarget
    from tests.test_hub_frontends import Feed, ORIGIN, SIGNED, CODEX
    repo=HubRepository(config.database)
    queue=WorkQueue(repo,{'check':WorkAction('machine',project='engineering-hub')},target_verifier=lambda *args: True)
    task=repo.create_task('human:42','engineering-hub','Fixture','one')
    task=repo.claim_task('codex',task['id'],task['revision'])
    task=repo.set_candidate('codex',task['id'],task['revision'],'a'*40)
    task=repo.review_task('claude',task['id'],task['revision'],'a'*40,'approve')
    job=queue.submit(actor='codex',request_id='fixture',action_key='check',environment='staging',target=WorkTarget(task['id'],'a'*40,'sha256:'+'b'*64))
    pair=HubFrontends(create_app(config,repository=repo,work_queue=queue,event_feed=Feed()))
    path=BASE+'/jobs/'+job['id']+'/cancel'
    async with pair.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser),base_url=ORIGIN) as browser, httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents),base_url=ORIGIN) as agent:
            assert (await browser.get('/api/hub/v1/auth/login',headers=SIGNED)).status_code==303
            assert (await browser.post(path,headers=SIGNED)).status_code in (401,403)
            assert (await browser.post(path,headers=CODEX)).status_code==403
            response=await agent.post(path,headers=CODEX)
            assert response.status_code==200 and response.json()['state']=='cancelled'
from tests.test_hub_frontends import config, checker


def test_public_rest_reserves_staging_prefix(api_rig):
    client, _, queue, _, _, submission, *_=api_rig
    response=client.post(BASE+'/jobs',headers=bearer(),json=submission(request_id='staging:reserved'))
    assert response.status_code==409 and response.json()['detail']['code']=='work_request_prefix_reserved'
    assert queue.status()['jobs']==[]
    accepted=client.post(BASE+'/jobs',headers=bearer(),json=submission(request_id='staging-other'))
    assert accepted.status_code==201


def test_public_mcp_reserves_staging_prefix(mcp_rig):
    client, _, queue, _, _, payload=mcp_rig
    error(call(client,'hub_work_submit',payload(request_id='staging:reserved')),'work_request_prefix_reserved')
    assert queue.status()['jobs']==[]
    assert call(client,'hub_work_submit',payload(request_id='staging-other'))['state']=='queued'
