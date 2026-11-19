"""Internal task-routing contract, not a running watcher or delivery grant.

Inputs must come from authenticated bus/task records and a trusted binding store
and runtime observer. Model validation authenticates none of those sources.
This module performs no I/O, registration, acknowledgement or cursor advancement.
"""
from __future__ import annotations

from typing import Annotated, Literal, Protocol
import unicodedata
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

Agent = Literal['codex', 'claude', 'hermes']
Role = Literal['owner', 'reviewer', 'lead']


def canonical_uuid(value: str) -> str:
    parsed = UUID(value)
    if str(parsed) != value or parsed.int == 0:
        raise ValueError('canonical full non-nil UUID required')
    return value


CanonicalUUID = Annotated[str, AfterValidator(canonical_uuid)]


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', strict=True,
                             revalidate_instances='always')

    def model_copy(self, *, update=None, deep=False):
        # Pydantic normally trusts copy updates. Contract records must not.
        return type(self).model_validate(super().model_copy(update=update, deep=deep))


class TaskRoute(Record):
    project: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9-]*$')
    task_id: CanonicalUUID
    role: Role


class SessionTarget(Record):
    agent: Agent
    host_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    generation: str = Field(min_length=1, max_length=256)

    @field_validator('host_id', 'session_id', 'generation')
    @classmethod
    def opaque_identifier(cls, value: str) -> str:
        if any(c.isspace() or unicodedata.category(c).startswith('C') for c in value):
            raise ValueError('nonblank opaque identifier required')
        return value


class TaskBinding(Record):
    route: TaskRoute
    target: SessionTarget


class TaskMessage(Record):
    """Projection of an authenticated message; body fields are cross-checked.

    `recipient` comes from the bus envelope, never the body. `topic` is the
    canonical project ID; the correlation must contain the full task UUID.
    """
    message_id: CanonicalUUID
    topic: str
    task_id: str
    from_role: Role
    to_role: Role
    recipient: Agent
    correlation_id: str
    kind: Literal['task', 'escalation'] = 'task'

    @model_validator(mode='after')
    def exact_route(self) -> TaskMessage:
        TaskRoute(project=self.topic, task_id=self.task_id, role=self.to_role)
        if self.from_role == self.to_role:
            raise ValueError('same-role self wake forbidden')
        if self.correlation_id != f'{self.topic}/task-{self.task_id}':
            raise ValueError('exact task correlation required')
        if (self.to_role == 'lead') != (self.kind == 'escalation'):
            raise ValueError('lead receives explicit escalations only')
        if self.kind == 'escalation' and self.from_role == 'lead':
            raise ValueError('escalation must originate with a worker')
        return self

    def route(self) -> TaskRoute:
        return TaskRoute(project=self.topic, task_id=self.task_id, role=self.to_role)


class AdapterCapabilities(Record):
    # Both default false: a thread ID, PID, title or heartbeat is not a generation.
    authoritative_generation: bool = False
    conditional_accept: bool = False


class DeliveryRequest(Record):
    message_id: CanonicalUUID
    binding: TaskBinding


class DeliveryReceipt(Record):
    request: DeliveryRequest
    outcome: Literal['accepted', 'stale', 'missing', 'unsupported', 'unknown']


class SessionAdapter(Protocol):
    """Required interface for a future independently qualified app adapter.

    observe returns only the requested host/agent/session, with its authoritative
    current generation, or None. accept_if_current MUST compare the complete
    target including generation atomically with accepting the message pointer.
    It must never create/find a replacement session. A timeout is unknown, never
    safe to retry automatically. No production implementation is supplied here.
    """

    def capabilities(self) -> AdapterCapabilities: ...

    async def observe(self, target: SessionTarget) -> SessionTarget | None: ...

    async def accept_if_current(self, request: DeliveryRequest) -> DeliveryReceipt: ...


class RouteDecision(Record):
    action: Literal['hold', 'ready']
    reason: Literal['binding_missing', 'binding_ambiguous', 'recipient_mismatch',
                    'adapter_unsupported', 'session_missing', 'session_stale', 'exact_binding']
    request: DeliveryRequest | None = None
    # Intent for the future durable escalation queue, not a sent lead message.
    escalation_intent: bool = True

    @model_validator(mode='after')
    def consistent_decision(self) -> RouteDecision:
        if self.action == 'ready':
            if self.reason != 'exact_binding' or self.request is None or self.escalation_intent:
                raise ValueError('ready requires exact request without escalation')
        elif self.request is not None or self.reason == 'exact_binding':
            raise ValueError('hold must not carry a delivery request or exact-binding reason')
        return self


def plan_task_delivery(message: TaskMessage, bindings: tuple[TaskBinding, ...], *,
                       capabilities: AdapterCapabilities,
                       observed: SessionTarget | None) -> RouteDecision:
    """Pure preflight; ready is NOT permission to use an unconditional send API.

    Caller must reserve a durable attempt before conditional acceptance and
    reconcile unknown outcomes. An observation can become stale immediately.
    Duplicate exact bindings are held even if their targets happen to agree.
    No project prefix, fallback session or short task ID is ever resolved.
    """
    # Revalidate internal records too: model_construct is explicitly unsafe.
    message = TaskMessage.model_validate(message)
    bindings = tuple(TaskBinding.model_validate(binding) for binding in bindings)
    capabilities = AdapterCapabilities.model_validate(capabilities)
    observed = None if observed is None else SessionTarget.model_validate(observed)

    def hold(reason):
        # A held escalation is already on the escalation path; never loop it.
        return RouteDecision(action='hold', reason=reason,
                             escalation_intent=message.kind != 'escalation')

    matches = [binding for binding in bindings if binding.route == message.route()]
    if not matches:
        return hold('binding_missing')
    if len(matches) != 1:
        return hold('binding_ambiguous')
    binding = matches[0]
    if binding.target.agent != message.recipient:
        return hold('recipient_mismatch')
    if not (capabilities.authoritative_generation and capabilities.conditional_accept):
        return hold('adapter_unsupported')
    if observed is None:
        return hold('session_missing')
    if observed != binding.target:
        return hold('session_stale')
    return RouteDecision(action='ready', reason='exact_binding', escalation_intent=False,
                         request=DeliveryRequest(message_id=message.message_id, binding=binding))
