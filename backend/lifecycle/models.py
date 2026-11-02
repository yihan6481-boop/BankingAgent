from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, StrictInt, model_validator, field_validator

from agent.schemas import (StrictModel, TaskState, TaskPlanProposal,
                           SimpleConstraint, _reject_sensitive_or_executable_text)


class Operation(StrictModel):
    operation_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    key: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$')
    arguments_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    state: Literal['ready', 'inflight', 'committed', 'unknown'] = 'ready'
    preview_generation: int = Field(default=0, ge=0)
    preview_revision: int | None = Field(default=None, ge=1)
    execution_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class CardReadScope(StrictModel):
    card_ids: list[StrictInt] = Field(default_factory=list,max_length=8)
    funding_step_id: str | None = Field(default=None,pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')

    @model_validator(mode='after')
    def bounded(self):
        if bool(self.card_ids)==bool(self.funding_step_id) or any(n<1 for n in self.card_ids):
            raise ValueError('invalid card scope')
        return self


class DurableTask(StrictModel):
    format_version: Literal[1] = 1
    owner_id: int
    principal_id: str
    # Internal provenance for implicit chat references; absent in old snapshots.
    origin_session_hash: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    card_read_scopes: dict[str,CardReadScope] = Field(default_factory=dict,max_length=8)
    revision: int = Field(ge=1)
    mode: Literal['single', 'workflow']
    status: Literal['active', 'waiting', 'paused', 'scheduled', 'manual',
                    'cancelled', 'completed', 'failed'] = 'active'
    task: TaskState
    operations: dict[str, Operation]
    retired_operations: list[Operation] = Field(default_factory=list)
    scheduled_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=300)
    notes: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def consistent(self):
        if set(self.operations) != {s.step_id for s in self.task.steps}:
            raise ValueError('operation/step mismatch')
        nodes={s.step_id:s for s in self.task.steps}
        for target,scope in self.card_read_scopes.items():
            if target not in nodes or nodes[target].tool_name!='get_cards':raise ValueError('invalid read scope target')
            if scope.funding_step_id and (scope.funding_step_id not in nodes or nodes[scope.funding_step_id].tool_name not in {'transfer_money','buy_wealth_product'}):
                raise ValueError('invalid funding scope source')
        if self.status == 'completed' and self.task.status != 'completed':
            raise ValueError('completion mismatch')
        if self.status == 'scheduled' and self.scheduled_at is None:
            raise ValueError('missing schedule')
        for value in [self.reason, *self.notes]:
            if value:
                _reject_sensitive_or_executable_text(value, 'manual context')
        return self


class RevisionRequest(StrictModel):
    revision: StrictInt = Field(ge=1)
    reason: str | None = Field(default=None, max_length=300)

    @field_validator('reason')
    @classmethod
    def safe_reason(cls, value):
        return _reject_sensitive_or_executable_text(value, 'reason') if value else value


class ScheduleRequest(RevisionRequest):
    at: str


class ModifyRequest(RevisionRequest):
    step_id: str
    changes: dict
    constraints: list[SimpleConstraint] = Field(default_factory=list, max_length=4)


class CreateTaskRequest(StrictModel):
    plan: TaskPlanProposal


class NoteRequest(RevisionRequest):
    note: str = Field(min_length=1, max_length=300)

    @field_validator('note')
    @classmethod
    def safe_note(cls, value):
        return _reject_sensitive_or_executable_text(value, 'note')


class TaskConfirmRequest(RevisionRequest):
    text: str = Field(min_length=1, max_length=100)


class RollbackRequest(RevisionRequest):
    target_step_id: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=300)


class TaskView(StrictModel):
    format_version: Literal[1]
    revision: int = Field(ge=1)
    mode: Literal['single', 'workflow']
    status: Literal['active','waiting','paused','scheduled','manual','cancelled','completed','failed']
    task: TaskState
    scheduled_at: datetime | None
    reason: str | None
    notes: list[str]


