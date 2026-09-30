from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .audit import AuditLog
from .auth import AuthSettings
from .database import InvalidTransition, MessageError, MessageRepository, NotFound, message_dict
from .retention import RetentionPolicy, cleanup


class MessageCreate(BaseModel):
    recipient: str
    message_type: str = Field(min_length=1, max_length=200)
    body: Any
    priority: str = "normal"
    correlation_id: str | None = None
    requires_user_approval: bool = False


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class Service:
    def __init__(self, repository: MessageRepository, audit: AuditLog, auth: AuthSettings,
                 policy: RetentionPolicy):
        self.repository, self.audit, self.auth, self.policy = repository, audit, auth, policy

    def audit_message(self, operation: str, agent: str, message: Any, result: str = "ok") -> None:
        self.audit.append(operation=operation, agent=agent, message_id=getattr(message, "id", None),
                          recipient=getattr(message, "recipient", None), result=result,
                          correlation_id=getattr(message, "correlation_id", None))


def router(service: Service) -> APIRouter:
    api = APIRouter(prefix="/api")

    def agent(request: Request) -> str: return service.auth.agent(request)
    def admin(request: Request) -> str: return service.auth.admin(request)

    @api.post("/messages", status_code=201)
    def send(payload: MessageCreate, sender: str = Depends(agent)):
        try:
            message = service.repository.insert(sender=sender, **payload.model_dump())
        except MessageError as exc: raise HTTPException(400, str(exc)) from exc
        service.audit_message("send", sender, message)
        return message_dict(message)

    @api.get("/messages")
    def receive(request: Request, status: str = "pending", limit: int = 50, recipient: str | None = None):
        identity = agent(request)
        if recipient is not None and recipient != identity: raise HTTPException(403, "recipient must match authenticated agent")
        try: result = service.repository.list_for_recipient(identity, status=status, limit=limit)
        except MessageError as exc: raise HTTPException(400, str(exc)) from exc
        return {"messages": [message_dict(message) for message in result]}

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
        service.audit_message("claim", identity, message); return message_dict(message)

    @api.post("/messages/{message_id}/ack")
    def ack(message_id: str, request: Request):
        identity = agent(request)
        try: message = service.repository.acknowledge(message_id, identity)
        except NotFound as exc: raise HTTPException(404, str(exc)) from exc
        except InvalidTransition as exc: raise HTTPException(409, str(exc)) from exc
        service.audit_message("acknowledge", identity, message); return message_dict(message)

    @api.post("/messages/{message_id}/reject")
    def reject(message_id: str, payload: RejectRequest, request: Request):
        identity = agent(request)
        try: message = service.repository.reject(message_id, identity, payload.reason)
        except NotFound as exc: raise HTTPException(404, str(exc)) from exc
        except InvalidTransition as exc: raise HTTPException(409, str(exc)) from exc
        except MessageError as exc: raise HTTPException(400, str(exc)) from exc
        service.audit_message("reject", identity, message); return message_dict(message)

    @api.post("/admin/cleanup")
    def run_cleanup(request: Request, _admin: str = Depends(admin)):
        result = cleanup(service.repository, service.policy)
        service.audit.append(operation="cleanup", agent="admin", result="ok")
        return result

    return api


def policy_from_env() -> RetentionPolicy:
    return RetentionPolicy(max(1, int(os.environ.get("ACK_RETENTION_DAYS", "30"))),
                           max(1, int(os.environ.get("REJECT_RETENTION_DAYS", "90"))))
