"""Additive mobile protocol. No PIN, gesture or OTP enters dialogue tools."""
from typing import Literal
from fastapi import Depends,Request
from pydantic import Field,StrictInt,StrictStr
from agent.schemas import StrictModel
from api.secure import router
from lifecycle.routes import require_task_principal
from security.schemas import Principal
from security.service import get_security_services
from security.payment_auth import enabled,guard,keyed
from security.strong_auth import StrongAuthError

class Setup(StrictModel):
    bootstrap:StrictStr=Field(min_length=20,max_length=128,repr=False)
    phone:StrictStr=Field(pattern=r'^1[3-9][0-9]{9}$',repr=False)
    payment_pin:StrictStr=Field(pattern=r'^[0-9]{6}$',repr=False)
    gesture:StrictStr=Field(pattern=r'^[1-9]{4,9}$',repr=False)

class Bound(StrictModel):
    task_id:StrictStr=Field(min_length=1,max_length=64)
    revision:StrictInt=Field(ge=1)
    preview_id:StrictStr=Field(pattern=r'^[a-f0-9]{64}$')

class Proof(Bound):
    stage:Literal['payment','gesture','otp']
    value:StrictStr=Field(min_length=1,max_length=32,repr=False)

def service():return get_security_services().payment_auth

@router.get('/mobile/capabilities')
def capabilities(session_id:str,principal:Principal=Depends(require_task_principal)):
    import os,json
    from dialogue.budget import Budget,BudgetError
    reason=os.environ.get('BA_MOBILE_PAID_REASON','')
    budget=None
    try:
        budget=Budget();status=budget.status()
        if status.get('remaining_requests')==0 or status.get('remaining_tokens')==0:reason='paid_budget_exhausted'
    except BudgetError as exc:reason=str(exc)
    model_reason=reason or ('offline_disabled' if os.environ.get('BA_CANDIDATE_MODE')=='offline' else 'model_disabled' if os.environ.get('BA_LLM_DISABLED')=='1' else '')
    vision_reason=reason if os.environ.get('BA_VISION_USE_RUNTIME_BUDGET')=='1' else ''
    try:
        from vision_gate import provider_settings
        provider_settings()
    except Exception:vision_reason='vision_not_configured'
    provider_reason=''
    if budget and budget.path.exists():
        try:
            rows=json.loads(budget.path.read_text('utf8'))['requests']
            last=next((r for r in reversed(rows) if r.get('requested_model')==os.environ.get('LLM_MODEL')),None)
            if last and last.get('status')=='failed':
                provider_reason={'model_http_401':'provider_authentication_failed','provider_http_401':'provider_authentication_failed',
                    'model_http_402':'provider_balance_insufficient','provider_http_402':'provider_balance_insufficient',
                    'model_http_429':'provider_rate_limited','provider_http_429':'provider_rate_limited',
                    'model_network_failure':'provider_network_unavailable','provider_network_unknown':'provider_network_unavailable'}.get(last.get('error'),'')
        except (OSError,ValueError,KeyError):pass
    return dict(manual_available=True,paid_model_enabled=not model_reason,
                reason=model_reason or provider_reason,ocr_enabled=not vision_reason,ocr_reason=vision_reason or provider_reason,
                memory_configured=os.environ.get('BA_R4_ENABLED')=='1',mode=os.environ.get('BA_CANDIDATE_MODE','configured'))

@router.post('/mobile/support/request')
def support_request(session_id:str,principal:Principal=Depends(require_task_principal)):
    from security.support import request
    return request(session_id)

@router.post('/mobile/support/resume')
def support_resume(session_id:str,principal:Principal=Depends(require_task_principal)):
    from security.support import request
    return request(session_id,False)
def contexts(request,sid):
    source=request.client.host if request.client else 'unknown'
    return keyed('device:'+sid+':'+request.headers.get('user-agent','')),source

@router.get('/mobile/security')
def status(session_id:str,principal:Principal=Depends(require_task_principal)):
    return service().status(principal) if enabled() else {'enabled':False}

@router.post('/mobile/security/configure')
def configure(session_id:str,body:Setup,principal:Principal=Depends(require_task_principal)):
    return service().configure(principal,session_id,**body.model_dump())

@router.post('/mobile/challenges/{challenge_id}/state')
def state(session_id:str,challenge_id:str,body:Bound,principal:Principal=Depends(require_task_principal)):
    return service().describe(principal,session_id,challenge_id,body.model_dump())

@router.post('/mobile/challenges/{challenge_id}/proof')
def proof(session_id:str,challenge_id:str,body:Proof,request:Request,principal:Principal=Depends(require_task_principal)):
    device,source=contexts(request,session_id)
    return service().proof(principal,session_id,challenge_id,body.model_dump(exclude={'stage','value'}),body.stage,body.value,device,source)

@router.post('/mobile/challenges/{challenge_id}/sms')
def sms(session_id:str,challenge_id:str,body:Bound,request:Request,principal:Principal=Depends(require_task_principal)):
    device,source=contexts(request,session_id)
    return service().send_sms(principal,session_id,challenge_id,body.model_dump(),device,source)

@router.get('/mobile/policy')
def policy(session_id:str,principal:Principal=Depends(require_task_principal)):
    guard()
    from security.policy import ACTION_POLICIES,WRITE_ACTIONS
    from security.payment_auth import PaymentAuthService
    config=get_security_services().config
    return dict(contract_version='payment-security-1.0',original_limits_unchanged=True,
      transfer_cumulative_threshold_cents=config.daily_transfer_confirm_cents,
      limit_escalation_cents=config.daily_limit_escalation_cents,
      new_demo_sms_threshold_cents=service().status(principal)['sms_threshold_cents'],
      rules=[dict(action=a,original_level=p.base_level,payment_pin=a in WRITE_ACTIONS,
        gesture='red' if a in WRITE_ACTIONS else False,
        sms='red且本次金额>演示短信阈值；或red挂失/限额调整、R5产品、未核验模拟外部收款' if a in WRITE_ACTIONS else False)
        for a,p in sorted(ACTION_POLICIES.items())])
