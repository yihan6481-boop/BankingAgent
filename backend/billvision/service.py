"""Owner-scoped deterministic import. Model output never confers authorization."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4
from .models import Candidate, Recognition, ReviewedRow, Query, QueryResult
from .provider import VisionError,decode_image

def Jsonb(value):
    # PostgreSQL remains optional for the original SQLite application.
    from psycopg.types.json import Jsonb as PGJsonb
    return PGJsonb(value)

def check_connection(c,ctx,write):
    if c.execute("SELECT to_regclass('billvision.documents') AS t").fetchone()['t'] is None:
        raise VisionError('billvision_migration_required','上传账单需要管理员增量迁移0006；未降级为SQLite',503)
    if write:
        # Short identity lock only during a local import transaction; released
        # before image-provider HTTP. Re-check after HTTP/lease recovery as well.
        c.execute('SELECT pg02.lock_identity(%s,%s,%s)',(ctx.principal_id,ctx.credential_id,ctx.session_id))
    row=c.execute('''SELECT s.credential_expires_at,s.revoked,c.revoked AS credential_revoked,p.status AS principal_status,u.status AS user_status
        FROM pg02.auth_sessions s JOIN pg02.credentials c ON c.owner_uuid=s.owner_uuid AND c.credential_id=s.credential_id
        JOIN pg02.principals p ON p.owner_uuid=s.owner_uuid AND p.principal_id=s.principal_id
        JOIN core.users u ON u.id=s.owner_uuid WHERE s.session_id=%s AND s.principal_id=%s AND s.credential_id=%s AND s.protocol='v2' ''',
        (ctx.session_id,ctx.principal_id,ctx.credential_id)).fetchone()
    from security.service import get_security_services
    now=get_security_services().identity._now()
    if not row or row['revoked'] or row['credential_revoked'] or row['principal_status']!='active' or row['user_status']!='active' or datetime.fromisoformat(row['credential_expires_at'])<=now:
        raise VisionError('credential_expired','会话已失效或撤销；未读取/导入账单',401)

def digest(document_id,revision,rows):
    return hashlib.sha256(json.dumps(dict(document_id=document_id,revision=revision,rows=rows),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

@contextmanager
def transaction(principal,write=False):
    from storage.config import pg02_enabled
    if not pg02_enabled():raise VisionError('billvision_pg_required','上传账单功能仅支持PostgreSQL',409)
    from storage.pg02 import current_context,store,read_snapshot,fault
    ctx=current_context()
    if principal is None or (principal.principal_id,principal.user_id)!=(ctx.principal_id,ctx.user_id):
        raise VisionError('billvision_owner_invalid','无权限',403)
    if not write:
        with read_snapshot() as db:
            check_connection(db.raw,ctx,False)
            yield db.raw
        return
    with store()._pool.connection() as c,c.transaction():
        c.execute("SELECT set_config('banking.owner_id',%s,true)",(str(ctx.owner_uuid),))
        # Independent short import lock; no funds/account lock and no HTTP in here.
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('billvision:'+str(ctx.owner_uuid),))
        check_connection(c,ctx,True)
        yield c
        fault('billvision_before_commit')

def document(c,doc,lock=False):
    row=c.execute('SELECT * FROM billvision.documents WHERE document_id=%s'+(' FOR UPDATE' if lock else ''),(doc,)).fetchone()
    if not row:raise VisionError('billvision_not_found','未找到本人账单',404)
    return row

def version_check(d,revision):
    if d['revision']!=revision:raise VisionError('billvision_revision_conflict',f"提交revision={revision}，当前revision={d['revision']}；版本已变化，本次未保存或确认，请重新读取账单核对。",409)

def audit(c,d,operation):
    c.execute('INSERT INTO billvision.audit(audit_id,document_id,revision,operation,row_count) VALUES(%s,%s,%s,%s,%s)',(uuid4(),d['document_id'],d['revision'],operation,len(d['rows'])))

def save_version(c,d,operation):
    c.execute('INSERT INTO billvision.versions(document_id,revision,operation,rows,platform) VALUES(%s,%s,%s,%s,%s)',(d['document_id'],d['revision'],operation,Jsonb(d['rows']),d['platform']))
    audit(c,d,operation)

def event_key(platform,v):
    # Refund shares original order id: no event identity unless separate event id is visible.
    number=v.event_number or (v.order_number if v.kind!='refund' else None)
    if not number or platform=='unknown':return None
    return hashlib.sha256(json.dumps([platform,v.kind,number],ensure_ascii=False).encode()).hexdigest()

def duplicates(c,d,rows):
    existing=c.execute('SELECT document_id,row_id,platform,event_key,value FROM billvision.records WHERE document_id<>%s',(d['document_id'],)).fetchall()
    seen={};result={}
    for row in rows:
        v=Candidate.model_validate(row['value']);key=event_key(d['platform'],v)
        matches=[]
        for r in existing:
            other=Candidate.model_validate(r['value'])
            certain=key is not None and key==r['event_key'] and d['platform']==r['platform']
            similar=(v.date,v.amount_cents,v.merchant,v.direction,v.kind)==(other.date,other.amount_cents,other.merchant,other.direction,other.kind)
            if certain or similar:matches.append(dict(document_id=r['document_id'],row_id=r['row_id'],type='event' if certain else 'possible'))
        if key and key in seen:matches.append(dict(document_id=d['document_id'],row_id=seen[key],type='event'))
        if key and row['included']:seen[key]=row['row_id']
        result[row['row_id']]=matches
    return result

def invoice_source_current(c,d):
    if d['platform']!='invoice':return True
    from .contracts import InvoiceSource
    try:source=InvoiceSource.model_validate(d['original'])
    except Exception:return False
    row=c.execute('SELECT revision,review_digest,kind FROM payments.payment_documents WHERE document_id=%s',(source.payment_document_id,)).fetchone()
    return bool(row and row['kind']=='invoice' and row['revision']==source.source_revision and row['review_digest']==source.source_digest)

def public(c,d,repeated=False):
    rows=d['rows'];h=digest(d['document_id'],d['revision'],rows)
    warnings=duplicates(c,d,rows)
    return dict(document_id=d['document_id'],revision=d['revision'],state=d['state'],platform=d['platform'],
        rows=rows,original=d['original'],digest=h,duplicates=warnings,error_code=d['error_code'],repeated_upload=repeated,
        issues={r['row_id']:Candidate.model_validate(r['value']).unresolved() for r in rows},
        confirmation=d['confirmation'],source_current=invoice_source_current(c,d),coverage='上传图片中已确认记录；不是整月完整账单；未与银行流水合并')

def upload(principal,data):
    checked=decode_image(data)
    with transaction(principal,True) as c:
        old=c.execute('SELECT * FROM billvision.documents WHERE image_hash=%s',(checked['sha256'],)).fetchone()
        if old:return public(c,old,True)
        doc='bv_'+uuid4().hex
        d=c.execute('''INSERT INTO billvision.documents(document_id,image_hash,image,mime,width,height,state)
            VALUES(%s,%s,%s,%s,%s,%s,'uploaded') RETURNING *''',(doc,checked['sha256'],data,checked['mime'],checked['width'],checked['height'])).fetchone()
        save_version(c,d,'upload')
        return public(c,d)

def get(principal,doc):
    with transaction(principal) as c:return public(c,document(c,doc))

def image(principal,doc):
    with transaction(principal) as c:
        d=document(c,doc);return bytes(d['image']),d['mime']

def list_documents(principal):
    with transaction(principal) as c:
        rows=c.execute('SELECT document_id,revision,state,created_at FROM billvision.documents ORDER BY created_at DESC LIMIT 101').fetchall()
        return dict(documents=[dict(r,created_at=r['created_at'].isoformat()) for r in rows[:100]],has_more=len(rows)>100)

def recognize(principal,doc,revision,provider=None):
    from .provider import get_provider
    token=uuid4().hex
    with transaction(principal,True) as c:
        d=document(c,doc,True);version_check(d,revision)
        retry_stale=(d['state']=='recognizing' and (datetime.now(timezone.utc)-d['recognition_started']).total_seconds()>90)
        if d['state'] not in {'uploaded','recognition_failed'} and not retry_stale:
            raise VisionError('billvision_state_conflict','该文档正在识别或已有核对内容；请读取当前版本',409)
        c.execute("UPDATE billvision.documents SET state='recognizing',recognition_token=%s,recognition_started=now(),error_code=NULL WHERE document_id=%s",(token,doc))
        data=bytes(d['image'])
    try:
        result=(provider or get_provider()).recognize(data)
        result=Recognition.model_validate(result.model_dump() if isinstance(result,Recognition) else result)
        if not result.supported and result.rows:raise VisionError('vision_output_invalid','不支持的图片不可含候选',502)
    except Exception as exc:
        failure=exc if isinstance(exc,VisionError) else VisionError('vision_recognition_failed','识别失败，请重试',502)
        with transaction(principal,True) as c:
            d=document(c,doc,True)
            if d['revision']==revision and d['recognition_token']==token and d['state']=='recognizing':
                c.execute("UPDATE billvision.documents SET state='recognition_failed',error_code=%s,recognition_token=NULL WHERE document_id=%s",(failure.code,doc))
                audit(c,d,'recognition_failed')
        raise failure from None
    with transaction(principal,True) as c:
        d=document(c,doc,True);version_check(d,revision)
        if d['recognition_token']!=token or d['state']!='recognizing':raise VisionError('billvision_revision_conflict','旧识别结果已失效',409)
        rows=[]
        for i,v in enumerate(result.rows,1):
            # Document-level ambiguity is part of every candidate's mandatory review.
            v.issues=list(dict.fromkeys(v.issues+result.issues))[:20]
            rows.append(ReviewedRow(row_id='r'+str(i),value=v).model_dump())
        d=c.execute('''UPDATE billvision.documents SET state=%s,revision=revision+1,platform=%s,original=%s,rows=%s,
            recognition_token=NULL,updated_at=now() WHERE document_id=%s RETURNING *''',('review' if result.supported and rows else 'unsupported',result.platform,Jsonb(result.model_dump()),Jsonb(rows),doc)).fetchone()
        save_version(c,d,'recognize');return public(c,d)

def edit(principal,doc,body):
    with transaction(principal,True) as c:
        d=document(c,doc,True);version_check(d,body.revision)
        if d['state'] not in {'review','confirmed'}:raise VisionError('billvision_state_conflict','先识别再核对编辑',409)
        rows=[r.model_dump() for r in body.rows]
        if {r['row_id'] for r in rows}!={r['row_id'] for r in d['rows']} or len(rows)!=len(d['rows']):raise VisionError('billvision_rows_invalid','须提交原行集合；不需要的行请排除',422)
        # Immediately remove old confirmed data from current statistics. New content needs explicit confirmation.
        c.execute('DELETE FROM billvision.records WHERE document_id=%s',(doc,))
        d=c.execute("UPDATE billvision.documents SET state='review',rows=%s,platform=%s,revision=revision+1,confirmation=NULL,updated_at=now() WHERE document_id=%s RETURNING *",(Jsonb(rows),body.platform or d['platform'],doc)).fetchone()
        save_version(c,d,'edit');return public(c,d)

def confirm(principal,doc,body):
    with transaction(principal,True) as c:
        d=document(c,doc,True);version_check(d,body.revision)
        if not invoice_source_current(c,d):raise VisionError('billvision_source_changed','原发票来源已修改或撤回，旧记账不再作为当前证据；请重新核对来源',409)
        ids=body.row_ids
        if len(ids)!=len(set(ids)) or body.digest!=digest(doc,d['revision'],d['rows']):raise VisionError('billvision_confirmation_stale','确认摘要或行集合失效',409)
        included=[r for r in d['rows'] if r['included'] and r['duplicate_resolution']!='exclude']
        if sorted(ids)!=sorted(r['row_id'] for r in included):raise VisionError('billvision_confirmation_stale','确认必须绑定当前选中行集合',409)
        if d['state']=='confirmed':return public(c,d)
        if d['state']!='review':raise VisionError('billvision_state_conflict','候选尚不可确认',409)
        dup=duplicates(c,d,included)
        reasons=[]
        if d['platform']=='unknown':reasons.append('来源平台尚未核实')
        for row in included:
            if not row['reviewed']:reasons.append(row['row_id']+'：未勾选“已逐项核对”，或该标记尚未保存')
            issues=Candidate.model_validate(row['value']).unresolved()
            if issues:reasons.append(row['row_id']+'：未解决疑点（'+'、'.join(issues)+'）')
        if reasons:raise VisionError('billvision_review_required','不能确认导入：'+'；'.join(reasons),422)
        for row in included:
            v=Candidate.model_validate(row['value'])
            matches=dup[row['row_id']]
            if any(x['type']=='event' for x in matches):raise VisionError('billvision_duplicate_event','该交易事件已导入；请排除重复行',409)
            if matches and row['duplicate_resolution']!='keep_distinct':raise VisionError('billvision_duplicate_review','存在疑似重复，请明确保留不同交易或排除',422)
            c.execute('INSERT INTO billvision.records(document_id,row_id,revision,platform,event_key,value) VALUES(%s,%s,%s,%s,%s,%s)',(doc,row['row_id'],d['revision'],d['platform'],event_key(d['platform'],v),Jsonb(v.model_dump())))
        receipt=dict(document_id=doc,revision=d['revision'],digest=body.digest,row_ids=sorted(ids),imported_count=len(ids),kind='personal_data_import')
        d=c.execute("UPDATE billvision.documents SET state='confirmed',confirmation=%s,updated_at=now() WHERE document_id=%s RETURNING *",(Jsonb(receipt),doc)).fetchone()
        audit(c,d,'confirm');return public(c,d)

def delete(principal,doc,revision):
    with transaction(principal,True) as c:
        d=document(c,doc,True);version_check(d,revision);audit(c,d,'delete')
        c.execute('DELETE FROM billvision.documents WHERE document_id=%s',(doc,))
        return dict(document_id=doc,deleted=True)

def versions(principal,doc):
    with transaction(principal) as c:
        document(c,doc)
        return [dict(r,created_at=r['created_at'].isoformat()) for r in c.execute('SELECT revision,operation,rows,platform,created_at FROM billvision.versions WHERE document_id=%s ORDER BY revision',(doc,)).fetchall()]

def query(principal,args):
    with transaction(principal) as c:
        if args.document_id:
            d=document(c,args.document_id)
            if d['state']!='confirmed':raise VisionError('billvision_review_required','请先到核对页核对并明确确认；尚无该图片的已确认统计',422)
            if not invoice_source_current(c,d):raise VisionError('billvision_source_changed','原发票来源已改变或撤回，请重新核对；旧金额未作为当前统计',409)
        filters=["d.state='confirmed'"];params=[]
        for sql,value in [('r.document_id=%s',args.document_id),("r.value->>'date'>=%s",args.start),("r.value->>'date'<%s",args.end_exclusive),("r.value->>'merchant'=%s",args.merchant)]:
            if value is not None:filters.append(sql);params.append(value)
        rows=c.execute('''SELECT r.document_id,r.row_id,r.revision,r.value FROM billvision.records r
            JOIN billvision.documents d ON d.owner_uuid=r.owner_uuid AND d.document_id=r.document_id WHERE '''+' AND '.join(filters)+" ORDER BY r.value->>'date',r.document_id,r.row_id LIMIT 1001",params).fetchall()
        if len(rows)>1000:raise VisionError('billvision_query_limit','结果超过1000条，请缩小日期或上传批次范围',422)
        retired=set()
        valid_rows=[]
        source_cache={}
        for r in rows:
            if r['document_id'] not in source_cache:
                d=c.execute('SELECT * FROM billvision.documents WHERE document_id=%s',(r['document_id'],)).fetchone()
                source_cache[r['document_id']]=invoice_source_current(c,d)
            if not source_cache[r['document_id']]:retired.add(r['document_id'])
            else:valid_rows.append(r)
        rows=valid_rows
        if retired:raise VisionError('billvision_source_changed','本次范围含来源已改变或撤回的发票；请重新核对或选择其他批次，不能把缺失证据当作零支出',409)
        records=[];totals=dict(purchase_expense_cents=0,other_expense_cents=0,refund_cents=0,income_cents=0,excluded_count=0)
        for r in rows:
            v=Candidate.model_validate(r['value'])
            records.append(dict(document_id=r['document_id'],row_id=r['row_id'],revision=r['revision'],**{k:getattr(v,k) for k in ['date','merchant','amount_cents','currency','direction','kind','status']}))
            if v.currency!='CNY' or v.status!='success':totals['excluded_count']+=1;continue
            key=('refund_cents' if v.kind=='refund' else 'purchase_expense_cents' if v.kind=='purchase' and v.direction=='expense' else 'other_expense_cents' if v.direction=='expense' else 'income_cents')
            totals[key]+=v.amount_cents
        result=QueryResult(scope=args,records=records,totals=totals).model_dump()
        return result

def read_tool(args,principal=None):
    try:return dict(success=True,data=query(principal,args),message='已核验本人确认上传账单；退款单列，不与银行流水相加。')
    except VisionError as exc:return dict(success=False,error_code=exc.code,message=exc.message)

def format_answer(data):
    if not data['records']:
        return '该过滤范围没有已确认上传记录；不能据此判断实际消费为零。未与银行流水合并。'
    t=data['totals']
    def yuan(n):return f'{n//100}.{n%100:02d}'
    return (f"上传图片中已确认记录：购买支出{yuan(t['purchase_expense_cents'])}元，其他支出{yuan(t['other_expense_cents'])}元；"
        f"退款单列{yuan(t['refund_cents'])}元，其他收入{yuan(t['income_cents'])}元。"
        f"未计入收支{t['excluded_count']}条（非成功或非CNY）。不是整月完整账单，未与银行流水合并。")
