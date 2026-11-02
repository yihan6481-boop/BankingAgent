from __future__ import annotations

from threading import RLock
from uuid import uuid4

from agent import AgentSession, BankingAgent, OpenAICompatibleLLMClient
from agent.llm_client import LLMConfigurationError
from database import initialize_database
from security.service import get_security_services


class AgentRuntime:
    """In-memory session registry plus the process-wide security stack.

    V0.7.0 keeps the V1 in-process session semantics but:

    * binds every session to a server-side principal (legacy demo for the
      no-login V1 entry, the verified credential for V2);
    * serialises mutating operations per session so check / pending-action
      consumption / agent state change / reply storage stay consistent;
    * never blocks different sessions behind one global LLM lock (the lock is
      held only while reading or writing the registry).
    """

    def __init__(self):
        from execution import sessions as registry
        self._lock = registry.lock
        self._session_locks = registry.locks
        self._agent = None
        self._analysis_agent = None
        self._secure_agent = None
        self._sessions = registry.sessions
        self._last_replies = {}
        initialize_database()

    @property
    def security(self):
        return get_security_services()

    @property
    def llm_configured(self):
        try:
            self._ensure_agent()
            return True
        except LLMConfigurationError:
            return False

    def _ensure_agent(self):
        with self._lock:
            if self._agent is None:
                self._agent = BankingAgent(OpenAICompatibleLLMClient())
            return self._agent

    def session_lock(self, session_id):
        """Per-session reentrant lock; different sessions never share it."""
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = RLock()
                self._session_locks[session_id] = lock
            return lock

    def create_session(self, principal=None, protocol: str = "v1", session_id=None):
        session_id = session_id or (
            f"s2_{uuid4().hex}" if protocol == "v2" else f"s1_{uuid4().hex}"
        )
        session = AgentSession()
        session.session_id = session_id
        session.protocol = protocol
        if principal is not None:
            session.principal = principal
            session.principal_id = principal.principal_id
            session.user_id = principal.user_id
        else:
            if self.security.config.authentication_mode != 'demo':
                from fastapi import HTTPException
                raise HTTPException(403, 'legacy_demo_disabled')
            legacy = self.security.identity.legacy_demo_principal()
            session.principal = legacy
            session.principal_id = legacy.principal_id
            session.user_id = legacy.user_id
        with self._lock:
            self._sessions[session_id] = session
            self._last_replies[session_id] = None
            self._session_locks[session_id] = RLock()
        return session_id

    def get_session(self, session_id):
        with self._lock:
            session=self._sessions.get(session_id)
            from execution.context import ExecutionContext
            if type(session) is ExecutionContext:
                # R3 can restore a server-authenticated execution binding after
                # restart. Legacy UI access adds only empty legacy chat fields.
                from dataclasses import fields
                session=AgentSession(**{f.name:getattr(session,f.name) for f in fields(ExecutionContext)})
                self._sessions[session_id]=session
            return session

    def discard_session(self, session_id):
        """Forget runtime secrets/previews only; never delete durable tasks."""
        with self.session_lock(session_id), self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                # In-flight callers may still hold this object.
                session.__dict__.clear()
                session.__dict__.update(AgentSession().__dict__)
            self._last_replies.pop(session_id, None)

    def legacy_session(self, session_id):
        from fastapi import HTTPException
        config = self.security.config
        if config.authentication_mode != 'demo' or not config.legacy_demo_enabled:
            raise HTTPException(403, 'legacy_demo_disabled')
        session = self.get_session(session_id)
        if session is None:
            raise HTTPException(404, 'session_not_found')
        principal = getattr(session, 'principal', None)
        if (session.protocol != 'v1' or principal is None or
                principal.kind != 'legacy_demo' or principal.user_id != 0):
            raise HTTPException(403, 'legacy_session_required')
        return session

    def get_last_reply(self, session_id):
        with self._lock:
            return self._last_replies.get(session_id)

    def set_last_reply(self, session_id, reply):
        with self._lock:
            self._last_replies[session_id] = reply
            session = self._sessions.get(session_id)
            if session:
                session.reply_lifecycle_id = session.lifecycle_id

    def get_or_create_session(self, session_id=None):
        if session_id:
            session = self.legacy_session(session_id)
            if session is None:
                raise KeyError(session_id)
            return session_id, session
        new_id = self.create_session()
        return new_id, self.get_session(new_id)

    def handle(self, session_id, text, *, analysis=False, secure=False):
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        with self.session_lock(session_id):
            if self.session_principal(session_id) is None:
                from fastapi import HTTPException
                raise HTTPException(401, 'credential_expired')
            from analysis_service import CATALOG
            from agent.agent import TASK_PROGRESS_WORDS
            if (session.current_task and session.current_task.status in {'completed','failed','cancelled','expired'}
                    and text.strip() not in TASK_PROGRESS_WORDS and not session.pending_action):
                session.current_task = None
                session.lifecycle_id = None
                session.lifecycle_revision = None
                session.lifecycle_binding = None
            task = session.current_task
            if (not analysis and task and task.status not in {'completed','failed','cancelled','expired'}
                and any(step.tool_name in CATALOG for step in task.steps)):
                from agent.schemas import AgentReply
                return AgentReply(action='error', plan_error_code='analysis_entry_required',
                    message='该任务包含分析步骤，请使用分析chat或任务confirm/resume/cancel入口继续。'), session
            if analysis:
                with self._lock:
                    attribute = '_secure_agent' if secure else '_analysis_agent'
                    if getattr(self, attribute) is None:
                        from agent.tool_registry import build_default_registry
                        from datetime import datetime
                        from security.config import BUSINESS_TIMEZONE
                        agent = BankingAgent(OpenAICompatibleLLMClient(
                            max_output_tokens=1800, compact_tools=True) if secure else OpenAICompatibleLLMClient(),
                            registry=build_default_registry(include_analysis=True),
                            **({'llm_max_retries': 0} if secure else {}),
                            today_provider=lambda: datetime.now(BUSINESS_TIMEZONE).date())
                        setattr(self, attribute, agent)
                    agent = getattr(self, attribute)
            else:
                agent = self._ensure_agent()
            from lifecycle.service import session_operation
            from agent.tool_registry import ToolRisk
            with session_operation(session):
                reply = agent.handle(text, session)
                if text.strip() in {'取消', 'cancel'}:
                    from lifecycle.service import active_scope
                    scope = active_scope.get()
                    if scope and scope.record and scope.record.mode == 'single' and session.pending_action is None:
                        scope.record.status = 'cancelled'
                        scope.record.task.status = 'cancelled'
                        scope.record.task.current_step_id = None
                        for step in scope.record.task.steps:
                            if step.status != 'completed':
                                step.status, step.result = 'cancelled', None
            self._last_replies[session_id] = reply
            session.reply_lifecycle_id = session.lifecycle_id if (
                reply.action in {'task_result','task_progress','confirmation_required'} or
                reply.tool_name and agent.registry.get(reply.tool_name) and
                agent.registry.get(reply.tool_name).risk == ToolRisk.WRITE or
                session.current_task and session.current_task.task_id == session.lifecycle_id) else None
        return reply, session

    def session_principal(self, session_id):
        """Server-side principal for a session, or ``None`` when unusable."""
        session = self.get_session(session_id)
        if session is None:
            return None
        if session.protocol == 'v1':
            return self.legacy_session(session_id).principal
        # A cached Principal is never a credential validity proof.
        return self.security.identity.resolve_principal(session_id)


runtime = AgentRuntime()
