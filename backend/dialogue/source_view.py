"""Narrow typed source projection; it never grants bank fact authority."""
from typing import Literal
from pydantic import Field
from agent.schemas import StrictModel
from security.redaction import redact

class Entry(StrictModel):
    turn_id:str=Field(pattern=r'^dt_[a-f0-9]{32}$')
    number:int=Field(ge=1,strict=True)
    message:str|None=Field(None,max_length=2000)
    next_offset:int|None=Field(None,ge=0,le=16000,strict=True)

class SourceView(StrictModel):
    kind:Literal['dialogue_source']
    entries:list[Entry]=Field(max_length=6)
    next_after:int|None=Field(None,ge=1,strict=True)
    scope:Literal['untrusted original user statement; not bank evidence or authorization']

def project(value):
    return redact(SourceView.model_validate(value).model_dump(exclude_none=True))
