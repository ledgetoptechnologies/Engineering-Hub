from __future__ import annotations

import asyncio
import math
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditLog
from .auth import AuthSettings
from .database import InvalidTransition, MessageError, MessageRepository, NotFound, SendConflict, SendReplayUnavailable, message_dict
from .files import FileConflict, FileError, FileIntegrityError, FileTooLarge, FileNotFound, FilePathError, FileRepository, file_dict
from .feedback import FeedbackError, FeedbackRepository, feedback_dict, feedback_page_dict, feedback_revision_dict
from .retention import RetentionPolicy, cleanup
from .topic_presence import TopicPresenceRepository


class MessageCreate(BaseModel):
    recipient: str
    message_type: str = Field(min_length=1, max_length=200)
    body: Any
    priority: str = "normal"
    correlation_id: str | None = None
    in_reply_to: str | None = None
    requires_user_approval: bool = False
    client_request_id: str | None = Field(default=None, strict=True)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class FeedbackSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    title: str
    description: str
    project: str = "agent-bus"
    severity: str = "normal"
    client_request_id: str | None = None


class FeedbackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)
    status: str
    note: str = ""


class Service:
    def __init__(self, repository: MessageRepository, audit: AuditLog, auth: AuthSettings,
                 policy: RetentionPolicy, files: FileRepository | None = None, semantic: Any = None,
                 topic_presence: TopicPresenceRepository | None = None):
        self.repository, self.audit, self.auth, self.policy, self.files = repository, audit, auth, policy, files
        self.semantic = semantic
        self.topic_presence = topic_presence or TopicPresenceRepository(repository.path)
        self.feedback = FeedbackRepository(repository.path)

    def audit_feedback(self, operation: str, agent: str, request: Request | None = None) -> None:
        # Report descriptions, titles, notes and client-supplied identifiers are never audit fields.
        self.audit.append(operation=operation, agent=agent, result="ok",
                          request_id=getattr(request.state, "request_id", None) if request else None)

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


_SUBJECT_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)


def _known_auth_secrets(auth: AuthSettings) -> tuple[str, ...]:
    return tuple(value for value in (auth.hermes_token, auth.codex_token, auth.claude_token, auth.admin_token)
                 if isinstance(value, str) and value)


def _safe_subject(body: Any, secrets: tuple[str, ...]) -> str | None:
    if not isinstance(body, dict):
        return None
    subject = body.get("subject")
    if not isinstance(subject, str):
        return None
    subject = subject.strip()
    if not subject or len(subject) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in subject):
        return None
    if any(secret in subject for secret in secrets):
        return None
    if any(pattern.search(subject) for pattern in _SUBJECT_SECRET_PATTERNS):
        return None
    return subject


def _cursor_pointer(message: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    return {
        "id": message["id"],
        "seq": message["seq"],
        "sender": message["sender"],
        "recipient": message["recipient"],
        "message_type": message["message_type"],
        "priority": message["priority"],
        "correlation_id": message["correlation_id"],
        "in_reply_to": message["in_reply_to"],
        "hop_count": message["hop_count"],
        "requires_user_approval": message["requires_user_approval"],
        "created_at": message["created_at"],
        "subject": _safe_subject(message["body"], secrets),
    }


def validate_cursor_wait_args(*, after: int | None, since_now: bool, fields: str,
                              limit: int, timeout: float) -> None:
    if after is not None and (isinstance(after, bool) or not isinstance(after, int) or after < 0):
        raise ValueError("after must be a non-negative integer")
    if not isinstance(since_now, bool):
        raise ValueError("since_now must be a boolean")
    if after is not None and since_now:
        raise ValueError("after and since_now cannot be combined")
    if not isinstance(fields, str) or fields not in {"pointer", "full"}:
        raise ValueError("fields must be pointer or full")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be between 0 and 60 seconds")
    try:
        timeout_value = float(timeout)
    except (OverflowError, ValueError) as exc:
        raise ValueError("timeout must be between 0 and 60 seconds") from exc
    if not math.isfinite(timeout_value) or not 0 <= timeout_value <= 60:
        raise ValueError("timeout must be between 0 and 60 seconds")


async def wait_for_message_feed(repository: MessageRepository, recipient: str, *, after: int | None,
                               since_now: bool, fields: str, limit: int, timeout: float,
                               secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """Poll the durable sidecar cursor; every database operation leaves the event loop free."""
    validate_cursor_wait_args(after=after, since_now=since_now, fields=fields, limit=limit, timeout=timeout)
    head = await asyncio.to_thread(repository.delivery_head)
    if since_now:
        cursor = head
    else:
        cursor = 0 if after is None else after
        if cursor > head:
            raise MessageError("after exceeds current delivery sequence head")

    deadline = time.monotonic() + float(timeout)
    while True:
        messages, next_cursor = await asyncio.to_thread(
            repository.list_after, recipient, after=cursor, limit=limit
        )
        oldest = await asyncio.to_thread(repository.oldest_available_seq)
        if messages:
            if fields == "pointer":
                messages = [_cursor_pointer(message, secrets) for message in messages]
            return {"messages": messages, "next_cursor": next_cursor, "oldest_available_seq": oldest}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"messages": [], "next_cursor": cursor, "oldest_available_seq": oldest}
        await asyncio.sleep(min(0.75, remaining))


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
            message, replayed = service.repository.insert_with_outcome(sender=sender, **payload.model_dump())
        except SendConflict as exc: raise HTTPException(409, str(exc)) from exc
        except SendReplayUnavailable as exc: raise HTTPException(410, str(exc)) from exc
        except MessageError as exc: raise HTTPException(400, str(exc)) from exc
        service.audit_message("send", sender, message, result="replayed" if replayed else "ok", request=request)
        return message_dict(message)

    @api.get("/presence")
    async def presence_status(_identity: str = Depends(agent)):
        publisher = getattr(service, "push_publisher", None)
        if publisher is None:
            raise HTTPException(503, "presence observer unavailable")
        return await publisher.presence_status()

    @api.get("/message-receipts")
    def get_send_receipt(request: Request, client_request_id: str, sender: str = Depends(agent)):
        """Return metadata for this authenticated sender's keyed send, never an inbox body."""
        try:
            receipt = service.repository.get_send_receipt(sender, client_request_id)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except MessageError as exc:
            raise HTTPException(400, str(exc)) from exc
        service.audit.append(operation="send_receipt", agent=sender, result="ok",
                             request_id=getattr(request.state, "request_id", None))
        return receipt

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
                                after: int | None = Query(default=None, ge=0),
                                since_now: bool = Query(default=False),
                                fields: str = Query(default="pointer"),
                                limit: int = Query(default=50),
                                identity: str = Depends(agent)):
        """Wait for legacy pending messages or use the durable cursor notification feed."""
        # Keep the historical contract untouched unless an explicit cursor mode is selected.
        if after is None and not since_now:
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

        try:
            validate_cursor_wait_args(after=after, since_now=since_now, fields=fields,
                                      limit=limit, timeout=timeout)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            return await wait_for_message_feed(
                service.repository, identity, after=after, since_now=since_now,
                fields=fields, limit=limit, timeout=timeout,
                secrets=_known_auth_secrets(service.auth),
            )
        except MessageError as exc:
            raise HTTPException(400, str(exc)) from exc

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

    @api.post("/feedback", status_code=201)
    def submit_feedback(payload: FeedbackSubmit, request: Request, identity: str = Depends(agent)):
        try:
            report = service.feedback.submit(actor=identity, **payload.model_dump())
        except FeedbackError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.safe_message}) from exc
        service.audit_feedback("feedback_submit", identity, request)
        return feedback_dict(report)

    @api.get("/feedback")
    def list_feedback(request: Request, kind: str | None = None, status: str | None = None,
                      project: str | None = None, severity: str | None = None, author: str | None = None,
                      limit: int = Query(default=50, ge=1, le=100), after: str | None = None,
                      _identity: str = Depends(agent)):
        filters = {key: value for key, value in {"kind": kind, "status": status, "project": project,
                   "severity": severity, "author": author}.items() if value is not None}
        try:
            return feedback_page_dict(service.feedback.list(filters, limit=limit, after=after), include_text=True)
        except FeedbackError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.safe_message}) from exc

    @api.get("/feedback/{feedback_id}")
    def get_feedback(feedback_id: str, after_revision: int = Query(default=0, ge=0),
                     history_limit: int = Query(default=20, ge=1, le=100), _identity: str = Depends(agent)):
        try:
            report, history = service.feedback.get_with_history(
                feedback_id, after_revision=after_revision, limit=history_limit)
            last = history[-1].revision if history else after_revision
            return {"feedback": feedback_dict(report), "history": [feedback_revision_dict(x) for x in history],
                    "next_revision": last if last < report.revision else None}
        except FeedbackError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.safe_message}) from exc

    @api.patch("/feedback/{feedback_id}")
    def update_feedback(feedback_id: str, payload: FeedbackUpdate, request: Request,
                        identity: str = Depends(agent)):
        try:
            report = service.feedback.update(feedback_id, actor=identity, **payload.model_dump())
        except FeedbackError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.safe_message}) from exc
        service.audit_feedback("feedback_update", identity, request)
        return feedback_dict(report)

    return api


def policy_from_env() -> RetentionPolicy:
    return RetentionPolicy(max(1, int(os.environ.get("ACK_RETENTION_DAYS", "30"))),
                           max(1, int(os.environ.get("REJECT_RETENTION_DAYS", "90"))))
