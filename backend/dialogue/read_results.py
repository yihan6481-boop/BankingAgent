"""Owned query snapshots. Original denials stay immutable for audit.

Only call rows loaded through DialogueStore may enter this module; an ID or
model-provided resolution is never evidence. Consent is for one saved query,
not a session/global grant. Expiry stops execution, not historical explanation.
"""
import hashlib
import json
from datetime import datetime,timedelta,timezone
from copy import deepcopy
from .projection import project
from agent.schemas import StrictModel
from typing import Literal
from pydantic import Field

LABELS={'get_active_subscriptions':'订阅清单','get_monthly_subscription_cost':'订阅费用折算统计'}
TTL=timedelta(minutes=30)


class SubscriptionRow(StrictModel):
    subscription_id:int=Field(gt=0,strict=True)
    name:str
    amount:float=Field(ge=0,allow_inf_nan=False)
    cycle:str
    next_charge_date:str
    card_id:int=Field(gt=0,strict=True)
    card_last_four:str=Field(pattern=r'^[0-9]{4}$')
    status:Literal['active','cancelled']


class SubscriptionCosts(StrictModel):
    active_count:int=Field(ge=0,strict=True)
    monthly_cost:float=Field(ge=0,allow_inf_nan=False)
    annual_cost:float=Field(ge=0,allow_inf_nan=False)
    annual_cost_cents:int|None=None
    cost_basis:str|None=None
    monthly_rounding:str|None=None
    complete:bool=True
    unknown_cycle_count:int=0
    subscriptions:list[SubscriptionRow]


class SubscriptionListSnapshot(StrictModel):
    kind:Literal['subscription_list']
    status:Literal['success']='success'
    message:str
    data:list[SubscriptionRow]


class SubscriptionCostSnapshot(StrictModel):
    kind:Literal['subscription_cost']
    status:Literal['success']='success'
    message:str
    data:SubscriptionCosts


def binding(p,cid,tid,call):
    payload=[str(p.user_id),p.principal_id,cid,tid,call['call_id'],call['name'],call['arguments']]
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def eligible(call):
    return call['name'] in LABELS and call.get('status')=='completed' and (call.get('result') or {}).get('code')=='query_authorization_required'


def checked_result(call,p,cid,tid):
    """Return validated resolution or None; never upgrade another refusal."""
    r=call.get('resolution') or {}
    if not eligible(call) or r.get('kind')!='authorized_query' or r.get('state') in {'revoked','failed'}:return None
    if r.get('binding') is not None and r['binding']!=binding(p,cid,tid,call):return None
    # Legacy snapshots have no binding metadata; the persisted row's owner,
    # dialogue/turn/call, original typed args and result schema still bind them.
    value=r.get('result') or {}
    if value.get('status')!='completed':return None
    # This reviewed boundary DTO deliberately has no dependency on the API/UI
    # layer or its legacy Agent. Both transport shapes are exercised via HTTP.
    model=SubscriptionListSnapshot if call['name']=='get_active_subscriptions' else SubscriptionCostSnapshot
    data=dict(value.get('data') or {});stamp=data.pop('queried_at',None);historical=data.pop('historical',None)
    try:
        parsed=model.model_validate(data).model_dump(mode='json')
        if not stamp or datetime.fromisoformat(stamp).tzinfo is None:return None
        from .tools import DESCRIPTIONS
        parsed_args=DESCRIPTIONS[call['name']][0].model_validate(call['arguments'])
        card=parsed_args.card_id
        rows=parsed['data'] if call['name']=='get_active_subscriptions' else parsed['data']['subscriptions']
        if card is not None and any(x['card_id']!=card for x in rows):return None
        if len({x['subscription_id'] for x in rows})!=len(rows):return None
    except (ValueError,TypeError):return None
    return dict(value,data=parsed|dict(queried_at=stamp,historical=True))


def effective(call,p,cid,tid):
    value=checked_result(call,p,cid,tid)
    r=call.get('resolution') or {}
    if value is None and eligible(call) and r.get('kind')=='authorized_query':
        from foundation.contracts import feedback
        state=authorization_view(call,p,cid,tid)['state']
        value=feedback('service_failure' if state=='failed' else 'denied','query_authorization_'+state)
    return dict(call,result=project(value)) if value is not None else call


def owned(p,cid,tid,call_id):
    from .store import DialogueStore
    return effective(DialogueStore().call_read(p,cid,tid,call_id),p,cid,tid)


def evidence_revision(calls,p,cid,tid):
    rows=[(c['call_id'],checked_result(c,p,cid,tid)) for c in calls]
    rows=sorted((key,value) for key,value in rows if value is not None)
    return hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest() if rows else None


def authorization_view(call,p,cid,tid,now=None):
    if not eligible(call):return None
    now=now or datetime.now(timezone.utc);r=call.get('resolution') or {}
    result=checked_result(call,p,cid,tid)
    state='authorized' if result else 'pending'
    if r and not result:state='revoked' if r.get('state')=='revoked' else 'failed'
    created=call.get('created_at')
    if not r and created is not None and created+TTL<=now:state='expired'
    card=call['arguments'].get('card_id')
    return dict(call_id=call['call_id'],scope_id=binding(p,cid,tid,call),label=LABELS[call['name']],
        scope=('本人全部卡' if card is None else '本人指定卡（编号'+str(card)+'）')+' · '+LABELS[call['name']],
        state=state,result=deepcopy(result['data']) if result else None,
        next_step={'pending':'仅授权并读取所列查询，随后自动整理原问题答复。','authorized':'已取得原查询结果；历史快照不代表重新查询。','expired':'本次授权已过期，请发起新的只读查询。','revoked':'本次授权已撤销，不会读取或继续使用该结果。','failed':'本次授权或查询未完成，请查看查询提示；不会自动重试。'}[state])
