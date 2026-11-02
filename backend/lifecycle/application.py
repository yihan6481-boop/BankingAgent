"""Structured task use cases shared by HTTP and tools; no HTTP/model imports."""
from execution.sessions import session_for, session_lock, assert_owner
from .store import TaskStore, TaskError
from .service import engine, session_operation, transition
from .views import task_response
from execution.context import execution_adapter

def prepare_structured(sid, body, principal, *, preparation_task_id=None):
    from security.support import assert_new_automation
    assert_new_automation(sid)
    session = session_for(sid)
    assert_owner(session,principal)
    workflow = engine()
    if preparation_task_id is not None:
        workflow.task_id_provider = lambda: preparation_task_id
    try: workflow.validator.validate(body.plan)
    except ValueError as error:
        failure=TaskError(getattr(error,'code','invalid_plan'),status=422)
        failure.preparation_no_task=True
        raise failure from error
    with session_lock(sid), execution_adapter(session) as session:
        if session.lifecycle_id:
            previous=TaskStore().load(session.lifecycle_id,principal)
            if previous.status in {'completed','cancelled','failed'}:
                session.clear_pending()
                session.current_task=previous.task.model_copy(deep=True)
        if (session.pending_action or session.pending_challenge or
                session.current_task is not None and session.current_task.status not in {'completed','cancelled','failed','expired'}):
            error=TaskError('task_in_progress')
            error.preparation_no_task=True
            raise error
        with session_operation(session):
            try: workflow.start(body.plan,session)
            except ValueError as error:
                raise TaskError(getattr(error,'code','invalid_plan')) from error
        return task_response(TaskStore().load(session.lifecycle_id,principal),session)

def perform(sid,task_id,body,action,principal):
    session=session_for(sid)
    assert_owner(session,principal)
    with session_lock(sid), execution_adapter(session) as session:
        return task_response(transition(session,task_id,body,action),session)
