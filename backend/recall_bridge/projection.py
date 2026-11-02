"""One narrowly typed exception at the R4 trusted result boundary."""
from typing import Literal
from pydantic import Field,ValidationError
from agent.schemas import StrictModel
from security.redaction import redact


class Item(StrictModel):
    reference:str
    version:int
    text:str|None=None
    availability:str|None=None
    state:str|None=None
    origin_ref:str|None=None
    origin_kind:str|None=None
    updated_at:str|None=None


class Operation(StrictModel):
    operation_ref:str
    operation_state:Literal['queued','running','reconciling','succeeded','failed','cancelled']
    effect_state:Literal['none','partial','committed','unknown']
    availability:Literal['pending','available','blocked','unknown']
    source_ref:str
    source_version:int
    updated_at:str
    error_code:str|None=None
    retry:bool=False
    next_action:str


class Payload(StrictModel):
    memory_contract:Literal['r4-memory-1']
    operation_ref:str|None=None
    operation_state:Literal['queued','running','reconciling','succeeded','failed','cancelled']|None=None
    effect_state:Literal['none','partial','committed','unknown']|None=None
    availability:Literal['pending','available','blocked','unknown']|None=None
    source_ref:str|None=None
    source_version:int|None=None
    updated_at:str|None=None
    error_code:str|None=None
    retry:bool|None=None
    next_action:str|None=None
    reference:str|None=None
    version:int|None=None
    text:str|None=None
    state:str|None=None
    origin_ref:str|None=None
    origin_kind:str|None=None
    items:list[Item]=Field(default_factory=list)
    operations:list[Operation]=Field(default_factory=list)
    total:int|None=None
    offset:int|None=None
    limit:int|None=None
    epoch:int|None=None
    scope:str|None=None
    reason:str|None=None


def project(value):
    try:return redact(Payload.model_validate(value).model_dump(exclude_none=True))
    except ValidationError:return dict(memory_contract='r4-memory-1',availability='blocked',reason='invalid_memory_projection')
