import httpx
import pytest
from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings


@pytest.mark.asyncio
async def test_rest_cas_requires_version_for_overwrite_and_delete(tmp_path):
    app = create_app(db_path=tmp_path/'m.sqlite3', audit_path=tmp_path/'audit.jsonl',
                     file_root=tmp_path/'files', auth=AuthSettings('h','c','a'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        headers = {'Authorization':'Bearer h'}
        async def put(data, version=None):
            fields={'path':'demo/plan.md'}
            if version is not None: fields['expected_version']=str(version)
            return await client.post('/api/files',headers=headers,data=fields,files={'file':('plan.md',data,'text/plain')})
        first=await put(b'v1')
        assert first.status_code == 201 and first.json()['created_by']=='hermes'
        assert (await put(b'overwrite')).status_code == 409
        second=await put(b'v2',1)
        assert second.status_code==201 and second.json()['version']==2
        admin={'Authorization':'Bearer a'}
        assert (await client.delete('/api/files/demo/plan.md',headers=admin)).status_code==422
        assert (await client.delete('/api/files/demo/plan.md?expected_version=1',headers=admin)).status_code==409
        assert (await client.get('/api/files/demo/plan.md/download',headers=headers)).content==b'v2'
        assert (await client.delete('/api/files/demo/plan.md?expected_version=2',headers=admin)).status_code==204
