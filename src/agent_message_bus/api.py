from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .audit import AuditLog
from .auth import AuthSettings
from .database import InvalidTransition, MessageError, MessageRepository, NotFound, message_dict
from .files import FileConflict, FileError, FileIntegrityError, FileTooLarge, FileNotFound, FilePathError, FileRepository, file_dict
from .retention import RetentionPolicy, cleanup


class MessageCreate(BaseModel):
    recipient: str
    message_type: str = Field(min_length=1, max_length=200)
    body: Any
    priority: str = "normal"
    correlation_id: str | None = None
    in_reply_to: str | None = None
    requires_user_approval: bool = False


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class Service:
    def __init__(self, repository: MessageRepository, audit: AuditLog, auth: AuthSettings,
                 policy: RetentionPolicy, files: FileRepository | None = None, semantic: Any = None):
        self.repository, self.audit, self.auth, self.policy, self.files = repository, audit, auth, policy, files
        self.semantic = semantic

    def audit_message(self, operation: str, agent: str, message: Any, result: str = "ok",
                      request: Request | None = None) -> None:
        correlation_id = getattr(message, "correlation_id", None)
        if correlation_id is None or self.auth.identity_for(correlation_id):
            correlation_id = getattr(request.state, "correlation_id", None) if request else None
        self.audit.append(
            operation=operation, agent=agent, message_id=getattr(message, "id", None),
            recipient=getattr(message, "recipient", None), result=result,
            correlation_id=correlation_id,
            request_id=getattr(request.state, "request_id", None) if request else None,
        )


def router(service: Service) -> APIRouter:
    api = APIRouter(prefix="/api")

    def agent(request: Request) -> str: return service.auth.agent(request)
    def admin(request: Request) -> str: return service.auth.admin(request)

    def file_repository() -> FileRepository:
        if service.files is None:
            raise HTTPException(503, "file service is not configured")
        return service.files

    @api.post("/messages", status_code=201)
    def send(payload: MessageCreate, request: Request, sender: str = Depends(agent)):
        try:
            message = service.repository.insert(sender=sender, **payload.model_dump())
        except MessageError as exc: raise HTTPException(400, str(exc)) from exc
        service.audit_message("send", sender, message, request=request)
        return message_dict(message)

    @api.get("/messages")
    def receive(request: Request, status: str = "pending", limit: int = 50, recipient: str | None = None):
        identity = agent(request)
        if recipient is not None and recipient != identity: raise HTTPException(403, "recipient must match authenticated agent")
        try: result = service.repository.list_for_recipient(identity, status=status, limit=limit)
        except MessageError as exc: raise HTTPException(400, str(exc)) from exc
        return {"messages": [message_dict(message) for message in result]}

    @api.get("/messages/wait")
    async def wait_for_messages(timeout: float = Query(default=30.0, ge=0, le=60),
                                actionable_only: bool = Query(default=False),
                                identity: str = Depends(agent)):
        """Wait for pending messages addressed to the identity; optionally skip non-actionable messages."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                await asyncio.to_thread(service.repository.expire_claims)
                messages = await asyncio.to_thread(
                    service.repository.list_for_recipient, identity, status="pending", limit=50,
                    actionable_only=actionable_only,
                )
            except MessageError as exc:
                raise HTTPException(400, str(exc)) from exc
            if messages:
                return {"messages": [message_dict(message) for message in messages]}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"messages": []}
            await asyncio.sleep(min(0.75, remaining))

    @api.get("/messages/{message_id}")
    def get(message_id: str, request: Request):
        identity = agent(request)
        try: message = service.repository.get(message_id)
        except NotFound as exc: raise HTTPException(404, str(exc)) from exc
        if message.recipient != identity: raise HTTPException(404, "message not found")
        return message_dict(message)

    @api.post("/messages/{message_id}/claim")
    def claim(message_id: str, request: Request):
        identity = agent(request)
        try: message = service.repository.claim(message_id, identity)
        except NotFound as exc: raise HTTPException(404, str(exc)) from exc
        except InvalidTransition as exc: raise HTTPException(409, str(exc)) from exc
        service.audit_message("claim", identity, message, request=request); return message_dict(message)

    @api.post("/messages/{message_id}/ack")
    def ack(message_id: str, request: Request):
        identity = agent(request)
        try: message = service.repository.acknowledge(message_id, identity)
        except NotFound as exc: raise HTTPException(404, str(exc)) from exc
        except InvalidTransition as exc: raise HTTPException(409, str(exc)) from exc
        service.audit_message("acknowledge", identity, message, request=request); return message_dict(message)

    @api.post("/messages/{message_id}/reject")
    def reject(message_id: str, payload: RejectRequest, request: Request):
        identity = agent(request)
        try: message = service.repository.reject(message_id, identity, payload.reason)
        except NotFound as exc: raise HTTPException(404, str(exc)) from exc
        except InvalidTransition as exc: raise HTTPException(409, str(exc)) from exc
        except MessageError as exc: raise HTTPException(400, str(exc)) from exc
        service.audit_message("reject", identity, message, request=request); return message_dict(message)

    @api.post("/admin/cleanup")
    def run_cleanup(request: Request, _admin: str = Depends(admin)):
        result = cleanup(service.repository, service.policy)
        service.audit.append(operation="cleanup", agent="admin", result="ok",
                             correlation_id=request.state.correlation_id, request_id=request.state.request_id)
        return result

    @api.post("/files", status_code=201)
    async def upload_file(request: Request, path: str = Form(...), file: UploadFile = File(...),
                          expected_version: int = Form(default=0, ge=0),
                          identity: str = Depends(agent), files: FileRepository = Depends(file_repository)):
        try:
            metadata = await files.save_upload(path, file, author=identity, expected_version=expected_version)
        except FileConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except FileTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except FilePathError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileError as exc:
            raise HTTPException(400, str(exc)) from exc
        service.audit.append(operation="file_upload", agent=identity, result="ok",
                             correlation_id=request.state.correlation_id, request_id=request.state.request_id)
        return file_dict(metadata)

    @api.get("/files")
    def list_files(prefix: str | None = None, after: str | None = None,
                   limit: int = Query(default=100, ge=1, le=999),
                   _identity: str = Depends(agent), files: FileRepository = Depends(file_repository)):
        entries = files.list(prefix=prefix, after=after, limit=limit + 1)
        page = [file_dict(metadata) for metadata in entries[:limit]]
        return {"files": page, "next_after": page[-1]["path"] if len(entries) > limit else None}

    @api.get("/files/{file_path:path}/download")
    def download_file(file_path: str, _identity: str = Depends(agent), files: FileRepository = Depends(file_repository)):
        try:
            metadata, fd = files.open_for_read(file_path)
        except FileIntegrityError as exc:
            raise HTTPException(409, "file integrity verification failed") from exc
        except (FileNotFound, FilePathError) as exc:
            raise HTTPException(404, "file not found") from exc
        filename = metadata.path.rsplit("/", 1)[-1].replace('"', "")
        return StreamingResponse(
            files.iter_fd(fd), media_type=metadata.content_type,
            headers={"Content-Length": str(metadata.size),
                     "Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @api.get("/files/{file_path:path}/metadata")
    @api.get("/files/{file_path:path}")
    def get_file(file_path: str, _identity: str = Depends(agent), files: FileRepository = Depends(file_repository)):
        try:
            return file_dict(files.get(file_path))
        except (FileNotFound, FilePathError) as exc:
            raise HTTPException(404, "file not found") from exc

    @api.delete("/files/{file_path:path}", status_code=204)
    def delete_file(file_path: str, expected_version: int = Query(ge=1),
                    _admin: str = Depends(admin), files: FileRepository = Depends(file_repository)):
        try:
            files.delete(file_path, expected_version=expected_version)
        except FileConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except FileNotFound as exc:
            raise HTTPException(404, "file not found") from exc
        except FilePathError as exc:
            raise HTTPException(400, str(exc)) from exc
        return None

    return api


def policy_from_env() -> RetentionPolicy:
    return RetentionPolicy(max(1, int(os.environ.get("ACK_RETENTION_DAYS", "30"))),
                           max(1, int(os.environ.get("REJECT_RETENTION_DAYS", "90"))))
