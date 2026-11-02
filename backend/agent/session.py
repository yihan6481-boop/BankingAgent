"""Legacy chat-only state on top of the business execution context (exit R4)."""
import re
from dataclasses import dataclass, field
from datetime import datetime
from .context import ConversationContext
from execution.context import ExecutionContext

@dataclass
class AgentSession(ExecutionContext):
    completed_request_signature: str | None = None
    completed_idempotency_key: str | None = None
    completed_action: str | None = None
    completed_action_digest: str | None = None
    completed_response: object | None = None
    completed_confirm_text: str | None = None
    completed_arguments: dict | None = None
    completed_pending_revision: int | None = None

    messages: list[dict[str, str]] = field(default_factory=list)
    clarification_expires_at: datetime | None = None
    planning_draft_id: str | None = None
    planning_draft_revision: int | None = None
    context: ConversationContext = field(default_factory=ConversationContext)
    # Read-only query lifecycle; never shares payment authority or workflow IDs.
    conversation_active_revision: str | None = None
    conversation_retired_revision: str | None = None
    conversation_query_status: str = 'inactive'
    conversation_last_success: dict | None = None
    conversation_last_candidate: dict | None = None


    def add(self, role, content):
        from security.redaction import redact
        import re
        safe_content = '[验证码已移除]' if re.fullmatch(r'\d{6}',str(content).strip()) else redact(str(content))
        self.messages.append({"role": role, "content": safe_content})
        # The LLM only needs a bounded recent window.  Durable/security facts
        # live in trusted state and the compact context summary above.
        self.messages = self.messages[-16:]


    def validate_legacy_plan(self, plan):
        from .query_requirements import check
        from .workflow import PlanValidationError
        if self.protocol == 'v2' and self.context.query_requirements is not None:
            failure = check(self.context.query_requirements,
                [(s.tool_name, s.arguments) for s in plan.steps], plan_steps=plan.steps)
            if failure: raise PlanValidationError(*failure)

    def legacy_card_inventory(self):
        return (self.context.query_requirements or {}).get('flow')

    def clear_pending(self):
        super().clear_pending()
        self.completed_request_signature = None
        self.completed_idempotency_key = None
        self.completed_action = None
        self.completed_action_digest = None
        self.completed_response = None
        self.completed_confirm_text = None
        self.completed_arguments = None
        self.completed_pending_revision = None
