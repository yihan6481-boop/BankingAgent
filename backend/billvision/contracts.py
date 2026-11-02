"""Public additive response contracts; existing HTTP contracts stay frozen."""
from typing import Generic,TypeVar,Literal
from pydantic import Field
from .models import Strict,Recognition,ReviewedRow

T=TypeVar('T')
class Envelope(Strict,Generic[T]):
    contract_version: Literal['bill-vision-1.0']='bill-vision-1.0'
    data: T

class ImportReceipt(Strict):
    document_id: str
    revision: int
    digest: str
    row_ids: list[str]
    imported_count: int
    kind: Literal['personal_data_import']

class Duplicate(Strict):
    document_id: str
    row_id: str
    type: Literal['event','possible']

class InvoiceSource(Strict):
    source:Literal['reviewed_invoice']
    payment_document_id:str=Field(pattern=r'^pd_[0-9a-f]{32}$')
    source_revision:int=Field(ge=1)
    source_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    payment_verified:Literal[False]=False

class Document(Strict):
    document_id: str
    revision: int
    state: Literal['uploaded','recognizing','recognition_failed','review','confirmed','unsupported']
    platform: Literal['wechat','alipay','unknown','invoice']
    rows: list[ReviewedRow]
    original: Recognition | InvoiceSource | None
    digest: str
    duplicates: dict[str,list[Duplicate]]
    error_code: str | None
    repeated_upload: bool
    issues: dict[str,list[str]]
    confirmation: ImportReceipt | None
    coverage: str
    source_current:bool=True

class Summary(Strict):
    document_id: str
    revision: int
    state: str
    created_at: str

class Documents(Strict):
    documents: list[Summary]
    has_more: bool

class Version(Strict):
    revision: int
    operation: str
    rows: list[ReviewedRow]
    platform: str
    created_at: str

class Deleted(Strict):
    document_id: str
    deleted: Literal[True]
