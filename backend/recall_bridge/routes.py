"""R4 authenticated management. Consent changes are never model tools."""
from fastapi import APIRouter,Depends,HTTPException,Query,Request
from fastapi.responses import JSONResponse
from pydantic import Field
from agent.schemas import StrictModel
from api.dialogue import protocol,require_task_principal
from security.schemas import Principal
from .service import Service,configured
from .state import Store
from .transport import MemoryUnavailable

def bounded_query(request:Request):
    if set(request.query_params)-{'offset','limit','q'}:raise HTTPException(422,detail='unknown_memory_query_fields')


router=APIRouter(prefix='/api/secure/sessions/{session_id}/recall',dependencies=[Depends(protocol),Depends(bounded_query)],tags=['r4-recall'])
factory=Service


def available():
    if not configured():raise HTTPException(503,detail='memory_not_enabled')


def invoke(method,*args,**kwargs):
    available();s=factory()
    try:return getattr(s,method)(*args,**kwargs)
    except MemoryUnavailable as e:
        return JSONResponse(status_code=503,content={'code':str(e),'message':'记忆服务暂不可用；尚不能核实的操作请按原引用查询。银行业务可继续使用。',
            'effect_state':e.effect,'availability':'unknown'})
    finally:s.close()


class Settings(StrictModel):
    version:int=Field(ge=1,strict=True)
    automatic:bool=Field(strict=True)
    profile:bool=Field(strict=True)


class Revision(StrictModel):version:int=Field(ge=1,strict=True)
class Edit(Revision):content:str=Field(min_length=1,max_length=4000)
class Search(StrictModel):query:str=Field(min_length=1,max_length=2000)


@router.get('/settings')
def settings(session_id:str,p:Principal=Depends(require_task_principal)):
    if not configured():return dict(available=False,automatic=False,profile=False,reason='memory_not_enabled')
    return dict(available=True,**Store().settings(p))


@router.get('/health')
def health(session_id:str,p:Principal=Depends(require_task_principal)):
    if not configured():return dict(configured=False,availability='blocked',reason='memory_not_enabled')
    service=factory()
    try:
        from dialogue.store import transaction
        with transaction() as c:space=Store().space(p,c)
        # Hindsight 1.1 has no GET /banks/{bank}; it returns 405. Its bounded
        # documents listing is authenticated, read-only, and sends no LLM call.
        service.remote().documents(space['bank_ref'],offset=0,limit=1)
        return dict(configured=True,availability='available',service_version='1.1.0')
    except MemoryUnavailable as e:return dict(configured=True,availability='unknown',reason=str(e))
    finally:service.close()


@router.put('/settings')
def configure(session_id:str,body:Settings,p:Principal=Depends(require_task_principal)):
    available()
    return Store().configure(p,version=body.version,automatic=body.automatic,profile=body.profile,origin='authenticated_settings_http')


@router.get('/sources')
def sources(session_id:str,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50),q:str|None=Query(None,max_length=200),p:Principal=Depends(require_task_principal)):
    return invoke('listing',p,offset,limit,q)


@router.get('/sources/{reference}')
def source(session_id:str,reference:str,p:Principal=Depends(require_task_principal)):
    return invoke('read',p,reference)


@router.patch('/sources/{reference}')
def edit(session_id:str,reference:str,body:Edit,p:Principal=Depends(require_task_principal)):
    return invoke('change',p,reference,body.version,body.content)


@router.post('/sources/{reference}/delete')
def delete(session_id:str,reference:str,body:Revision,p:Principal=Depends(require_task_principal)):
    return invoke('change',p,reference,body.version,delete=True)


@router.get('/operations/{reference}')
def operation(session_id:str,reference:str,p:Principal=Depends(require_task_principal)):
    return invoke('state',p,reference)


@router.post('/operations/{reference}/recover')
def recover(session_id:str,reference:str,p:Principal=Depends(require_task_principal)):
    return invoke('recover',p,reference)


@router.get('/operations')
def operations(session_id:str,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50),p:Principal=Depends(require_task_principal)):
    available()
    from dialogue.store import transaction
    with transaction() as c:
        Store().space(p,c)
        refs=[r['id'] for r in c.execute('SELECT id FROM recall.operations ORDER BY created_at DESC,id LIMIT %s OFFSET %s',(limit,offset))]
    return dict(operations=[invoke('state',p,ref) for ref in refs],offset=offset,limit=limit)


@router.post('/query')
def query(session_id:str,body:Search,p:Principal=Depends(require_task_principal)):
    return invoke('recall',p,body.query)


@router.post('/origins/{reference}/invalidate')
def invalidate(session_id:str,reference:str,body:Revision,p:Principal=Depends(require_task_principal)):
    available()
    from .state import public_operation
    return dict(operations=[public_operation(o) for o in Store().exclude(p,reference,body.version)],
        scope='long_term_memory_dependencies_only_chat_and_bank_records_retained')
