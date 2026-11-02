"""Additive bill-vision-1.0 API on the existing secure authenticated protocol."""
from fastapi import Depends, Request, Response, Query as HttpQuery
from fastapi.responses import FileResponse
from pathlib import Path
from lifecycle.routes import require_task_principal,session_for
from security.schemas import Principal
from api.secure import router
from api.runtime import runtime
from .models import Revision,Edit,Confirm,Query
from .models import QueryResult
from .contracts import Envelope,Document,Documents,Version,Deleted
from .provider import VisionError,MAX_BYTES
from . import service

def envelope(data):return dict(contract_version='bill-vision-1.0',data=data)


from data_query import QueryBatch

@router.post('/data/query')
def data_query(session_id:str,body:QueryBatch,principal:Principal=Depends(require_task_principal)):
    """Additive read-only plan API. No SQL or owner arguments are accepted."""
    from data_query import verified_execute
    with runtime.session_lock(session_id):
        from security.service import get_security_services
        from security.config import BUSINESS_TIMEZONE
        session=session_for(session_id)
        # Capture once per request; never inherit a previous chat day's cutoff.
        session.conversation_business_date=get_security_services().strong_auth.now_provider().astimezone(BUSINESS_TIMEZONE).date()
        return dict(contract_version='data-query-1',data=verified_execute(principal,body,session))

def remember(session_id,value,action):
    from conversation import event
    with runtime.session_lock(session_id):
        session=session_for(session_id)
        event(session,value,action)

def evict(principal,deleted=None):
    with runtime._lock:
        sessions=[s for s in runtime._sessions.values() if s.principal and s.principal.principal_id==principal.principal_id]
    for s in sessions:
        with runtime.session_lock(s.session_id):
            reply=runtime._last_replies.get(s.session_id)
            if getattr(reply,'tool_name',None)=='query_uploaded_bills':runtime.set_last_reply(s.session_id,None)
            if getattr(reply,'tool_name',None)=='query_data':runtime.set_last_reply(s.session_id,None)
            if deleted:
                s.billvision_documents=[x for x in getattr(s,'billvision_documents',[]) if x!=deleted]
                ref=getattr(s,'billvision_reference',None) or {}
                if ref.get('object_id')==deleted:
                    from datetime import datetime,timezone
                    s.billvision_reference=dict(ref,retired=True,action='delete',
                        occurred_at=datetime.now(timezone.utc).isoformat())
            s.billvision_last_query=None
            # No old query/result copy may survive correction/deletion.
            from dialogue_state import clear
            clear(s)
            invalid_turns={t.get('turn_id') for t in getattr(s,'conversation_turns',[]) if t.get('source')=='uploaded'}
            s.conversation_turns=[t for t in getattr(s,'conversation_turns',[]) if t.get('turn_id') not in invalid_turns]

@router.post('/bills',response_model=Envelope[Document])
async def upload(session_id:str,request:Request,principal:Principal=Depends(require_task_principal)):
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>MAX_BYTES:raise VisionError('image_size_unsupported','图片须在10MiB以内',413)
    # Synchronous image decoding/DB work runs outside the event loop.
    from starlette.concurrency import run_in_threadpool
    value=await run_in_threadpool(service.upload,principal,bytes(data))
    with runtime.session_lock(session_id):
        s=session_for(session_id)
        s.billvision_last_query=None
        s.billvision_documents=list(dict.fromkeys([*getattr(s,'billvision_documents',[]),value['document_id']]))
        from conversation import event
        event(s,value,'upload')
    return envelope(value)

@router.get('/bills',response_model=Envelope[Documents])
def listing(session_id:str,principal:Principal=Depends(require_task_principal)):
    return envelope(service.list_documents(principal))

@router.post('/bills/query',response_model=Envelope[QueryResult])
def query(session_id:str,body:Query,principal:Principal=Depends(require_task_principal)):
    return envelope(service.query(principal,body))

@router.get('/bills/{document_id}',response_model=Envelope[Document])
def detail(session_id:str,document_id:str,principal:Principal=Depends(require_task_principal)):
    return envelope(service.get(principal,document_id))

@router.get('/bills/{document_id}/image')
def image(session_id:str,document_id:str,principal:Principal=Depends(require_task_principal)):
    raw,mime=service.image(principal,document_id)
    return Response(raw,media_type=mime,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@router.get('/bills/{document_id}/versions',response_model=Envelope[list[Version]])
def versions(session_id:str,document_id:str,principal:Principal=Depends(require_task_principal)):
    return envelope(service.versions(principal,document_id))

@router.post('/bills/{document_id}/select',response_model=Envelope[Document])
def select(session_id:str,document_id:str,principal:Principal=Depends(require_task_principal)):
    data=service.get(principal,document_id)
    with runtime.session_lock(session_id):
        s=session_for(session_id)
        s.billvision_documents=[document_id]
        s.billvision_last_query=None
        from conversation import event
        event(s,data,'select')
    return envelope(data)

@router.post('/bills/{document_id}/recognize',response_model=Envelope[Document])
def recognize(session_id:str,document_id:str,body:Revision,principal:Principal=Depends(require_task_principal)):
    value=service.recognize(principal,document_id,body.revision)
    evict(principal)
    remember(session_id,value,'recognize')
    return envelope(value)

@router.put('/bills/{document_id}/rows',response_model=Envelope[Document])
def edit(session_id:str,document_id:str,body:Edit,principal:Principal=Depends(require_task_principal)):
    value=service.edit(principal,document_id,body);evict(principal)
    remember(session_id,value,'edit')
    return envelope(value)

@router.post('/bills/{document_id}/confirm',response_model=Envelope[Document])
def confirm(session_id:str,document_id:str,body:Confirm,principal:Principal=Depends(require_task_principal)):
    value=service.confirm(principal,document_id,body);evict(principal)
    remember(session_id,value,'confirm')
    return envelope(value)

@router.delete('/bills/{document_id}',response_model=Envelope[Deleted])
def delete(session_id:str,document_id:str,revision:int=HttpQuery(ge=1),principal:Principal=Depends(require_task_principal)):
    value=service.delete(principal,document_id,revision);evict(principal,document_id)
    remember(session_id,value,'delete')
    return envelope(value)

def register_page(app):
    @app.get('/bill-vision-review',include_in_schema=False)
    def page():
        return FileResponse(Path(__file__).resolve().parents[2]/'manual/bill_vision_review.html',headers={'Cache-Control':'no-store'})

