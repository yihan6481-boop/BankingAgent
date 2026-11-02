"""Independent seat authentication and customer-owned support/risk endpoints."""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Depends,Header,Request
from pydantic import Field,StrictStr,StrictBool
from agent.schemas import StrictModel
from api.dialogue import require_task_principal
from security.schemas import Principal
from assistance import service,risk
from lifecycle.store import TaskError

router=APIRouter()
BASE='/api/secure/sessions/{session_id}/mobile'
class Message(StrictModel):
    text:StrictStr=Field(min_length=1,max_length=2000)
    client_id:StrictStr=Field(min_length=8,max_length=100)
class CustomerMessage(Message):
    case_id:UUID|None=None
class Login(StrictModel):
    username:StrictStr=Field(min_length=1,max_length=60)
    password:StrictStr=Field(min_length=1,max_length=256,repr=False)
class Presence(StrictModel):online:StrictBool
class Tool(StrictModel):name:Literal['task_status','task_verify','task_resume']
class Choice(StrictModel):
    version:StrictStr=Field(min_length=24,max_length=24)
    choice:Literal['continue','modify','dismiss','support']

def seat(authorization:str|None=Header(None)):
    if not authorization or not authorization.startswith('Bearer '):raise TaskError('seat_unauthorized',status=401)
    return service.authenticate(authorization[7:])

@router.get(BASE+'/support')
def status(session_id:str,p:Principal=Depends(require_task_principal)):return service.status(session_id)
@router.get(BASE+'/support/chat')
def chat(session_id:str,p:Principal=Depends(require_task_principal)):return service.customer_view(session_id)
@router.post(BASE+'/support/messages')
def message(session_id:str,body:CustomerMessage,p:Principal=Depends(require_task_principal)):return service.customer_message(session_id,body.text,body.client_id,str(body.case_id) if body.case_id else None)
@router.post(BASE+'/support/end')
def end(session_id:str,p:Principal=Depends(require_task_principal)):return service.end(session_id)
@router.get(BASE+'/risk/clues')
def clues(session_id:str,p:Principal=Depends(require_task_principal)):return dict(clues=risk.semantic(session_id))
@router.get(BASE+'/risk/{task_id}')
def evaluate(session_id:str,task_id:UUID,p:Principal=Depends(require_task_principal)):
    try:return risk.evaluate(p,session_id,str(task_id))
    except TaskError:raise
    except Exception:return dict(state='unavailable',signals=[],semantic_clues=[],limitations=['提醒服务暂不可用，无法判断。请自行核实收款信息。'],choice=None)
@router.post(BASE+'/risk/{task_id}/choice')
def choice(session_id:str,task_id:UUID,body:Choice,p:Principal=Depends(require_task_principal)):return risk.choose(p,session_id,str(task_id),body.version,body.choice)

@router.post('/api/seat/login')
def login(body:Login,request:Request):return service.login(body.username,body.password,request.client.host if request.client else 'unknown')
@router.post('/api/seat/presence')
def presence(body:Presence,auth=Depends(seat)):return service.seat_presence(auth,body.online)
@router.get('/api/seat/queue')
def queue(auth=Depends(seat)):return service.queue(auth)
@router.post('/api/seat/logout')
def logout(auth=Depends(seat)):
    service.seat_presence(auth,False)
    with service.connection(auth[0]) as c:c.execute('UPDATE assistance.seat_sessions SET expires_at=now() WHERE token_hash=%s',(auth[0],))
    return dict(logged_out=True)
@router.post('/api/seat/cases/{case_id}/accept')
def accept(case_id:UUID,auth=Depends(seat)):return service.accept(auth,str(case_id))
@router.get('/api/seat/cases/{case_id}')
def view(case_id:UUID,auth=Depends(seat)):return service.seat_view(auth,str(case_id))
@router.post('/api/seat/cases/{case_id}/messages')
def reply(case_id:UUID,body:Message,auth=Depends(seat)):return service.seat_message(auth,str(case_id),body.text,body.client_id)
@router.post('/api/seat/cases/{case_id}/end')
def close(case_id:UUID,auth=Depends(seat)):return service.seat_end(auth,str(case_id))
@router.post('/api/seat/cases/{case_id}/tool')
def tool(case_id:UUID,body:Tool,auth=Depends(seat)):return service.tool(auth,str(case_id),body.name)
