"""Minimal transient adapter required by Workflow and the confirmation kernel.

No transcript, NL requirements, selected chat objects or query history. Fields
are execution bindings, previews, challenges and workflow/clarification state.
R4 can retire clarification state once all six structured capabilities adapt.
"""
from dataclasses import dataclass, field
from datetime import datetime
from agent.schemas import ConstraintDraft, PendingAction, TaskState

@dataclass
class ExecutionContext:
    pending_clarification: object | None = None
    pending_action: PendingAction | None = None
    current_task: TaskState | None = None
    # V0.7.0：服务器端身份。身份只能来自后端验证的凭据，不能来自请求正文。
    principal: object | None = None
    principal_id: str | None = None
    user_id: int | None = None
    session_id: str | None = None
    # V0.7.0：等待模拟强验证的挑战；不包含验证码本身。
    pending_challenge: object | None = None
    audit_chain: object | None = None
    # 同一次业务请求在"预览 → 确认/验证 → 执行"之间必须复用同一个幂等键，
    # 否则授权/挑战绑定的参数摘要会漂移。
    request_idempotency_key: str | None = None
    request_signature: str | None = None
    # FIX2: immutable server-side preview binding.  ``request_signature`` is
    # kept for the existing chat key-reuse path; execution trusts the
    # normalized action digest/arguments below, never a caller-shaped body.
    request_action: str | None = None
    request_action_digest: str | None = None
    request_arguments: dict | None = None
    request_expires_at: datetime | None = None
    # Protocol is server-assigned by AgentRuntime, never accepted from chat text.
    protocol: str = "v1"
    pending_revision: int = 0
    issued_confirmation_phrases: set[str] = field(default_factory=set)
    # V0.8.1：废止旧 PendingAction 后仍需继承的公开参数与金额上限约束。
    # 只绑定对应的操作/步骤；取消、新任务、新操作或执行成功时清除。
    constraint_draft: ConstraintDraft | None = None
    lifecycle_id: str | None = None
    lifecycle_revision: int | None = None
    lifecycle_requested: dict | None = None
    lifecycle_binding: object | None = None
    # Evidence association is independent of the live pending task.
    reply_lifecycle_id: str | None = None

    def reserve_confirmation_phrase(self, candidate: str) -> str:
        """Keep the existing six-digit protocol unique for this session.

        Session locks serialize issuance. Cancellation must not erase history:
        delayed old confirmations must never acquire a new operation identity.
        """
        prefix, number = candidate.rsplit(" ", 1)
        start = int(number)
        for offset in range(1_000_000):
            phrase = f"{prefix} {(start + offset) % 1_000_000:06d}"
            if phrase not in self.issued_confirmation_phrases:
                if self.protocol == 'v2' and self.principal_id:
                    import hashlib
                    from lifecycle.store import db
                    with db() as connection:
                        inserted = connection.execute(
                            'INSERT OR IGNORE INTO lifecycle_confirmation_hashes VALUES(?,?)',
                            (self.principal_id, hashlib.sha256(phrase.encode()).hexdigest()))
                        if inserted.rowcount != 1:
                            continue
                self.issued_confirmation_phrases.add(phrase)
                return phrase
        raise RuntimeError("Confirmation phrases exhausted; create a new session")

    def clear_pending(self):
        self.lifecycle_binding = None
        self.pending_clarification = None
        self.pending_action = None
        self.pending_challenge = None
        self.constraint_draft = None
        self.request_idempotency_key = None
        self.request_signature = None
        self.request_action = None
        self.request_action_digest = None
        self.request_arguments = None
        self.request_expires_at = None


from contextlib import contextmanager
from dataclasses import fields
@contextmanager
def execution_adapter(source):
    if type(source) is ExecutionContext:
        yield source
        return
    names=[f.name for f in fields(ExecutionContext)]
    target=ExecutionContext(**{n:getattr(source,n) for n in names})
    try: yield target
    finally:
        for name in names: setattr(source,name,getattr(target,name))
