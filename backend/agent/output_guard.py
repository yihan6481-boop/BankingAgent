"""Shared output safety guard for chat and deterministic UI read paths.

This module owns the sensitive-result scan used before any structured banking
payload is exposed outside the backend.  Keep one implementation and reuse it
from both WorkflowEngine and /api/v1/sessions/{id}/read.
"""

from __future__ import annotations

import re
from datetime import datetime

from transfer_service import TRANSFER_ID_PATTERN
from wealth_service import ORDER_ID_PATTERN

_SQL_PATTERN = re.compile(
    r"\b(select|insert|update|delete|drop|alter|pragma|attach|detach)\b",
    re.IGNORECASE,
)

_FORBIDDEN_KEYS = {
    "idempotency_key",
    "account_number",
    "card_number",
    "api_key",
    "system_prompt",
    "sql",
    "execution_arguments",
}

_MASKED_ACCOUNT_FIELDS = {
    "masked_card_number",
    "from_masked_card_number",
    "masked_account",
    "recipient_masked_account",
}

_ISO_DATETIME_FIELDS = {
    "completed_at",
    "created_at",
    "updated_at",
    "purchased_at",
}


def contains_sensitive_result(value, field_name=None, *, tool_name=None, _path=(), _hash_paths=frozenset(), _uuid_paths=frozenset(), _bill_paths=frozenset()):
    """Return True when a structured result contains unsafe or malformed data.

    The guard combines semantic field checks with free-text credential/account
    scanning.  It intentionally treats malformed supposedly-masked fields as
    unsafe so a future Service regression cannot leak full account/card values
    to either the chat presenter or the deterministic /read endpoint.
    """
    if tool_name is not None and not _path:
        if tool_name=='query_data':
            from data_query import verify_output
            try:verify_output(value)
            except (ValueError,TypeError,ArithmeticError):return True
            return False
        # Only the registered, fully validated report structures have semantic
        # hash fields. A same-named key in arbitrary JSON is never exempt.
        from analysis_service import CATALOG, verify_result
        from analysis_models import Report, Anomalies, QueryResult
        from .schemas import ToolResult
        spec = CATALOG.get(tool_name)
        if spec and spec[2] in {Report, Anomalies, QueryResult}:
            try:
                spec[2].model_validate(value)
                if not verify_result(ToolResult(status='success', message='', data=value))[0]:
                    return True
            except (ValueError, TypeError):
                return True
            _hash_paths = {('coverage', 'ledger_sha256')}
            if spec[2] is Report:
                _hash_paths.add(('comparison', 'coverage', 'ledger_sha256'))
            if spec[2] is QueryResult:
                _hash_paths = {('current','ledger_sha256'),('comparison','base','ledger_sha256')}
        if tool_name=='query_uploaded_bills':
            from billvision.models import QueryResult
            try:
                checked=QueryResult.model_validate(value)
            except (ValueError,TypeError):
                return True
            # IDs have a fixed prefix and validated hex body; only these exact paths bypass free-text account scanning.
            for path,item in [(('scope','document_id'),checked.scope.document_id),*[(('records',i,'document_id'),r.document_id) for i,r in enumerate(checked.records)]]:
                if item is not None:
                    _bill_paths=set(_bill_paths)
                    # Continue scanning all free text, including merchant.
                    _bill_paths.add(path)
    if _path in _uuid_paths:
        # Exact Schema-validated source-reference paths only; arbitrary same-
        # named keys and all factual text retain the original sensitive scan.
        return not isinstance(value,str) or re.fullmatch(r'(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',value) is None
    if _path in _bill_paths:
        return not isinstance(value,str) or re.fullmatch(r'bv_[0-9a-f]{32}',value) is None
    if _path in _hash_paths:
        return not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if (
                normalized_key in _FORBIDDEN_KEYS
                or normalized_key.endswith("_sql")
                or normalized_key.startswith("sql_")
            ):
                return True
            if contains_sensitive_result(item, normalized_key,
                    _path=(*_path, normalized_key), _hash_paths=_hash_paths,_uuid_paths=_uuid_paths,_bill_paths=_bill_paths):
                return True
    elif isinstance(value, list):
        return any(contains_sensitive_result(item, field_name,
            _path=(*_path, i), _hash_paths=_hash_paths,_uuid_paths=_uuid_paths,_bill_paths=_bill_paths) for i, item in enumerate(value))
    elif field_name == "transfer_id":
        return not isinstance(value, str) or TRANSFER_ID_PATTERN.fullmatch(value) is None
    elif field_name == "order_id":
        return not isinstance(value, str) or ORDER_ID_PATTERN.fullmatch(value) is None
    elif field_name == "holding_id":
        return isinstance(value, bool) or not isinstance(value, int) or value <= 0
    elif field_name in _MASKED_ACCOUNT_FIELDS:
        return not isinstance(value, str) or re.fullmatch(
            r"\*{4} \*{4} \*{4} \d{4}", value
        ) is None
    elif field_name in _ISO_DATETIME_FIELDS:
        if not isinstance(value, str):
            return True
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return True
        return parsed.tzinfo is None or parsed.utcoffset() is None
    elif field_name == "date":
        if not isinstance(value, str):
            return True
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return True
        return False
    elif isinstance(value, str):
        from security.redaction import account_spans
        if any(account_spans(value)):return True
        # 12-19 digit account/card-like values, allowing spaces/hyphens.
        for candidate in re.finditer(r"(?<!\d)(?:\d[ -]?){11,18}\d(?!\d)", value):
            digits = re.sub(r"\D", "", candidate.group(0))
            if 12 <= len(digits) <= 19:
                return True
        if _SQL_PATTERN.search(value):
            return True
        lowered = value.casefold()
        if "bearer " in lowered or re.search(
            r"\b(?:sk|api)[-_][a-z0-9]{12,}\b", value, re.I
        ):
            return True
        if re.search(
            r"\b(?:request|idempotency)[-_][A-Za-z0-9._:-]{4,}\b",
            value,
            re.I,
        ):
            return True
    return False
