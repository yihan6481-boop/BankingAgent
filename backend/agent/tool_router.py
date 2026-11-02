"""白名单 Tool Router：校验参数、隔离异常并强制写操作确认。

V0.7.0 变化：写操作不再由 Router 自行"检查并执行"，而是统一走
``security.runners.ActionPlanner`` + ``SecurityExecutionGateway``：

* 预览阶段不写业务数据，只生成绑定参数摘要与权限等级的待办；
* 红色操作在 V1 协议下**安全拒绝**（既有稳定错误码 + 新 detail_code），
  不降级、不暗中执行；
* 执行阶段由服务器端一次性授权行驱动，授权消费与业务写入在同一事务内完成。
"""

import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from .config import PENDING_ACTION_TTL_SECONDS
from .schemas import DispatchResult, PendingAction, ToolResult
from .tool_registry import ToolRegistry, ToolRisk, service_result_to_tool_result

from security.config import load_security_config
from security.execution import AuthorizationGrant, WriteSecurityContext
from security.runners import ActionPlanner, ServiceFailure
from security.write_context import owner_scope


class ToolRouter:
    def __init__(
        self,
        registry: ToolRegistry,
        now_provider=None,
        pending_ttl_seconds=PENDING_ACTION_TTL_SECONDS,
        confirmation_token_provider=None,
        idempotency_key_provider=None,
        planner: ActionPlanner | None = None,
        security_services=None,
        write_context_factory=None,
        plan_hook=None,
    ):
        self.registry = registry
        from storage.config import pg02_enabled as enabled
        if enabled() and now_provider is None:
            from security.service import get_security_services
            now_provider=get_security_services().now_provider
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        if pending_ttl_seconds <= 0:
            raise ValueError("pending_action TTL 必须大于 0")
        self.pending_ttl_seconds = pending_ttl_seconds
        from security.payment_auth import enabled as payment_enabled
        if payment_enabled():self.pending_ttl_seconds=max(360,self.pending_ttl_seconds)
        self.confirmation_token_provider = confirmation_token_provider or (
            lambda: f"{secrets.randbelow(1_000_000):06d}"
        )
        self.idempotency_key_provider = idempotency_key_provider or (
            lambda: f"request_{uuid.uuid4().hex}"
        )
        self.config = load_security_config()
        self.planner = planner or ActionPlanner(self.config, plan_hook=plan_hook)
        self.security_services = security_services
        # 在受控的离线评测中，评测驾驞可以注入一个已完成强验证的授权上下文，
        # 使红色步骤仍然经过同一安全入口执行（而不是绕过）。
        self.write_context_factory = write_context_factory

    def _now(self):
        current = self.now_provider()
        if not isinstance(current, datetime):
            raise TypeError("now_provider 必须返回 datetime")
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now_provider 必须返回包含时区的 datetime")
        return current.replace(microsecond=0)

    def is_pending_expired(self, pending):
        return self._now() >= pending.expires_at

    @staticmethod
    def matches_dynamic_confirmation(pending, text):
        if pending.confirmation_phrase is None or not isinstance(text, str):
            return False
        surrounding = " \t\r\n。．.!！?？,，;；:："
        normalized = text.strip(surrounding).casefold()
        return normalized == pending.confirmation_phrase.casefold()

    def _new_confirmation_phrase(self):
        token = str(self.confirmation_token_provider()).strip()
        if not re.fullmatch(r"\d{6}", token):
            raise ValueError("confirmation_token_provider 必须返回6位数字")
        return f"确认转账 {token}"

    def _new_v2_confirmation_phrase(self):
        token = str(self.confirmation_token_provider()).strip()
        if not re.fullmatch(r"\d{6}", token):
            raise ValueError("confirmation_token_provider 必须返回6位数字")
        return f"确认操作 {token}"

    def _new_idempotency_key(self):
        key = str(self.idempotency_key_provider()).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", key):
            raise ValueError("idempotency_key_provider 返回了无效格式")
        return key

    @staticmethod
    def _error(code, message, data=None):
        return DispatchResult(
            result=ToolResult(
                status="error", error_code=code, message=message, data=data
            )
        )

    # ------------------------------------------------------------ dispatch
    def dispatch(self, tool_name, arguments, principal=None, session=None):
        from storage.config import pg02_enabled,PG02_SUPPORTED_TOOLS
        if pg02_enabled() and tool_name not in PG02_SUPPORTED_TOOLS:
            return self._error('pg02_unsupported_business','PG02暂不支持该业务，请求未执行。')
        spec = self.registry.get(tool_name)
        if spec is None:
            return self._error("unknown_tool", f"拒绝执行未注册工具：{tool_name}")
        try:
            validated = spec.argument_model.model_validate(arguments)
        except ValidationError as error:
            return self._error(
                "invalid_arguments",
                "工具参数校验失败",
                {"errors": error.errors(include_url=False, include_input=False)},
            )

        if spec.risk == ToolRisk.READ_ONLY:
            if spec.execute is None:
                return self._error("tool_misconfigured", "只读工具未配置执行函数")
            denied = self.read_permission_error(tool_name, validated.model_dump(mode='json'), principal, session)
            if denied:
                return denied
            from storage.financial_reads import snapshot
            try:
                with snapshot():
                    from security.tool_guard import guarded_service_call
                    result = service_result_to_tool_result(guarded_service_call(
                        tool_name, lambda: spec.execute(validated, principal), read_only=True))
                    from analysis_service import CATALOG, analyze
                    if tool_name in CATALOG and result.success:
                        if not spec.verify(result)[0]:
                            return self._error('analysis_verification_failed', '分析结果Schema或事实不一致')
                        fresh = analyze(tool_name, validated, principal)
                        if not fresh['success'] or fresh['data'] != result.data:
                            return self._error('analysis_snapshot_changed', '分析依据已变化或事实不一致，请重新查询')
                    elif result.success:
                        from security.totp import independent_protocol
                        if independent_protocol.get():
                            # NL 1.0 requires a fresh canonical registered read to
                            # agree before publishing facts or evaluating conditions.
                            from .tool_registry import build_default_registry
                            canonical = build_default_registry(include_analysis=True).get(tool_name)
                            fresh = service_result_to_tool_result(guarded_service_call(
                                tool_name, lambda: canonical.execute(validated, principal), read_only=True))
                            if not fresh.success or fresh.data != result.data:
                                return self._error('read_verification_failed', '查询结果事实不足、已变化或与数据库不一致，请重新查询')
                    if result.success:
                        from .output_guard import contains_sensitive_result
                        from read_models import verify_read
                        if (contains_sensitive_result(result.data, tool_name=tool_name) or
                                contains_sensitive_result(result.message) or
                                not verify_read(tool_name, result.data, validated.model_dump(mode='json'))):
                            return self._error('verification_failed', '查询结果未通过字段或安全核验，未作为业务事实返回。')
                    return DispatchResult(result=result)
            except TypeError:
                from security.totp import independent_protocol
                if independent_protocol.get():
                    return self._error('tool_execution_error', '工具结果类型异常，未视为已核验的银行事实')
                return DispatchResult(
                    result=service_result_to_tool_result(guarded_service_call(
                        tool_name, lambda: spec.execute(validated), read_only=True))
                )
            except Exception:
                return self._error(
                    "tool_execution_error", "工具执行异常，银行数据未被视为成功修改"
                )

        if session is not None and getattr(session, "protocol", "v1") == "v2":
            return self._dispatch_write_v2(spec, validated, principal, session)
        return self._dispatch_write(spec, validated, principal)

    def read_permission_error(self, tool_name, arguments, principal, session):
        spec = self.registry.get(tool_name)
        if not spec or spec.risk != ToolRisk.READ_ONLY or not getattr(self.registry, 'enforce_read_permissions', False):
            return None
        from security.service import get_security_services
        outcome = get_security_services().gateway.authorize_read(tool_name, principal, arguments,
            session_id=getattr(session, 'session_id', None))
        if outcome.stage != 'read_ok':
            return self._error(outcome.error_code or 'permission_denied',
                outcome.message + '；订阅查询请使用 /api/v2 的 read 并显式 authorize_query。')
        return None

    def _dispatch_write_v2(self, spec, validated, principal, session):
        """Create a V2 pending action bound to the real authenticated session."""
        requested = validated.model_dump(mode="json")
        signature = json.dumps(
            {"action": spec.name, "arguments": requested},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        )
        idempotency_key = None
        if spec.uses_idempotency_key:
            from lifecycle.service import operation_key
            durable_key = operation_key(session, spec.name)
            if (
                durable_key
            ):
                idempotency_key = durable_key
                session.request_signature = signature
                session.request_idempotency_key = idempotency_key
            elif (
                getattr(session, "request_signature", None) == signature
                and getattr(session, "request_idempotency_key", None)
            ):
                idempotency_key = session.request_idempotency_key
            else:
                idempotency_key = self._new_idempotency_key()
                session.request_signature = signature
                session.request_idempotency_key = idempotency_key
        try:
            plan = self.planner.prepare(
                spec.name, requested, principal, idempotency_key=idempotency_key
            )
        except Exception:
            return self._error(
                "tool_execution_error", "工具预览异常，未执行任何银行操作"
            )
        if not plan.permitted or plan.decision is None:
            return DispatchResult(
                result=ToolResult(
                    status="error",
                    error_code=plan.error_code or "preview_failed",
                    message=plan.message,
                    data=plan.preview,
                )
            )

        # Persist the normalized operation that this preview actually showed.
        # Deterministic V2 confirmation may legally follow an Agent preview;
        # it must compare against these trusted final arguments, not against a
        # freshly supplied request body.
        session.request_action = spec.name
        session.request_action_digest = plan.decision.action_digest
        session.request_arguments = dict(plan.arguments)
        session.request_expires_at = self._now() + timedelta(
            seconds=self.pending_ttl_seconds
        )

        services = self.security_services
        if services is None:
            from security.service import get_security_services
            services = get_security_services()
        stale = getattr(session, "pending_challenge", None)
        if stale is not None:
            try:
                services.strong_auth.cancel(
                    principal, session.session_id, stale.challenge_id
                )
            except Exception:
                pass
            session.pending_challenge = None

        outcome = services.gateway.authorize_write(
            spec.name,
            principal,
            session.session_id,
            plan.arguments,
            chain=services.gateway.new_chain(),
            idempotency_key=plan.idempotency_key,
            decision=plan.decision,
        )
        if outcome.stage not in {"need_confirmation", "need_strong_auth", "authorized"}:
            return self._error(
                outcome.error_code or "authorization_required",
                outcome.message or "无法建立可信授权上下文，拒绝执行",
            )
        # Missing first-time factor state keeps the trusted preview but issues no
        # challenge yet. The write never runs; the user binds the authenticator
        # from the operation card and then re-reads/confirms the original task.
        factor_binding_required = outcome.challenge is None and outcome.error_code in {
            "factor_not_bound", "factor_not_confirmed"}
        created_at = self._now()
        confirmation_phrase = (
            self._new_confirmation_phrase()
            if spec.dynamic_confirmation else self._new_v2_confirmation_phrase()
        )
        confirmation_phrase = session.reserve_confirmation_phrase(confirmation_phrase)
        confirmation_message = spec.confirmation_builder(
            plan.preview or {}, confirmation_phrase
        ) + f" 该确认在 {self.pending_ttl_seconds} 秒内有效。"
        if not spec.dynamic_confirmation:
            confirmation_message += f" 请回传确认短语：{confirmation_phrase}。"
        session.pending_revision = int(getattr(session, "pending_revision", 0)) + 1
        pending = PendingAction(
            tool_name=spec.name,
            requested_arguments=requested,
            execution_arguments=dict(plan.arguments),
            confirmation_message=confirmation_message,
            confirmation_phrase=confirmation_phrase,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=self.pending_ttl_seconds),
            action=plan.action,
            permission_level=plan.decision.permission_level,
            decision_id=plan.decision.decision_id,
            policy_version=plan.decision.policy_version,
            action_digest=plan.decision.action_digest,
            requires_strong_auth=plan.decision.requires_strong_auth,
            requires_confirmation=True,
            execution_permitted=True,
            authorization_id=(
                outcome.authorization.authorization_id
                if outcome.authorization is not None else None
            ),
            revision=session.pending_revision,
        )
        if outcome.challenge is not None:
            session.pending_challenge = outcome.challenge
        return DispatchResult(
            result=ToolResult(
                status="success",
                data=plan.preview or {},
                message=(
                    "操作尚未执行。该操作需要独立TOTP验证；当前账号尚未完成验证器"
                    "绑定/确认。请先在安全面板完成绑定，然后返回此任务重新确认。"
                    if factor_binding_required else (plan.message or "操作尚未执行，请确认后继续")
                ),
            ),
            pending_action=pending,
        )

    def _dispatch_write(self, spec, validated, principal):
        requested = validated.model_dump(mode="json")
        try:
            plan = self.planner.prepare(spec.name, requested, principal)
        except Exception:
            return self._error(
                "tool_execution_error", "工具执行异常，银行数据未被视为成功修改"
            )
        if not plan.permitted:
            return DispatchResult(
                result=ToolResult(
                    status="error",
                    error_code=plan.error_code or "preview_failed",
                    message=plan.message,
                    data=plan.preview,
                )
            )
        decision = plan.decision
        if decision is None:
            return self._error("security_fact_unavailable", "缺少安全事实，拒绝执行")

        preview = plan.preview or {}
        if decision.requires_strong_auth and self.write_context_factory is None:
            # V1 无强验证能力：安全拒绝，不降级、不暗中执行。
            return DispatchResult(
                result=ToolResult(
                    status="error",
                    error_code="validation_failed",
                    message=(
                        "该操作在当前权限等级下需要模拟强验证；"
                        "V1 接口不支持强验证，已安全拒绝执行。"
                        "请使用 /api/v2 完成确认与验证。"
                    ),
                    data={
                        "detail_code": "strong_auth_required_via_v2",
                        "permission_level": decision.permission_level,
                        "decision_id": decision.decision_id,
                        "policy_version": decision.policy_version,
                        "required_authentication": decision.required_authentication,
                        "reason_codes": list(decision.reason_codes),
                    },
                )
            )

        authorized = self._issue_grant(spec.name, plan, principal)
        if authorized is None:
            return self._error(
                "authorization_required", "无法建立可信授权上下文，拒绝执行"
            )
        grant, security_context = authorized
        created_at = self._now()
        expires_at = created_at + timedelta(seconds=self.pending_ttl_seconds)
        confirmation_phrase = (
            self._new_confirmation_phrase() if spec.dynamic_confirmation else None
        )
        confirmation_message = spec.confirmation_builder(preview, confirmation_phrase)
        confirmation_message += (
            f" 该确认在 {self.pending_ttl_seconds} 秒内有效。"
        )
        pending = PendingAction(
            tool_name=spec.name,
            requested_arguments=requested,
            execution_arguments=dict(plan.arguments),
            confirmation_message=confirmation_message,
            confirmation_phrase=confirmation_phrase,
            created_at=created_at,
            expires_at=expires_at,
            action=plan.action,
            permission_level=decision.permission_level,
            decision_id=decision.decision_id,
            policy_version=decision.policy_version,
            action_digest=decision.action_digest,
            requires_strong_auth=False,
            requires_confirmation=True,
            execution_permitted=True,
        )
        # 一次预览只发放一份待确认授权；未确认的预览不会写入业务数据。
        return DispatchResult(
            result=ToolResult(
                status="success",
                data=preview,
                message=plan.message or "操作尚未执行，请确认后继续",
            ),
            pending_action=pending,
        )

    def _issue_grant(self, action, plan, principal):
        services = self.security_services
        if services is None:
            from security.service import get_security_services

            services = get_security_services()
        if principal is None:
            principal = services.identity.legacy_demo_principal()
        decision = plan.decision
        session_id = f"v1_{action}_{decision.decision_id[:16]}"
        try:
            grant = services.gateway  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - defensive
            return None
        from security.execution import issue_authorization

        authorization = issue_authorization(
            principal,
            session_id,
            action,
            decision.action_digest,
            decision.permission_level,
            decision.policy_version,
            decision.decision_id,
            now=self._now(),
            idempotency_key=plan.idempotency_key,
        )
        context = WriteSecurityContext(
            principal=principal,
            session_id=session_id,
            grant=authorization,
            request_id=f"req_v1_{secrets.token_hex(6)}",
            action=action,
            policy_version=decision.policy_version,
            permission_level=decision.permission_level,
            decision_id=decision.decision_id,
            challenge_id=None,
            strong_auth_method="confirmation",
            reason_codes=list(decision.reason_codes),
            audit_chain=services.gateway.new_chain(),
            audit_recorder=services.gateway.audit,
            policy_engine=services.gateway.policy,
        )
        return authorization, context

    def build_context(self, pending: PendingAction, principal=None, chain=None):
        """Rebuild the trusted write context for a pending action."""
        services = self.security_services
        if services is None:
            from security.service import get_security_services

            services = get_security_services()
        if principal is None:
            principal = services.identity.legacy_demo_principal()
        session_id = f"v1_{pending.action or pending.tool_name}"
        grant = AuthorizationGrant(
            authorization_id=f"az_pending_{pending.decision_id or secrets.token_hex(6)}",
            principal_id=principal.principal_id,
            user_id=int(principal.user_id),
            session_id=session_id,
            action=pending.action or pending.tool_name,
            action_digest=pending.action_digest or "",
            permission_level=pending.permission_level or "yellow",
            policy_version=pending.policy_version or self.config.policy_version,
            decision_id=pending.decision_id or "",
            challenge_id=None,
            strong_auth_method="confirmation",
            idempotency_key=pending.execution_arguments.get("idempotency_key", ""),
        )
        return WriteSecurityContext(
            principal=principal,
            session_id=session_id,
            grant=grant,
            request_id=f"req_v1_{secrets.token_hex(6)}",
            action=pending.action or pending.tool_name,
            policy_version=pending.policy_version or self.config.policy_version,
            permission_level=pending.permission_level or "yellow",
            decision_id=pending.decision_id or "",
            challenge_id=None,
            strong_auth_method="confirmation",
            reason_codes=[],
            audit_chain=chain or services.gateway.new_chain(),
            audit_recorder=services.gateway.audit,
            policy_engine=services.gateway.policy,
        )

    def execute_pending(self, pending, principal=None, session=None):
        if session is not None and getattr(session, "protocol", "v1") == "v2":
            return self._execute_pending_v2(pending, principal, session)
        if self.is_pending_expired(pending):
            return ToolResult(
                status="error",
                error_code="pending_action_expired",
                message="该操作确认已过期，请重新发起。",
            )
        spec = self.registry.get(pending.tool_name)
        if spec is None:
            return ToolResult(
                status="error",
                error_code="unknown_tool",
                message=f"待确认工具已不存在：{pending.tool_name}",
            )
        if spec.risk != ToolRisk.WRITE or spec.confirm is None:
            return ToolResult(
                status="error",
                error_code="invalid_pending_action",
                message="待确认操作不是可执行的写工具",
            )
        services = self.security_services
        if services is None:
            from security.service import get_security_services

            services = get_security_services()
        if principal is None:
            principal = services.identity.legacy_demo_principal()
        action = pending.action or pending.tool_name
        chain = services.gateway.new_chain()

        # 重新预览：任何业务状态或参数变化都会改变摘要并导致授权失效。
        try:
            plan = self.planner.prepare(
                action,
                pending.requested_arguments,
                principal,
                # 复用预览阶段生成的幂等键，否则重新计算的参数摘要会漂移，
                # 合法确认就会被误判为"目标不一致"。
                idempotency_key=pending.execution_arguments.get("idempotency_key"),
            )
        except Exception:
            return ToolResult(
                status="error",
                error_code="pending_action_revalidation_failed",
                message="确认前重新校验失败，未执行任何银行操作",
            )
        if not plan.permitted:
            return ToolResult(
                status="error",
                error_code=plan.error_code or "pending_action_revalidation_failed",
                message=plan.message,
            )
        if plan.decision is None:
            return ToolResult(
                status="error",
                error_code="security_fact_unavailable",
                message="缺少安全事实，未执行任何银行操作",
            )
        # 黄变红：等待期间累计额变化导致升级 -> 停止执行并要求强验证。
        # 离线评测注入了已完成强验证的授权工厂时，该步骤已经拥有授权。
        if (
            plan.decision.requires_strong_auth
            and self.write_context_factory is None
        ):
            return ToolResult(
                status="error",
                error_code="validation_failed",
                message=(
                    "等待确认期间权限等级已升级为需要模拟强验证，已停止执行；"
                    "请使用 /api/v2 重新预览、确认并完成验证。"
                ),
                data={
                    "detail_code": "permission_escalated_to_strong_auth",
                    "permission_level": plan.decision.permission_level,
                    "decision_id": plan.decision.decision_id,
                    "reason_codes": list(plan.decision.reason_codes),
                },
            )
        if plan.decision.action_digest != pending.action_digest:
            return ToolResult(
                status="error",
                error_code="invalid_pending_action",
                message="待确认操作目标与原预览不一致，未执行任何银行操作",
            )

        original_arguments = dict(pending.execution_arguments)
        revalidated_arguments = dict(plan.arguments)
        if "idempotency_key" in original_arguments:
            revalidated_arguments["idempotency_key"] = original_arguments[
                "idempotency_key"
            ]
        if revalidated_arguments != original_arguments:
            return ToolResult(
                status="error",
                error_code="invalid_pending_action",
                message="待确认操作目标与原预览不一致，未执行任何银行操作",
            )

        # 授权消费与业务写入：使用预览阶段签发的授权行，一次性。
        from security.execution import issue_authorization
        from security.write_context import assert_write_authorization

        session_id = f"v1_{action}"
        final_arguments = self.planner.final_arguments(action, revalidated_arguments)
        authorization = None
        if self.write_context_factory is not None:
            context = self.write_context_factory(
                action, final_arguments, plan.decision
            )
            context = context.model_copy(
                update={"permission_level": plan.decision.permission_level}
            )
        else:
            authorization = issue_authorization(
                principal,
                session_id,
                action,
                plan.decision.action_digest,
                plan.decision.permission_level,
                plan.decision.policy_version,
                pending.decision_id or plan.decision.decision_id,
                now=self._now(),
                idempotency_key=revalidated_arguments.get("idempotency_key"),
            )
            context = WriteSecurityContext(
                principal=principal,
                session_id=session_id,
                grant=authorization,
                request_id=chain.request_id,
                action=action,
                policy_version=plan.decision.policy_version,
                permission_level=plan.decision.permission_level,
                decision_id=plan.decision.decision_id,
                challenge_id=None,
                strong_auth_method="confirmation",
                reason_codes=list(plan.decision.reason_codes),
                audit_chain=chain,
                audit_recorder=services.gateway.audit,
                policy_engine=services.gateway.policy,
            )
        try:
            result = self.planner.execute(action, final_arguments, context)
        except ServiceFailure as error:
            return ToolResult(
                status="error",
                error_code=error.error_code,
                message=error.message,
                data=error.data,
            )
        except Exception:
            return ToolResult(
                status="error",
                error_code="tool_execution_error",
                message="确认后的工具执行异常，不能声称操作成功",
            )
        tool_result = service_result_to_tool_result(result)
        services.gateway._audit(
            chain, "final_outcome", "ok" if tool_result.success else "failed",
            principal, session_id, action=action,
            authorization_id=getattr(authorization, "authorization_id", None),
            error_code=None if tool_result.success else tool_result.error_code,
        )
        return tool_result

    def _execute_pending_v2(self, pending, principal, session):
        """Re-read facts, enforce constraints, then consume one V2 grant."""
        from lifecycle.service import validate_preview
        validate_preview(session, pending)
        if self.is_pending_expired(pending):
            return ToolResult(
                status="error", error_code="pending_action_expired",
                message="该操作确认已过期，请重新发起。",
            )
        services = self.security_services
        if services is None:
            from security.service import get_security_services
            services = get_security_services()
        action = pending.action or pending.tool_name
        try:
            plan = self.planner.prepare(
                action,
                pending.requested_arguments,
                principal,
                idempotency_key=pending.execution_arguments.get("idempotency_key"),
            )
        except Exception:
            return ToolResult(
                status="error", error_code="pending_action_revalidation_failed",
                message="确认前重新校验失败，未执行任何银行操作",
            )
        if not plan.permitted or plan.decision is None:
            return ToolResult(
                status="error",
                error_code=plan.error_code or "pending_action_revalidation_failed",
                message=plan.message or "确认前重新校验失败",
            )
        for constraint in pending.constraints:
            if constraint.field == "amount" and constraint.operator == "lte":
                try:
                    from decimal import Decimal
                    actual = Decimal(str(pending.requested_arguments.get("amount")))
                except Exception:
                    return ToolResult(
                        status="error", error_code="constraint_not_supported",
                        message="金额约束无法安全判定，操作未执行。",
                    )
                if actual > constraint.value:
                    return ToolResult(
                        status="error", error_code="constraint_conflict",
                        message="当前金额与“金额不超过”约束冲突，操作未执行。",
                    )
        if (
            plan.decision.action_digest != pending.action_digest
            or dict(plan.arguments) != dict(pending.execution_arguments)
        ):
            return ToolResult(
                status="error", error_code="authorization_parameter_changed",
                message="操作参数已变化，旧确认和验证已失效，请重新预览。",
            )

        authorization = None
        if plan.decision.requires_strong_auth:
            if not pending.requires_strong_auth:
                # Trusted business facts (for example the user's cumulative
                # daily transfer total) may upgrade a yellow preview while it
                # waits.  Create a fresh red challenge and confirmation phrase;
                # the old confirmation attempt never executes the operation.
                escalated = services.gateway.authorize_write(
                    action,
                    principal,
                    session.session_id,
                    plan.arguments,
                    chain=services.gateway.new_chain(),
                    idempotency_key=plan.idempotency_key,
                    decision=plan.decision,
                )
                if escalated.challenge is None:
                    return ToolResult(
                        status="error", error_code="authorization_required",
                        message="权限升级后无法建立新的验证挑战，操作未执行。",
                    )
                session.pending_revision = int(
                    getattr(session, "pending_revision", pending.revision)
                ) + 1
                pending.permission_level = plan.decision.permission_level
                pending.decision_id = plan.decision.decision_id
                pending.policy_version = plan.decision.policy_version
                pending.action_digest = plan.decision.action_digest
                pending.requires_strong_auth = True
                pending.authorization_id = None
                pending.revision = session.pending_revision
                pending.created_at = self._now()
                pending.expires_at = pending.created_at + timedelta(
                    seconds=self.pending_ttl_seconds
                )
                pending.confirmation_phrase = (
                    self._new_confirmation_phrase()
                    if pending.tool_name == "transfer_money"
                    else self._new_v2_confirmation_phrase()
                )
                pending.confirmation_phrase = session.reserve_confirmation_phrase(
                    pending.confirmation_phrase
                )
                pending.confirmation_message = (
                    "权限已升级，请完成新的模拟强验证后回传新确认短语："
                    f"{pending.confirmation_phrase}。"
                )
                session.pending_action = pending
                session.pending_challenge = escalated.challenge
                return ToolResult(
                    status="error",
                    error_code="permission_escalated_to_strong_auth",
                    message=(
                        "等待期间权限已升级；旧确认未执行。"
                        "请完成新的模拟强验证并使用新确认短语。"
                    ),
                    data={"permission_level": "red"},
                )
            challenge = getattr(session, "pending_challenge", None)
            view = (
                services.strong_auth.load_view(challenge.challenge_id)
                if challenge is not None else None
            )
            if view is None:
                # First-time binding recovery: the preview was kept while the
                # factor was missing, so confirming after enrollment issues a
                # fresh challenge bound to the same trusted preview. No write.
                reissued = services.gateway.authorize_write(
                    action,
                    principal,
                    session.session_id,
                    plan.arguments,
                    chain=services.gateway.new_chain(),
                    idempotency_key=plan.idempotency_key,
                    decision=plan.decision,
                )
                if reissued.challenge is not None:
                    session.pending_challenge = reissued.challenge
                    binding = (pending.lifecycle_binding
                               or getattr(session, "lifecycle_binding", None))
                    if binding is not None:
                        services.totp.bind_preview(
                            reissued.challenge.challenge_id, binding)
                    return ToolResult(
                        status="error", error_code="strong_auth_challenge_issued",
                        message=(
                            "独立TOTP挑战已签发。请完成验证后再次确认；"
                            "本次未执行任何银行操作。"
                        ),
                        data={"challenge": reissued.challenge.model_dump(mode="json"),
                              "required_authentication": "totp"},
                    )
                if reissued.error_code in {"factor_not_bound", "factor_not_confirmed"}:
                    return ToolResult(
                        status="error", error_code=reissued.error_code,
                        message=(
                            "需要先绑定并确认独立验证器；本次未执行任何银行操作。"
                        ),
                        data={"factor_binding_required": True},
                    )
                return ToolResult(
                    status="error", error_code=reissued.error_code or "authorization_required",
                    message=reissued.message or "无法建立新的验证挑战，操作未执行。",
                )
            if view.status != "verified":
                return ToolResult(
                    status="error", error_code="challenge_not_verified",
                    message="该操作需要先完成当前参数绑定的模拟强验证。",
                )
            outcome = services.gateway.grant_for_verified_challenge(
                principal,
                session.session_id,
                view.challenge_id,
                action,
                self.planner.final_arguments(action, plan.arguments),
                idempotency_key=plan.idempotency_key,
            )
            authorization = outcome.authorization
            if authorization is None:
                return ToolResult(
                    status="error",
                    error_code=outcome.error_code or "authorization_required",
                    message=outcome.message or "强验证授权失败，操作未执行。",
                )
        else:
            # A yellow preview cannot be reused after policy escalation.
            if pending.requires_strong_auth or pending.permission_level == "red":
                return ToolResult(
                    status="error", error_code="authorization_parameter_changed",
                    message="权限要求已变化，请重新预览并确认。",
                )
            if pending.authorization_id is None:
                return ToolResult(
                    status="error", error_code="authorization_required",
                    message="缺少当前待办绑定的授权，操作未执行。",
                )
            authorization = AuthorizationGrant(
                authorization_id=pending.authorization_id,
                principal_id=principal.principal_id,
                user_id=int(principal.user_id),
                session_id=session.session_id,
                action=action,
                action_digest=pending.action_digest,
                permission_level=pending.permission_level or "yellow",
                policy_version=pending.policy_version or self.config.policy_version,
                decision_id=pending.decision_id or plan.decision.decision_id,
                challenge_id=None,
                strong_auth_method="confirmation",
                idempotency_key=plan.idempotency_key or "",
            )

        final_arguments = self.planner.final_arguments(action, plan.arguments)

        def runner(context):
            return self.planner.execute(action, final_arguments, context)

        outcome = services.gateway.execute_authorized(
            action,
            principal,
            session.session_id,
            authorization,
            final_arguments,
            runner=runner,
            chain=services.gateway.new_chain(),
            expected_digest=pending.action_digest,
        )
        if not outcome.success:
            return ToolResult(
                status="error",
                error_code=outcome.error_code or "operation_failed",
                message=outcome.message or "操作未成功",
                data=outcome.data,
            )
        return service_result_to_tool_result(outcome.result)
