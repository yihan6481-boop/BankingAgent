"""Mobile views and explicit simulated external destinations; reuse old writes."""
from datetime import date,datetime
import uuid
from typing import Literal
from fastapi import Depends,Query
from pydantic import Field,StrictStr,StrictInt
from api.secure import router
from agent.schemas import StrictModel,TaskPlanProposal
from lifecycle.routes import require_task_principal
from security.schemas import Principal
from payments.service import transaction
from payments.errors import PaymentError

class ExternalAccount(StrictModel):
    account_number:StrictStr=Field(pattern=r'^[0-9]{12,19}$',repr=False)
    account_name:StrictStr=Field(min_length=2,max_length=50)
    bank_name:StrictStr=Field(min_length=2,max_length=50)

class AccountLookup(StrictModel):
    account_number:StrictStr=Field(pattern=r'^[0-9]{12,19}$',repr=False)
    account_name:StrictStr=Field(default='',max_length=50)

class PhoneMapping(StrictModel):
    recipient_id:StrictInt=Field(ge=1)
    phone:StrictStr=Field(pattern=r'^1[3-9][0-9]{9}$',repr=False)
    note:StrictStr=Field(default='',max_length=100)

class PhoneLookup(StrictModel):
    phone:StrictStr=Field(pattern=r'^1[3-9][0-9]{9}$',repr=False)
    request_sequence:StrictInt=Field(default=1,ge=1,le=2147483647)

class PhoneBinding(PhoneLookup):
    session_id:StrictStr=Field(min_length=1,max_length=200,repr=False)
    mode:Literal['phone']
    resolution_id:StrictStr=Field(pattern=r'^rr_[a-f0-9]{32}$')
    revision:StrictInt=Field(ge=1)

class PhonePrepare(StrictModel):
    binding:PhoneBinding
    plan:TaskPlanProposal

class InvoiceReview(StrictModel):
    revision:StrictInt=Field(ge=1)
    digest:StrictStr=Field(pattern=r'^[a-f0-9]{64}$')

@router.post('/mobile/invoices/{document_id}/bookkeeping')
def invoice_review(session_id:str,document_id:str,body:InvoiceReview,principal:Principal=Depends(require_task_principal)):
    from payments.invoice_bridge import prepare
    return prepare(principal,document_id,body.revision,body.digest)

@router.post('/mobile/recipients/lookup')
def lookup(session_id:str,body:AccountLookup,principal:Principal=Depends(require_task_principal)):
    with transaction(principal) as c:
        row=c.execute("SELECT bank_name,name FROM pg02.recipients WHERE account_number=%s AND status='active'",(body.account_number,)).fetchone()
        if row and body.account_name and row['name']!=body.account_name:return {'bank_name':None,'name_matches':False,'source':'private_directory','identity_verified':False}
        if row:return {'bank_name':row['bank_name'],'name_matches':True if body.account_name else None,'source':'private_directory','identity_verified':False}
        from banking_reference.issuer import identify
        return dict(identify(body.account_number),name_matches=None)

@router.get('/mobile/contacts')
def mobile_contacts(session_id:str,principal:Principal=Depends(require_task_principal)):
    with transaction(principal) as c:
        rows=c.execute("SELECT recipient_id,name,bank_name,right(account_number,4) tail FROM pg02.recipients WHERE status='active' ORDER BY recipient_id").fetchall()
        maps=c.execute("SELECT recipient_id,note,phone FROM payments.contacts WHERE status='active' ORDER BY updated_at DESC").fetchall()
        notes={}
        for m in maps:notes.setdefault(m['recipient_id'],m)
        return {'contacts':[dict(r,note=notes.get(r['recipient_id'],{}).get('note',''),phone_masked=(lambda p:p[:6]+'****'+p[-4:] if p else None)(notes.get(r['recipient_id'],{}).get('phone'))) for r in rows]}

@router.post('/mobile/contacts/phone')
def map_phone(session_id:str,body:PhoneMapping,principal:Principal=Depends(require_task_principal)):
    from security.redaction import redact
    if redact(body.note)!=body.note:raise PaymentError('contact_note_sensitive','备注不能包含密码或敏感凭据',422)
    with transaction(principal,True) as c:
        if not c.execute("SELECT 1 FROM pg02.recipients WHERE recipient_id=%s AND status='active'",(body.recipient_id,)).fetchone():raise PaymentError('recipient_not_found','收款人不属于本人目录或不可用',404)
        recipient=c.execute('SELECT name FROM pg02.recipients WHERE recipient_id=%s',(body.recipient_id,)).fetchone()
        if c.execute('''SELECT 1 FROM payments.contacts p JOIN pg02.recipients r ON r.owner_uuid=p.owner_uuid AND r.recipient_id=p.recipient_id
          WHERE p.phone=%s AND p.status='active' AND r.name<>%s''',('+86'+body.phone,recipient['name'])).fetchone():
            raise PaymentError('phone_already_bound','该完整手机号已登记给其他收款人',409)
        c.execute('''INSERT INTO payments.contacts(contact_id,recipient_id,display_name,phone,phone_bound_at,note)
          VALUES(%s,%s,%s,%s,now(),%s) ON CONFLICT(owner_uuid,recipient_id) DO UPDATE SET
          phone=excluded.phone,phone_bound_at=now(),note=excluded.note,status='active',phone_source='user',revision=payments.contacts.revision+1,updated_at=now()''',
          ('ct_'+uuid.uuid4().hex,body.recipient_id,recipient['name'],'+86'+body.phone,body.note))
        return {'saved':True,'phone_masked':body.phone[:3]+'****'+body.phone[-4:],'notification_phone_changed':False}

@router.post('/mobile/recipients/phone')
def phone_lookup(session_id:str,body:PhoneLookup,principal:Principal=Depends(require_task_principal)):
    from payments.phone_binding import lookup
    return lookup(principal,session_id,body.phone,body.request_sequence)

@router.post('/mobile/phone-transfer')
def phone_transfer(session_id:str,body:PhonePrepare,principal:Principal=Depends(require_task_principal)):
    from payments.phone_binding import prepare
    from lifecycle.routes import create
    from lifecycle.models import CreateTaskRequest
    return prepare(principal,session_id,body.binding,body.plan,
                   lambda:create(session_id,CreateTaskRequest(plan=body.plan),principal))

@router.get('/mobile/card-transactions')
def card_transactions(session_id:str,card_id:int=Query(ge=1),month:str=Query(pattern=r'^[0-9]{4}-(0[1-9]|1[0-2])$'),page:int=Query(1,ge=1),principal:Principal=Depends(require_task_principal)):
    with transaction(principal) as c:
        if not c.execute('SELECT 1 FROM pg02.cards WHERE card_id=%s',(card_id,)).fetchone():raise PaymentError('card_not_found','未找到本人银行卡',404)
        rows=c.execute('SELECT transaction_id,date,event_at,time_source,merchant,amount_cents,direction,category,transaction_type FROM pg02.visible_transactions WHERE card_id=%s AND date LIKE %s ORDER BY date DESC,event_at DESC NULLS LAST,transaction_id DESC LIMIT 21 OFFSET %s',(card_id,month+'-%',(page-1)*20)).fetchall()
        return {'month':month,'card_id':card_id,'transactions':[dict(r) for r in rows[:20]],'has_more':len(rows)>20}

@router.get('/mobile/monthly-summary')
def monthly_summary(session_id:str,month:str=Query(pattern=r'^[0-9]{4}-(0[1-9]|1[0-2])$'),principal:Principal=Depends(require_task_principal)):
    with transaction(principal) as c:
        row=c.execute('''SELECT count(*) count,coalesce(sum(amount_cents) FILTER(WHERE direction='income'),0) income_cents,
          coalesce(sum(amount_cents) FILTER(WHERE direction='expense'),0) expense_cents,
          coalesce(sum(amount_cents) FILTER(WHERE transaction_type='purchase'),0) purchase_cents,
          coalesce(sum(amount_cents) FILTER(WHERE transaction_type='refund'),0) refund_cents
          FROM pg02.visible_transactions WHERE left(date,7)=%s''',(month,)).fetchone()
        return dict(month=month,**row,source='local_simulated_transactions',timezone='Asia/Shanghai',as_of=datetime.now(__import__('datetime').timezone.utc).isoformat())

@router.get('/mobile/transactions/{transaction_id}')
def transaction_detail(session_id:str,transaction_id:int,principal:Principal=Depends(require_task_principal)):
    with transaction(principal) as c:
        row=c.execute('''SELECT t.transaction_id,t.card_id,right(k.masked_number,4) card_tail,t.date,t.event_at,t.time_source,
          t.merchant,t.category,t.transaction_type,t.amount_cents,t.direction,
          coalesce(o.remark,'') remark,r.task_id receipt_task_id
          FROM pg02.visible_transactions t JOIN core.cards k ON k.public_id=t.card_id AND k.owner_id=t.owner_uuid
          LEFT JOIN pg02.transfer_orders o ON o.owner_uuid=t.owner_uuid AND o.transfer_id=t.source_order_id
          LEFT JOIN pg02.wealth_orders w ON w.owner_uuid=t.owner_uuid AND w.order_id=t.source_wealth_order_id
          LEFT JOIN pg02.lifecycle_receipts r ON r.owner_uuid=t.owner_uuid AND r.business_key=coalesce(o.idempotency_key,w.idempotency_key)
          WHERE t.transaction_id=%s''',(transaction_id,)).fetchone()
        if not row:raise PaymentError('transaction_not_found','未找到本人已发生的交易',404)
        return dict(row,status='success',source='local_simulated_transactions')

@router.post('/mobile/recipients/external')
def external(session_id:str,body:ExternalAccount,principal:Principal=Depends(require_task_principal)):
    from security.payment_auth import guard
    guard()
    from security.redaction import redact
    if any(redact(v)!=v for v in [body.account_name,body.bank_name]):raise PaymentError('recipient_invalid','户名或银行名称含敏感字段，请核对',422)
    with transaction(principal,True) as c:
        rows=c.execute('SELECT * FROM pg02.recipients WHERE account_number=%s',(body.account_number,)).fetchall()
        if rows:
            match=next((r for r in rows if r['name']==body.account_name and r['bank_name']==body.bank_name and r['status']=='active'),None)
            if not match:raise PaymentError('recipient_conflict','已登记账户的户名或银行信息不一致，未覆盖',409)
            rid=match['recipient_id']
        else:
            rid=c.execute("SELECT nextval('mobile_security.external_recipient_ids') rid").fetchone()['rid']
            # Explicit simulation eligibility is separate from directory trust.
            c.execute('''INSERT INTO pg02.recipients(recipient_id,name,bank_name,account_number,status,trusted,created_at,owner_id,id)
              VALUES(%s,%s,%s,%s,'active',0,%s,%s,%s)''',(rid,body.account_name,body.bank_name,body.account_number,datetime.now().isoformat(),principal.user_id,uuid.uuid4()))
            c.execute('INSERT INTO mobile_security.external_accounts(recipient_id) VALUES(%s)',(rid,))
        return dict(recipient_id=rid,name=body.account_name,bank_name=body.bank_name,masked_account='**** '+body.account_number[-4:],simulation=True,identity_verified=False,notice='仅登记模拟结算目标；银行及户名由你提供，未核验真实账户归属。')

@router.get('/mobile/products')
def products(session_id:str,principal:Principal=Depends(require_task_principal)):
    with transaction(principal) as c:
        rows=c.execute("SELECT product_id,name,category,risk_level,min_purchase_cents,reference_annual_rate_bps,duration_days,terms FROM pg02.wealth_products WHERE status='active' ORDER BY product_id").fetchall()
        return {'products':[dict(r) for r in rows],'notice':'全部为模拟产品。基金演示净值固定1元，保险不构成真实承保；本轮不计提利息或投资收益。'}

@router.get('/mobile/product-contracts')
def product_contracts(session_id:str,principal:Principal=Depends(require_task_principal)):
    with transaction(principal) as c:
        return {'contracts':[dict(r) for r in c.execute('SELECT order_id,category,product_id,amount_cents,units,terms,created_at FROM mobile_security.product_contracts ORDER BY created_at DESC LIMIT 100').fetchall()]}

@router.get('/mobile/transfer-history')
def transfer_history(session_id:str,page:int=Query(1,ge=1),status:str=Query('all',pattern='^(all|waiting|completed|failed|cancelled|unknown)$'),principal:Principal=Depends(require_task_principal)):
    from lifecycle.store import TaskStore
    from lifecycle.service import public
    from security.db import read_connection
    # Page the durable task table, not just completed transfer orders. Pending,
    # failed and uncertain operations remain visible even without a receipt.
    with read_connection() as db:
        rows=db.execute('SELECT * FROM lifecycle_tasks ORDER BY rowid DESC').fetchall()
        tasks=[TaskStore()._decode(db,r,principal) for r in rows]
    tasks=[r for r in tasks if any(s.tool_name=='transfer_money' for s in r.task.steps)]
    if status!='all':
        if status=='waiting':tasks=[r for r in tasks if r.status in {'active','waiting','paused','scheduled','manual'}]
        elif status=='unknown':tasks=[r for r in tasks if any(o.state in {'inflight','unknown'} for o in r.operations.values())]
        else:tasks=[r for r in tasks if r.status==status]
    total=len(tasks);selected=tasks[(page-1)*10:page*10]
    return {'tasks':[public(t) for t in selected],'page':page,'total':total,'has_more':page*10<total}

@router.get('/mobile/demo-ledger')
def demo_ledger(session_id:str,include_future:bool=False,principal:Principal=Depends(require_task_principal)):
    with transaction(principal) as c:
        rows=c.execute('SELECT card_id,day,direction,amount_cents,label,closing_snapshot_cents,future_scenario FROM mobile_security.demo_ledger WHERE (%s OR NOT future_scenario) ORDER BY day,source_key',(include_future,)).fetchall()
        return {'source':'independent_demo_snapshot','actual_bank_transactions':False,'future_included':include_future,'events':[dict(r) for r in rows],
          'notice':'独立演示账本：每月收支净额为零，期初与期末取生成时的账面余额快照。未并入真实流水或修改余额。未来情景只在隔离候选库显式展示。'}
