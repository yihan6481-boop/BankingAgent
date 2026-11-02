"""Bounded, deterministic task-plan validation and sequential workflow execution."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import ValidationError

from database import get_connection
from transfer_service import TRANSFER_ID_PATTERN
from wealth_service import ORDER_ID_PATTERN

from .constraints import check_amount, merge_amount_constraints
from .output_guard import contains_sensitive_result
from .schemas import (
    PendingClarification,
    PlannedStep,
    TaskPlanProposal,
    TaskState,
    TaskStepState,
    ToolResult,
)
from .tool_registry import ToolRegistry, ToolRisk


MAX_STEPS = 8
MAX_WRITE_STEPS = 3
TOOL_DOMAINS = {
    "get_monthly_cash_flow_report": "bill",
    "query_financial_records": "bill",
    "query_data": "bill",
    "get_annual_cash_flow_report": "bill",
    "get_expense_clues": "bill",
    "compare_wealth_products": "wealth",
    "explain_wealth_suitability": "wealth",
    "get_monthly_spending": "bill",
    "get_transactions_by_month": "bill",
    "get_category_spending": "bill",
    "get_cards": "card",
    "report_card_lost": "card",
    "set_daily_limit": "card",
    "get_active_subscriptions": "subscription",
    "cancel_subscription": "subscription",
    "get_transfer_recipients": "transfer",
    "transfer_money": "transfer",
    "get_risk_profile": "wealth",
    "get_wealth_products": "wealth",
    "get_wealth_holdings": "wealth",
    "buy_wealth_product": "wealth",
    "redeem_wealth_product": "wealth",
}
FORBIDDEN_ARGUMENT_KEYS = {
    "confirmed",
    "confirmation",
    "confirmation_phrase",
    "idempotency_key",
    "amount_cents",
    "execution_arguments",
    "task_status",
    "step_status",
    "verification_status",
    "result",
    "sql",
}
SQL_PATTERN = re.compile(
    r"\b(select|insert|update|delete|drop|alter|pragma|attach|detach)\b",
    re.IGNORECASE,
)
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "expired"}
TERMINAL_STEP_STATUSES = {"completed", "failed", "skipped", "cancelled"}


class PlanValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = message


class ConditionEvaluationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PlanClarificationRequired(PlanValidationError):
    """A plan step is safe except for missing required public business fields."""

    def __init__(self, step_id, tool_name, known_arguments, missing_fields):
        super().__init__(
            "missing_public_arguments",
            f"步骤 {step_id} 缺少必要公开业务字段。",
        )
        self.step_id = step_id
        self.tool_name = tool_name
        self.known_arguments = dict(known_arguments)
        self.missing_fields = list(missing_fields)


def _only_missing_required_public_fields(error, argument_model):
    """Return missing top-level public fields only when every error is `missing`."""
    errors = error.errors(include_url=False, include_input=False)
    if not errors or any(item.get("type") != "missing" for item in errors):
        return []
    missing = []
    for item in errors:
        location = item.get("loc", ())
        if len(location) != 1 or not isinstance(location[0], str):
            return []
        field_name = location[0]
        field = argument_model.model_fields.get(field_name)
        if field is None or not field.is_required() or field_name.startswith("_"):
            return []
        missing.append(field_name)
    return list(dict.fromkeys(missing))


@dataclass(frozen=True)
class VerificationOutcome:
    verified: bool
    message: str
    error_code: str | None = None


def _contains_forbidden_argument(value, key_path="arguments"):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in FORBIDDEN_ARGUMENT_KEYS or "__" in normalized:
                return f"{key_path}.{key}"
            if normalized.endswith("_sql") or normalized.startswith("sql_"):
                return f"{key_path}.{key}"
            nested = _contains_forbidden_argument(item, f"{key_path}.{key}")
            if nested:
                return nested
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            nested = _contains_forbidden_argument(item, f"{key_path}[{index}]")
            if nested:
                return nested
    elif isinstance(value, str):
        lowered = value.casefold()
        if SQL_PATTERN.search(value):
            return key_path
        if re.search(r"确认转账\s*\d{6}", value):
            return key_path
        if re.search(r"(?<!\d)\d{12,19}(?!\d)", value):
            return key_path
        if re.search(r"\b(?:sk|api)[-_][A-Za-z0-9]{12,}\b", value, re.I):
            return key_path
        if any(
            token in lowered
            for token in (
                "__import__", "eval(", "exec(", "subprocess", "powershell",
                "cmd.exe", "file://", "http://", "https://",
            )
        ):
            return key_path
    return None


class PlanValidator:
    """Validate the entire proposal before any task state or business call exists."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def validate(self, proposal) -> tuple[TaskPlanProposal, list[str]]:
        # Uploaded data is retractable and may not be copied into durable tasks.
        # The secure conversation controller executes these reads transiently.
        raw_steps=proposal.steps if isinstance(proposal,TaskPlanProposal) else proposal.get('steps') if isinstance(proposal,dict) else None
        tools=([s.tool_name if hasattr(s,'tool_name') else s.get('tool_name') if isinstance(s,dict) else None for s in raw_steps]
               if isinstance(raw_steps,list) else [])
        if 'query_uploaded_bills' in tools:
            raise PlanValidationError('billvision_read_channel_required','上传账单请使用专用只读查询或secure聊天，不建立持久银行任务。')
        try:
            plan = TaskPlanProposal.model_validate(proposal)
        except ValidationError as exc:
            locations = [tuple(item.get("loc", ())) for item in exc.errors()]
            if any("condition" in loc and "operator" in loc for loc in locations):
                raise PlanValidationError(
                    "invalid_condition_operator",
                    "条件比较操作符不合法，未执行任何银行操作。",
                ) from exc
            if any("condition" in loc and "field_path" in loc for loc in locations):
                raise PlanValidationError(
                    "invalid_condition_field",
                    "条件字段路径不合法，未执行任何银行操作。",
                ) from exc
            if any("condition" in loc and "value" in loc for loc in locations):
                raise PlanValidationError(
                    "invalid_condition_value_type",
                    "条件比较值不合法，未执行任何银行操作。",
                ) from exc
            raise PlanValidationError(
                "invalid_plan_schema", "任务计划结构不合法，未执行任何银行操作。"
            ) from exc

        for item in plan.steps:
            if item.tool_name=='query_data':
                from data_query import QueryBatch
                try:queries=QueryBatch.model_validate(item.arguments)
                except ValueError as exc:raise PlanValidationError('invalid_arguments','查询参数无效。') from exc
                if any(s.source=='uploaded' for p in queries.plans for s in [p.selection,p.comparison] if s):
                    raise PlanValidationError('retractable_query_requires_chat','上传账单请直接在聊天查询，不复制到持久任务。')
        from storage.config import pg02_enabled,PG02_SUPPORTED_TOOLS
        if pg02_enabled() and any(name not in PG02_SUPPORTED_TOOLS for name in
                [*plan.contract.requested_tools,*(s.tool_name for s in plan.steps)]):
            raise PlanValidationError('pg02_unsupported_business','PG02暂不支持计划中的业务，整条计划未执行。')
        if not 1 <= len(plan.steps) <= MAX_STEPS:
            raise PlanValidationError(
                "step_limit", f"任务步骤必须为1到{MAX_STEPS}个。"
            )
        unknown_contract_tools = [
            name for name in plan.contract.requested_tools if self.registry.get(name) is None
        ]
        if unknown_contract_tools:
            raise PlanValidationError(
                "unknown_tool", "任务合同包含未注册工具，计划已拒绝。"
            )

        step_by_id = {step.step_id: step for step in plan.steps}
        if len(step_by_id) != len(plan.steps):
            raise PlanValidationError("duplicate_step_id", "任务步骤编号必须唯一。")
        planned_tools = {step.tool_name for step in plan.steps}
        if planned_tools != set(plan.contract.requested_tools):
            raise PlanValidationError(
                "contract_tool_mismatch", "任务合同中的工具集合与步骤不一致。"
            )

        normalized_steps = []
        write_count = 0
        for step in plan.steps:
            spec = self.registry.get(step.tool_name)
            if spec is None:
                raise PlanValidationError(
                    "unknown_tool", f"步骤 {step.step_id} 使用了未注册工具。"
                )
            required_domain = TOOL_DOMAINS.get(step.tool_name)
            if required_domain not in plan.contract.domains:
                raise PlanValidationError(
                    "domain_mismatch", f"步骤 {step.step_id} 的业务领域未在合同中声明。"
                )
            if spec.risk == ToolRisk.WRITE:
                write_count += 1
            forbidden = _contains_forbidden_argument(step.arguments)
            if forbidden:
                raise PlanValidationError(
                    "forbidden_argument",
                    f"步骤 {step.step_id} 包含禁止的内部或可执行参数。",
                )
            try:
                validated = spec.argument_model.model_validate(step.arguments)
            except ValidationError as exc:
                missing_fields = _only_missing_required_public_fields(
                    exc, spec.argument_model
                )
                if missing_fields:
                    raise PlanClarificationRequired(
                        step.step_id,
                        step.tool_name,
                        step.arguments,
                        missing_fields,
                    ) from exc
                raise PlanValidationError(
                    "invalid_arguments", f"步骤 {step.step_id} 的公开工具参数不合法。"
                ) from exc
            for dependency in step.depends_on:
                if dependency not in step_by_id:
                    raise PlanValidationError(
                        "missing_dependency", f"步骤 {step.step_id} 引用了不存在的依赖。"
                    )
                if dependency == step.step_id:
                    raise PlanValidationError(
                        "self_dependency", f"步骤 {step.step_id} 不能依赖自身。"
                    )
            normalized_steps.append(
                step.model_copy(
                    update={"arguments": validated.model_dump(mode="json")}
                )
            )
        if write_count > MAX_WRITE_STEPS:
            raise PlanValidationError(
                "write_step_limit", f"一个任务最多包含{MAX_WRITE_STEPS}个写操作。"
            )

        plan = plan.model_copy(update={"steps": normalized_steps})
        order = self._topological_order(plan)
        normalized_by_id = {step.step_id: step for step in plan.steps}
        ancestors: dict[str, set[str]] = {}
        for step_id in order:
            direct = normalized_by_id[step_id].depends_on
            ancestors[step_id] = set(direct)
            for dependency in direct:
                ancestors[step_id].update(ancestors[dependency])
            condition = normalized_by_id[step_id].condition
            if condition is not None:
                if condition.source_step_id not in normalized_by_id:
                    raise PlanValidationError(
                        "invalid_condition_source",
                        f"步骤 {step_id} 的条件引用了不存在的步骤。",
                    )
                if condition.source_step_id not in ancestors[step_id]:
                    raise PlanValidationError(
                        "invalid_condition_dependency",
                        f"步骤 {step_id} 的条件只能读取依赖链中的前置步骤。",
                    )
                source_tool = normalized_by_id[condition.source_step_id].tool_name
                source_spec = self.registry.get(source_tool)
                field_contract = (
                    source_spec.condition_fields.get(condition.field_path)
                    if source_spec is not None
                    else None
                )
                if field_contract is None:
                    raise PlanValidationError(
                        "invalid_condition_field",
                        f"步骤 {step_id} 引用了来源工具未声明的条件字段。",
                    )
                if condition.operator not in field_contract.operators:
                    raise PlanValidationError(
                        "invalid_condition_operator",
                        f"步骤 {step_id} 的条件操作符不适用于该字段。",
                    )
                if not self._condition_value_matches(
                    field_contract.value_type, condition.value
                ):
                    raise PlanValidationError(
                        "invalid_condition_value_type",
                        f"步骤 {step_id} 的条件比较值类型与字段契约不兼容。",
                    )
        return plan, order

    @staticmethod
    def _condition_value_matches(value_type, value):
        if value_type == "boolean":
            return type(value) is bool
        if value_type == "integer":
            return type(value) is int
        if value_type == "number":
            return (
                not isinstance(value, bool)
                and isinstance(value, (int, float, Decimal))
                and (not isinstance(value, float) or math.isfinite(value))
            )
        if value_type == "string":
            return type(value) is str
        return False

    @staticmethod
    def _topological_order(plan: TaskPlanProposal) -> list[str]:
        positions = {step.step_id: index for index, step in enumerate(plan.steps)}
        indegree = {step.step_id: len(step.depends_on) for step in plan.steps}
        children = {step.step_id: [] for step in plan.steps}
        for step in plan.steps:
            for dependency in step.depends_on:
                children[dependency].append(step.step_id)
        ready = sorted(
            (step_id for step_id, degree in indegree.items() if degree == 0),
            key=positions.get,
        )
        order = []
        while ready:
            step_id = ready.pop(0)
            order.append(step_id)
            for child in sorted(children[step_id], key=positions.get):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort(key=positions.get)
        if len(order) != len(plan.steps):
            raise PlanValidationError("dependency_cycle", "任务步骤依赖形成了循环。")
        return order


class WorkflowEngine:
    """Execute one validated task sequentially and pause at every write step."""

    def __init__(self, registry, router, now_provider=None, task_id_provider=None):
        self.registry = registry
        self.router = router
        self.validator = PlanValidator(registry)
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.task_id_provider = task_id_provider or (lambda: str(uuid.uuid4()))

    @staticmethod
    def _session_principal(session):
        """Server-side identity for the workflow; never taken from the plan."""
        principal = getattr(session, "principal", None)
        if principal is not None:
            return principal
        from security.service import get_security_services

        principal = get_security_services().identity.legacy_demo_principal()
        try:
            session.principal = principal
            session.principal_id = principal.principal_id
            session.user_id = principal.user_id
        except Exception:
            pass
        return principal

    def _now(self):
        value = self.now_provider()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Workflow 时钟必须返回带时区的 datetime")
        return value

    def start(self, proposal, session) -> TaskState:
        if session.current_task is not None and session.current_task.status not in TERMINAL_TASK_STATUSES:
            raise PlanValidationError(
                "task_in_progress", "当前已有未完成任务，请先确认或取消当前任务。"
            )
        legacy_validation = getattr(session, 'validate_legacy_plan', None)
        if legacy_validation: legacy_validation(TaskPlanProposal.model_validate(proposal))
        plan, order = self.validator.validate(proposal)
        by_id = {step.step_id: step for step in plan.steps}
        now = self._now()
        task = TaskState(
            task_id=self._new_task_id(),
            contract=plan.contract,
            steps=[
                TaskStepState(
                    step_id=by_id[step_id].step_id,
                    tool_name=by_id[step_id].tool_name,
                    purpose=by_id[step_id].purpose,
                    arguments=by_id[step_id].arguments,
                    depends_on=by_id[step_id].depends_on,
                    condition=by_id[step_id].condition,
                    condition_status=(
                        "not_evaluated"
                        if by_id[step_id].condition is not None
                        else "not_applicable"
                    ),
                    condition_source_step_id=(
                        by_id[step_id].condition.source_step_id
                        if by_id[step_id].condition is not None
                        else None
                    ),
                    condition_field_path=(
                        by_id[step_id].condition.field_path
                        if by_id[step_id].condition is not None
                        else None
                    ),
                    condition_operator=(
                        by_id[step_id].condition.operator
                        if by_id[step_id].condition is not None
                        else None
                    ),
                )
                for step_id in order
            ],
            status="running",
            created_at=now,
            updated_at=now,
        )
        session.current_task = task
        from lifecycle.service import capture_workflow
        capture_workflow(session)
        if not self._ensure_consistent(session):
            return task
        self._event(task, None, "task_created", "running")
        try:
            self._run(session)
        except Exception:
            self.fail_safely(
                session,
                "workflow_execution_exception",
                "任务执行时发生异常，后续步骤未执行。",
            )
        self._ensure_consistent(session)
        return task

    def _new_task_id(self):
        raw = str(self.task_id_provider()).strip()
        try:
            parsed = uuid.UUID(raw)
        except (ValueError, AttributeError) as exc:
            raise ValueError("task_id_provider 必须返回 UUID") from exc
        if str(parsed) != raw.casefold():
            raise ValueError("task_id_provider 必须返回规范 UUID")
        return str(parsed)

    def _run(self, session):
        task = session.current_task
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            self._ensure_consistent(session)
            return
        if not self._ensure_consistent(session):
            return
        task.status = "running"
        while True:
            if not self._ensure_consistent(session):
                return
            step = next((item for item in task.steps if item.status == "pending"), None)
            if step is None:
                now = self._now()
                task.status = "completed"
                task.current_step_id = None
                task.updated_at = now
                task.completed_at = now
                session.pending_action = None
                if not self._ensure_consistent(session):
                    return
                self._event(task, None, "task_completed", "completed")
                self._ensure_consistent(session)
                return
            dependency_states = {
                item.step_id: item.status for item in task.steps if item.step_id in step.depends_on
            }
            if any(status in {"failed", "cancelled"} for status in dependency_states.values()):
                self._fail_task(
                    session,
                    step,
                    "dependency_failed",
                    "前置步骤未成功，后续步骤已停止。",
                )
                return
            if any(status == "skipped" for status in dependency_states.values()):
                step.status = "skipped"
                step.verification_status = "not_run"
                step.message = "前置依赖步骤已跳过，当前步骤不会执行。"
                task.current_step_id = None
                task.updated_at = self._now()
                self._event(
                    task,
                    step,
                    "step_skipped",
                    "skipped",
                    {"reason": "dependency_skipped"},
                )
                continue
            if not all(status == "completed" for status in dependency_states.values()):
                self._fail_task(
                    session,
                    step,
                    "invalid_task_state",
                    "任务依赖状态不一致，已安全停止。",
                )
                return

            if step.condition is not None:
                try:
                    condition_result = self._evaluate_condition(task, step)
                except ConditionEvaluationError as exc:
                    step.condition_status = "error"
                    step.condition_result = None
                    step.condition_error_code = exc.code
                    self._fail_task(session, step, exc.code, str(exc))
                    return
                step.condition_result = condition_result
                step.condition_status = "true" if condition_result else "false"
                if not condition_result:
                    step.status = "skipped"
                    step.verification_status = "not_run"
                    step.message = "条件不满足，步骤已跳过。"
                    task.updated_at = self._now()
                    self._event(
                        task,
                        step,
                        "step_skipped",
                        "skipped",
                        {"condition_result": False},
                    )
                    continue

            from lifecycle.service import due_boundary
            if due_boundary(session, step):
                return
            step.status = "running"
            task.current_step_id = step.step_id
            task.updated_at = self._now()
            if not self._ensure_consistent(session):
                return
            self._event(task, step, "step_started", "running")
            constraint_error = check_amount(step.arguments, step.constraints)
            if constraint_error is not None:
                # 步骤级约束在轮到该步骤时重新校验；冲突时零业务写入。
                code, message = constraint_error
                step.result = ToolResult(
                    status="error", error_code=code, message=message
                )
                self._fail_task(session, step, code, message)
                return
            dispatch = self.router.dispatch(
                step.tool_name, step.arguments,
                principal=self._session_principal(session), session=session,
            )
            if not dispatch.result.success:
                if (
                    step.tool_name == "transfer_money"
                    and dispatch.result.error_code == "recipient_ambiguous"
                ):
                    self._wait_for_step_clarification(
                        session, step, dispatch.result
                    )
                    return
                step.result = dispatch.result
                self._fail_task(
                    session,
                    step,
                    dispatch.result.error_code or "tool_failed",
                    dispatch.result.message,
                )
                return
            if dispatch.pending_action is not None:
                step.status = "waiting_confirmation"
                step.result = dispatch.result
                # 步骤级约束属于该步骤的可信状态，必须随预览进入 PendingAction。
                dispatch.pending_action.constraints = list(step.constraints)
                step.message = dispatch.pending_action.confirmation_message
                task.status = "waiting_confirmation"
                task.updated_at = self._now()
                session.pending_action = dispatch.pending_action
                if not self._ensure_consistent(session):
                    return
                self._event(task, step, "waiting_confirmation", "waiting_confirmation")
                self._ensure_consistent(session)
                return
            outcome = self.verify_tool_result(step.tool_name, dispatch.result)
            if outcome.verified and step.tool_name=='get_cards':
                from .card_scope import project
                dispatch.result=project(session,step,dispatch.result)
                outcome=self.verify_tool_result(step.tool_name,dispatch.result)
            step.result = dispatch.result
            step.message = (
                dispatch.result.message if outcome.verified else outcome.message
            )
            step.verification_status = "passed" if outcome.verified else "failed"
            if not outcome.verified:
                self._fail_task(
                    session,
                    step,
                    outcome.error_code or "verification_failed",
                    outcome.message,
                    preserve_verification=True,
                )
                return
            step.status = "completed"
            task.current_step_id = None
            task.updated_at = self._now()
            self._event(task, step, "step_completed", "completed")

    def _wait_for_step_clarification(self, session, step, result):
        """Pause an existing task without creating a PendingAction or idempotency key."""
        task = session.current_task
        now = self._now()
        step.status = "waiting_clarification"
        step.result = result
        step.message = result.message
        task.status = "waiting_clarification"
        task.current_step_id = step.step_id
        task.updated_at = now
        session.pending_action = None
        session.pending_clarification = PendingClarification(
            kind="workflow_execution",
            tool_name=step.tool_name,
            known_arguments=step.arguments,
            missing_fields=["recipient_id"],
            task_id=task.task_id,
            step_id=step.step_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.router.pending_ttl_seconds),
        )
        if not self._ensure_consistent(session):
            return
        self._event(
            task,
            step,
            "waiting_clarification",
            "waiting_clarification",
            {"reason": "recipient_ambiguous"},
        )

    def resume_clarification(self, session, supplied_arguments):
        """Resume only the paused step and allow only its declared missing fields."""
        task = session.current_task
        pending = session.pending_clarification
        if (
            task is None
            or task.status != "waiting_clarification"
            or pending is None
            or pending.kind != "workflow_execution"
        ):
            raise ValueError("当前任务没有等待补充的执行步骤")
        if self.is_clarification_expired(pending):
            self.expire(session)
            return task
        if not self._ensure_consistent(session):
            return task
        step = next(
            (item for item in task.steps if item.status == "waiting_clarification"),
            None,
        )
        if step is None or step.step_id != pending.step_id:
            raise ValueError("任务澄清状态不一致")
        if not isinstance(supplied_arguments, dict) or not supplied_arguments:
            raise ValueError("没有提供需要补充的公开字段")
        if set(supplied_arguments) - set(pending.missing_fields):
            raise ValueError("只能补充当前步骤声明缺少的公开字段")

        candidates = ((step.result.data or {}).get("candidates", [])) if step.result else []
        candidate_ids = {
            item.get("recipient_id")
            for item in candidates
            if isinstance(item, dict) and isinstance(item.get("recipient_id"), int)
        }
        recipient_id = supplied_arguments.get("recipient_id")
        if recipient_id not in candidate_ids:
            raise ValueError("请选择候选列表中的收款人编号")
        selection_error = self.recipient_selection_error(
            recipient_id, step.arguments, candidate_ids
        )
        if selection_error is not None:
            raise ValueError(selection_error)

        spec = self.registry.get(step.tool_name)
        if spec is None:
            self._fail_task(session, step, "unknown_tool", "当前步骤工具已不存在。")
            return task
        merged = {**step.arguments, **supplied_arguments}
        try:
            validated = spec.argument_model.model_validate(merged)
        except ValidationError as exc:
            raise ValueError("补充的公开字段未通过参数校验") from exc

        constraint_error = check_amount(
            validated.model_dump(mode="json"), step.constraints
        )
        if constraint_error is not None:
            # 补充参数后仍必须先满足步骤级约束；冲突时保持原步骤状态，零写入。
            raise ValueError(constraint_error[1])

        step.arguments = validated.model_dump(mode="json")
        step.status = "running"
        step.result = None
        step.message = None
        task.status = "running"
        task.current_step_id = step.step_id
        task.updated_at = self._now()
        session.pending_clarification = None
        self._event(task, step, "clarification_resolved", "running")
        dispatch = self.router.dispatch(
                step.tool_name, step.arguments,
                principal=self._session_principal(session), session=session,
            )
        if not dispatch.result.success:
            step.result = dispatch.result
            self._fail_task(
                session,
                step,
                dispatch.result.error_code or "tool_failed",
                dispatch.result.message,
            )
            return task
        if dispatch.pending_action is not None:
            step.status = "waiting_confirmation"
            step.result = dispatch.result
            dispatch.pending_action.constraints = list(step.constraints)
            step.message = dispatch.pending_action.confirmation_message
            task.status = "waiting_confirmation"
            task.updated_at = self._now()
            session.pending_action = dispatch.pending_action
            if self._ensure_consistent(session):
                self._event(
                    task, step, "waiting_confirmation", "waiting_confirmation"
                )
            return task

        outcome = self.verify_tool_result(step.tool_name, dispatch.result)
        step.result = dispatch.result
        step.verification_status = "passed" if outcome.verified else "failed"
        step.message = dispatch.result.message if outcome.verified else outcome.message
        if not outcome.verified:
            self._fail_task(
                session,
                step,
                outcome.error_code or "verification_failed",
                outcome.message,
                preserve_verification=True,
            )
            return task
        step.status = "completed"
        task.current_step_id = None
        task.updated_at = self._now()
        self._event(task, step, "step_completed", "completed")
        self._run(session)
        return task

    def modify_unexecuted_step(
        self, session, *, tool_name, changes, step_id=None, constraints=None
    ):
        """Validate and mutate exactly one not-yet-executed workflow step.

        Completed/running steps and their results are immutable.  The whole DAG
        is reconstructed and passed through the existing PlanValidator before
        state is changed, so conditions and dependencies cannot drift.

        ``constraints`` 是本轮提案；它只会继承并收紧该步骤已有的金额上限，
        空列表或省略都不会清除步骤级约束。
        """
        task = session.current_task
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            raise PlanValidationError(
                "workflow_not_modifiable", "当前没有可修改的未完成任务。"
            )
        candidates = [
            item for item in task.steps
            if item.tool_name == tool_name
            and item.status in {
                "pending", "waiting_confirmation", "waiting_clarification"
            }
            and (step_id is None or item.step_id == step_id)
        ]
        if len(candidates) != 1:
            raise PlanValidationError(
                "workflow_step_not_unique",
                "可修改步骤不唯一或已经执行，请明确指定尚未执行的步骤。",
            )
        target = candidates[0]
        spec = self.registry.get(target.tool_name)
        if spec is None:
            raise PlanValidationError("unknown_tool", "目标步骤工具已不存在。")
        allowed = set(spec.argument_model.model_fields)
        if set(changes) - allowed:
            raise PlanValidationError(
                "field_not_modifiable", "修改包含该业务步骤不允许变更的字段。"
            )
        outcome = merge_amount_constraints(target.constraints, constraints)
        if not outcome.ok:
            # 放宽/删除/不受支持的约束一律拒绝，并保留原步骤约束。
            raise PlanValidationError(outcome.error_code, outcome.message)
        merged = {**target.arguments, **changes}
        try:
            validated = spec.argument_model.model_validate(merged)
        except ValidationError as exc:
            raise PlanValidationError(
                "invalid_modified_arguments", "修改后的步骤参数不合法。"
            ) from exc
        conflict = check_amount(validated.model_dump(mode="json"), outcome.constraints)
        if conflict is not None:
            raise PlanValidationError(conflict[0], conflict[1])

        proposals = []
        for item in task.steps:
            arguments = (
                validated.model_dump(mode="json")
                if item.step_id == target.step_id else dict(item.arguments)
            )
            proposals.append(
                PlannedStep(
                    step_id=item.step_id,
                    purpose=item.purpose,
                    tool_name=item.tool_name,
                    arguments=arguments,
                    depends_on=list(item.depends_on),
                    condition=item.condition,
                )
            )
        proposal = TaskPlanProposal(contract=task.contract, steps=proposals)
        self.validator.validate(proposal)

        was_waiting = target.status in {
            "waiting_confirmation", "waiting_clarification"
        }
        target.arguments = validated.model_dump(mode="json")
        target.constraints = list(outcome.constraints)
        target.result = None
        target.message = None
        target.error_code = None
        target.verification_status = "not_run"
        target.status = "pending"
        task.updated_at = self._now()
        if was_waiting:
            task.status = "running"
            task.current_step_id = None
            session.pending_action = None
            session.pending_clarification = None
            self._event(task, target, "step_modified", "pending")
            self._run(session)
        else:
            self._event(task, target, "step_modified", "pending")
        self._ensure_consistent(session)
        return target

    def recipient_selection_error(self, recipient_id, step_arguments, candidate_ids):
        """Re-read the recipient before accepting a disambiguation choice.

        Format-correct input is not enough: the recipient must still be in the
        candidate list, still exist, still be enabled and trusted, and still carry
        the name used in the original request. The recipient row is re-read inside
        the request handler; the Router re-reads it again in its own preview.
        """
        if recipient_id not in candidate_ids:
            return "请选择候选列表中的收款人编号"
        requested_name = (step_arguments or {}).get("recipient_name")
        if requested_name is not None and not isinstance(requested_name, str):
            return "原请求的收款人姓名不可用，请重新发起转账。"
        connection = None
        try:
            connection = get_connection()
            row = connection.execute(
                "SELECT name, status, trusted FROM recipients WHERE recipient_id = ?",
                (recipient_id,),
            ).fetchone()
        except sqlite3.Error:
            return "当前无法重新核对收款人信息，请稍后重试。"
        finally:
            if connection is not None:
                connection.close()
        if row is None:
            return "该收款人已不存在，请重新选择或重新发起请求。"
        if row["status"] != "active":
            return "该收款人已停用，不能转账，请重新选择。"
        if row["trusted"] != 1:
            return "该收款人已被标记为不可信，不能转账，请重新选择。"
        if requested_name is not None and row["name"] != requested_name.strip():
            return "该编号的收款人姓名与原请求不一致，请重新选择。"
        return None

    def is_clarification_expired(self, pending):
        return (
            pending is not None
            and pending.kind == "workflow_execution"
            and pending.expires_at is not None
            and self._now() >= pending.expires_at
        )

    def resume(self, session, result: ToolResult):
        task = session.current_task
        if task is None or task.status != "waiting_confirmation":
            raise ValueError("当前任务没有等待确认的步骤")
        if not self._ensure_consistent(session):
            return task
        step = next(
            (item for item in task.steps if item.status == "waiting_confirmation"),
            None,
        )
        if step is None:
            raise ValueError("任务等待确认状态不一致")
        try:
            step.result = result
            session.pending_action = None
            if not result.success:
                self._fail_task(
                    session,
                    step,
                    result.error_code or "tool_failed",
                    result.message,
                )
                return task
            outcome = self.verify_tool_result(step.tool_name, result)
            step.verification_status = "passed" if outcome.verified else "failed"
            step.message = result.message if outcome.verified else outcome.message
            if not outcome.verified:
                self._fail_task(
                    session,
                    step,
                    outcome.error_code or "verification_failed",
                    outcome.message,
                    preserve_verification=True,
                )
                return task
            step.status = "completed"
            task.status = "running"
            task.current_step_id = None
            task.updated_at = self._now()
            self._event(task, step, "step_completed", "completed")
            self._run(session)
        except Exception:
            self.fail_safely(
                session,
                "workflow_resume_exception",
                "操作可能已经提交，需要人工核对；系统不会自动重试。"
                "需要人工检查；任务恢复发生异常，后续步骤未执行。",
            )
        self._ensure_consistent(session)
        return task

    def cancel(self, session):
        task = session.current_task
        if not self._ensure_consistent(session):
            return task
        if task is None or task.status not in {
            "waiting_confirmation", "waiting_clarification"
        }:
            return None
        waiting = next(
            (
                item
                for item in task.steps
                if item.status in {"waiting_confirmation", "waiting_clarification"}
            ),
            None,
        )
        if waiting is not None:
            waiting.status = "cancelled"
            waiting.message = "用户取消了当前写操作。"
        self._cancel_remaining(task)
        now = self._now()
        task.status = "cancelled"
        task.current_step_id = None
        task.updated_at = now
        task.completed_at = now
        task.failure_reason = "用户取消了任务。"
        session.pending_action = None
        session.pending_clarification = None
        self._event(task, waiting, "task_cancelled", "cancelled")
        self._ensure_consistent(session)
        return task

    def expire(self, session):
        task = session.current_task
        if not self._ensure_consistent(session):
            return task
        if task is None or task.status not in {
            "waiting_confirmation", "waiting_clarification"
        }:
            return None
        waiting = next(
            (
                item
                for item in task.steps
                if item.status in {"waiting_confirmation", "waiting_clarification"}
            ),
            None,
        )
        if waiting is not None:
            waiting.status = "cancelled"
            waiting.error_code = (
                "pending_clarification_expired"
                if task.status == "waiting_clarification"
                else "pending_action_expired"
            )
            waiting.message = (
                "当前步骤澄清已过期。"
                if task.status == "waiting_clarification"
                else "当前写操作确认已过期。"
            )
        self._cancel_remaining(task)
        now = self._now()
        task.status = "expired"
        task.current_step_id = None
        task.updated_at = now
        task.completed_at = now
        task.failure_reason = waiting.message if waiting is not None else "任务已过期。"
        session.pending_action = None
        session.pending_clarification = None
        self._event(task, waiting, "task_expired", "expired")
        self._ensure_consistent(session)
        return task

    def _evaluate_condition(self, task, step):
        condition = step.condition
        source = next(
            (item for item in task.steps if item.step_id == condition.source_step_id),
            None,
        )
        if (
            source is None
            or source.status != "completed"
            or source.verification_status != "passed"
            or source.result is None
            or not source.result.success
        ):
            raise ConditionEvaluationError(
                "condition_source_unavailable",
                "条件引用的前置步骤没有可用的已核验结果。",
            )
        source_spec = self.registry.get(source.tool_name)
        field_contract = (
            source_spec.condition_fields.get(condition.field_path)
            if source_spec is not None
            else None
        )
        if field_contract is None:
            raise ConditionEvaluationError(
                "condition_field_not_declared",
                "条件字段不在来源工具的公开契约中，任务已安全停止。",
            )
        if condition.operator not in field_contract.operators:
            raise ConditionEvaluationError(
                "condition_operator_not_allowed",
                "条件操作符不在字段契约中，任务已安全停止。",
            )
        from lifecycle.service import fresh_condition_result
        current = fresh_condition_result(source).model_dump(mode="json")
        for part in condition.field_path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise ConditionEvaluationError(
                    "condition_field_missing_runtime",
                    "已声明的条件字段在核验结果中缺失，任务已安全停止。",
                )
            current = current[part]
        expected = condition.value
        if not PlanValidator._condition_value_matches(
            field_contract.value_type, current
        ) or not PlanValidator._condition_value_matches(
            field_contract.value_type, expected
        ):
            raise ConditionEvaluationError(
                "condition_type_mismatch_runtime",
                "条件字段与比较值类型不兼容，任务已安全停止。",
            )
        operators = {
            "eq": lambda left, right: left == right,
            "ne": lambda left, right: left != right,
            "gt": lambda left, right: left > right,
            "gte": lambda left, right: left >= right,
            "lt": lambda left, right: left < right,
            "lte": lambda left, right: left <= right,
        }
        try:
            return bool(operators[condition.operator](current, expected))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ConditionEvaluationError(
                "condition_comparison_failed",
                "条件无法安全比较，任务已停止。",
            ) from exc

    def verify_tool_result(self, tool_name, result) -> VerificationOutcome:
        if not isinstance(result, ToolResult) or not result.success or result.error_code is not None:
            return VerificationOutcome(
                False, "工具结果结构或状态不合法。", "verification_failed"
            )
        spec = self.registry.get(tool_name)
        if spec is None:
            return VerificationOutcome(
                False, "工具在核验前已不存在。", "verification_failed"
            )
        if spec.result_data_type is not None and not isinstance(
            result.data, spec.result_data_type
        ):
            return VerificationOutcome(
                False,
                "工具结果数据类型与登记类型不一致。",
                "verification_failed",
            )
        from read_models import verify_read
        if not verify_read(tool_name, result.data):
            return VerificationOutcome(False, '只读字段、金额或汇总关系不合法。', 'verification_failed')
        if contains_sensitive_result(result.data, tool_name=tool_name) or contains_sensitive_result(result.message):
            return VerificationOutcome(
                False,
                "工具结果包含不允许暴露的内部敏感字段。",
                "verification_failed",
            )
        if spec.verify is None:
            return VerificationOutcome(True, "结果结构核验通过。")
        try:
            verified, message = spec.verify(result)
            if bool(verified):
                return VerificationOutcome(True, str(message))
            return VerificationOutcome(
                False,
                "操作可能已经提交，需要人工核对；系统不会自动重试。"
                "需要人工检查；状态核验失败，后续步骤未执行；系统不会自动回滚。",
                "verification_failed",
            )
        except Exception:
            return VerificationOutcome(
                False,
                "操作可能已经提交，需要人工核对；系统不会自动重试。"
                "需要人工检查；状态核验发生异常，后续步骤未执行；系统不会自动回滚。",
                "verification_exception",
            )

    @staticmethod
    def _contains_sensitive_result(value, field_name=None):
        """Backward-compatible wrapper around the shared output guard."""
        return contains_sensitive_result(value, field_name)

    def _fail_task(
        self,
        session,
        step,
        error_code,
        message,
        preserve_verification=False,
    ):
        if preserve_verification:
            step.verification_status = "failed"
        self._converge_failed(
            session,
            error_code,
            message,
            step=step,
        )

    def fail_safely(self, session, error_code, message):
        """Public fallback for callers that encounter an unexpected workflow error."""
        return self._converge_failed(session, error_code, message)

    def _converge_failed(self, session, error_code, message, step=None):
        task = session.current_task
        session.pending_action = None
        session.pending_clarification = None
        if task is None:
            return None
        if step is None or step.status in {"completed", "skipped", "cancelled"}:
            step = next(
                (
                    item
                    for item in task.steps
                    if item.status in {
                        "running",
                        "waiting_clarification",
                        "waiting_confirmation",
                        "pending",
                    }
                ),
                None,
            )
        if step is not None:
            step.status = "failed"
            step.error_code = error_code
            step.message = message
        for item in task.steps:
            if item is not step and item.status in {
                "pending",
                "running",
                "waiting_clarification",
                "waiting_confirmation",
            }:
                item.status = "cancelled"
                item.error_code = "dependency_failed"
                item.message = "前序步骤未完成，后续步骤已停止。"
        now = self._now()
        task.status = "failed"
        task.current_step_id = step.step_id if step is not None else None
        task.updated_at = now
        task.completed_at = now
        task.failure_reason = message
        self._event(
            task,
            step,
            "step_failed",
            "failed",
            {"error_code": error_code},
        )
        return task

    def _ensure_consistent(self, session):
        error = self._state_consistency_error(session)
        if error is None:
            return True
        self._converge_failed(
            session,
            "invalid_task_state",
            "任务状态不一致，已安全停止。",
        )
        return False

    @staticmethod
    def _state_consistency_error(session):
        task = session.current_task
        pending_action = session.pending_action
        pending_clarification = session.pending_clarification
        if task is None:
            if pending_action is not None:
                return "pending_without_task"
            if (
                pending_clarification is not None
                and pending_clarification.kind == "workflow_execution"
            ):
                return "workflow_clarification_without_task"
            return None
        running = [step for step in task.steps if step.status == "running"]
        clarifying = [
            step for step in task.steps if step.status == "waiting_clarification"
        ]
        waiting = [
            step for step in task.steps if step.status == "waiting_confirmation"
        ]
        if len(running) > 1:
            return "multiple_running_steps"
        if len(waiting) > 1:
            return "multiple_waiting_steps"
        if len(clarifying) > 1:
            return "multiple_clarifying_steps"
        if sum(bool(items) for items in (running, clarifying, waiting)) > 1:
            return "conflicting_active_steps"
        if clarifying and task.status != "waiting_clarification":
            return "clarifying_step_without_clarifying_task"
        if task.status == "waiting_clarification":
            if len(clarifying) != 1:
                return "clarifying_task_without_clarifying_step"
            if pending_action is not None:
                return "clarification_with_pending_action"
            if (
                pending_clarification is None
                or pending_clarification.kind != "workflow_execution"
            ):
                return "clarifying_task_without_pending_clarification"
            if (
                pending_clarification.task_id != task.task_id
                or pending_clarification.step_id != clarifying[0].step_id
                or pending_clarification.tool_name != clarifying[0].tool_name
            ):
                return "pending_clarification_mismatch"
        elif (
            pending_clarification is not None
            and pending_clarification.kind == "workflow_execution"
        ):
            return "workflow_clarification_without_waiting_task"
        if waiting and task.status != "waiting_confirmation":
            return "waiting_step_without_waiting_task"
        if task.status == "waiting_confirmation":
            if len(waiting) != 1:
                return "waiting_task_without_waiting_step"
            if pending_action is None:
                return "waiting_task_without_pending_action"
            if pending_action.tool_name != waiting[0].tool_name:
                return "pending_action_tool_mismatch"
        elif pending_action is not None:
            return "pending_action_without_waiting_task"
        if task.status in TERMINAL_TASK_STATUSES:
            if any(
                step.status in {
                    "pending",
                    "running",
                    "waiting_clarification",
                    "waiting_confirmation",
                }
                for step in task.steps
            ):
                return "terminal_task_with_nonterminal_step"
        if task.status == "completed" and any(
            step.status not in {"completed", "skipped"} for step in task.steps
        ):
            return "completed_task_with_unsuccessful_step"
        return None

    @staticmethod
    def _cancel_remaining(task):
        for item in task.steps:
            if item.status == "pending":
                item.status = "cancelled"
                item.message = "前序步骤未完成，后续步骤已停止。"

    def _event(self, task, step, event_type, status, detail=None):
        from lifecycle.service import active_scope, capture_workflow
        scope = active_scope.get()
        if scope is not None and scope.session.current_task is task:
            capture_workflow(scope.session)
        allowed_detail = {}
        detail = detail or {}
        for key in ("error_code", "condition_result", "reason"):
            value = detail.get(key)
            if isinstance(value, (str, bool, int, float)):
                allowed_detail[key] = value
        if step is not None:
            allowed_detail["tool_name"] = step.tool_name
        # The established PG audit enum uses clarification_resolved for a
        # parameter revision; retain the precise cause in the safe detail.
        if event_type == 'step_modified':
            event_type = 'clarification_resolved'
            allowed_detail['reason'] = 'step_modified'
        connection = None
        try:
            connection = get_connection()
            connection.execute(
                """
                INSERT INTO agent_task_events (
                    task_id, step_id, event_type, status,
                    safe_detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    step.step_id if step is not None else None,
                    event_type,
                    status,
                    json.dumps(allowed_detail, ensure_ascii=False, sort_keys=True),
                    self._now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
        finally:
            if connection is not None:
                connection.close()
