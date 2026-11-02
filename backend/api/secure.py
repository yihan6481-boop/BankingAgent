"""Small strict strong-auth-1.0 adapter over the existing task/execution kernel."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import Field, StrictStr, StrictInt

from agent.schemas import StrictModel
from security.schemas import Principal, ChallengePublicView, AuditRecord
from security.totp import independent_protocol
from security.service import get_security_services
from security.strong_auth import StrongAuthError
from lifecycle.models import (TaskView, CreateTaskRequest, RevisionRequest, ModifyRequest,
                              RollbackRequest, TaskConfirmRequest, ScheduleRequest, NoteRequest)
from lifecycle.routes import require_task_principal, create, perform, session_for
from lifecycle.service import public
from lifecycle.store import TaskStore, digest
from api.runtime import runtime


class PasswordRequest(StrictModel):
    password: StrictStr = Field(min_length=1, max_length=256, repr=False)


class CodeRequest(StrictModel):
    code: StrictStr = Field(pattern=r'^[0-9]{6}$', repr=False)


class VerifyRequest(CodeRequest):
    task_id: StrictStr = Field(min_length=1, max_length=64)
    revision: StrictInt = Field(ge=1)
    preview_id: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')


class FactorStatus(StrictModel):
    contract_version: Literal['strong-auth-1.0'] = 'strong-auth-1.0'
    method: Literal['totp'] = 'totp'
    bound: bool
    confirmed: bool
    # Additive recovery view: only an unconfirmed first-time enrollment may be
    # re-created after an account-password recheck. Confirmed factors are immutable.
    enrollment_pending: bool = False
    enrollment_expires_at: datetime | None = None
    enrollment_expired: bool = False
    can_reenroll: bool = False
    interval_seconds: Literal[30] = 30
    window_steps: Literal[1] = 1
    mode: Literal['demo', 'secure']


class Enrollment(StrictModel):
    contract_version: Literal['strong-auth-1.0'] = 'strong-auth-1.0'
    method: Literal['totp'] = 'totp'
    otpauth_uri: str = Field(repr=False)
    expires_at: datetime


class PolicyAction(StrictModel):
    action: str
    base_level: Literal['green', 'yellow', 'red']
    requires_confirmation: bool
    requires_strong_auth: bool
    effective_authentication: Literal['none', 'confirmation', 'mock_otp', 'totp','payment']
    escalation_rules: list[str] = Field(default_factory=list)


class StrongAuthPolicy(StrictModel):
    method: Literal['totp', 'mock_otp','payment']
    interval_seconds: int
    window_steps: int
    challenge_ttl_seconds: int
    max_attempts: int
    enrollment_ttl_minutes: int = 10


class PolicyEscalation(StrictModel):
    daily_transfer_cumulative_threshold_cents: int
    daily_limit_escalation_cents: int
    rule: Literal['服务端按可信事实重算；每笔交易以服务器预览决定'] = '服务端按可信事实重算；每笔交易以服务器预览决定'


class PolicyMatrix(StrictModel):
    """Versioned *effective* secure policy view.

    It is a read-only description of the runtime configuration. It never
    authorizes anything: the server-side preview/challenge of the concrete
    transaction remains the only decision authority.
    """
    contract_version: Literal['secure-policy-1.0'] = 'secure-policy-1.0'
    policy_version: str
    authentication_mode: Literal['demo', 'secure']
    decision_authority: Literal['server_preview'] = 'server_preview'
    strong_auth: StrongAuthPolicy
    actions: list[PolicyAction]
    escalation: PolicyEscalation


class SecureProblem(StrictModel):
    code: str
    message: str


Problem = SecureProblem


from lifecycle.views import Preview, SecureTaskResponse, task_response

# Preserve the adapter's local export while keeping the historical OpenAPI
# TaskResponse schema name stable for existing clients.
TaskResponse = SecureTaskResponse


class TaskList(StrictModel):
    contract_version: Literal['strong-auth-1.0'] = 'strong-auth-1.0'
    tasks: list[TaskView]


class ChallengeResponse(StrictModel):
    contract_version: Literal['strong-auth-1.0'] = 'strong-auth-1.0'
    challenge: ChallengePublicView


class AuditView(StrictModel):
    record_id: str
    request_id: str
    stage: str
    outcome: str
    task_id: str | None
    step_id: str | None
    action: str | None
    decision_id: str | None
    challenge_id: str | None
    authorization_id: str | None
    authentication_method: Literal['none','confirmation','mock_otp','totp','payment'] | None
    error_code: str | None
    detail: dict[str, str | int | bool | float | None]
    created_at: datetime


class AuditResponse(StrictModel):
    contract_version: Literal['strong-auth-1.0'] = 'strong-auth-1.0'
    task_id: str
    records: list[AuditView]
    truncated: bool


async def protocol(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    token = independent_protocol.set(True)
    try:
        yield
    finally:
        independent_protocol.reset(token)


router = APIRouter(prefix='/api/secure/sessions/{session_id}', tags=['independent-auth'],
                   dependencies=[Depends(protocol)], responses={409:{'model':Problem},422:{'model':Problem},401:{'model':Problem}})


@router.get('/factor', response_model=FactorStatus)
def status(session_id: str, principal: Principal = Depends(require_task_principal)):
    return get_security_services().totp.status(principal)


@router.post('/factor/enroll', response_model=Enrollment)
def enroll(session_id: str, body: PasswordRequest, principal: Principal = Depends(require_task_principal)):
    from security.payment_auth import enabled
    if enabled():raise StrongAuthError('legacy_factor_disabled','当前候选版本请在安全中心设置支付验证')
    return get_security_services().totp.enroll(principal, session_id, body.password)


@router.post('/factor/confirm', response_model=FactorStatus)
def confirm_factor(session_id: str, body: CodeRequest, principal: Principal = Depends(require_task_principal)):
    from security.payment_auth import enabled
    if enabled():raise StrongAuthError('legacy_factor_disabled','当前候选版本不再接受TOTP绑定')
    return get_security_services().totp.confirm_enrollment(principal, session_id, body.code)


@router.post('/challenges/{challenge_id}/verify', response_model=ChallengeResponse)
def verify(session_id: str, challenge_id: str, body: VerifyRequest, principal: Principal = Depends(require_task_principal)):
    from security.payment_auth import enabled
    if enabled():raise StrongAuthError('legacy_factor_disabled','旧TOTP接口不可替代当前任务的支付验证')
    with runtime.session_lock(session_id):
        view = get_security_services().totp.verify(principal, session_id, challenge_id, body.code,
            task_id=body.task_id, revision=body.revision, preview_id=body.preview_id)
        session_for(session_id).pending_challenge = view
        return ChallengeResponse(challenge=view)


@router.get('/policy-matrix', response_model=PolicyMatrix)
def policy_matrix(session_id: str, principal: Principal = Depends(require_task_principal)):
    """Effective secure policy matrix for the current runtime configuration.

    The frozen V2 permission-requirements view keeps its historical enum values
    for existing clients; secure clients must use this versioned view.
    """
    from security.policy import ACTION_POLICIES, ALWAYS_STRONG_AUTH_ACTIONS
    services = get_security_services()
    config = services.config
    secure = config.authentication_mode == 'secure'
    from security.payment_auth import enabled as payment_enabled
    from security.policy import WRITE_ACTIONS
    mobile=payment_enabled()
    strong_method = 'payment' if mobile else 'totp' if secure else 'mock_otp'
    actions = []
    for action, base in sorted(ACTION_POLICIES.items()):
        level = base.base_level
        confirmation = base.requires_confirmation
        strong = base.requires_strong_auth
        rules = []
        if action in {'get_active_subscriptions', 'get_monthly_subscription_cost'}:
            if not config.subscription_query_requires_confirmation:
                level, confirmation, strong = 'green', False, False
                rules.append('subscription_query_downgraded_by_runtime_config')
            else:
                rules.append('subscription_query_confirmation_by_config')
        if action in ALWAYS_STRONG_AUTH_ACTIONS:
            level, confirmation, strong = 'red', True, True
            rules.append('always_strong_auth')
        elif action == 'transfer_money':
            rules.append('daily_transfer_cumulative_threshold')
        elif action == 'set_daily_limit':
            rules.append('daily_limit_upper_threshold')
        elif action == 'redeem_wealth_product':
            rules.append('redemption_fixed_yellow')
        if level == 'red':
            confirmation, strong = True, True
        if not confirmation:
            strong = False
        if mobile and action in WRITE_ACTIONS:
            confirmation=strong=True;rules.append('payment_pin_and_server_tier')
        authentication = (strong_method if strong else
                          'confirmation' if confirmation else 'none')
        actions.append(PolicyAction(action=action, base_level=level,
            requires_confirmation=confirmation, requires_strong_auth=strong,
            effective_authentication=authentication, escalation_rules=rules))
    return PolicyMatrix(
        policy_version=config.policy_version+('-payment1' if mobile else ''),
        authentication_mode=config.authentication_mode,
        strong_auth=StrongAuthPolicy(
            method=strong_method,
            interval_seconds=0 if mobile else 30, window_steps=0 if mobile else 1,
            challenge_ttl_seconds=300 if mobile else config.challenge_ttl_seconds,
            max_attempts=config.otp_max_attempts),
        actions=actions,
        escalation=PolicyEscalation(
            daily_transfer_cumulative_threshold_cents=config.daily_transfer_confirm_cents,
            daily_limit_escalation_cents=config.daily_limit_escalation_cents),
    )


@router.get('/tasks', response_model=TaskList)
def list_tasks(session_id: str, principal: Principal = Depends(require_task_principal)):
    return TaskList(tasks=[public(t) for t in TaskStore().list(principal)])


@router.get('/tasks/{task_id}', response_model=TaskResponse)
def detail(session_id: str, task_id: str, principal: Principal = Depends(require_task_principal)):
    record = TaskStore().load(task_id, principal)
    # A live session may supply its current preview/challenge, but only for the
    # task it is actually bound to. A restored session never borrows another
    # task's preview; re-reading the task is the recovery read after enrollment.
    session = session_for(session_id)
    bound = session if session.lifecycle_id == task_id else None
    if bound is None or record.status == 'failed':
        return task_response(record)
    return task_response(record, bound)


@router.post('/tasks', response_model=TaskResponse)
def create_task(session_id: str, body: CreateTaskRequest, principal: Principal = Depends(require_task_principal)):
    return create(session_id, body, principal)


@router.post('/tasks/{task_id}/confirm', response_model=TaskResponse)
def confirm_task(session_id: str, task_id: str, body: TaskConfirmRequest, principal: Principal = Depends(require_task_principal)):
    return perform(session_id, task_id, body, 'confirm', principal)


@router.post('/tasks/{task_id}/modify', response_model=TaskResponse)
def modify(session_id: str, task_id: str, body: ModifyRequest, principal: Principal = Depends(require_task_principal)):
    return perform(session_id, task_id, body, 'modify', principal)


@router.post('/tasks/{task_id}/rollback', response_model=TaskResponse)
def rollback(session_id: str, task_id: str, body: RollbackRequest, principal: Principal = Depends(require_task_principal)):
    return perform(session_id, task_id, body, 'rollback', principal)


@router.post('/tasks/{task_id}/schedule', response_model=TaskResponse)
def schedule(session_id: str, task_id: str, body: ScheduleRequest, principal: Principal = Depends(require_task_principal)):
    return perform(session_id, task_id, body, 'schedule', principal)


@router.post('/tasks/{task_id}/note', response_model=TaskResponse)
def note(session_id: str, task_id: str, body: NoteRequest, principal: Principal = Depends(require_task_principal)):
    return perform(session_id, task_id, body, 'note', principal)


def revision_endpoint(action):
    def endpoint(session_id: str, task_id: str, body: RevisionRequest, principal: Principal = Depends(require_task_principal)):
        return perform(session_id, task_id, body, action, principal)
    endpoint.__name__ = 'secure_' + action
    return endpoint


for action in ['pause','cancel','takeover','return','resume']:
    router.add_api_route('/tasks/{task_id}/'+action, revision_endpoint(action), methods=['POST'], response_model=TaskResponse)


@router.get('/tasks/{task_id}/audit', response_model=AuditResponse)
def audit(session_id: str, task_id: str, principal: Principal = Depends(require_task_principal)):
    from security.db import read_connection
    import json
    TaskStore().load(task_id, principal)
    with read_connection() as db:
        rows = db.execute('SELECT * FROM security_audit_records WHERE task_id=? AND user_id=? '
            'AND principal_id=? ORDER BY rowid LIMIT 1001', (task_id,principal.user_id,principal.principal_id)).fetchall()
    fields = AuditView.model_fields
    records = []
    for row in rows[:1000]:
        item = dict(row)
        item['detail'] = json.loads(item['detail_json'])
        # Session identifiers are bearer credentials in V2; never export them.
        records.append(AuditView(**{k:item[k] for k in fields}))
    return AuditResponse(task_id=task_id,records=records,truncated=len(rows)>1000)
