from typing import Literal
from uuid import uuid4
from pydantic import Field
from agent.schemas import StrictModel

class Call(StrictModel):
    operation: Literal['candidates','draft.create','draft.read','draft.patch','draft.close',
        'draft.validate','query.execute','result.read','task.prepare','task.recover','task.read','task.list',
        'context.snapshot','context.focus','task.modify','task.cancel','task.pause','task.resume','task.verify','query.context']
    limit: int = Field(8, ge=1, le=30, strict=True)
    reference: str | None = Field(None, max_length=100)
    revision: int | None = Field(None, ge=1, strict=True)
    kind: Literal['query','transfer'] | None = None
    fields: dict = Field(default_factory=dict)


def feedback(status='completed', code='ok', *, data=None, effects=None, next_actions=None, retry=False, trace=None):
    return dict(protocol='tool-foundation-1',status=status,code=code,data=data,
        effects=effects or [],next_actions=next_actions or [],retry=dict(allowed=retry,
        prerequisite='read current reference and revision before retry'),trace_id=trace or uuid4().hex)


def classify(code):
    if 'unknown' in code and 'commit' in code: return 'commit_unknown'
    if 'unsupported' in code: return 'unsupported'
    if code in {'stale_revision','reference_expired','stale_preview','draft_not_open','task_in_progress','terminal_task','invalid_transition','preparation_not_pending'}: return 'conflict'
    if 'ambiguous' in code or code in {'card_choice','document_choice'}: return 'ambiguous'
    if code.endswith('required'): return 'missing_information'
    if code in {'reference_not_found','task_not_found'} or any(s in code for s in ['denied','unauthorized','forbidden','unsafe']): return 'denied'
    if 'unavailable' in code: return 'service_failure'
    if code.startswith('invalid_') or code in {'schema_invalid','conflicting_filter'}: return 'invalid_parameters'
    return 'business_condition'


