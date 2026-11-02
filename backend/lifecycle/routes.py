"""Independent strict lifecycle protocol, outside frozen V1/V2 enums."""
from fastapi import APIRouter, Depends

from api.runtime import runtime
from api.v2_routes import require_principal
from api.v2_presenter import present_v2
from security.schemas import Principal
from .models import (CreateTaskRequest, RevisionRequest, ScheduleRequest,
                     ModifyRequest, NoteRequest, TaskConfirmRequest)
from api.task_models import TaskResponse, TaskListResponse
from .models import RollbackRequest
from .service import (engine, session_operation, transition, public, Scope,
                      active_scope, bind)
from .store import TaskStore, TaskError

router = APIRouter(prefix='/api/tasks/sessions/{session_id}', tags=['task-lifecycle'])


def require_task_principal(session_id: str, principal: Principal = Depends(require_principal)):
    # The persisted V2 session row is not a restart-recovery credential.
    session_for(session_id)
    return principal


def session_for(session_id):
    session = runtime.get_session(session_id)
    if session is None:
        raise TaskError('session_not_found', status=404)
    return session


def response(record, session_id=None, session=None):
    from security.totp import independent_protocol
    if independent_protocol.get():
        from api.secure import task_response
        return task_response(record, session)
    value = {'task': public(record)}
    if session is not None:
        # Present the current preview; do not feed a previous operation's reply to the presenter.
        value['interaction'] = present_v2(None, session_id, session).model_dump(mode='json')
    return value


@router.get('/tasks', response_model=TaskListResponse)
def list_tasks(session_id: str, principal: Principal = Depends(require_task_principal)):
    return {'tasks': [public(t) for t in TaskStore().list(principal)]}


@router.get('/tasks/{task_id}', response_model=TaskResponse)
def detail(session_id: str, task_id: str, principal: Principal = Depends(require_task_principal)):
    return response(TaskStore().load(task_id, principal))


@router.post('/tasks', response_model=TaskResponse)
def create(session_id: str, body: CreateTaskRequest, principal: Principal = Depends(require_task_principal)):
    return _create(session_id,body,principal)


from .application import prepare_structured

def _create(session_id,body,principal,**kwargs):
    data=prepare_structured(session_id,body,principal,**kwargs)
    record=TaskStore().load(data['task']['task']['task_id'],principal)
    session=session_for(session_id)
    from conversation import task_event
    task_event(session,record,'create')
    return response(record,session_id,session)

def perform(session_id, task_id, body, action, principal):
    from .application import perform as apply
    apply(session_id,task_id,body,action,principal)
    record=TaskStore().load(task_id,principal)
    session=session_for(session_id)
    from conversation import task_event
    task_event(session,record,action)
    runtime.set_last_reply(session_id,None)
    return response(record,session_id,session)


@router.post('/tasks/{task_id}/resume', response_model=TaskResponse)
def resume(session_id: str, task_id: str, body: RevisionRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'resume', principal)


@router.post('/tasks/{task_id}/pause', response_model=TaskResponse)
def pause(session_id: str, task_id: str, body: RevisionRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'pause', principal)


@router.post('/tasks/{task_id}/cancel', response_model=TaskResponse)
def cancel(session_id: str, task_id: str, body: RevisionRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'cancel', principal)


@router.post('/tasks/{task_id}/modify', response_model=TaskResponse)
def modify(session_id: str, task_id: str, body: ModifyRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'modify', principal)


@router.post('/tasks/{task_id}/schedule', response_model=TaskResponse)
def schedule(session_id: str, task_id: str, body: ScheduleRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'schedule', principal)


@router.post('/tasks/{task_id}/takeover', response_model=TaskResponse)
def takeover(session_id: str, task_id: str, body: RevisionRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'takeover', principal)


@router.post('/tasks/{task_id}/note', response_model=TaskResponse)
def note(session_id: str, task_id: str, body: NoteRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'note', principal)


@router.post('/tasks/{task_id}/return', response_model=TaskResponse)
def return_task(session_id: str, task_id: str, body: RevisionRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'return', principal)


@router.post('/tasks/{task_id}/confirm', response_model=TaskResponse)
def confirm(session_id: str, task_id: str, body: TaskConfirmRequest, principal: Principal = Depends(require_principal)):
    return perform(session_id, task_id, body, 'confirm', principal)


@router.post('/tasks/{task_id}/rollback', response_model=TaskResponse)
def rollback(session_id: str, task_id: str, body: RollbackRequest, principal: Principal = Depends(require_task_principal)):
    return perform(session_id, task_id, body, 'rollback', principal)
