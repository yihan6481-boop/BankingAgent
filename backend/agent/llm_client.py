"""OpenAI-compatible LLM 客户端；只处理模型 I/O 和结构化输出。"""

import json
import os
import re
import socket
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from copy import deepcopy
from http.client import HTTPException
from urllib import error, request

from pydantic import ValidationError

from .prompts import build_system_prompt
from .schemas import (
    ActionModificationControl,
    AgentDecision,
    ClarificationControl,
    ResponseControl,
    TaskPlanProposal,
)

_request_timeout = ContextVar('llm_request_timeout', default=None)


def decision_from_call(name, arguments):
    """Convert a preselected legacy call; the existing kernel validates execution."""
    if name == 'request_clarification':
        c=ClarificationControl.model_validate(arguments)
        return AgentDecision(action='clarification',tool_name=c.tool_name,arguments=c.arguments,
            message=c.message,missing_fields=c.missing_fields,clarification_scope=c.scope)
    if name == 'respond_to_user':
        c=ResponseControl.model_validate(arguments)
        return AgentDecision(action='response',response_type=c.response_type,message=c.message)
    if name == 'create_task_plan':
        return AgentDecision(action='task_plan',task_plan=TaskPlanProposal.model_validate(arguments))
    if name == 'modify_action':
        return AgentDecision(action='modify_action',modification=ActionModificationControl.model_validate(arguments))
    return AgentDecision(action='tool_call',tool_name=name,arguments=arguments)


class LLMError(RuntimeError):
    pass


class LLMConfigurationError(LLMError):
    pass


class LLMAPIError(LLMError):
    def __init__(self, message, *, retryable=True, http_status=None):
        super().__init__(message)
        self.error_type = "api_error"
        self.retryable = retryable
        self.evidence = {}
        self.http_status = http_status


class LLMOutputError(LLMError):
    def __init__(
        self,
        message,
        *,
        error_type="malformed_response",
        retryable=True,
        evidence=None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.evidence = dict(evidence or {})


class LLMClient(ABC):
    @abstractmethod
    def decide(self, messages, tools, current_date, pending_clarification=None):
        raise NotImplementedError


PROTOCOL_CORRECTION_INSTRUCTION = (
    "上一次响应不符合结构化 Tool Calling 协议。请保持用户业务意图不变，并严格纠正："
    "本次必须且只能返回一个 Tool Call；多目标任务只能调用一次 create_task_plan；"
    "不得同时调用多个业务 Tool；不得用普通解释代替 Tool Call；不得编造未注册 Tool；"
    "不得加入所选 Tool Schema 不允许的字段。"
)


def _safe_tool_name(value):
    if not isinstance(value, str):
        return None
    return value if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value) else "[invalid_tool_name]"


def _safe_usage(value):
    if not isinstance(value, dict):
        return None
    usage = {
        key: value[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
    }
    return usage or None


def _safe_error_message(error, secret=None):
    message = str(error)
    if secret:
        message = message.replace(secret, "[REDACTED]")
    message = re.sub(
        r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", message
    )
    message = re.sub(
        r"(?i)(api[_-]?key\s*[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+",
        r"\1[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)([?&](?:api[_-]?key|access_token|token|signature|secret)=)[^&\s\"']+",
        r"\1[REDACTED]",
        message,
    )
    message = re.sub(r"(?<!\d)\d{12,19}(?!\d)", "[REDACTED_ACCOUNT]", message)
    message = re.sub(r"确认转账\s*\d{6}", "[REDACTED_CONFIRMATION]", message)
    return re.sub(r"\s+", " ", message).strip()[:500]


class RetryingLLMClient(LLMClient):
    """Shared bounded retry boundary for normal Agent and benchmarks."""

    def __init__(
        self,
        delegate,
        max_retries=3,
        retry_backoff_seconds=1.0,
        sleep_fn=time.sleep,
        retry_api_errors=True,
    ):
        if max_retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("重试次数和退避时间不能小于0")
        self.delegate = delegate
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.sleep_fn = sleep_fn
        self.retry_api_errors = retry_api_errors
        self.reset()

    def reset(self):
        self.decisions = []
        self.errors = []
        self.agent_errors = []
        self.usages = []
        self.attempt_diagnostics = []
        self.call_count = 0
        self.retry_count = 0
        self.request_count = 0
        self.last_usage = None
        self.last_terminal_error = None
        self.terminal_errors = []

    def _diagnostic(
        self,
        *,
        request_number,
        attempt_number,
        outcome,
        error=None,
        correction_applied=False,
        latency_ms=None,
    ):
        evidence = getattr(error, "evidence", None)
        if not isinstance(evidence, dict):
            evidence = getattr(self.delegate, "last_protocol_metadata", None)
        evidence = evidence if isinstance(evidence, dict) else {}
        usage = _safe_usage(evidence.get("usage"))
        if usage is None and outcome == "success":
            usage = _safe_usage(getattr(self.delegate, "last_usage", None))
        tool_names = evidence.get("tool_names")
        if isinstance(tool_names, list):
            tool_names = [
                safe_name
                for item in tool_names
                if (safe_name := _safe_tool_name(item)) is not None
            ]
        else:
            tool_names = None
        return {
            "request_number": request_number,
            "attempt_number": attempt_number,
            "outcome": outcome,
            "error_type": getattr(error, "error_type", None) if error else None,
            "retryable": bool(getattr(error, "retryable", False)) if error else None,
            "tool_call_count": (
                evidence.get("tool_call_count")
                if isinstance(evidence.get("tool_call_count"), int)
                and not isinstance(evidence.get("tool_call_count"), bool)
                else None
            ),
            "tool_names": tool_names,
            "assistant_content_present": (
                evidence.get("assistant_content_present")
                if isinstance(evidence.get("assistant_content_present"), bool)
                else None
            ),
            "assistant_content_length": (
                evidence.get("assistant_content_length")
                if isinstance(evidence.get("assistant_content_length"), int)
                and not isinstance(evidence.get("assistant_content_length"), bool)
                else None
            ),
            "finish_reason": (
                evidence.get("finish_reason")[:64]
                if isinstance(evidence.get("finish_reason"), str)
                else None
            ),
            "usage": usage,
            "latency_ms": latency_ms,
            "protocol_correction_applied": correction_applied,
            "final_recovered_success": False,
        }

    def decide(self, messages, tools, current_date, pending_clarification=None):
        self.request_count += 1
        request_number = self.request_count
        correction_applied = False
        request_diagnostic_start = len(self.attempt_diagnostics)
        self.last_terminal_error = None
        for attempt_index in range(self.max_retries + 1):
            attempt_number = attempt_index + 1
            self.call_count += 1
            attempt_messages = list(messages)
            if correction_applied:
                attempt_messages = [
                    {"role": "system", "content": PROTOCOL_CORRECTION_INSTRUCTION},
                    *attempt_messages,
                ]
            started = time.perf_counter()
            try:
                decision = self.delegate.decide(
                    attempt_messages, tools, current_date, pending_clarification
                )
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                diagnostic = self._diagnostic(
                    request_number=request_number,
                    attempt_number=attempt_number,
                    outcome="success",
                    correction_applied=correction_applied,
                    latency_ms=latency_ms,
                )
                if attempt_number > 1:
                    for item in self.attempt_diagnostics[request_diagnostic_start:]:
                        item["final_recovered_success"] = True
                    diagnostic["final_recovered_success"] = True
                self.attempt_diagnostics.append(diagnostic)
                self.decisions.append(decision)
                self.last_usage = diagnostic["usage"]
                if diagnostic["usage"]:
                    self.usages.append(dict(diagnostic["usage"]))
                return decision
            except (LLMAPIError, LLMOutputError) as error:
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                is_api_error = isinstance(error, LLMAPIError)
                allowed = bool(getattr(error, "retryable", False)) and (
                    self.retry_api_errors or not is_api_error
                )
                diagnostic = self._diagnostic(
                    request_number=request_number,
                    attempt_number=attempt_number,
                    outcome="error",
                    error=error,
                    correction_applied=correction_applied,
                    latency_ms=latency_ms,
                )
                diagnostic["retryable"] = allowed
                self.attempt_diagnostics.append(diagnostic)
                self.errors.append(
                    {
                        "type": type(error).__name__,
                        "error_type": diagnostic["error_type"],
                        "message": _safe_error_message(
                            error, getattr(self.delegate, "api_key", None)
                        ),
                        "attempts": attempt_number,
                        "request_number": request_number,
                    }
                )
                self.last_usage = diagnostic["usage"]
                if diagnostic["usage"]:
                    self.usages.append(dict(diagnostic["usage"]))
                if not allowed or attempt_index >= self.max_retries:
                    self.last_terminal_error = error
                    self.terminal_errors.append(
                        {
                            "request_number": request_number,
                            "type": type(error).__name__,
                            "error_type": diagnostic["error_type"],
                        }
                    )
                    raise
                self.retry_count += 1
                correction_applied = correction_applied or isinstance(
                    error, LLMOutputError
                )
                delay = self.retry_backoff_seconds * (2**attempt_index)
                if delay:
                    self.sleep_fn(delay)
            except Exception as error:
                self.agent_errors.append(
                    {
                        "type": type(error).__name__,
                        "message": _safe_error_message(
                            error, getattr(self.delegate, "api_key", None)
                        ),
                        "attempts": attempt_number,
                        "request_number": request_number,
                    }
                )
                raise
        raise AssertionError("unreachable retry state")


def _function_tool(name, description, model):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": model.model_json_schema(),
        },
    }


CLARIFICATION_TOOL = _function_tool(
    "request_clarification",
    "关键业务参数不足时向用户澄清；不得猜测缺失参数。必须用 scope 字段声明澄清作用域："
    "单一业务目标缺参用 single_tool；原请求包含多个需要顺序联动的目标、条件任务或跨场景任务时"
    "用 workflow_plan，此时补齐后必须重新提交完整 create_task_plan。用户未提供月份或明确的"
    "相对月份时不得根据当前日期猜测；多目标请求缺月份必须用 workflow_plan。",
    ClarificationControl,
)
RESPONSE_TOOL = _function_tool(
    "respond_to_user",
    "仅处理问候、帮助、能力介绍、超出范围或一般闲聊；任何银行事实必须使用银行工具。",
    ResponseControl,
)
CREATE_TASK_PLAN_TOOL = _function_tool(
    "create_task_plan",
    "同一请求包含前置、后续、并列、条件或跨场景目标时，必须用本工具一次性保留全部目标，提出有界 Financial Intent Contract 和 Task DAG；不得只直接调用第一个业务 Tool。"
    "‘然后’‘再’‘完成后’‘成功后’只表示顺序，使用 depends_on；前置失败时 Python 自动停止依赖步骤，不需要成功条件。"
    "只有用户明确提出如果/若公开结果字段满足比较条件时才生成 StepCondition；不得用 data.success、data.status、result.success 等未声明字段，也不得为表示成功顺序而编造条件。"
    "条件 field_path 只能选择来源银行 Tool 描述中由 Registry 动态提供的条件可读字段，并遵守其类型、单位和操作符；Python 将在创建 Task 前重新校验。"
    "月份只能来自用户明确的绝对月份或受支持相对时间；完全缺少时间范围时必须先以 workflow_plan 澄清，补充后重新提交全部目标。",
    TaskPlanProposal,
)
MODIFY_ACTION_TOOL = _function_tool(
    "modify_action",
    "仅用于连续对话中的业务修改或指代：修改尚未执行的待办/Workflow 步骤，"
    "引用本会话已明确选择的收款人，或选择唯一的另一张合格银行卡。"
    "只提交公开业务字段、引用和 amount<=上限约束；不得提交确认、OTP、授权、"
    "风险、身份、状态、结果或任何后端生成字段。已完成操作只能标记为"
    "completed_action，由 Python 询问是否创建新操作，绝不能修改历史交易。"
    "修改金额、卡片或收款人时不需要重复已有约束：省略 constraints 或给出空列表"
    "都不会删除已有金额上限；constraints 只用于新增或收紧上限，Python 会取更紧的值，"
    "放宽或删除上限必须先由用户明确发起新的独立操作。",
    ActionModificationControl,
)


def control_tools():
    return [CREATE_TASK_PLAN_TOOL, MODIFY_ACTION_TOOL, CLARIFICATION_TOOL, RESPONSE_TOOL]


class OpenAICompatibleLLMClient(LLMClient):
    ERROR_BODY_LIMIT = 500
    ERROR_BODY_READ_LIMIT = 2048

    def __init__(
        self,
        api_key=None,
        base_url=None,
        model=None,
        timeout=30,
        temperature=0,
        max_output_tokens=None,
        compact_tools=False,
    ):
        if os.environ.get('BA091_LLM_ACCESS', 'configured') == 'disabled':
            raise LLMConfigurationError('此隔离部署已关闭真实模型访问')
        self.api_key = api_key or os.getenv("LLM_API_KEY")
        self.base_url = (base_url or os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.getenv("LLM_MODEL")
        self.timeout = timeout
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.compact_tools = compact_tools
        self.tool_choice_mode = "required"
        self.last_usage = None
        self.last_protocol_metadata = None
        self.tool_argument_models = {}
        if not self.api_key:
            raise LLMConfigurationError("缺少 LLM_API_KEY 环境变量")
        if not self.model:
            raise LLMConfigurationError("缺少 LLM_MODEL 环境变量")

    def set_tool_argument_models(self, models):
        self.tool_argument_models = dict(models)

    def structured(self, messages, tools, instruction, *, timeout=25):
        """One bounded control call, sharing the original transport and usage.

        No automatic retries or second provider. External data stays in user
        messages; the caller supplies only a project-owned system instruction.
        """
        from .input_builder import sanitize_messages
        self.last_usage = None
        messages=sanitize_messages(messages)
        payload = dict(model=self.model, messages=[dict(role='system',content=instruction),*messages],
            tools=deepcopy(tools), tool_choice='required', parallel_tool_calls=False,
            temperature=self.temperature, thinking={'type':'disabled'},
            max_tokens=self.max_output_tokens or 2400)
        self._checked_body(payload)
        try:
            # Per-call timeout is thread-local; do not mutate a shared client.
            token=_request_timeout.set(timeout)
            try: response=self._post(payload)
            finally: _request_timeout.reset(token)
            self.last_usage=_safe_usage(response.get('usage')) if isinstance(response,dict) else None
            name,args,metadata=self._single_tool_call(response,allowed_tool_names={t['function']['name'] for t in tools})
            self.last_protocol_metadata=metadata
            return name,args
        finally:
            self.last_input_metrics['provider_usage']=self.last_usage

    def _safe_http_error_body(self, http_error):
        """读取有限长度的服务端错误正文，并移除可能的凭据与换行。"""
        try:
            raw_body = http_error.read(self.ERROR_BODY_READ_LIMIT)
        except (OSError, HTTPException, ValueError, AttributeError):
            return ""
        if not raw_body:
            return ""

        if isinstance(raw_body, bytes):
            detail = raw_body.decode("utf-8", errors="replace")
        else:
            detail = str(raw_body)
        if self.api_key:
            detail = detail.replace(self.api_key, "[REDACTED]")
        detail = re.sub(
            r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+",
            "Bearer [REDACTED]",
            detail,
        )
        detail = re.sub(
            r"(?i)(api[_-]?key\s*[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+",
            r"\1[REDACTED]",
            detail,
        )
        detail = re.sub(
            r"(?i)([?&](?:api[_-]?key|access_token|token|signature|secret)=)[^&\s\"']+",
            r"\1[REDACTED]",
            detail,
        )
        detail = re.sub(r"\s+", " ", detail).strip()
        if len(detail) > self.ERROR_BODY_LIMIT:
            detail = detail[: self.ERROR_BODY_LIMIT].rstrip() + "…"
        return detail

    def _checked_body(self, payload):
        from .input_builder import validate_payload, InputBudgetExceeded
        removed = getattr(self, 'last_input_metrics', {}).get('removed')
        try:
            body, self.last_input_metrics = validate_payload(payload, removed=removed)
        except InputBudgetExceeded as exc:
            self.last_input_metrics = exc.metrics
            raise
        return body

    def _post(self, payload):
        from dialogue.quota import legacy_admitted
        if not legacy_admitted.get():raise LLMConfigurationError('model_message_admission_required')
        # The authenticated legacy chat adapter also uses the shared durable
        # usage ledger. Validate before reserving; unknown outcomes retain debt.
        self._checked_body(payload)
        from dialogue.budget import Budget
        import time
        budget=Budget();rid=budget.reserve(payload);started=time.monotonic()
        result=None;failure=None
        try:
            result=self._post_metered(payload)
            return result
        except Exception:
            failure='legacy_provider_failed_or_unknown'
            raise
        finally:budget.settle(rid,result,time.monotonic()-started,failure)

    def _post_metered(self, payload):
        body = self._checked_body(payload)
        http_request = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=_request_timeout.get() or self.timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = self._safe_http_error_body(exc)
            message = f"LLM API 请求失败（HTTP {exc.code}）"
            if detail:
                message += f"：{detail}"
            raise LLMAPIError(
                message,
                retryable=exc.code in {408, 409, 425, 429} or exc.code >= 500,
                http_status=exc.code,
            ) from exc
        except (
            error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            HTTPException,
            OSError,
        ) as exc:
            raise LLMAPIError("LLM API 无法连接或请求超时") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LLMOutputError(
                "LLM API 返回了无效 JSON", error_type="malformed_response"
            ) from exc
        return decoded

    @staticmethod
    def _response_metadata(response):
        metadata = {
            "tool_call_count": None,
            "tool_names": None,
            "assistant_content_present": None,
            "assistant_content_length": None,
            "finish_reason": None,
            "usage": _safe_usage(response.get("usage")) if isinstance(response, dict) else None,
        }
        if not isinstance(response, dict):
            return metadata
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return metadata
        choice = choices[0]
        if isinstance(choice.get("finish_reason"), str):
            metadata["finish_reason"] = choice["finish_reason"][:64]
        message = choice.get("message")
        if not isinstance(message, dict):
            return metadata
        content = message.get("content")
        if isinstance(content, str):
            metadata["assistant_content_present"] = bool(content)
            metadata["assistant_content_length"] = len(content)
        elif content is None:
            metadata["assistant_content_present"] = False
            metadata["assistant_content_length"] = 0
        calls = message.get("tool_calls")
        if calls is None:
            metadata["tool_call_count"] = 0
            metadata["tool_names"] = []
        elif isinstance(calls, list):
            metadata["tool_call_count"] = len(calls)
            names = []
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                safe_name = _safe_tool_name(name)
                if safe_name is not None:
                    names.append(safe_name)
            metadata["tool_names"] = names
        return metadata

    @classmethod
    def _single_tool_call(cls, response, allowed_tool_names=None):
        evidence = cls._response_metadata(response)
        try:
            choices = response["choices"]
            if not isinstance(choices, list) or not choices:
                raise KeyError("choices")
            message = choices[0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message")
            calls = message.get("tool_calls")
            if calls is None:
                calls = []
            if not isinstance(calls, list):
                raise TypeError("tool_calls")
            if not calls:
                error_type = (
                    "assistant_text_instead_of_tool_call"
                    if evidence["assistant_content_present"]
                    else "no_tool_call"
                )
                raise LLMOutputError(
                    "LLM 未返回结构化调用",
                    error_type=error_type,
                    evidence=evidence,
                )
            if len(calls) > 1:
                raise LLMOutputError(
                    "LLM 返回了多个结构化调用",
                    error_type="multiple_tool_calls",
                    evidence=evidence,
                )
            function = calls[0]["function"]
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise TypeError("function")
            name = function["name"]
            if allowed_tool_names is not None and name not in allowed_tool_names:
                raise LLMOutputError(
                    "LLM 返回了未注册 Tool",
                    error_type="invalid_tool_name",
                    evidence=evidence,
                )
            raw_arguments = function["arguments"]
            if not isinstance(raw_arguments, str):
                raise TypeError("arguments")
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise LLMOutputError(
                    "LLM 工具参数不是有效 JSON",
                    error_type="invalid_tool_arguments",
                    evidence=evidence,
                ) from exc
            if not isinstance(arguments, dict):
                raise LLMOutputError(
                    "LLM 工具参数必须是 JSON 对象",
                    error_type="invalid_tool_arguments",
                    evidence=evidence,
                )
            return name, arguments, evidence
        except LLMOutputError:
            raise
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMOutputError(
                "LLM 输出不符合 Tool Calling Schema",
                error_type="malformed_response",
                evidence=evidence,
            ) from exc

    def decide(self, messages, tools, current_date, pending_clarification=None):
        self.last_usage = None
        self.last_protocol_metadata = None
        from .input_builder import assemble, InputBudgetExceeded
        from .input_builder import data_message
        # Compatibility for direct clients: pending natural-language data still
        # belongs to a user-role data message, never build_system_prompt.
        if pending_clarification:
            messages = [data_message('clarification', pending_clarification.model_dump(mode='json')), *messages]
        fields = {
            "model": self.model,
            "thinking": {"type": "disabled"},
            "tool_choice": self.tool_choice_mode,
            "parallel_tool_calls": False,
            "temperature": self.temperature,
        }
        if self.max_output_tokens is not None:
            fields['max_tokens'] = self.max_output_tokens
        all_tools = deepcopy([*tools, *control_tools()])
        if self.compact_tools:
            # This repeated paragraph already lives in the system/plan prompt.
            # Keep the full schemas and dynamic condition-field directory.
            for item in all_tools:
                function = dict(item['function'])
                function['description'] = function['description'].split(' 仅当这是用户唯一业务目标时')[0]
                item['function'] = function
        try:
            assembled, all_tools, self.last_input_metrics = assemble(
                messages, all_tools, current_date, payload_fields=fields)
        except InputBudgetExceeded as exc:
            self.last_input_metrics = exc.metrics
            raise
        payload = {**fields, 'messages': assembled, 'tools': all_tools}
        self._checked_body(payload)
        response = self._post(payload)
        usage = response.get("usage") if isinstance(response, dict) else None
        self.last_usage = (
            {
                key: usage[key]
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if isinstance(usage.get(key), int)
            }
            if isinstance(usage, dict)
            else None
        )
        self.last_input_metrics['provider_usage'] = self.last_usage
        allowed_tool_names = {
            item.get("function", {}).get("name")
            for item in payload["tools"]
            if isinstance(item, dict)
        }
        try:
            name, arguments, metadata = self._single_tool_call(
                response, allowed_tool_names=allowed_tool_names
            )
            self.last_protocol_metadata = metadata
        except LLMOutputError as error:
            self.last_protocol_metadata = error.evidence
            raise
        try:
            argument_model = self.tool_argument_models.get(name)
            if argument_model is not None:
                argument_model.model_validate(arguments)
            if name == "request_clarification":
                control = ClarificationControl.model_validate(arguments)
                return AgentDecision(
                    action="clarification",
                    tool_name=control.tool_name,
                    arguments=control.arguments,
                    message=control.message,
                    missing_fields=control.missing_fields,
                    clarification_scope=control.scope,
                )
            if name == "respond_to_user":
                control = ResponseControl.model_validate(arguments)
                return AgentDecision(
                    action="response",
                    response_type=control.response_type,
                    message=control.message,
                )
            if name == "create_task_plan":
                plan = TaskPlanProposal.model_validate(arguments)
                return AgentDecision(action="task_plan", task_plan=plan)
            if name == "modify_action":
                modification = ActionModificationControl.model_validate(arguments)
                return AgentDecision(
                    action="modify_action", modification=modification
                )
            return AgentDecision(
                action="tool_call", tool_name=name, arguments=arguments
            )
        except ValidationError as exc:
            raise LLMOutputError(
                "LLM 决策未通过 Schema 校验",
                error_type="invalid_tool_arguments",
                evidence=self.last_protocol_metadata,
            ) from exc

