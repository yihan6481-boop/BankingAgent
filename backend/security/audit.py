"""Desensitized structured security-decision audit chain.

Recorded chain (see the V0.7.0 requirement):

    request_id -> principal/session -> task/step/action -> plan_validation ->
    permission_decision -> preview -> confirmation -> strong_auth ->
    revalidation -> execution -> verification -> final_outcome

Every record stores the correlation ids, policy version, action summary,
permission level, rule reasons, authentication method, stage status, time and
error reason, so partial completion and invalidated authorizations remain
traceable.

Never stored: OTP values, access credentials, API keys, full card/account
numbers, biometric data, raw prompts, or model hidden chain-of-thought.  Only
structured decision facts and rule bases are written.

Critical permission/verification/transaction facts are written **inside the
same transaction** as the business change they describe; they are not deferred
to an asynchronous in-process logger that could be lost on crash.  When the
audit write itself fails, the sensitive execution is refused.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime

from .db import read_connection, write_transaction
from .schemas import AuditRecord


class AuditFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_DETAIL_KEY_ALLOWLIST = {
    "preview_id", "operation_id", "execution_digest", "generation", "revision",
    "target_step", "affected_steps", "authorization_invalidated", "task_status",
    "amount_cents",
    "from_card_id",
    "card_id",
    "recipient_id",
    "subscription_id",
    "holding_id",
    "product_id",
    "business_date",
    "error_code",
    "reason",
    "result",
    "tool_name",
    "declared_risk_level",
    "permission_level",
    "required_authentication",
    "attempts_remaining",
    "threshold_cents",
    "projected_total_cents",
    "revalidation",
    "write_verification",
    "partial_completion",
    "step_count",
    "completed_steps",
    "skipped",
    "escalated_from",
    "escalated_to",
    "breaker_state",
    "restriction_kind",
    "retry_after_seconds",
    "source",
}

_FORBIDDEN_TEXT = re.compile(
    r"(?<!\d)(?:\d[ -]?){11,18}\d(?!\d)|"
    r"\b(?:sk|api)[-_][A-Za-z0-9]{12,}\b|"
    r"\bbearer\s+[A-Za-z0-9._-]{8,}\b|"
    r"\b(select|insert|update|delete|drop|alter|pragma)\b",
    re.IGNORECASE,
)


def _sanitize_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if len(text) > 200:
        text = text[:200]
    if _FORBIDDEN_TEXT.search(text):
        return "[redacted]"
    return text


def sanitize_detail(detail: dict | None) -> dict:
    """Keep only allow-listed, scalar, desensitized keys."""
    if not detail:
        return {}
    cleaned: dict = {}
    for key, value in detail.items():
        normalized = str(key)
        if normalized not in _DETAIL_KEY_ALLOWLIST:
            continue
        cleaned[normalized] = _sanitize_scalar(value)
    return cleaned


class AuditChain:
    """Appends ordered audit records for a single request."""

    def __init__(self, request_id: str | None = None, sequence: int = 0):
        self.request_id = request_id or f"req_{secrets.token_hex(12)}"
        self._sequence = sequence

    def next_sequence(self) -> int:
        value = self._sequence
        self._sequence += 1
        return value


class AuditRecorder:
    """Writes audit records, optionally inside a caller-provided transaction."""

    def __init__(self, now_provider=None):
        from .identity import _utcnow

        self.now_provider = now_provider or _utcnow

    def _now(self) -> datetime:
        value = self.now_provider()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("审计时钟必须包含时区")
        return value.replace(microsecond=0)

    @staticmethod
    def _insert(connection, record: AuditRecord):
        connection.execute(
            """
            INSERT INTO security_audit_records (
                record_id, request_id, sequence, stage, outcome, principal_id,
                user_id, session_id, task_id, step_id, action, policy_version,
                permission_level, decision_id, challenge_id, authorization_id,
                authentication_method, reason_codes_json, error_code,
                detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?)
            """,
            (
                record.record_id,
                record.request_id,
                record.sequence,
                record.stage,
                record.outcome,
                record.principal_id,
                record.user_id,
                record.session_id,
                record.task_id,
                record.step_id,
                record.action,
                record.policy_version,
                record.permission_level,
                record.decision_id,
                record.challenge_id,
                record.authorization_id,
                record.authentication_method,
                json.dumps(list(record.reason_codes), ensure_ascii=False),
                record.error_code,
                json.dumps(record.detail, ensure_ascii=False, sort_keys=True),
                record.created_at.isoformat(),
            ),
        )

    def build(
        self,
        chain: AuditChain,
        stage,
        outcome,
        *,
        principal=None,
        session_id=None,
        task_id=None,
        step_id=None,
        action=None,
        policy_version=None,
        permission_level=None,
        decision_id=None,
        challenge_id=None,
        authorization_id=None,
        authentication_method=None,
        reason_codes=None,
        error_code=None,
        detail=None,
    ) -> AuditRecord:
        # Correlate all kernel stages to the current durable task. Callers that
        # already supply explicit scope keep it. No model reasoning is stored.
        from lifecycle.service import active_scope
        scope = active_scope.get()
        if scope and scope.record:
            task_id = task_id or scope.record.task.task_id
            binding = getattr(scope.session, 'lifecycle_binding', None)
            task = scope.session.current_task
            if binding and binding.task_id == scope.record.task.task_id:
                step_id = step_id or binding.step_id
            elif task and task.task_id == scope.record.task.task_id:
                step_id = step_id or task.current_step_id
            elif scope.record.mode == 'single':
                step_id = step_id or 'operation'
        return AuditRecord(
            record_id=f"ar_{secrets.token_hex(10)}",
            request_id=chain.request_id,
            sequence=chain.next_sequence(),
            stage=stage,
            outcome=outcome,
            principal_id=getattr(principal, "principal_id", None),
            user_id=(
                int(principal.user_id)
                if principal is not None and getattr(principal, "user_id", None) is not None
                else None
            ),
            session_id=session_id,
            task_id=task_id,
            step_id=step_id,
            action=action,
            policy_version=policy_version,
            permission_level=permission_level,
            decision_id=decision_id,
            challenge_id=challenge_id,
            authorization_id=authorization_id,
            authentication_method=authentication_method,
            reason_codes=list(reason_codes or []),
            error_code=error_code,
            detail=sanitize_detail(detail),
            created_at=self._now(),
        )

    def record(self, chain: AuditChain, stage, outcome, *, connection=None, **fields):
        """Write one audit record.

        When ``connection`` is provided the record joins the caller's open
        transaction, so a business change and its audit evidence commit or roll
        back together.  The key is that the record is *never* silently dropped:
        a failure raises :class:`AuditFailure` so callers can refuse the
        sensitive execution.
        """
        record = self.build(chain, stage, outcome, **fields)
        if connection is not None:
            try:
                self._insert(connection, record)
            except Exception as error:  # pragma: no cover - defensive
                raise AuditFailure(
                    "audit_write_failed", "审计写入失败，拒绝敏感执行"
                ) from error
            return record
        try:
            with write_transaction() as own:
                self._insert(own, record)
        except AuditFailure:
            raise
        except Exception as error:  # pragma: no cover - defensive
            raise AuditFailure(
                "audit_write_failed", "审计写入失败，拒绝敏感执行"
            ) from error
        return record


def record_write_audit(
    context,
    connection,
    chain,
    stage,
    outcome,
    *,
    error_code=None,
    detail=None,
):
    """Append a write-stage audit record inside the caller's transaction.

    Used by the banking services so that execution/verification evidence shares
    the transaction of the money movement itself.
    """
    if context is None or connection is None or chain is None:
        raise AuditFailure(
            "audit_context_missing",
            "缺少必需安全审计上下文，拒绝敏感执行",
        )
    recorder = getattr(context, "audit_recorder", None)
    if recorder is None:
        recorder = AuditRecorder()
    return recorder.record(
        chain,
        stage,
        outcome,
        connection=connection,
        principal=getattr(context, "principal", None),
        session_id=getattr(context, "session_id", None),
        task_id=getattr(context, "task_id", None),
        step_id=getattr(context, "step_id", None),
        action=getattr(context, "action", None),
        policy_version=getattr(context, "policy_version", None),
        permission_level=getattr(context, "permission_level", None),
        decision_id=getattr(context, "decision_id", None),
        challenge_id=getattr(context, "challenge_id", None),
        authorization_id=getattr(getattr(context, "grant", None), "authorization_id", None),
        authentication_method=getattr(context, "strong_auth_method", None),
        reason_codes=getattr(context, "reason_codes", None),
        error_code=error_code,
        detail=detail,
    )


class WriteAuditContext:
    """Lightweight view of the caller's audit context for the services."""

    __slots__ = (
        "action", "task_id", "step_id", "policy_version", "permission_level",
        "decision_id", "challenge_id", "strong_auth_method", "reason_codes",
    )

    def __init__(
        self,
        action: str,
        *,
        task_id: str | None = None,
        step_id: str | None = None,
        policy_version: str | None = None,
        permission_level: str | None = None,
        decision_id: str | None = None,
        challenge_id: str | None = None,
        strong_auth_method: str | None = None,
        reason_codes: list[str] | None = None,
    ):
        self.action = action
        self.task_id = task_id
        self.step_id = step_id
        self.policy_version = policy_version
        self.permission_level = permission_level
        self.decision_id = decision_id
        self.challenge_id = challenge_id
        self.strong_auth_method = strong_auth_method
        self.reason_codes = list(reason_codes or [])
