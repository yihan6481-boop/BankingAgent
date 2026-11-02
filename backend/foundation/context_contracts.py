"""R2 handoff contracts. No history store, summarizer, language rules or recall."""
from typing import Literal
from pydantic import Field
from agent.schemas import StrictModel

class Reference(StrictModel):
    reference: str = Field(min_length=1,max_length=100)
    kind: Literal['query','transfer','result','task']
    status: str
    revision: int = Field(ge=1)

class ContextSnapshot(StrictModel):
    protocol: Literal['r1-context-1']
    session_reference: str = Field(pattern='^[0-9a-f]{64}$')
    user_id: int = Field(ge=0)
    # Values are read-through public draft/task/result projections; never
    # accepted back as writable state or authorization.
    focus: dict | None
    items: list[dict] = Field(max_length=30)
    unfinished: list[Reference] = Field(max_length=30)
    truncated: bool
    limit: int = Field(ge=1,le=30)
    server_time: str
    business_timezone: Literal['Asia/Shanghai']
    scope: str

class ToolExchange(StrictModel):
    """R2 pairs each model call with exactly one matching gateway result."""
    call_id: str = Field(min_length=1,max_length=100)
    operation: str = Field(min_length=1,max_length=80)
    arguments: dict
    result: dict
    trace_id: str = Field(min_length=1,max_length=64)

class RecentMessage(StrictModel):
    role: Literal['user','assistant']
    turn_id: str = Field(min_length=1,max_length=100)
    content: str = Field(max_length=2000)

class ContextBudget(StrictModel):
    recent_messages: int = Field(default=16,ge=2,le=32)
    tool_exchanges: int = Field(default=8,ge=1,le=16)
    references: int = Field(default=8,ge=1,le=30)
    serialized_bytes: int = Field(default=32000,ge=4000,le=64000)
