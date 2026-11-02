"""Public execution projection, with no HTTP dependencies."""
from datetime import datetime
from typing import Literal
from agent.schemas import StrictModel
from security.schemas import ChallengePublicView
from security.service import get_security_services
from .models import TaskView
from .service import public
from .store import digest

class Preview(StrictModel):
    preview_id: str
    step_id: str
    operation_id: str
    action: str
    execution_digest: str
    generation: int
    issued_revision: int
    permission_level: Literal['green','yellow','red']
    required_authentication: Literal['confirmation','totp','payment']
    confirmation_phrase: str | None
    confirmation_message: str
    requested_arguments: dict[str, str | int | bool | float | None]
    expires_at: datetime


class SecureTaskResponse(StrictModel):
    contract_version: Literal['strong-auth-1.0'] = 'strong-auth-1.0'
    task: TaskView
    preview: Preview | None = None
    challenge: ChallengePublicView | None = None


def task_response(record, session=None):
    from security.payment_auth import enabled as payment_enabled
    if session and record.status == 'failed':
        from lifecycle.store import TaskError
        step = next((s for s in record.task.steps if s.status == 'failed'), None)
        raise TaskError(step.error_code if step and step.error_code else 'operation_failed')
    pending = session.pending_action if session else None
    binding = pending.lifecycle_binding if pending else None
    preview = None
    if pending and binding:
        preview = Preview(preview_id=digest(binding.model_dump(mode='json')),
            step_id=binding.step_id, operation_id=binding.operation_id, action=binding.action,
            execution_digest=binding.execution_digest, generation=binding.generation,
            issued_revision=binding.issued_revision, permission_level=pending.permission_level,
            required_authentication=('payment' if payment_enabled() else 'totp') if pending.requires_strong_auth else 'confirmation',
            confirmation_phrase=pending.confirmation_phrase, confirmation_message=pending.confirmation_message,
            requested_arguments=pending.requested_arguments, expires_at=pending.expires_at)
    challenge = get_security_services().strong_auth.load_view(session.pending_challenge.challenge_id) if session and session.pending_challenge else None
    return SecureTaskResponse(task=public(record), preview=preview, challenge=challenge).model_dump(mode='json')


