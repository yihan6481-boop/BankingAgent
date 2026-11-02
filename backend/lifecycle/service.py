"""Lifecycle orchestration over the existing Workflow and security gateway."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agent.constraints import check_amount, merge_amount_constraints
from agent.schemas import (TaskState, TaskStepState, TaskPlanProposal, PlannedStep,
                           FinancialIntentContract, ToolResult, TaskPreviewBinding)
from agent.tool_registry import build_default_registry, ToolRisk
from agent.tool_router import ToolRouter
from agent.workflow import WorkflowEngine, PlanValidator, TOOL_DOMAINS
from database import get_connection
from security.execution import AuthorizationError
from .models import DurableTask, Operation
from .store import TaskError, TaskStore, digest, now, db


@dataclass
class Scope:
    session: object
    record: DurableTask | None = None
    token: str | None = None
    due: bool = False
    due_wait: bool = False
    recovering: bool = False


active_scope = ContextVar('lifecycle_scope', default=None)
# Test-only injection. Never selected by HTTP input or environment variables.
fault_hook = None


def fault(point):
    if fault_hook:
        fault_hook(point)


def engine():
    tools = registry()
    from storage.config import pg02_enabled
    if pg02_enabled():
        from security.service import get_security_services
        return WorkflowEngine(tools,ToolRouter(tools),now_provider=get_security_services().now_provider)
    return WorkflowEngine(tools, ToolRouter(tools))


def registry():
    # V2 has always accepted either subscription name or public subscription ID.
    # Retain that selector when representing a deterministic V2 operation as a task.
    from pydantic import Field, model_validator
    from agent.schemas import StrictModel
    class SubscriptionSelector(StrictModel):
        subscription: str | None = Field(default=None, min_length=1, max_length=100)
        subscription_id: int | None = Field(default=None, gt=0)

        @model_validator(mode='after')
        def selector(self):
            if self.subscription is None and self.subscription_id is None:
                raise ValueError('missing subscription selector')
            return self
    tools = build_default_registry(include_analysis=True)
    tools._tools['cancel_subscription'] = replace(tools.get('cancel_subscription'),
                                                 argument_model=SubscriptionSelector)
    return tools


def proposal(task):
    return TaskPlanProposal(contract=task.contract, steps=[PlannedStep(
        step_id=s.step_id, purpose=s.purpose, tool_name=s.tool_name,
        arguments=s.arguments, depends_on=s.depends_on, condition=s.condition) for s in task.steps])


def validate_task(record):
    PlanValidator(registry()).validate(proposal(record.task))
    ids = set()
    terminal = {'completed','failed','skipped','cancelled'}
    if record.task.status in {'completed','failed','cancelled','expired'} and any(s.status not in terminal for s in record.task.steps):
        raise ValueError('terminal task with unfinished steps')
    if record.status == 'cancelled' and record.task.status != 'cancelled':
        raise ValueError('cancellation mismatch')
    for step in record.task.steps:
        op = record.operations[step.step_id]
        if op.arguments_hash != digest({'tool': step.tool_name, 'arguments': step.arguments}):
            raise ValueError('operation arguments mismatch')
        if op.operation_id in ids:
            raise ValueError('duplicate operation identity')
        ids.add(op.operation_id)
        if step.status == 'completed':
            if not step.result or not step.result.success or step.verification_status != 'passed':
                raise ValueError('unverified completion')
            from agent.output_guard import contains_sensitive_result
            retained = step.result.model_dump(mode='json')
            data = retained.pop('data', None)
            # Only schema-validated registered analysis data gets its exact hash
            # paths. The envelope and free text retain the ordinary scanner.
            if (contains_sensitive_result(data, tool_name=step.tool_name) or
                    contains_sensitive_result(retained)):
                raise ValueError('unsafe result')
            from analysis_service import CATALOG, verify_result
            if step.tool_name in CATALOG and not verify_result(step.result)[0]:
                raise ValueError('invalid retained analysis facts')
            if registry().get(step.tool_name).risk == ToolRisk.WRITE:
                with db() as connection:
                    receipt = connection.execute('SELECT * FROM lifecycle_receipts WHERE operation_id=?',
                                                 (op.operation_id,)).fetchone()
                if (receipt is None or receipt['task_id'] != record.task.task_id or
                    receipt['owner_id'] != record.owner_id or receipt['business_key'] != op.key or
                    receipt['arguments_hash'] != op.arguments_hash or receipt['action'] != step.tool_name):
                    raise ValueError('completed write without atomic evidence')
                if op.execution_digest is not None and receipt['execution_digest'] != op.execution_digest:
                    raise ValueError('completed execution digest mismatch')
                result = json.loads(receipt['result'])
                if result.get('data') != step.result.data:
                    raise ValueError('completed result inconsistent with receipt')
        if step.status not in {'completed', 'skipped'} and step.result is not None:
            raise ValueError('unfinished step stores preview/result')
        if check_amount(step.arguments, step.constraints):
            raise ValueError('invalid amount constraint')
    if record.task.current_step_id is not None and record.task.current_step_id not in record.operations:
        raise ValueError('invalid current step')


def new_operation(step, key=None):
    return Operation(operation_id=uuid4().hex, key=key or uuid4().hex,
                     arguments_hash=digest({'tool': step.tool_name, 'arguments': step.arguments}))


def clean_task(task):
    # Previews and messages can carry confirmation text; persist only verified results.
    payload = task.model_dump(mode='json')
    payload['failure_reason'] = None
    for step in payload['steps']:
        step['message'] = None
        if step['status'] != 'completed':
            step['result'] = None
    return TaskState.model_validate(payload)


def bind(session, record):
    session.lifecycle_id = record.task.task_id
    session.reply_lifecycle_id = record.task.task_id
    session.lifecycle_revision = record.revision


def save(scope):
    scope.record.task.updated_at = now()
    scope.record = TaskStore().save(scope.record, token=scope.token)
    bind(scope.session, scope.record)


def issue_preview_binding(record, session, step_id, *, creating=False):
    """Bind an already issued preview, without replacing its grant or arguments."""
    pending = session.pending_action
    if pending and pending.lifecycle_binding is not None:
        return
    if not pending and session.lifecycle_binding is not None:
        return
    action = pending.tool_name if pending else session.request_action
    execution_digest = pending.action_digest if pending else session.request_action_digest
    if not action or not execution_digest:
        return
    step = next(s for s in record.task.steps if s.step_id == step_id)
    op = record.operations[step_id]
    from security.runners import ActionPlanner, NEEDS_IDEMPOTENCY_KEY
    # Public selectors are resolved by the same planner/runner as execution.
    # This validates the binding; it does not issue a new authorization.
    plan = ActionPlanner().prepare(step.tool_name, step.arguments, session.principal,
                                   idempotency_key=op.key if action in NEEDS_IDEMPOTENCY_KEY else None)
    if (action != step.tool_name or not plan.decision or
            plan.decision.action_digest != execution_digest):
        raise TaskError('preview_operation_mismatch')
    actual_key = (pending.execution_arguments.get('idempotency_key') if pending else
                  session.request_idempotency_key) if action in NEEDS_IDEMPOTENCY_KEY else None
    if action in NEEDS_IDEMPOTENCY_KEY and actual_key != op.key:
        raise TaskError('preview_operation_mismatch')
    op.preview_generation += 1
    op.execution_digest = execution_digest
    # Internal saves may advance CAS revision; they never change this issuance revision.
    op.preview_revision = record.revision if creating else record.revision + 1
    binding = TaskPreviewBinding(task_id=record.task.task_id, step_id=step_id,
        operation_id=op.operation_id, issued_revision=op.preview_revision,
        generation=op.preview_generation, action=action,
        execution_digest=execution_digest, business_key=actual_key)
    if pending:
        pending.lifecycle_binding = binding
    session.lifecycle_binding = binding
    if session.pending_challenge and session.pending_challenge.method in {'totp','payment'}:
        from security.service import get_security_services
        get_security_services().totp.bind_preview(session.pending_challenge.challenge_id, binding)
    from security.audit import AuditRecorder, AuditChain
    AuditRecorder().record(AuditChain(), 'preview', 'pending', principal=session.principal,
        session_id=session.session_id, task_id=binding.task_id, step_id=binding.step_id,
        action=binding.action, challenge_id=session.pending_challenge.challenge_id if session.pending_challenge else None,
        detail={'operation_id':binding.operation_id, 'execution_digest':binding.execution_digest,
                'generation':binding.generation, 'revision':binding.issued_revision})


def matches_preview(record, binding):
    if binding is None or binding.task_id != record.task.task_id:
        return False
    op = record.operations.get(binding.step_id)
    step = next((s for s in record.task.steps if s.step_id == binding.step_id), None)
    from security.runners import NEEDS_IDEMPOTENCY_KEY
    return bool(op and step and step.status not in {'completed', 'cancelled', 'skipped', 'failed'}
        and op.state in {'ready', 'inflight'} and op.operation_id == binding.operation_id
        and op.preview_generation == binding.generation
        and op.preview_revision == binding.issued_revision
        and op.execution_digest == binding.execution_digest and step.tool_name == binding.action
        and (record.task.current_step_id in {None, binding.step_id})
        and (record.mode == 'single' or record.task.status == 'waiting_confirmation'
             and record.task.current_step_id == binding.step_id and step.status == 'waiting_confirmation')
        and (binding.business_key == op.key if step.tool_name in NEEDS_IDEMPOTENCY_KEY
             else binding.business_key is None))


def validate_preview(session, pending=None):
    scope = active_scope.get()
    if not scope:
        return
    if not scope.record:
        # Terminal tasks deliberately have no executable lease. A stale session
        # must not turn its durable preview into an unmanaged V2 write.
        if session.lifecycle_binding is not None or pending and pending.lifecycle_binding is not None:
            raise TaskError('stale_preview', '任务执行绑定已失效，请读取任务状态')
        return
    binding = pending.lifecycle_binding if pending else session.lifecycle_binding
    if (scope.record.status not in {'active', 'waiting'} or
            session.lifecycle_id != scope.record.task.task_id or
            not matches_preview(scope.record, binding)):
        raise TaskError('stale_preview', '任务或操作已变化，请恢复任务并使用新的预览确认')
    if pending and (pending.tool_name != binding.action or pending.action_digest != binding.execution_digest
                    or pending.execution_arguments.get('idempotency_key') != binding.business_key):
        raise TaskError('stale_preview')
    if scope.record.mode == 'workflow':
        task = session.current_task
        waiting = [s for s in task.steps if s.status == 'waiting_confirmation'] if task else []
        if (task is None or task.task_id != binding.task_id or task.status != 'waiting_confirmation'
            or task.current_step_id != binding.step_id or len(waiting) != 1
            or waiting[0].step_id != binding.step_id or waiting[0].tool_name != binding.action):
            raise TaskError('no_pending_interaction')
    session.lifecycle_binding = binding


def revoke_previews(record):
    for step in record.task.steps:
        if step.status != 'completed':
            op = record.operations[step.step_id]
            op.preview_generation += 1
            op.execution_digest = None
            op.preview_revision = None


def capture_workflow(session):
    scope = active_scope.get()
    if scope is None or session.protocol != 'v2':
        return
    task = clean_task(session.current_task)
    if scope.record is None or scope.record.task.task_id != task.task_id:
        from agent.card_scope import scopes_for
        record = DurableTask(owner_id=session.user_id, principal_id=session.principal_id,
            origin_session_hash=digest(session.session_id),
            card_read_scopes=scopes_for(session,task),
            revision=1, mode='workflow', task=task,
            operations={s.step_id: new_operation(s) for s in task.steps})
        TaskStore().save(record, create=True)
        scope.record, scope.token = TaskStore().acquire(task.task_id, session.principal, 1)
    else:
        for step in task.steps:
            old = scope.record.operations[step.step_id]
            if step.status == 'completed' and registry().get(step.tool_name).risk == ToolRisk.WRITE:
                # validate_task below proves the immutable atomic receipt before
                # this state is persisted; do not leave known commits inflight.
                old.state = 'committed'
            if old.arguments_hash != digest({'tool': step.tool_name, 'arguments': step.arguments}):
                if old.state in {'inflight', 'unknown', 'committed'}:
                    raise TaskError('reconciliation_required')
                scope.record.retired_operations.append(old)
                scope.record.operations[step.step_id] = new_operation(step)
        scope.record.task = task
    scope.record.status = {'completed': 'completed', 'cancelled': 'cancelled',
        'failed': 'failed', 'expired': 'failed', 'waiting_confirmation': 'waiting',
        'waiting_clarification': 'waiting'}.get(task.status, 'active')
    if scope.due_wait:
        scope.record.status = 'waiting'
    from storage.config import pg02_enabled as enabled
    if enabled() and any(op.state=='unknown' for op in scope.record.operations.values()):
        scope.record.status='manual'
        scope.record.reason='提交状态未知，需要核查原请求键的PG回执'
    if task.current_step_id and task.status == 'waiting_confirmation':
        issue_preview_binding(scope.record, session, task.current_step_id)
    save(scope)


def operation_key(session, tool):
    scope = active_scope.get()
    if scope and scope.record and session.current_task:
        step_id = session.current_task.current_step_id
        if step_id and scope.record.task.task_id == session.current_task.task_id:
            step = next(s for s in scope.record.task.steps if s.step_id == step_id)
            if step.tool_name == tool:
                current = next(s for s in session.current_task.steps if s.step_id == step_id)
                old = scope.record.operations[step_id]
                if old.arguments_hash != digest({'tool': current.tool_name, 'arguments': current.arguments}):
                    if old.state != 'ready':
                        raise TaskError('reconciliation_required')
                    # Rotate before issuing the modified preview so its grant,
                    # Challenge and durable binding all use the same new key.
                    scope.record.retired_operations.append(old)
                    scope.record.operations[step_id] = new_operation(current)
                return scope.record.operations[step_id].key
    return None


def capture_single(session):
    if session.protocol != 'v2':
        return
    pending = session.pending_action
    action = pending.tool_name if pending else session.request_action
    if not action or not (pending or session.pending_challenge):
        return
    requested = dict(pending.requested_arguments) if pending else dict(session.lifecycle_requested or {})
    if not requested:
        return
    constraints = list(pending.constraints) if pending else list(
        session.constraint_draft.constraints if session.constraint_draft else [])
    current = now()
    step = TaskStepState(step_id='operation', tool_name=action, purpose='单操作待办',
                         arguments=requested, depends_on=[], constraints=constraints)
    task = TaskState(task_id=str(uuid4()), contract=FinancialIntentContract(
        contract_version='1.0', goal='单操作待办', domains=[TOOL_DOMAINS[action]],
        requested_tools=[action]), steps=[step], created_at=current, updated_at=current)
    key = (pending.execution_arguments.get('idempotency_key') if pending else
           session.request_idempotency_key)
    record = DurableTask(owner_id=session.user_id, principal_id=session.principal_id,
        origin_session_hash=digest(session.session_id), revision=1, mode='single', status='waiting', task=task,
        operations={'operation': new_operation(step, key)})
    issue_preview_binding(record, session, 'operation', creating=True)
    TaskStore().save(record, create=True)
    bind(session, record)


@contextmanager
def session_operation(session, *, fresh=False):
    if active_scope.get() is not None or session.protocol != 'v2':
        yield
        return
    scope = Scope(session)
    ctx = active_scope.set(scope)
    try:
        if fresh:
            session.current_task = None
            session.lifecycle_binding = None
        task_id = getattr(session, 'lifecycle_id', None)
        if task_id:
            record = TaskStore().load(task_id, session.principal)
            if not fresh and record.status not in {'completed', 'cancelled', 'failed'}:
                scope.record, scope.token = TaskStore().acquire(
                    task_id, session.principal, session.lifecycle_revision)
                if record.status in {'paused', 'manual', 'scheduled'}:
                    raise TaskError('task_blocked')
            elif fresh:
                # A fresh preview is its own FIX3 operation identity; never borrow a previous key.
                session.lifecycle_id = None
        yield
        if scope.record:
            if session.current_task and session.current_task.task_id == scope.record.task.task_id:
                capture_workflow(session)
            else:
                reconcile(scope.record)
                pending = session.pending_action
                if pending and scope.record.mode == 'single' and scope.record.status != 'manual':
                    step = scope.record.task.steps[0]
                    arguments = dict(pending.requested_arguments)
                    changed = digest({'tool': pending.tool_name, 'arguments': arguments}) != scope.record.operations[step.step_id].arguments_hash
                    actual_key = pending.execution_arguments.get('idempotency_key')
                    # FIX3 issues a fresh identity/key even for an Agent constraint-only modification.
                    changed = changed or actual_key is not None and actual_key != scope.record.operations[step.step_id].key
                    if changed:
                        old = scope.record.operations[step.step_id]
                        if old.state != 'ready':
                            raise TaskError('reconciliation_required')
                        scope.record.retired_operations.append(old)
                        step.tool_name, step.arguments = pending.tool_name, arguments
                        scope.record.operations[step.step_id] = new_operation(step, pending.execution_arguments.get('idempotency_key'))
                    step.constraints = list(pending.constraints)
                if all(s.status == 'completed' for s in scope.record.task.steps):
                    scope.record.status = 'completed'
                    scope.record.task.status = 'completed'
                elif pending:
                    issue_preview_binding(scope.record, session, 'operation')
                save(scope)
        elif session.current_task is None or session.current_task.status in {'completed','cancelled','failed','expired'}:
            capture_single(session)
    finally:
        if scope.record and scope.token:
            TaskStore().release(scope.record.task.task_id, scope.token)
        active_scope.reset(ctx)


def executing_step(scope):
    task = scope.session.current_task
    step_id = task.current_step_id if task and task.task_id == scope.record.task.task_id else 'operation'
    return step_id, scope.record.operations[step_id]


def prepare_write(context, arguments):
    scope = active_scope.get()
    if not scope:
        return
    validate_preview(scope.session, scope.session.pending_action)
    if not scope.record:
        return
    binding = scope.session.lifecycle_binding
    from security.schemas import compute_action_digest
    if (context.action != binding.action or
        compute_action_digest(context.action, arguments) != binding.execution_digest or
        context.grant.action_digest != binding.execution_digest or
        (binding.business_key is not None and context.grant.idempotency_key != binding.business_key)):
        raise AuthorizationError('stale_preview', '实际执行参数与任务预览绑定不一致')
    context.lifecycle_binding = binding
    op = scope.record.operations[binding.step_id]
    if op.state in {'unknown', 'committed'}:
        raise TaskError('reconciliation_required')
    op.state = 'inflight'
    save(scope)
    fault('before_business')


def assert_transaction(connection, context, *, action=None, action_digest=None):
    scope = active_scope.get()
    if not scope or not scope.record:
        if context.lifecycle_binding is not None:
            raise AuthorizationError('stale_preview', '任务执行绑定缺少有效任务租约')
        return
    if hasattr(connection,'lock_task'):
        connection.lock_task(scope.record.task.task_id)
    row = connection.execute('SELECT * FROM lifecycle_tasks WHERE task_id=?',
                             (scope.record.task.task_id,)).fetchone()
    if (row is None or row['status'] not in {'active','waiting'} or
        row['revision'] != scope.record.revision or row['lease_token'] != scope.token or
        row['lease_until'] <= now().isoformat() or row['owner_id'] != context.principal.user_id):
        raise AuthorizationError('stale_preview', '任务状态或租约已变化，拒绝执行')
    try:
        current = TaskStore()._decode(connection, row, context.principal)
    except TaskError as exc:
        raise AuthorizationError('stale_preview', '任务持久化状态不合法') from exc
    binding = context.lifecycle_binding
    if (not matches_preview(current, binding) or context.session_id != scope.session.session_id
        or context.action != binding.action or context.grant.action_digest != binding.execution_digest
        or (action is not None and action != binding.action)
        or (action_digest is not None and action_digest != binding.execution_digest)
        or (binding.business_key is not None and context.grant.idempotency_key != binding.business_key)):
        raise AuthorizationError('stale_preview', '实际授权、操作或执行摘要已变化')
    step = next(s for s in current.task.steps if s.step_id == binding.step_id)
    if check_amount(step.arguments, step.constraints):
        raise AuthorizationError('authorization_required', '金额约束冲突')


def record_commit(connection, context, result):
    scope = active_scope.get()
    if not scope or not scope.record:
        assert_transaction(connection, context)
        from storage.config import pg02_enabled
        if pg02_enabled() and context.action in {'report_card_lost','set_daily_limit','cancel_subscription'}:
            raise AuthorizationError('stale_preview','PG变更必须绑定持久任务及同事务回执')
        return
    fault('before_commit')
    assert_transaction(connection, context)
    binding = context.lifecycle_binding
    op = scope.record.operations[binding.step_id]
    if binding.business_key is not None:
        table = 'transfer_orders' if binding.action == 'transfer_money' else 'wealth_orders'
        id_field = 'transfer_id' if table == 'transfer_orders' else 'order_id'
        order = connection.execute(f'SELECT * FROM {table} WHERE idempotency_key=? AND owner_id=?',
            (binding.business_key, context.principal.user_id)).fetchone()
        if order is None or order[id_field] != (result.get('data') or {}).get(id_field):
            raise AuthorizationError('stale_preview', '业务订单与实际执行操作不一致')
        from security.args import transfer_args, wealth_purchase_args, wealth_redemption_args
        from security.schemas import compute_action_digest
        if binding.action == 'transfer_money':
            actual = transfer_args(order['from_card_id'], order['recipient_id'], order['amount_cents'],
                                   order['idempotency_key'], order['remark'])
        elif binding.action == 'buy_wealth_product':
            if order['order_type'] != 'BUY':
                raise AuthorizationError('stale_preview', '订单类型不一致')
            actual = wealth_purchase_args(order['product_id'], order['card_id'], order['amount_cents'],
                                          order['idempotency_key'])
        else:
            if order['order_type'] != 'REDEEM':
                raise AuthorizationError('stale_preview', '订单类型不一致')
            actual = wealth_redemption_args(order['holding_id'], order['amount_cents'], order['idempotency_key'])
        if compute_action_digest(binding.action, actual) != binding.execution_digest:
            raise AuthorizationError('stale_preview', '订单参数与实际执行操作不一致')
    if WorkflowEngine._contains_sensitive_result(result):
        raise AuthorizationError('authorization_required', '执行回执包含敏感字段')
    from storage.config import pg02_enabled
    if pg02_enabled() and binding.action in {'report_card_lost','set_daily_limit','cancel_subscription'}:
        connection.execute('''INSERT INTO mutation_orders(operation_id,business_key,action,authorization_id,execution_digest,result)
            VALUES(?,?,?,?,?,?)''',(binding.operation_id,binding.business_key or op.key,binding.action,
            context.grant.authorization_id,binding.execution_digest,json.dumps(result,ensure_ascii=False)))
    connection.execute('INSERT INTO lifecycle_receipts '
        '(operation_id,task_id,owner_id,action,arguments_hash,business_key,result,committed_at,execution_digest) '
        'VALUES(?,?,?,?,?,?,?,?,?)',
        (binding.operation_id, binding.task_id, context.principal.user_id,
         binding.action, op.arguments_hash, binding.business_key or op.key, json.dumps(result, ensure_ascii=False),
         now().isoformat(), binding.execution_digest))


def after_business():
    if active_scope.get() and active_scope.get().record:
        fault('after_business')


def complete_single(scope):
    # Call only after reconciliation proved the atomic receipt and business ID.
    if scope.record.task.steps[0].status != 'completed':
        raise TaskError('reconciliation_required')
    scope.record.status = scope.record.task.status = 'completed'
    scope.record.task.current_step_id = None
    scope.record.task.completed_at = now()
    scope.session.clear_pending()
    fault('before_task_completion_save')
    save(scope)


def execute_challenge_preview(scope, text):
    """Use the existing deterministic executor for a challenge-only single preview."""
    from security.confirmation import _is_explicit_confirmation
    from execution.confirmed import _execute_confirmed
    from security.runners import ActionPlanner, WritePlan
    from security.service import get_security_services
    from security.policy import PolicyError
    session = scope.session
    if session.request_expires_at is None or now() >= session.request_expires_at:
        raise TaskError('pending_action_expired', '该操作预览已过期，请恢复任务重新预览')
    if not _is_explicit_confirmation(text):
        raise TaskError('confirmation_mismatch')
    # These are the ORIGINAL server-side normalized preview arguments, not a new preview.
    arguments = dict(session.request_arguments)
    action = session.request_action
    services = get_security_services()
    planner = ActionPlanner(services.config, policy=services.policy)
    try:
        decision = services.policy.evaluate(action, session.principal, planner.final_arguments(action, arguments))
    except PolicyError as exc:
        raise TaskError(exc.code, exc.message) from exc
    plan = WritePlan(action=action, permitted=True, decision=decision, preview=None,
        arguments=arguments, message='', idempotency_key=session.lifecycle_binding.business_key,
        requires_strong_auth=decision.requires_strong_auth,
        requires_confirmation=decision.requires_confirmation)
    envelope = _execute_confirmed(session.session_id, session, plan, session.principal,
                                  action=action, confirm_text=text)
    return ToolResult(status='success' if envelope.status == 'success' else 'error',
        message=envelope.message, error_code=None if envelope.status == 'success' else 'operation_failed')


def reconcile(record):
    import json
    unresolved = False
    with db() as connection:
        for step in record.task.steps:
            op = record.operations[step.step_id]
            receipt = connection.execute('SELECT * FROM lifecycle_receipts WHERE operation_id=?',
                                         (op.operation_id,)).fetchone()
            if receipt:
                if (receipt['owner_id'] != record.owner_id or receipt['task_id'] != record.task.task_id or
                    receipt['action'] != step.tool_name or receipt['arguments_hash'] != op.arguments_hash or
                    receipt['business_key'] != op.key):
                    raise TaskError('invalid_storage')
                if op.execution_digest is not None and receipt['execution_digest'] != op.execution_digest:
                    raise TaskError('invalid_storage', '执行回执摘要不一致')
                try:
                    result = json.loads(receipt['result'])
                except (ValueError, TypeError) as exc:
                    raise TaskError('invalid_storage', '执行回执损坏') from exc
                data = result.get('data') or {}
                if step.tool_name in {'transfer_money', 'buy_wealth_product', 'redeem_wealth_product'}:
                    table = 'transfer_orders' if step.tool_name == 'transfer_money' else 'wealth_orders'
                    order = connection.execute(f'SELECT * FROM {table} WHERE idempotency_key=? AND owner_id=?',
                                               (op.key, record.owner_id)).fetchone()
                    id_field = 'transfer_id' if table == 'transfer_orders' else 'order_id'
                    if order is None or order['status'] != 'completed' or order[id_field] != data.get(id_field):
                        raise TaskError('invalid_storage', '执行回执与业务订单不一致')
                if result.get('success') is not True or result.get('data', {}).get('write_verification') != 'passed':
                    op.state = 'unknown'
                else:
                    # Receipt was committed in the same SQLite transaction as the business order/state.
                    step.result = ToolResult(status='success', data=result['data'], message=result['message'])
                    step.status, step.verification_status = 'completed', 'passed'
                    op.state = 'committed'
            elif op.state == 'committed' or step.status == 'completed' and registry().get(step.tool_name).risk == ToolRisk.WRITE:
                raise TaskError('invalid_storage', '已完成写操作缺少事务回执')
            elif op.state == 'inflight':
                # All six local write tools require an atomic receipt. Absence after lease acquisition
                # proves that their SQLite transaction did not commit; unsupported provenance is unknown.
                op.state = 'ready'
            if op.state == 'unknown':
                unresolved = True
                record.status, record.reason = 'manual', '提交状态未知，需要核查服务端执行回执'
    return not unresolved


def invalidate(session):
    from security.service import get_security_services
    if session.pending_challenge:
        get_security_services().strong_auth.cancel(session.principal, session.session_id,
                                                   session.pending_challenge.challenge_id)
    if session.pending_action and session.pending_action.authorization_id:
        with db() as connection:
            connection.execute('UPDATE security_authorizations SET consumed=1 WHERE authorization_id=? AND consumed=0',
                               (session.pending_action.authorization_id,))
    session.clear_pending()


def invalidate_task(task_id):
    from execution.sessions import sessions as registry, lock
    with lock:
        sessions = [s for s in registry.values() if s.lifecycle_id == task_id]
    for session in sessions:
        invalidate(session)


def reset_unfinished(record):
    task = record.task
    for step in task.steps:
        if step.status != 'completed':
            step.status, step.result, step.error_code = 'pending', None, None
            step.verification_status = 'not_run'
            if step.condition:
                step.condition_status, step.condition_result, step.condition_error_code = 'not_evaluated', None, None
    task.status, task.current_step_id, task.completed_at = 'running', None, None


def run_remaining(scope):
    record, session = scope.record, scope.session
    invalidate_task(record.task.task_id)
    invalidate(session)
    if not reconcile(record):
        save(scope)
        return
    revoke_previews(record)
    reset_unfinished(record)
    scope.recovering = True
    record.status = 'active'
    session.current_task = record.task.model_copy(deep=True)
    session.pending_revision += 1
    save(scope)
    engine()._run(session)
    capture_workflow(session)


def fresh_condition_result(source):
    scope = active_scope.get()
    if scope is None or not scope.recovering:
        return source.result
    from security.service import get_security_services
    from agent.workflow import ConditionEvaluationError
    decision = get_security_services().policy.evaluate(source.tool_name, scope.session.principal, source.arguments)
    if decision.permission_level != 'green':
        raise ConditionEvaluationError('condition_source_unavailable', '条件来源需要用户重新授权')
    workflow = engine()
    dispatch = workflow.router.dispatch(source.tool_name, source.arguments,
                                        principal=scope.session.principal, session=scope.session)
    if not workflow.verify_tool_result(source.tool_name, dispatch.result).verified:
        raise ConditionEvaluationError('condition_source_unavailable', '无法重新核验条件来源')
    return dispatch.result


def due_boundary(session, step):
    """No worker authorization: only current-policy green reads advance unattended."""
    scope = active_scope.get()
    if scope is None or not scope.due:
        return False
    from security.service import get_security_services
    from security.policy import PolicyError
    from security.runners import ActionPlanner
    services = get_security_services()
    spec = registry().get(step.tool_name)
    if spec.risk == ToolRisk.WRITE:
        plan = ActionPlanner(services.config).prepare(step.tool_name, step.arguments, session.principal,
                                               idempotency_key=scope.record.operations[step.step_id].key)
        if not plan.permitted:
            step.status, step.error_code = 'failed', plan.error_code or 'preview_failed'
            session.current_task.status = 'failed'
            session.current_task.current_step_id = None
            scope.record.reason = '到期预览未通过，需用户检查最新业务事实'
            return True
        decision = plan.decision
    else:
        try:
            decision = services.policy.evaluate(step.tool_name, session.principal, step.arguments)
        except PolicyError:
            session.current_task.status = 'failed'
            step.status, step.error_code = 'failed', 'permission_denied'
            return True
    if decision.permission_level != 'green':
        scope.due_wait = True
        session.current_task.status, session.current_task.current_step_id = 'planned', None
        scope.record.reason = '到期已重新校验，等待用户重新确认或强验证：' + decision.permission_level
        return True
    return False


def public(record):
    value = record.model_dump(mode='json')
    value.pop('origin_session_hash',None)
    value.pop('card_read_scopes',None)
    from task_presentation import step_title
    for original,visible in zip(record.task.steps,value['task']['steps']):
        visible['purpose']=step_title(original)
    value.pop('operations')
    value.pop('retired_operations')
    value.pop('principal_id')
    value.pop('owner_id')
    return value


def transition(session, task_id, body, action, *, clock=None, due=False):
    clock = clock or now
    store = TaskStore(clock)
    record, token = store.acquire(task_id, session.principal, body.revision, due=due)
    scope = Scope(session, record, token, due=due)
    ctx = active_scope.set(scope)
    try:
        if record.status in {'completed', 'cancelled'}:
            raise TaskError('terminal_task')
        if action == 'rollback':
            # Never infer that an inflight operation did not commit during a
            # rollback. Recovery/reconciliation must resolve it first.
            if any(op.state in {'inflight','unknown'} for op in record.operations.values()):
                raise TaskError('reconciliation_required')
            steps = record.task.steps
            target = next((s for s in steps if s.step_id == body.target_step_id), None) if body.target_step_id else next(
                (s for s in steps if s.status != 'completed'), None)
            if target is None:
                raise TaskError('rollback_target_not_found')
            affected = steps[steps.index(target):]
            if any(registry().get(s.tool_name).risk == ToolRisk.WRITE and
                   (s.status == 'completed' or record.operations[s.step_id].state == 'committed') for s in affected):
                raise TaskError('committed_step_immutable')
            # Also defend against a completed dependent outside the target suffix.
            ids = {s.step_id for s in affected}
            if any(s.status == 'completed' and registry().get(s.tool_name).risk == ToolRisk.WRITE and
                   ids.intersection(s.depends_on) for s in steps):
                raise TaskError('committed_step_immutable')
            history = {s.step_id:s.result.model_dump(mode='json') for s in affected if s.result}
            revoke_previews(record)
            for step in affected:
                record.retired_operations.append(record.operations[step.step_id])
                record.operations[step.step_id] = new_operation(step)
                step.status, step.result, step.error_code = 'pending', None, None
                step.verification_status = 'not_run'
                if step.condition:
                    step.condition_status, step.condition_result, step.condition_error_code = 'not_evaluated', None, None
            record.status, record.reason, record.scheduled_at = 'paused', body.reason, None
            record.task.status, record.task.current_step_id, record.task.completed_at = 'planned', None, None
            validate_task(record)
            event = {'target':target.step_id, 'reason':body.reason, 'affected':sorted(ids), 'history':history,
                'detail':{'reason':body.reason, 'target_step':target.step_id,
                          'affected_steps':','.join(sorted(ids)), 'authorization_invalidated':True}}
            scope.record = store.save(record, token=token, rollback=event)
            invalidate_task(task_id)
            invalidate(session)
        elif action == 'confirm':
            if record.status != 'waiting' or session.lifecycle_id != task_id:
                raise TaskError('no_pending_interaction')
            pending = session.pending_action
            if not pending and (record.mode != 'single' or not session.pending_challenge
                                or not session.request_arguments or not session.lifecycle_binding):
                raise TaskError('no_pending_interaction')
            validate_preview(session, pending)
            if session.pending_challenge and session.pending_challenge.method == 'totp':
                from security.service import get_security_services
                view = get_security_services().strong_auth.load_view(session.pending_challenge.challenge_id)
                if not view or view.status != 'verified':
                    raise TaskError('challenge_not_verified')
            if record.mode == 'single' and record.operations['operation'].state == 'inflight':
                if not reconcile(scope.record):
                    save(scope)
                    raise TaskError('reconciliation_required')
                if scope.record.task.steps[0].status == 'completed':
                    complete_single(scope)
                    raise TaskError('already_committed', '业务已提交并完成核查，请读取任务详情')
            if record.mode == 'workflow':
                task = session.current_task
                waiting = next((s for s in record.task.steps if s.step_id == pending.lifecycle_binding.step_id), None)
                if (task is None or task.task_id != task_id or task.status != 'waiting_confirmation'
                    or task.current_step_id != pending.lifecycle_binding.step_id
                    or waiting is None or waiting.status != 'waiting_confirmation'):
                    raise TaskError('no_pending_interaction')
            if pending:
                from security.confirmation import _is_explicit_confirmation
                if not (engine().router.matches_dynamic_confirmation(pending, body.text)
                        if pending.confirmation_phrase else _is_explicit_confirmation(body.text, pending)):
                    raise TaskError('confirmation_mismatch')
            fault('after_confirm_check')
            from security.audit import AuditChain, AuditRecorder
            AuditRecorder().record(AuditChain(), 'confirmation', 'ok', principal=session.principal,
                session_id=session.session_id, task_id=task_id,
                step_id=pending.lifecycle_binding.step_id if pending else 'operation',
                action=pending.tool_name if pending else session.request_action,
                challenge_id=session.pending_challenge.challenge_id if session.pending_challenge else None)
            result = (engine().router.execute_pending(pending, principal=session.principal, session=session)
                      if pending else execute_challenge_preview(scope, body.text))
            if result.error_code == 'stale_preview':
                raise TaskError('stale_preview')
            if record.mode == 'single':
                if not result.success:
                    reconcile(scope.record)
                    if scope.record.task.steps[0].status == 'completed':
                        complete_single(scope)
                        raise TaskError('already_committed', '业务已提交并完成核查，请读取任务详情')
                    save(scope)
                    raise TaskError(result.error_code or 'operation_failed')
                if not reconcile(scope.record) or scope.record.task.steps[0].status != 'completed':
                    raise TaskError('reconciliation_required')
                complete_single(scope)
            else:
                if result.error_code in {'strong_auth_challenge_issued','factor_not_bound','factor_not_confirmed',
                                         'challenge_not_verified','tool_circuit_open'}:
                    # First-time TOTP recovery: a fresh challenge was issued (or
                    # binding is still missing). The workflow step stays waiting;
                    # this is not a business failure and nothing was executed.
                    save(scope)
                    raise TaskError(result.error_code)
                engine().resume(session, result)
                capture_workflow(session)
        elif action in {'resume', 'return'}:
            if record.status == 'manual' and action != 'return':
                raise TaskError('manual_task')
            if record.status == 'scheduled' and not due:
                raise TaskError('task_blocked')
            if action == 'return' and record.status != 'manual':
                raise TaskError('invalid_transition')
            run_remaining(scope)
        else:
            reconcile(record)
            revoke_previews(record)
            invalidate_task(task_id)
            invalidate(session)
            if action == 'modify':
                if record.status == 'manual' and any(o.state == 'unknown' for o in record.operations.values()):
                    raise TaskError('reconciliation_required')
                target = next((s for s in record.task.steps if s.step_id == body.step_id), None)
                if not target or target.status == 'completed':
                    raise TaskError('completed_step_immutable')
                if record.operations[target.step_id].state != 'ready':
                    raise TaskError('reconciliation_required')
                candidate = record.task.model_copy(deep=True)
                temporary = record.model_copy(deep=True)
                temporary.task = candidate
                reset_unfinished(temporary)
                session.current_task = candidate
                # Keep target pending to reuse V0.8.1 validation without starting the next step.
                try:
                    suspended = active_scope.set(None)
                    try:
                        engine().modify_unexecuted_step(session, tool_name=target.tool_name,
                            step_id=target.step_id, changes=body.changes, constraints=body.constraints)
                    finally:
                        active_scope.reset(suspended)
                except ValueError as exc:
                    raise TaskError(getattr(exc, 'code', 'invalid_plan'), str(exc), 422) from exc
                record = scope.record
                changed = next(s for s in session.current_task.steps if s.step_id == body.step_id)
                record.task = clean_task(session.current_task)
                record.status, record.reason = 'active', None
                old = record.operations[body.step_id]
                if old.arguments_hash != digest({'tool': changed.tool_name, 'arguments': changed.arguments}):
                    record.retired_operations.append(old)
                    record.operations[body.step_id] = new_operation(changed)
            elif action == 'schedule':
                try:
                    date = datetime.fromisoformat(body.at)
                    if date.tzinfo is None:
                        raise ValueError('请提供带时区的明确时间')
                    date = date.astimezone(timezone.utc)
                    if not clock() < date <= clock() + timedelta(days=366):
                        raise ValueError('预约必须在未来366天内')
                except ValueError as exc:
                    raise TaskError('invalid_schedule', str(exc), 422) from exc
                if record.status == 'manual':
                    raise TaskError('manual_task')
                record.scheduled_at, record.status = date, 'scheduled'
            elif action == 'note':
                if record.status != 'manual':
                    raise TaskError('invalid_transition')
                record.notes.append(body.note)
            elif action in {'pause', 'cancel', 'takeover'}:
                record.status = {'pause':'paused', 'cancel':'cancelled', 'takeover':'manual'}[action]
                record.reason = body.reason or action
                if action == 'cancel':
                    for step in record.task.steps:
                        if step.status != 'completed':
                            step.status, step.result = 'cancelled', None
                    record.task.status, record.task.current_step_id = 'cancelled', None
            else:
                raise TaskError('invalid_transition')
            scope.record = record
            save(scope)
        session.current_task = scope.record.task.model_copy(deep=True)
        bind(session, scope.record)
        return scope.record
    finally:
        store.release(task_id, token)
        active_scope.reset(ctx)
