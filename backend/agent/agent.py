"""Banking Agent 编排：LLM 决策、Router 执行、Session 与确认状态机。"""

from task_presentation import step_title
import json
import re
from datetime import date, datetime, timezone

from pydantic import ValidationError

from card_service import get_cards

from .llm_client import LLMError, RetryingLLMClient
from .constraints import (
    NOT_APPLICABLE_MESSAGE,
    check_amount,
    describe as describe_constraints,
    merge_amount_constraints,
    tool_supports_amount,
)
from .schemas import (
    AgentReply,
    ConstraintDraft,
    PendingClarification,
    SimpleConstraint,
    ToolResult,
)
from .context import ClarificationSummary, PendingSummary
from .input_builder import context_messages, assemble, InputBudgetExceeded, FIXED_DATA_INSTRUCTION
from security.redaction import redact
from .session import AgentSession
from .tool_registry import build_default_registry
from .tool_router import ToolRouter
from .workflow import (
    PlanClarificationRequired,
    PlanValidationError,
    WorkflowEngine,
)


from security.confirmation import (CONFIRM_WORDS, TOOL_CONFIRM_WORDS, _normalized_confirmation,
    _is_explicit_confirmation, _looks_like_transfer_confirmation, _looks_like_confirmation_attempt)

CANCEL_WORDS = {
    "取消",
    "取消操作",
    "不用了",
    "算了",
    "不要执行",
    "否",
    "不要",
    "no",
    "cancel",
    "cancelaction",
}
TASK_PROGRESS_WORDS = {
    "查看任务进度",
    "当前任务",
    "任务状态",
    "查看当前任务",
    "查看任务状态",
}
SAFE_RESPONSE_MESSAGES = {
    "greeting": "你好！我是 Banking Agent，可以安全查询银行卡、消费、订阅、收款人和模拟理财数据，也可以在程序校验与独立确认后办理支持的写操作。",
    "help": "请直接描述要查询或办理的业务，例如查询消费、修改每日限额、转账，或查询和操作本地模拟理财。信息不足时我会继续询问。",
    "capabilities": "我支持账单、银行卡、订阅、预登记收款人和安全转账；也支持修改每日限额，以及查询、购买和赎回本地模拟理财。所有写操作均需程序预览和用户确认。",
    "out_of_scope": "这个请求不在当前 Banking Agent 的能力范围内，我不会据此读取、编造或修改银行数据。",
    "general_conversation": "我可以继续协助当前支持的银行查询与安全操作；银行事实只会依据系统工具结果回答。",
}


# 收款人消歧只接受“明确肯定选择”，不得从任意句子中搜索数字。
RECIPIENT_SELECTION_PATTERNS = (
    re.compile(r"^(?:我)?选择(?:收款人|收款账户|收款方)(\d{1,6})(?:号)?$"),
    re.compile(r"^我选择(\d{1,6})号(?:收款人|收款账户|收款方|账号|卡)?$"),
    re.compile(r"^我选(\d{1,6})号(?:收款人|收款账户|收款方|账号|卡)?$"),
    re.compile(r"^选择(\d{1,6})号(?:收款人|收款账户|收款方|账号|卡)?$"),
    re.compile(r"^选(\d{1,6})号(?:收款人|收款账户|收款方|账号|卡)?$"),
    re.compile(r"^就用(\d{1,6})号(?:收款人|收款账户|收款方|账号|卡)?$"),
    re.compile(r"^就用(\d{1,6})$"),
    re.compile(r"^(\d{1,6})号(?:收款人|收款账户|收款方|账号|卡)?$"),
    re.compile(r"^(\d{1,6})$"),
)
RECIPIENT_SELECTION_DENY_PATTERN = re.compile(
    # 否定、不确定、二选一、反问、并列或附加指令都不能构成肯定选择。
    r"[不别否非没]|排除|还是|或者|以及|其中|哪个|哪一个|多少|怎么|连同|并且|然后|，|,|、|？|\?|！|!"
)
RECIPIENT_SELECTION_DENY_TAIL_PATTERN = re.compile(r"(?:吗|呢|吧|怎样|如何)$")

# Workflow Plan 澄清期间 Python 绝不执行普通业务 Tool，只要求重新生成完整计划。
WORKFLOW_PLAN_ENFORCEMENT_MESSAGE = (
    "补充信息必须用于重新生成完整任务计划；"
    "未执行单独业务工具，也未创建 Task、PendingAction 或幂等键。"
)
# 澄清作用域必须由结构化字段显式声明，Python 不猜测、不默认。
CLARIFICATION_SCOPE_MISSING_MESSAGE = (
    "模型澄清没有声明结构化作用域（single_tool 或 workflow_plan），"
    "已拒绝该澄清；未创建 Task、PendingAction 或幂等键，也未执行任何银行操作。"
)


def _strip_selection_punctuation(text):
    stripped = "".join(
        " " if character in "。．.!！?？,，;；:：、" else character
        for character in text.casefold()
    )
    return " ".join(stripped.split())


def parse_recipient_selection(text):
    """只接受完整匹配的明确肯定选择；否定、二选一、附加指令一律返回 None。"""
    if not isinstance(text, str):
        return None
    normalized = _strip_selection_punctuation(text)
    if not normalized or len(normalized) > 64:
        return None
    if RECIPIENT_SELECTION_DENY_PATTERN.search(normalized):
        return None
    if RECIPIENT_SELECTION_DENY_TAIL_PATTERN.search(normalized):
        return None
    for pattern in RECIPIENT_SELECTION_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match:
            return int(match.group(1))
    return None


WORKFLOW_MISSING_FIELD_QUESTIONS = {
    "from_card_id": "请问这笔转账使用哪张付款卡？请提供银行卡编号，例如“1号卡”。",
    "card_id": "请提供要使用的银行卡编号，例如“1号卡”。",
    "month": "请提供要查询的月份，格式为 YYYY-MM。",
    "recipient_name": "请提供预登记收款人的姓名。",
    "recipient_id": "请提供要选择的预登记收款人编号。",
    "subscription": "请提供要停止扣款的订阅名称。",
    "product_id": "请提供模拟理财产品编号。",
    "holding_id": "请提供模拟理财持仓编号。",
    "amount": "请提供明确的业务金额。",
    "daily_limit": "请提供新的每日交易限额。",
}


class BankingAgent:
    def __init__(
        self,
        llm_client,
        registry=None,
        today_provider=None,
        now_provider=None,
        confirmation_token_provider=None,
        idempotency_key_provider=None,
        transfer_id_provider=None,
        wealth_order_id_provider=None,
        task_id_provider=None,
        llm_max_retries=3,
        llm_retry_backoff_seconds=1.0,
        llm_sleep_fn=None,
        retry_api_errors=True,
        router=None,
    ):
        effective_now_provider = now_provider or (
            lambda: datetime.now(timezone.utc)
        )
        self.registry = registry or build_default_registry(
            now_provider=effective_now_provider,
            transfer_id_provider=transfer_id_provider,
            wealth_order_id_provider=wealth_order_id_provider,
        )
        if isinstance(llm_client, RetryingLLMClient):
            self.llm = llm_client
        else:
            retry_options = {
                "max_retries": llm_max_retries,
                "retry_backoff_seconds": llm_retry_backoff_seconds,
                "retry_api_errors": retry_api_errors,
            }
            if llm_sleep_fn is not None:
                retry_options["sleep_fn"] = llm_sleep_fn
            self.llm = RetryingLLMClient(llm_client, **retry_options)
        delegate = self.llm.delegate
        if hasattr(delegate, "set_tool_argument_models"):
            delegate.set_tool_argument_models(
                {
                    name: self.registry.get(name).argument_model
                    for name in self.registry.names
                }
            )
        # 离线评测可注入一个已完成强验证授权的 Router；生产路径使用默认安全入口。
        if router is not None:
            self.router = router
            # 确保 Agent 与 Router 使用同一个工具注册表，
            # 否则某些路径会拿到不同的 spec（分派与执行不一致）。
            self.registry = router.registry
        else:
            self.router = ToolRouter(
                self.registry,
                now_provider=effective_now_provider,
                confirmation_token_provider=confirmation_token_provider,
                idempotency_key_provider=idempotency_key_provider,
            )
        self.workflow = WorkflowEngine(
            self.registry,
            self.router,
            now_provider=effective_now_provider,
            task_id_provider=task_id_provider,
        )
        self.today_provider = today_provider or date.today

    def handle(self, user_input, session=None):
        session = session or AgentSession()
        text = user_input.strip() if isinstance(user_input, str) else ""
        task = session.current_task
        active = task and task.status not in {'completed', 'failed', 'cancelled', 'expired'}
        from .query_requirements import complete_other_goal, query_followup, WRITE_TOOLS
        # Only unattached drafts may be replaced implicitly. Real previews,
        # challenges and workflow execution retain the original state machine.
        same_write_draft = (session.pending_clarification and
            session.pending_clarification.tool_name in WRITE_TOOLS)
        if (session.pending_clarification and not session.pending_action and
                not session.pending_challenge and not active and complete_other_goal(text) and not same_write_draft):
            session.pending_clarification = None
            self._clear_constraint_draft(session)
            session.context.reset_scope()
            session.messages = []
        continuing = bool(session.pending_action or session.pending_clarification or active or
            session.context.query_requirements and query_followup(text))
        referenced_requirements = None
        if not continuing:
            explicit_previous = bool(re.search(r'刚才(?:那个|那位|选的|的)(?:人|收款人)|刚才那个人', text))
            references = list(session.context.reference_candidates) if explicit_previous else []
            facts = list(session.context.tool_facts) if re.search(r'刚才|上述|前面|这个结果', text) else []
            referenced_requirements = session.context.query_requirements if facts else None
            referenced_date = session.context.original_request_date if facts else None
            session.context.reset_scope()
            if referenced_requirements:
                session.context.original_request_date = referenced_date
            session.context.reference_candidates = references
            session.context.tool_facts = facts
            session.messages = []
        if text and not _looks_like_confirmation_attempt(text):
            if not session.context.original_request_date:
                session.context.original_request_date = self.today_provider().isoformat()
            session.context.original_requests.append({'text': redact(text),
                'issued_date': self.today_provider().isoformat()})
            from .query_requirements import extract
            session.context.query_requirements = extract(text,
                session.context.original_request_date,
                session.context.query_requirements if continuing else referenced_requirements)
            verified_data=getattr(session,'conversation_data_requirement',None)
            session.conversation_data_requirement=None
            if verified_data and verified_data['request']==text:
                if 'query' in verified_data:
                    session.context.query_requirements['data_query']=verified_data['query']
                if 'step_queries' in verified_data:
                    session.context.query_requirements['verified_step_queries']=verified_data['step_queries']
            if session.protocol=='v2' and (session.context.query_requirements or {}).get('flow'):
                from .flow_requirements import verify_card_references
                verify_card_references(session.context.query_requirements['flow'],self.router,session)
        flow=(session.context.query_requirements or {}).get('flow')
        if flow and not flow.get('error') and not session.pending_action and not active:
            from planning_drafts import missing
            from .plan_recovery import clarification
            fields=missing(flow)
            if fields:
                reply=clarification(session,fields,'规划草稿尚未形成正式任务。请补充：'+ '、'.join(fields)+'；原目标、金额和条件会保留，尚未授权或执行资金操作。')
            elif flow.get('ordering')=='ambiguous':
                reply=clarification(session,['depends_on'],'你希望先查询再准备办理，还是将它们作为互不依赖的操作？请回复“按顺序办理”或“互不依赖”；完整目标会保留。')
            else:reply = self._handle(user_input, session)
        else:reply = self._handle(user_input, session)
        if session.pending_clarification is not None and session.clarification_expires_at is None:
            from datetime import timedelta
            session.clarification_expires_at=self.router.now_provider()+timedelta(minutes=5)
        elif session.pending_clarification is None:
            session.clarification_expires_at=None
        if reply.tool_result is not None:
            result = reply.tool_result
            stage = ('preview' if reply.requires_confirmation else 'failed' if not result.success else
                     'committed_receipt' if isinstance(result.data, dict) and result.data.get('write_verification') == 'passed' else 'tool_fact')
            session.context.tool_facts.append({'tool':reply.tool_name, 'stage':stage,
                'observed_at':self.router.now_provider().isoformat(), 'result':redact(result.model_dump(mode='json'))})
            session.context.tool_facts = session.context.tool_facts[-4:]
        if text.casefold() in CANCEL_WORDS:
            references = list(session.context.reference_candidates)
            session.context.reset_scope()
            # Only an explicit "刚才那个人" in the immediately following scope
            # may use this short-lived reference; amounts/conditions are cleared.
            session.context.reference_candidates = references
            session.messages = []
        return reply

    def _handle(self, user_input, session=None):
        session = session or AgentSession()
        text = user_input.strip() if isinstance(user_input, str) else ""
        if not text:
            return AgentReply(
                message="请输入你想办理或查询的银行业务。",
                action="error",
            )

        session.add("user", text)
        session.context.next_turn()
        if _normalized_confirmation(text) in {
            _normalized_confirmation(item) for item in TASK_PROGRESS_WORDS
        }:
            return self._task_progress_reply(session)
        if session.pending_action is not None:
            if (
                _normalized_confirmation(text) not in CANCEL_WORDS
                and not _looks_like_confirmation_attempt(text)
                and getattr(session, "protocol", "v1") != "v2"
            ):
                return self._pending_action_blocks_new_request(session)
            if _normalized_confirmation(text) in CANCEL_WORDS or _looks_like_confirmation_attempt(text):
                if (
                session.current_task is not None
                and session.current_task.status == "waiting_confirmation"
                ):
                    return self._handle_workflow_confirmation(text, session)
                return self._handle_confirmation(text, session)
        if (
            session.pending_clarification is not None
            and session.pending_clarification.kind == "workflow_execution"
        ):
            return self._handle_workflow_clarification(text, session)
        if (
            session.pending_clarification is not None
            and session.pending_clarification.kind == "workflow_plan"
            and _normalized_confirmation(text) in CANCEL_WORDS
        ):
            session.pending_clarification = None
            self._clear_constraint_draft(session)
            message = "任务规划已取消；尚未创建 Task，也未执行任何银行操作。"
            session.add("assistant", message)
            return AgentReply(message=message, action="response")
        if _is_explicit_confirmation(text) or _looks_like_transfer_confirmation(text):
            message = "当前没有等待确认的操作，未执行任何银行操作。"
            session.add("assistant", message)
            return AgentReply(message=message, action="response")

        current_date = self.today_provider()
        try:
            messages = context_messages(session, current_date)
            from .llm_client import control_tools, OpenAICompatibleLLMClient
            if not isinstance(self.llm.delegate, OpenAICompatibleLLMClient):
                assembled, _, metrics = assemble(messages, [*self.registry.llm_tools(), *control_tools()], current_date)
                session.input_metrics = metrics
                # Custom clients have no HTTP payload; transport clients perform
                # pruning after schema compression and adding all request fields.
                messages = [{'role':'system','content':FIXED_DATA_INSTRUCTION}, *assembled[1:]]
            decision = getattr(session, 'conversation_predecision', None)
            session.conversation_predecision = None
            if decision is None:
                decision = self.llm.decide(messages, self.registry.llm_tools(), current_date,
                                           session.pending_clarification)
            if getattr(self.llm.delegate, 'last_input_metrics', None):
                session.input_metrics = dict(self.llm.delegate.last_input_metrics)
            session.input_metrics['provider_usage'] = self.llm.last_usage
        except InputBudgetExceeded as exc:
            session.input_metrics = getattr(exc, 'metrics', {})
            return AgentReply(action='error', message=str(exc), plan_error_code='input_budget_exceeded')
        except LLMError:
            message = "暂时无法连接 LLM API 或解析模型输出，未执行任何银行操作。"
            session.add("assistant", message)
            return AgentReply(message=message, action="error")
        except Exception:
            message = "自然语言处理发生异常，未执行任何银行操作。"
            session.add("assistant", message)
            return AgentReply(message=message, action="error")

        upload_proposed=([decision.tool_name] if decision.action=='tool_call' else
            [s.tool_name for s in decision.task_plan.steps] if decision.action=='task_plan' else [])
        if 'query_uploaded_bills' in upload_proposed:
            # The secure upload-query adapter validates source/merchant/range and
            # avoids copying retractable bill data into task/history stores.
            return AgentReply(action='error',message='上传账单请通过secure聊天明确查询上传对象；当前计划未执行。',plan_error_code='billvision_source_required')

        if getattr(session, 'analysis_only', False):
            from .tool_registry import ToolRisk
            proposed = ([decision.tool_name] if decision.action == 'tool_call' else
                [s.tool_name for s in decision.task_plan.steps] if decision.action == 'task_plan' else [])
            if decision.action == 'modify_action' or any(
                self.registry.get(name) and self.registry.get(name).risk == ToolRisk.WRITE for name in proposed
            ):
                return AgentReply(action='error', message='本次只授权分析；提出业务动作需明确选择 action 模式，仍须逐项确认。',
                    plan_error_code='analysis_intent_only')

        if decision.action == "modify_action":
            return self._handle_modification(decision.modification, session)

        if session.pending_action is not None:
            # V2 lets the model propose a typed modification, but an unrelated
            # tool call/response cannot silently replace the current preview.
            return self._pending_action_blocks_new_request(session)

        if session.protocol == 'v2' and decision.action == 'tool_call':
            denied = self.router.read_permission_error(decision.tool_name, decision.arguments,
                self._principal(session), session)
            if denied:
                return AgentReply(action='error', message=denied.result.message,
                    tool_name=decision.tool_name, tool_result=denied.result)
            from .query_requirements import check
            failure = check(session.context.query_requirements,
                [(decision.tool_name, decision.arguments)])
            if failure:
                session.pending_clarification = PendingClarification(kind='single_tool',
                    tool_name=decision.tool_name, missing_fields=[], known_arguments={})
                return AgentReply(action='error', message=failure[1], plan_error_code=failure[0])

        if decision.action == "clarification":
            if 'query_scope' in decision.missing_fields:
                # query_scope is an internal diagnosis, never a public slot.
                decision.missing_fields=[x for x in decision.missing_fields if x!='query_scope']
                if not decision.missing_fields:
                    decision.clarification_scope='single_tool'
            if decision.tool_name and self.registry.get(decision.tool_name) is None:
                message = f"无法继续：模型请求了未注册工具 {decision.tool_name}。"
                session.add("assistant", message)
                return AgentReply(message=message, action="error")
            if decision.clarification_scope is None:
                # 没有结构化作用域时不得猜成单 Tool 澄清，否则会悄悄丢失多目标 Workflow。
                message = CLARIFICATION_SCOPE_MISSING_MESSAGE
                session.add("assistant", message)
                return AgentReply(message=message, action="error")
            workflow_scope = decision.clarification_scope == "workflow_plan"
            if workflow_scope and (not decision.missing_fields or not decision.tool_name):
                # Workflow Plan 澄清必须声明缺失的公开字段，否则无法保证补齐后重新规划。
                message = "模型请求的多目标任务计划澄清缺少明确的缺失字段，未创建或执行任何任务。"
                session.add("assistant", message)
                return AgentReply(message=message, action="error")
            previous_pending = session.pending_clarification
            planning_recovery = (
                previous_pending is not None
                and previous_pending.kind == "workflow_plan"
            )
            if planning_recovery and not workflow_scope:
                # 已经进入 Workflow Plan 澄清后，不因模型换作用域而降级为单 Tool 澄清。
                session.pending_clarification = previous_pending
                session.add("assistant", decision.message)
                return AgentReply(
                    message=decision.message,
                    action="clarification",
                    tool_name=previous_pending.tool_name,
                    clarification_scope="workflow_plan",
                )
            pending_kind = (
                "workflow_plan" if (workflow_scope or planning_recovery) else "single_tool"
            )
            session.pending_clarification = PendingClarification(
                kind=pending_kind,
                tool_name=(
                    decision.tool_name
                    or (previous_pending.tool_name if planning_recovery else None)
                ),
                known_arguments=(
                    decision.arguments
                    or (previous_pending.known_arguments if planning_recovery else {})
                ),
                missing_fields=(
                    decision.missing_fields
                    or (previous_pending.missing_fields if planning_recovery else [])
                ),
                requires_full_replan=pending_kind == "workflow_plan",
            )
            session.context.missing_fields = list(
                session.pending_clarification.missing_fields
            )
            session.context.clarification = ClarificationSummary(
                tool_name=session.pending_clarification.tool_name,
                missing_fields=list(session.pending_clarification.missing_fields),
                reason="missing_or_ambiguous_information",
            )
            session.add("assistant", decision.message)
            return AgentReply(
                message=decision.message,
                action="clarification",
                tool_name=decision.tool_name,
                clarification_scope=pending_kind,
            )

        if decision.action == "response":
            if (
                session.pending_clarification is not None
                and session.pending_clarification.kind == "workflow_plan"
            ):
                message = "仍需补齐任务计划缺少的公开业务字段，尚未创建或执行任务。"
                session.add("assistant", message)
                return AgentReply(
                    message=message,
                    action="clarification",
                    tool_name=session.pending_clarification.tool_name,
                    clarification_scope="workflow_plan",
                )
            # Natural conversational prose is allowed; it may not manufacture
            # numeric bank facts or claim a write succeeded.
            message = (decision.message or '').strip()
            if not message or re.search(r'\d|余额|已(?:到账|转账|付款|扣款|保存)|成功|合计|总额', message):
                message = SAFE_RESPONSE_MESSAGES[decision.response_type]
            session.add("assistant", message)
            return AgentReply(message=message, action="response")

        if decision.action == "task_plan":
            session.pending_clarification = None
            # 新任务不继承任何其他操作/步骤的约束草稿。
            self._clear_constraint_draft(session)
            try:
                from .plan_recovery import prepare
                decision.task_plan=prepare(decision.task_plan,session,self.workflow.validator)
                from planning_drafts import consume
                consume(session)
                self.workflow.start(decision.task_plan, session)
            except PlanClarificationRequired as error:
                session.pending_clarification = PendingClarification(
                    kind="workflow_plan",
                    tool_name=error.tool_name,
                    known_arguments=error.known_arguments,
                    missing_fields=error.missing_fields,
                    requires_full_replan=True,
                )
                message = self._workflow_missing_fields_message(
                    error.missing_fields
                )
                session.add("assistant", message)
                return AgentReply(
                    message=message,
                    action="clarification",
                    tool_name=error.tool_name,
                    clarification_scope="workflow_plan",
                    plan_error_code=error.code,
                )
            except PlanValidationError as error:
                flow=(session.context.query_requirements or {}).get('flow')
                if flow and not flow.get('error'):
                    from .plan_recovery import clarification
                    field='depends_on' if ('依赖' in error.safe_message or error.code=='dependency_order_unclear') else 'complete_plan'
                    message=('计划没有保留已确认的依赖或条件。请回复“按原顺序继续”，我会依据原目标重新生成完整计划。' if field=='depends_on' else
                        '候选计划未保留完整目标、参数或条件，已安全拒绝。请回复“按原顺序继续”重新规划；原需求仍保留。')
                    reply=clarification(session,[field],message)
                    return reply.model_copy(update={'plan_error_code':error.code})
                if error.code.startswith('query_'):
                    session.pending_clarification = PendingClarification(kind='single_tool',
                        tool_name=decision.task_plan.steps[0].tool_name,
                        missing_fields=[], known_arguments={})
                message = f"任务计划未通过安全校验：{error.safe_message}"
                session.add("assistant", message)
                return AgentReply(
                    message=message,
                    action="error",
                    plan_error_code=error.code,
                )
            except Exception:
                message = "任务计划处理异常，未继续执行银行操作。"
                session.add("assistant", message)
                return AgentReply(
                    message=message,
                    action="error",
                    plan_error_code="plan_execution_exception",
                )
            return self._workflow_reply(session)

        if (
            session.pending_clarification is not None
            and session.pending_clarification.kind == "workflow_plan"
        ):
            message = WORKFLOW_PLAN_ENFORCEMENT_MESSAGE
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="clarification",
                tool_name=session.pending_clarification.tool_name,
                clarification_scope="workflow_plan",
            )

        rejected = self._rejected_single_tool_selection(decision, session)
        if rejected is not None:
            session.add("assistant", rejected.message)
            return rejected

        # 补参轮次沿用同一澄清：重新预览前必须继承仍未执行的金额上限。
        continuation_constraints = self._continuation_constraints(session, decision)
        if continuation_constraints:
            conflict = check_amount(decision.arguments, continuation_constraints)
            if conflict is not None:
                return self._modification_clarification(
                    session, decision.tool_name, conflict[1], ["amount"],
                    conflict[0],
                    known_arguments=dict(decision.arguments),
                    constraints=continuation_constraints,
                )

        dispatch = self.router.dispatch(
            decision.tool_name, decision.arguments,
            principal=self._principal(session), session=session,
        )
        if not dispatch.result.success:
            if (
                decision.tool_name == "transfer_money"
                and dispatch.result.error_code == "recipient_ambiguous"
            ):
                return self._set_recipient_clarification(
                    session, decision.arguments, dispatch.result,
                    constraints=continuation_constraints,
                )
            session.pending_clarification = (PendingClarification(kind='single_tool',
                tool_name=decision.tool_name, missing_fields=['merchant'], known_arguments={})
                if decision.tool_name=='query_financial_records' and dispatch.result.error_code=='query_scope_unclear' else None)
            message = dispatch.result.message
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="error",
                tool_name=decision.tool_name,
                tool_result=dispatch.result,
            )

        session.pending_clarification = None
        session.context.missing_fields = []
        session.context.clarification = None
        if dispatch.pending_action is not None:
            if continuation_constraints:
                # 约束随新的可信预览继续生效，并结束跨轮草稿。
                dispatch.pending_action.constraints = list(
                    continuation_constraints
                )
            self._clear_constraint_draft(session)
            session.pending_action = dispatch.pending_action
            self._remember_pending(session, dispatch.pending_action, dispatch.result)
            message = dispatch.pending_action.confirmation_message
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="confirmation_required",
                tool_name=decision.tool_name,
                tool_result=dispatch.result,
                requires_confirmation=True,
            )

        verification_error = self._direct_verification_error(
            decision.tool_name, dispatch.result
        )
        if verification_error is not None:
            session.add("assistant", verification_error.message)
            return verification_error
        message = self._format_tool_result(decision.tool_name, dispatch.result)
        session.add("assistant", message)
        return AgentReply(
            message=message,
            action="tool_result",
            tool_name=decision.tool_name,
            tool_result=dispatch.result,
        )

    def _handle_modification(self, modification, session):
        """Apply one LLM proposal through deterministic Python validation.

        已有约束是可信状态：本轮省略 ``constraints`` 或给出空列表都不会清除，
        收紧由 Python 取更紧的上限，放宽/删除只能澄清并保留原约束。
        """
        if getattr(session, "protocol", "v1") != "v2":
            message = "当前协议不支持自然语言修改待办；原操作未改变。"
            session.add("assistant", message)
            return AgentReply(message=message, action="error")
        if modification.target == "completed_action":
            message = (
                "已完成的历史交易不能修改，也不会自动创建或执行新交易。"
                "如果你希望按新参数发起一笔新的操作，请明确说“发起新转账”并补齐参数。"
            )
            session.context.clarification = ClarificationSummary(
                tool_name=modification.tool_name,
                missing_fields=["new_operation_confirmation"],
                reason="completed_action_is_immutable",
            )
            session.add("assistant", message)
            return AgentReply(
                message=message, action="clarification",
                tool_name=modification.tool_name,
                clarification_scope="single_tool",
            )

        base = {}
        pending = session.pending_action
        task = session.current_task
        if (modification.target == 'pending_action' and pending is not None and task is not None
                and task.status == 'waiting_confirmation'):
            # A model may refer to the current preview without knowing the
            # workflow protocol. Modify its unique owning step, preserving DAG
            # dependencies, conditions and constraints instead of detaching it.
            candidates = [s for s in task.steps if s.step_id == task.current_step_id
                and s.status == 'waiting_confirmation' and s.tool_name == pending.tool_name
                and s.tool_name == modification.tool_name]
            if len(candidates) == 1:
                modification = modification.model_copy(update={
                    'target': 'workflow_step', 'step_id': candidates[0].step_id})
        target_step = None
        existing_constraints: list[SimpleConstraint] = []
        draft = self._matching_constraint_draft(session, modification)
        if modification.target == "pending_action":
            if pending is not None and pending.tool_name != modification.tool_name:
                return self._modification_error(
                    session, modification.tool_name,
                    "当前没有与修改目标匹配的未执行待办。",
                    "modification_target_missing",
                )
            if pending is not None:
                base = dict(pending.requested_arguments)
                existing_constraints = list(pending.constraints)
            elif draft is not None:
                # 澄清/失败后继续修改同一操作：继承仍然明确归属该操作的参数与约束。
                base = dict(draft.arguments)
                existing_constraints = list(draft.constraints)
            else:
                return self._modification_error(
                    session, modification.tool_name,
                    "当前没有与修改目标匹配的未执行待办。",
                    "modification_target_missing",
                )
        elif modification.target == "workflow_step":
            task = session.current_task
            if task is None:
                return self._modification_error(
                    session, modification.tool_name, "当前没有可修改的任务。",
                    "workflow_not_modifiable",
                )
            candidates = [
                item for item in task.steps
                if item.tool_name == modification.tool_name
                and item.status in {
                    "pending", "waiting_confirmation", "waiting_clarification"
                }
                and (
                    modification.step_id is None
                    or item.step_id == modification.step_id
                )
            ]
            if len(candidates) != 1:
                return self._modification_clarification(
                    session, modification.tool_name,
                    "存在多个或没有匹配的未执行步骤，请明确要修改的步骤。",
                    ["step_id"], "workflow_step_not_unique",
                )
            target_step = candidates[0]
            base = dict(target_step.arguments)
            existing_constraints = list(target_step.constraints)
            if draft is not None and draft.step_id == target_step.step_id:
                existing_constraints = merge_amount_constraints(
                    existing_constraints, draft.constraints
                ).constraints
        elif modification.target == "new_action":
            if pending is not None:
                return self._pending_action_blocks_new_request(session)
            # 明确的新操作：不继承任何旧的公开参数或约束。
            self._clear_constraint_draft(session)
        else:
            return self._modification_error(
                session, modification.tool_name, "不支持的修改目标。",
                "modification_target_invalid",
            )

        step_id = modification.step_id or (
            target_step.step_id if target_step is not None else None
        )

        # Once the user has issued a semantic modification command, the old
        # preview must never remain executable even if the new value later
        # proves invalid or needs clarification.
        if modification.target == "pending_action":
            self._invalidate_pending_security(session)
        elif (
            modification.target == "workflow_step"
            and target_step is not None
            and target_step.status in {"waiting_confirmation", "waiting_clarification"}
        ):
            self._invalidate_pending_security(session)

        spec = self.registry.get(modification.tool_name)
        if spec is None:
            return self._modification_error(
                session, modification.tool_name, "修改目标不是已注册业务工具。",
                "unknown_tool",
                known_arguments=base, constraints=existing_constraints,
                step_id=step_id,
            )
        allowed = set(spec.argument_model.model_fields)
        if set(modification.changes) - allowed:
            return self._modification_error(
                session, modification.tool_name,
                "修改包含该业务不允许变更的字段，原待办未执行。",
                "field_not_modifiable",
                known_arguments=base, constraints=existing_constraints,
                step_id=step_id,
            )

        # 约束合并发生在任何预览之前：省略/空列表不清除，收紧取更紧，
        # 放宽/删除/不受支持一律当轮澄清并保留原约束。
        outcome = merge_amount_constraints(
            existing_constraints, modification.constraints
        )
        if not outcome.ok:
            return self._modification_clarification(
                session, modification.tool_name, outcome.message,
                ["amount"], outcome.error_code,
                known_arguments=base, constraints=outcome.constraints,
                step_id=step_id,
            )
        merged_constraints = outcome.constraints
        if merged_constraints and not tool_supports_amount(spec.argument_model):
            return self._modification_clarification(
                session, modification.tool_name, NOT_APPLICABLE_MESSAGE,
                ["amount"], "constraint_not_supported",
                known_arguments=base, constraints=[], step_id=step_id,
            )

        changes = dict(modification.changes)
        resolved = self._resolve_references(
            session, modification, base, changes,
            constraints=merged_constraints, step_id=step_id,
        )
        if isinstance(resolved, AgentReply):
            return resolved
        changes = resolved
        if modification.tool_name == "transfer_money":
            if "recipient_name" in changes and "recipient_id" not in changes:
                base.pop("recipient_id", None)
            if "recipient_id" in changes and "recipient_name" not in changes:
                base.pop("recipient_name", None)
        merged = {**base, **changes}
        try:
            validated = spec.argument_model.model_validate(merged)
        except ValidationError:
            return self._modification_clarification(
                session, modification.tool_name,
                "修改后的参数不完整或不合法，请补充明确且合法的业务参数。",
                self._missing_public_fields(spec, merged),
                "invalid_modified_arguments",
                known_arguments=merged, constraints=merged_constraints,
                step_id=step_id,
            )
        public_arguments = validated.model_dump(mode="json")
        from storage.config import pg02_enabled,PG04_WEALTH_TOOLS
        if pg02_enabled() and modification.tool_name in PG04_WEALTH_TOOLS:
            from .wealth_requirements import check as check_wealth
            wanted=(session.context.query_requirements or {}).get('wealth')
            selected=[s for s in (wanted or {}).get('steps',[]) if s[0]==modification.tool_name]
            failure=(check_wealth(dict(steps=selected,unclear=wanted.get('unclear',True)),
                [(modification.tool_name,public_arguments)],None) if wanted else
                ('query_scope_unclear','无法核实理财修改目标，请明确产品/持仓、卡和金额。'))
            if failure:
                return self._modification_error(session,modification.tool_name,failure[1],failure[0],
                    known_arguments=base,constraints=merged_constraints,step_id=step_id)
        conflict = check_amount(public_arguments, merged_constraints)
        if conflict is not None:
            return self._modification_clarification(
                session, modification.tool_name, conflict[1],
                ["amount"], conflict[0],
                known_arguments=public_arguments, constraints=merged_constraints,
                step_id=step_id,
            )

        if modification.target == "workflow_step":
            try:
                updated_step = self.workflow.modify_unexecuted_step(
                    session,
                    tool_name=modification.tool_name,
                    changes=changes,
                    step_id=modification.step_id,
                    constraints=merged_constraints,
                )
            except PlanValidationError as error:
                return self._modification_error(
                    session, modification.tool_name,
                    f"任务步骤修改未通过安全校验：{error.safe_message}",
                    error.code,
                    known_arguments=public_arguments,
                    constraints=merged_constraints, step_id=step_id,
                )
            self._clear_constraint_draft(session)
            waiting_step = next(
                (item for item in session.current_task.steps
                 if item.status == "waiting_confirmation"),
                None,
            )
            if (
                session.pending_action is not None
                and waiting_step is not None
                and waiting_step.step_id == updated_step.step_id
            ):
                # 只有被修改的步骤本身进入了等待确认，才能把步骤约束写入该预览；
                # 其他步骤的待办绝不能被写入本步骤的约束。
                session.pending_action.constraints = list(updated_step.constraints)
                self._remember_pending(
                    session, session.pending_action, waiting_step.result
                )
            return self._workflow_reply(session)

        dispatch = self.router.dispatch(
            modification.tool_name,
            public_arguments,
            principal=self._principal(session),
            session=session,
        )
        if not dispatch.result.success:
            if (
                modification.tool_name == "transfer_money"
                and dispatch.result.error_code == "recipient_ambiguous"
            ):
                return self._set_recipient_clarification(
                    session, public_arguments, dispatch.result,
                    constraints=merged_constraints,
                )
            return self._modification_error(
                session, modification.tool_name, dispatch.result.message,
                dispatch.result.error_code or "modified_preview_failed",
                tool_result=dispatch.result,
                known_arguments=public_arguments,
                constraints=merged_constraints,
                step_id=step_id,
            )
        if dispatch.pending_action is None:
            return self._modification_error(
                session, modification.tool_name,
                "修改目标没有生成新的安全预览，原操作未执行。",
                "modified_preview_missing",
                known_arguments=public_arguments,
                constraints=merged_constraints, step_id=step_id,
            )
        dispatch.pending_action.constraints = list(merged_constraints)
        session.pending_action = dispatch.pending_action
        self._clear_constraint_draft(session)
        self._remember_pending(session, dispatch.pending_action, dispatch.result)
        message = "参数已安全修改；旧预览、确认和验证均已失效。" + dispatch.pending_action.confirmation_message
        if dispatch.pending_action.requires_strong_auth:
            message = "参数已安全修改；旧预览、确认和验证均已失效。新操作需要重新完成模拟强验证和确认。" + dispatch.pending_action.confirmation_message
        session.add("assistant", message)
        return AgentReply(
            message=message,
            action="confirmation_required",
            tool_name=modification.tool_name,
            tool_result=dispatch.result,
            requires_confirmation=True,
        )

    @staticmethod
    def _matching_constraint_draft(session, modification):
        draft = getattr(session, "constraint_draft", None)
        if draft is None or draft.tool_name != modification.tool_name:
            return None
        if modification.target == "pending_action":
            # 单操作草稿只在没有新的 PendingAction 时作为继续修改的基础。
            return draft if draft.step_id is None else None
        if modification.target == "workflow_step":
            return draft
        return None

    @staticmethod
    def _clear_constraint_draft(session):
        session.constraint_draft = None

    @staticmethod
    def _set_constraint_draft(session, *, tool_name, arguments, constraints,
                              reason, step_id=None, task_id=None):
        if not constraints:
            return
        session.constraint_draft = ConstraintDraft(
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            constraints=list(constraints),
            step_id=step_id,
            task_id=task_id,
            reason=reason,
        )

    @staticmethod
    def _continuation_constraints(session, decision):
        """补参轮次：同一澄清下重新预览时必须继承仍未执行的金额上限。"""
        clarification = session.pending_clarification
        draft = getattr(session, "constraint_draft", None)
        if clarification is None or draft is None:
            return None
        if getattr(clarification, "kind", "single_tool") != "single_tool":
            return None
        if clarification.tool_name != decision.tool_name:
            return None
        if draft.tool_name != decision.tool_name or draft.step_id is not None:
            return None
        return list(draft.constraints)

    def _resolve_references(self, session, modification, base, changes,
                            constraints=None, step_id=None):
        for reference in modification.references:
            if reference.field == "recipient":
                recipients = {
                    item.object_id: item
                    for item in session.context.reference_candidates
                    if item.kind == "recipient"
                }
                selected = next(iter(recipients.values())) if len(recipients) == 1 else None
                if reference.reference not in {"selected", "previous"} or selected is None:
                    return self._modification_clarification(
                        session, modification.tool_name,
                        "当前会话没有唯一、明确选择的收款人；请说明姓名或选择收款人编号。",
                        ["recipient_id"], "recipient_reference_ambiguous",
                        known_arguments=base, constraints=constraints,
                        step_id=step_id,
                    )
                changes["recipient_id"] = selected.object_id
                changes.pop("recipient_name", None)
            elif reference.field == "from_card" and reference.reference == "other":
                current = changes.get("from_card_id", base.get("from_card_id"))
                result = get_cards(principal=self._principal(session))
                cards = [
                    item for item in (result.get("data") or [])
                    if item.get("status") in {"normal", "正常"}
                    and item.get("card_id") != current
                ]
                if len(cards) != 1:
                    self._invalidate_pending_security(session)
                    return self._modification_clarification(
                        session, modification.tool_name,
                        "“另一张卡”不能唯一确定，请明确选择付款银行卡编号。",
                        ["from_card_id"], "other_card_ambiguous",
                        known_arguments=base, constraints=constraints,
                        step_id=step_id,
                    )
                changes["from_card_id"] = cards[0]["card_id"]
            else:
                return self._modification_clarification(
                    session, modification.tool_name,
                    "该对象引用不受支持，请明确提供业务对象。",
                    [reference.field], "reference_not_supported",
                    known_arguments=base, constraints=constraints,
                    step_id=step_id,
                )
        return changes

    @staticmethod
    def _validate_constraints(arguments, constraints):
        """保留旧调用点：金额上限校验统一走约束模块。"""
        outcome = check_amount(arguments, constraints)
        return outcome[1] if outcome is not None else None

    @staticmethod
    def _missing_public_fields(spec, arguments):
        return [
            name for name, field in spec.argument_model.model_fields.items()
            if field.is_required() and name not in arguments
        ] or ["business_parameters"]

    def _invalidate_pending_security(self, session, clear_constraints=False):
        challenge = getattr(session, "pending_challenge", None)
        if challenge is not None:
            try:
                from security.service import get_security_services
                get_security_services().strong_auth.cancel(
                    self._principal(session), session.session_id,
                    challenge.challenge_id,
                )
            except Exception:
                pass
        session.pending_challenge = None
        session.pending_action = None
        session.context.pending = None
        session.request_idempotency_key = None
        session.request_signature = None
        session.request_action = None
        session.request_action_digest = None
        session.request_arguments = None
        session.request_expires_at = None
        if clear_constraints:
            # 取消/过期意味着该操作被放弃，绑定它的约束草稿必须一起清除。
            self._clear_constraint_draft(session)

    def _remember_pending(self, session, pending, result):
        preview = result.data if result is not None and isinstance(result.data, dict) else {}
        if pending.tool_name == "transfer_money":
            recipient_id = preview.get("recipient_id") or pending.requested_arguments.get("recipient_id")
            recipient_name = preview.get("recipient_name") or pending.requested_arguments.get("recipient_name")
            card_id = preview.get("from_card_id") or pending.requested_arguments.get("from_card_id")
            if isinstance(recipient_id, int) and recipient_name:
                session.context.select("recipient", recipient_id, str(recipient_name), "preview")
            if isinstance(card_id, int):
                display = preview.get("from_masked_card_number") or f"卡 {card_id}"
                session.context.select("card", card_id, str(display), "preview")
        task = session.current_task
        waiting = (
            next((item for item in task.steps if item.status == "waiting_confirmation"), None)
            if task is not None else None
        )
        session.context.pending = PendingSummary(
            kind="workflow_step" if waiting is not None else "action",
            tool_name=pending.tool_name,
            step_id=waiting.step_id if waiting is not None else None,
            public_arguments=dict(pending.requested_arguments),
            constraints=[item.model_dump(mode="json") for item in pending.constraints],
            revision=pending.revision,
        )
        session.context.missing_fields = []
        session.context.clarification = None

    def _set_recipient_clarification(self, session, arguments, result,
                                     constraints=None):
        constraints = list(constraints or [])
        session.pending_clarification = PendingClarification(
            tool_name="transfer_money",
            known_arguments=arguments,
            missing_fields=["recipient_id"],
        )
        session.context.pending = None
        session.context.missing_fields = ["recipient_id"]
        session.context.clarification = ClarificationSummary(
            tool_name="transfer_money", missing_fields=["recipient_id"],
            reason="recipient_ambiguous",
            constraints=describe_constraints(constraints),
        )
        self._set_constraint_draft(
            session, tool_name="transfer_money", arguments=arguments,
            constraints=constraints, reason="recipient_ambiguous",
        )
        candidates = (result.data or {}).get("candidates", [])
        details = "；".join(
            f"{item['recipient_id']}号 {item['name']}，{item['bank_name']} {item['masked_account']}"
            for item in candidates
        )
        message = f"{result.message}：{details}。{self.RECIPIENT_SELECTION_NOTICE}"
        session.add("assistant", message)
        return AgentReply(
            message=message, action="clarification", tool_name="transfer_money",
            tool_result=result, clarification_scope="single_tool",
        )

    def _modification_clarification(self, session, tool_name, message,
                                    missing_fields, reason,
                                    known_arguments=None, constraints=None,
                                    step_id=None):
        constraints = list(constraints or [])
        session.pending_clarification = PendingClarification(
            tool_name=tool_name,
            known_arguments=dict(known_arguments or {}),
            missing_fields=list(missing_fields),
        )
        session.context.missing_fields = list(missing_fields)
        session.context.clarification = ClarificationSummary(
            tool_name=tool_name, missing_fields=list(missing_fields),
            reason=reason, constraints=describe_constraints(constraints),
        )
        self._set_constraint_draft(
            session, tool_name=tool_name, arguments=known_arguments,
            constraints=constraints, reason=reason, step_id=step_id,
        )
        session.add("assistant", message)
        return AgentReply(
            message=message, action="clarification", tool_name=tool_name,
            clarification_scope="single_tool", plan_error_code=reason,
        )

    def _modification_error(self, session, tool_name, message, code,
                            tool_result=None, known_arguments=None,
                            constraints=None, step_id=None):
        self._set_constraint_draft(
            session, tool_name=tool_name, arguments=known_arguments,
            constraints=constraints or [], reason=code, step_id=step_id,
        )
        session.add("assistant", message)
        return AgentReply(
            message=message, action="error", tool_name=tool_name,
            tool_result=tool_result, plan_error_code=code,
        )

    def _principal(self, session):
        """Server-side identity for this session.

        V1 has no login entry point, so an unauthenticated session binds to the
        isolated legacy demo principal (owner scope 0).  V2 sets the principal
        from the verified credential; the request body can never set it.
        """
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

    @staticmethod
    def _workflow_missing_fields_message(missing_fields):
        questions = [
            WORKFLOW_MISSING_FIELD_QUESTIONS.get(
                field, f"请补充任务步骤所需的 {field}。"
            )
            for field in missing_fields
        ]
        return " ".join(questions) + " 当前尚未创建或执行任务。"

    def _pending_action_blocks_new_request(self, session):
        pending = session.pending_action
        message = (
            "当前仍有一项操作等待确认。你的新请求尚未执行，请先确认或取消当前操作。\n"
            + pending.confirmation_message
        )
        session.add("assistant", message)
        return AgentReply(
            message=message,
            action="confirmation_required",
            tool_name=pending.tool_name,
            requires_confirmation=True,
        )

    def _rejected_single_tool_selection(self, decision, session):
        """只允许在单 Tool 消歧澄清的候选中选择收款人，且必须保持原姓名。"""
        pending = session.pending_clarification
        if (
            pending is None
            or pending.kind not in {"single_tool", "workflow_plan"}
            or decision.tool_name != "transfer_money"
            or pending.tool_name != "transfer_money"
        ):
            return None
        requested_name = (pending.known_arguments or {}).get("recipient_name")
        arguments = decision.arguments or {}
        recipient_id = arguments.get("recipient_id")
        if recipient_id is None:
            return None
        candidate_ids = self.registry.candidate_recipient_ids(
            decision.tool_name, pending.known_arguments, self._principal(session)
        )
        if candidate_ids is None:
            return None
        if recipient_id in candidate_ids and (
            requested_name is None or arguments.get("recipient_name") == requested_name
        ):
            return None
        session.pending_clarification = None
        message = (
            "该收款人编号不在当前澄清候选中，或姓名与原请求不一致；"
            "未创建 Task、PendingAction 或幂等键，也未执行任何银行操作。请重新发起或重新选择。"
        )
        return AgentReply(
            message=message,
            action="error",
            tool_name=decision.tool_name,
        )

    def _handle_workflow_clarification(self, text, session):
        pending = session.pending_clarification
        normalized = _normalized_confirmation(text)
        if self.workflow.is_clarification_expired(pending):
            self.workflow.expire(session)
            message = self._format_task_summary(session.current_task)
            session.add("assistant", message)
            return AgentReply(message=message, action="error", tool_name=pending.tool_name, plan_error_code="pending_clarification_expired")
        if normalized in CANCEL_WORDS:
            self.workflow.cancel(session)
            message = self._format_task_summary(session.current_task)
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="task_result",
                tool_name=pending.tool_name,
            )
        recipient_id = parse_recipient_selection(text)
        if recipient_id is None:
            # 否定、二选一或附加指令都不得改变当前步骤参数。
            message = self._workflow_clarification_message(
                session, notice=self.RECIPIENT_SELECTION_NOTICE
            )
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="clarification",
                tool_name=pending.tool_name,
            )
        try:
            self.workflow.resume_clarification(
                session, {"recipient_id": recipient_id}
            )
        except ValueError as error:
            message = self._workflow_clarification_message(
                session, notice=str(error)
            )
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="clarification",
                tool_name=pending.tool_name,
            )
        return self._workflow_reply(session, last_tool_name=pending.tool_name)

    RECIPIENT_SELECTION_NOTICE = (
        "请只使用明确的肯定格式选择，例如“4”“4号”“选择4号收款人”；"
        "否定、二选一或附加其他指令都不会被当作选择，当前步骤参数未改变。"
    )

    @staticmethod
    def _workflow_clarification_message(session, notice=None):
        task = session.current_task
        step = next(
            (
                item
                for item in task.steps
                if item.status == "waiting_clarification"
            ),
            None,
        )
        candidates = ((step.result.data or {}).get("candidates", [])) if step and step.result else []
        details = "；".join(
            f"{item['recipient_id']}号 {item['name']}，"
            f"{item['bank_name']} {item['masked_account']}，"
            f"可信状态 {item['trusted']}"
            for item in candidates
        )
        base = step.result.message if step and step.result else "请明确选择收款人编号"
        message = f"{base}：{details}。" if details else base
        if notice:
            message = f"{message}{notice}"
        return message

    def _handle_confirmation(self, text, session):
        normalized = _normalized_confirmation(text)
        pending = session.pending_action
        if self.router.is_pending_expired(pending):
            session.pending_action = None
            session.context.pending = None
            self._clear_constraint_draft(session)
            message = "该操作确认已过期，请重新发起。"
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="error",
                tool_name=pending.tool_name,
                plan_error_code="pending_action_expired",
            )
        if normalized in CANCEL_WORDS:
            self._invalidate_pending_security(session, clear_constraints=True)
            message = "操作已取消，银行数据未发生变化。"
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="response",
                tool_name=pending.tool_name,
            )
        confirmed = (
            self.router.matches_dynamic_confirmation(pending, text)
            if pending.confirmation_phrase is not None
            else _is_explicit_confirmation(text, pending)
        )
        if not confirmed:
            message = pending.confirmation_message
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="confirmation_required",
                tool_name=pending.tool_name,
                requires_confirmation=True,
            )

        result = self.router.execute_pending(
            pending, principal=self._principal(session), session=session
        )
        if not result.success:
            if result.error_code == "permission_escalated_to_strong_auth":
                self._remember_pending(session, pending, result)
            if (
                getattr(session, "protocol", "v1") != "v2"
                or result.error_code not in {
                    "challenge_not_verified",
                    "permission_escalated_to_strong_auth",
                }
            ):
                session.pending_action = None
            message = result.message
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="error",
                tool_name=pending.tool_name,
                tool_result=result,
            )

        session.pending_action = None
        session.pending_challenge = None
        session.context.pending = None
        session.request_action = None
        session.request_action_digest = None
        session.request_arguments = None
        session.request_expires_at = None
        session.request_idempotency_key = None
        session.request_signature = None
        self._clear_constraint_draft(session)
        session.context.record_executed(
            pending.tool_name,
            pending.requested_arguments,
            (result.data or {}).get("transfer_id")
            if isinstance(result.data, dict) else None,
        )

        verification_error = self._direct_verification_error(
            pending.tool_name, result
        )
        if verification_error is not None:
            session.add("assistant", verification_error.message)
            return verification_error
        message = self._format_tool_result(pending.tool_name, result)
        session.add("assistant", message)
        return AgentReply(
            message=message,
            action="tool_result",
            tool_name=pending.tool_name,
            tool_result=result,
        )

    def _handle_workflow_confirmation(self, text, session):
        normalized = _normalized_confirmation(text)
        pending = session.pending_action
        if self.router.is_pending_expired(pending):
            tool_name = pending.tool_name
            self.workflow.expire(session)
            session.context.pending = None
            self._clear_constraint_draft(session)
            message = self._format_task_summary(session.current_task)
            session.add("assistant", message)
            return AgentReply(message=message, action="error", tool_name=tool_name, plan_error_code="pending_action_expired")
        if normalized in CANCEL_WORDS:
            tool_name = pending.tool_name
            self.workflow.cancel(session)
            self._invalidate_pending_security(session, clear_constraints=True)
            message = self._format_task_summary(session.current_task)
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="task_result",
                tool_name=tool_name,
            )
        confirmed = (
            self.router.matches_dynamic_confirmation(pending, text)
            if pending.confirmation_phrase is not None
            else _is_explicit_confirmation(text, pending)
        )
        if not confirmed:
            message = pending.confirmation_message
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="confirmation_required",
                tool_name=pending.tool_name,
                requires_confirmation=True,
            )

        tool_name = pending.tool_name
        result = self.router.execute_pending(
            pending, principal=self._principal(session), session=session
        )
        if (
            not result.success
            and getattr(session, "protocol", "v1") == "v2"
            and result.error_code in {
                "challenge_not_verified",
                "permission_escalated_to_strong_auth",
            }
        ):
            if result.error_code == "permission_escalated_to_strong_auth":
                self._remember_pending(session, pending, result)
            message = result.message
            session.add("assistant", message)
            return AgentReply(
                message=message, action="error", tool_name=tool_name,
                tool_result=result,
            )
        if result.success:
            session.pending_challenge = None
            session.context.pending = None
            self._clear_constraint_draft(session)
            session.context.record_executed(
                pending.tool_name,
                pending.requested_arguments,
                (result.data or {}).get("transfer_id")
                if isinstance(result.data, dict) else None,
            )
        try:
            self.workflow.resume(session, result)
        except Exception:
            self.workflow.fail_safely(
                session,
                "workflow_resume_exception",
                "操作可能已经提交，需要人工核对；系统不会自动重试。"
                "需要人工检查；任务恢复发生异常，后续步骤未执行。",
            )
            message = self._format_task_summary(session.current_task)
            session.add("assistant", message)
            return AgentReply(message=message, action="error", tool_name=tool_name)
        return self._workflow_reply(session, last_tool_name=tool_name, last_result=result)

    def _direct_verification_error(self, tool_name, result):
        outcome = self.workflow.verify_tool_result(tool_name, result)
        if outcome.verified:
            return None
        safe_result = ToolResult(
            status="error",
            error_code=outcome.error_code or "verification_failed",
            message=outcome.message,
        )
        return AgentReply(
            message=outcome.message,
            action="error",
            tool_name=tool_name,
            tool_result=safe_result,
        )

    def _workflow_reply(self, session, last_tool_name=None, last_result=None):
        task = session.current_task
        if (
            task.status == "waiting_clarification"
            and session.pending_clarification is not None
        ):
            pending_kind = session.pending_clarification.kind
            message = self._workflow_clarification_message(session)
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="clarification",
                tool_name=session.pending_clarification.tool_name,
                clarification_scope=(
                    "workflow_plan"
                    if pending_kind == "workflow_plan"
                    else "single_tool"
                ),
            )
        if task.status == "waiting_confirmation" and session.pending_action is not None:
            waiting_step = next(
                step for step in session.current_task.steps
                if step.status == "waiting_confirmation"
            )
            self._remember_pending(
                session, session.pending_action, waiting_step.result
            )
            message = session.pending_action.confirmation_message
            session.add("assistant", message)
            return AgentReply(
                message=message,
                action="confirmation_required",
                tool_name=session.pending_action.tool_name,
                tool_result=waiting_step.result,
                requires_confirmation=True,
            )
        message = self._format_task_summary(task)
        session.add("assistant", message)
        action = "task_result" if task.status in {
            "completed", "cancelled"
        } else "error"
        return AgentReply(
            message=message,
            action=action,
            tool_name=last_tool_name,
            tool_result=last_result,
        )

    def _task_progress_reply(self, session):
        task = session.current_task
        if task is None:
            message = "当前没有任务。"
        else:
            completed = [step for step in task.steps if step.status == "completed"]
            waiting = [
                step for step in task.steps if step.status == "waiting_confirmation"
            ]
            clarifying = [
                step for step in task.steps if step.status == "waiting_clarification"
            ]
            exceptional = [
                step
                for step in task.steps
                if step.status in {"failed", "cancelled", "skipped"}
            ]
            waiting_text = step_title(waiting[0]) if waiting else "无"
            clarification_text = step_title(clarifying[0]) if clarifying else "无"
            exceptional_text = (
                "、".join(f"{step_title(step)}({step.status})" for step in exceptional)
                if exceptional
                else "无"
            )
            message = (
                f"任务 {task.task_id[:8]}：状态 {task.status}；"
                f"共 {len(task.steps)} 步，已完成 {len(completed)} 步；"
                f"当前等待澄清：{clarification_text}；"
                f"当前等待确认：{waiting_text}；异常或跳过步骤：{exceptional_text}。"
            )
        session.add("assistant", message)
        return AgentReply(message=message, action="task_progress")

    def _format_task_summary(self, task):
        labels = {
            "completed": "完成",
            "failed": "失败",
            "skipped": "跳过",
            "cancelled": "取消",
            "waiting_confirmation": "等待确认",
            "waiting_clarification": "等待澄清",
            "pending": "未执行",
            "running": "执行中",
        }
        if task.status == "completed":
            heading = f"任务已完成，共 {len(task.steps)} 个步骤："
        elif task.status == "cancelled":
            heading = "任务已取消；已完成的历史步骤不会被伪装为已回滚："
        elif task.status == "expired":
            heading = "任务已过期，旧确认语句已失效；后续步骤未执行："
        else:
            heading = "任务未全部完成；后续步骤已停止："
        lines = [heading]
        completed_count = 0
        for index, step in enumerate(task.steps, 1):
            status_text = labels.get(step.status, step.status)
            detail = ""
            if step.status == "completed" and step.result is not None:
                completed_count += 1
                detail = "；" + self._format_tool_result(step.tool_name, step.result)
            elif step.message:
                detail = "；" + step.message
            lines.append(f"{index}. {step_title(step)}：{status_text}{detail}")
        if task.status in {"failed", "cancelled", "expired"} and completed_count:
            lines.append("任务存在部分完成；已经提交的步骤不会自动补偿或自动回滚。")
        return "\n".join(lines)

    def _format_tool_result(self, tool_name, result):
        """银行事实由 Python 直接格式化，模型不能改写 Tool Result。"""
        try:
            return self._deterministic_fallback(tool_name, result)
        except (KeyError, TypeError, ValueError):
            return '工具结果事实不足，无法生成已核验的业务结论。'

    @staticmethod
    def _deterministic_fallback(tool_name, result):
        """模型回复失败时只展示真实 Tool Result，不把失败伪装为成功。"""
        if not result.success:
            return result.message
        data = result.data
        if tool_name == 'query_financial_records' and isinstance(data, dict):
            from datetime import date
            p=data['current'];r=p['range'];d=date.fromisoformat(r['start'])
            next_month=f'{d.year+(d.month==12):04d}-{d.month%12+1:02d}-01'
            period=f'{d.year}年{d.month}月' if d.day==1 and r['end_exclusive']==next_month else f"[{r['start']}, {r['end_exclusive']})"
            def yuan(cents):return f'{cents//100}.{cents%100:02d}'
            metric='购买支出' if data['metric']=='purchase_expense' else '已记录现金流出'
            text=(f"{period}{data['merchant'] or '全部商户'}{metric}{yuan(p['amount_cents'])}元，共{p['matched_count']}笔；"
                f"另有退款{yuan(p['refund_cents'])}元，共{p['refund_count']}笔（单列不扣减）。")
            if data['comparison']:
                b=data['comparison']['base'];text+=f"比较期间[{b['range']['start']}, {b['range']['end_exclusive']})同口径{yuan(b['amount_cents'])}元，共{b['matched_count']}笔。"
            if not p['recorded_count']:text+='未发现相关记录，不能断言实际零消费。'
            if p['unclassified_count']:text+=f"另有{p['unclassified_count']}笔未知类型，未认定为购买。"
            return text+'仅统计已记录数据。'
        if tool_name == "get_monthly_spending" and isinstance(data, dict):
            if data['transaction_count'] == 0:
                return f"{data['month']} 该期间已记录数据中未发现相关流水；记录支出汇总为 0.00 元，不能据此断言实际零消费。"
            return (
                f"{data['month']} 已记录数据总支出为 {data['total_spending']:.2f} 元，"
                f"共 {data['transaction_count']} 笔支出。"
            )
        if tool_name == "get_category_spending" and isinstance(data, dict):
            categories = data.get("categories", {})
            if not categories:
                return f"{data.get('month', '该月')} 该期间已记录数据中未发现相关流水；分类记录汇总为零，数据覆盖未知。"
            details = "、".join(
                f"{name} {item['amount']:.2f} 元"
                for name, item in categories.items()
            )
            return f"{data.get('month', '该月')} 分类消费为：{details}。"
        if tool_name == 'get_transactions_by_month' and data == []:
            return '该期间已记录数据中未发现相关流水；数据覆盖未知，不能断言实际零消费。'
        if tool_name == "get_active_subscriptions" and isinstance(data, list):
            if not data:
                return "当前没有有效订阅或自动扣款授权。"
            details = "、".join(
                f"{item['name']}（{item['amount']:.2f} 元/{item['cycle']}）"
                for item in data
            )
            return f"当前有效订阅有：{details}。"
        if tool_name == "get_cards" and isinstance(data, list):
            if not data:
                return "当前没有可查询的银行卡。"
            details = "；".join(
                f"{item['bank_name']} {item['masked_card_number']}，"
                f"余额 {item['balance']:.2f} 元，状态 {item['status']}"
                for item in data
            )
            return f"共查询到 {len(data)} 张银行卡：{details}。"
        if tool_name == "get_transfer_recipients" and isinstance(data, list):
            if not data:
                return "当前没有可用的预登记收款人。"
            details = "；".join(
                f"{item['recipient_id']}号 {item['name']}，"
                f"{item['bank_name']} {item['masked_account']}，"
                f"可信状态 {item['trusted']}"
                for item in data
            )
            return f"共查询到 {len(data)} 位预登记收款人：{details}。"
        if tool_name == "cancel_subscription" and isinstance(data, dict):
            return f"{result.message}：{data.get('name', '该订阅')}。"
        if tool_name == "report_card_lost" and isinstance(data, dict):
            return f"{result.message}：{data.get('masked_card_number', '该银行卡')}。"
        if tool_name == "set_daily_limit" and isinstance(data, dict):
            return (
                f"{result.message}：银行卡 {data['masked_card_number']}，"
                f"每日交易限额已从 {data['old_limit']:.2f} 元修改为 "
                f"{data['new_limit']:.2f} 元。"
            )
        if tool_name == "transfer_money" and isinstance(data, dict):
            return (
                f"{result.message}：转账单号 {data['transfer_id']}，"
                f"收款人 {data['recipient_name']}，"
                f"金额 {data['amount']:.2f} 元，"
                f"付款卡 {data['from_masked_card_number']}，"
                f"收款账户 {data['recipient_masked_account']}，"
                f"余额 {data['balance_after']:.2f} 元，"
                f"完成时间 {data['completed_at']}。"
            )
        if tool_name == "get_risk_profile" and isinstance(data, dict):
            return (
                f"当前演示用户的模拟风险等级为 "
                f"{data['risk_level']}（{data['risk_name']}），"
                f"更新时间 {data['updated_at']}。{data['notice']}"
            )
        if tool_name == "get_wealth_products" and isinstance(data, list):
            if not data:
                return "没有符合筛选条件的模拟理财产品。"
            details = "；".join(
                f"{item['product_id']}号 {item['name']}，风险 {item['risk_level']}，"
                f"最低购买 {item['min_purchase']:.2f} 元，状态 {item['status']}，"
                f"参考年化收益率 {item['reference_annual_rate']:.2f}%，"
                f"期限 {item['duration_days']} 天"
                for item in data
            )
            return f"模拟理财产品：{details}。参考年化收益率不代表实际或保证收益。"
        if tool_name == "get_wealth_holdings" and isinstance(data, list):
            if not data:
                return "当前没有模拟理财持仓。"
            details = "；".join(
                f"持仓 {item['holding_id']}，产品 {item['product_id']} 号 "
                f"{item['product_name']}，模拟持仓本金 {item['principal']:.2f} 元，"
                f"状态 {item['status']}，购买时间 {item['purchased_at']}"
                for item in data
            )
            return f"模拟理财持仓：{details}。本金不是实时市值，当前版本不计算投资收益。"
        if tool_name in {"buy_wealth_product", "redeem_wealth_product"} and isinstance(data, dict):
            action = "购买" if data["order_type"] == "BUY" else "赎回"
            return (
                f"模拟理财{action}成功：订单 {data['order_id']}，"
                f"产品 {data['product_id']} 号 {data['product_name']}，"
                f"持仓 {data['holding_id']}，金额 {data['amount']:.2f} 元，"
                f"交易后模拟持仓本金 {data['principal_after']:.2f} 元，"
                f"银行卡 {data['masked_card_number']}，"
                f"交易后余额 {data['card_balance_after']:.2f} 元，"
                f"完成时间 {data['completed_at']}。{data['notice']}"
            )
        safe_data = json.dumps(data, ensure_ascii=False) if data is not None else ""
        return f"{result.message}{'：' + safe_data if safe_data else ''}"
