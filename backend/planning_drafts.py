"""Non-authorizing, five-minute planning context. Never a TaskStore record.

Only the independently parsed goal inventory is persisted, not model candidates,
chat history, bank observations, previews, credentials or grants.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import re
from decimal import Decimal,InvalidOperation
from uuid import uuid4

from lifecycle.store import db, digest, TaskError
from security.service import get_security_services

STATES = {'pending', 'resumed', 'consumed', 'cancelled', 'expired', 'superseded'}
ACTIVE = {'pending', 'resumed'}


def now():
    return get_security_services().now_provider()


def binding(session_id):
    return hashlib.sha256(('planning-source-v1:' + session_id).encode()).hexdigest()


def seal(row,payload):
    return digest(dict(payload=payload,**{k:row[k] for k in (
        'draft_id','owner_id','principal_id','revision','status','created_at','expires_at')}))


def migrate_sqlite(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS planning_drafts (
        draft_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, principal_id TEXT NOT NULL,
        revision INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL, snapshot TEXT NOT NULL, checksum TEXT NOT NULL)''')


def validate_flow(flow):
    """Closed durable structure; no arbitrary model keys can be stored."""
    from security.redaction import redact
    if (not isinstance(flow, dict) or set(flow)-{'goals','error','bound_date','ordering'} or
            flow.get('error') or not 2 <= len(flow.get('goals', [])) <= 8 or
            len(json.dumps(flow, ensure_ascii=False)) > 8000 or redact(flow) != flow):
        raise TaskError('invalid_draft', '需求暂不能安全保存，请重新明确业务目标。')
    datetime.fromisoformat(flow['bound_date'])
    from agent.flow_requirements import WRITES, BILLS
    allowed = {'from_card_id','card_id','amount','recipient_name','product_id','holding_id',
               'suitable_only','risk_level','status'}
    for g in flow['goals']:
        if (set(g)-{'kind','args','request','condition','card_ids'} or
                g['kind'] not in WRITES | BILLS | {'bill','get_cards','get_wealth_products',
                'get_wealth_holdings','get_risk_profile'} or set(g.get('args', {}))-allowed):
            raise TaskError('invalid_draft')
        for key,v in g.get('args',{}).items():
            if v is None:continue
            if key in {'from_card_id','card_id','product_id','holding_id'} and (type(v) is not int or v<=0):raise TaskError('invalid_draft')
            if key=='amount':
                try:
                    number=Decimal(str(v))
                    if isinstance(v,bool) or not number.is_finite() or number<=0 or number*100!=(number*100).to_integral_value():raise ValueError()
                except (ValueError,InvalidOperation):raise TaskError('invalid_draft') from None
            if key=='recipient_name' and (not isinstance(v,str) or not 1<=len(v)<=20):raise TaskError('invalid_draft')
        if 'request' in g and (not isinstance(g['request'],str) or len(g['request'])>500):
            raise TaskError('invalid_draft')
        if 'condition' in g and set(g['condition']) != {'source','operator','value'}:
            raise TaskError('invalid_draft')
    if flow.get('ordering') not in {None,'explicit','ambiguous','independent'}:
        raise TaskError('invalid_draft')
    return deepcopy(flow)


def missing(flow):
    return [f'goal_{i+1}.{key}' for i,g in enumerate(flow['goals'])
            for key,value in g.get('args',{}).items()
            if value is None and key not in {'risk_level','status'}]


def summary(flow):
    labels={'bill':'账单查询','get_cards':'余额查询','get_wealth_products':'理财产品查询',
            'transfer_money':'转账','buy_wealth_product':'理财购买',
            'redeem_wealth_product':'理财赎回','get_wealth_holdings':'持仓查询'}
    return ' → '.join(labels.get(g['kind'],g['kind']) for g in flow['goals'])


def public(row):
    p=row['payload']
    return dict(draft_id=row['draft_id'],revision=row['revision'],status=row['status'],
        kind='workflow_plan',summary=summary(p['flow']),missing_fields=p['missing_fields'],
        goals=p['flow']['goals'],ordering=p['flow'].get('ordering'),
        created_at=row['created_at'],expires_at=row['expires_at'],authorized=False)


class DraftStore:
    def _decode(self,row,principal):
        if not row or row['owner_id']!=principal.user_id or row['principal_id']!=principal.principal_id:
            raise TaskError('draft_not_found','草稿不存在或不属于当前身份。',404)
        try:
            p=json.loads(row['snapshot'])
            if (seal(row,p)!=row['checksum'] or set(p)!={'version','source_binding','resume_binding','flow','missing_fields'} or
                    p['version']!=1 or not re.fullmatch('[0-9a-f]{64}',p['source_binding']) or
                    row['status'] not in STATES or row['revision']<1):
                raise ValueError()
            if datetime.fromisoformat(row['expires_at'])-datetime.fromisoformat(row['created_at'])!=timedelta(minutes=5):raise ValueError()
            if not re.fullmatch('[0-9a-f]{64}',p['resume_binding']):raise ValueError()
            validate_flow(p['flow'])
            if not all(isinstance(v,str) and len(v)<=80 for v in p['missing_fields']):raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise TaskError('invalid_draft','草稿完整性核验失败，未恢复需求。') from None
        return dict(row,payload=p)

    def load(self,principal,draft_id):
        with db() as c:
            row=self._decode(c.execute('SELECT * FROM planning_drafts WHERE draft_id=? AND owner_id=? AND principal_id=?',
                (draft_id,principal.user_id,principal.principal_id)).fetchone(),principal)
        if row['status'] in ACTIVE and now()>=datetime.fromisoformat(row['expires_at']):
            row=self.update(principal,row,status='expired')
        return row

    def list(self,principal):
        with db() as c:
            ids=c.execute('SELECT draft_id FROM planning_drafts WHERE owner_id=? AND principal_id=? ORDER BY created_at',
                          (principal.user_id,principal.principal_id)).fetchall()
        return [r for r in (self.load(principal,v['draft_id']) for v in ids) if r['status'] in ACTIVE]

    def create(self,session,flow,fields):
        p=dict(version=1,source_binding=binding(session.session_id),resume_binding=binding(session.session_id),
               flow=validate_flow(flow),missing_fields=fields)
        stamp=now(); row=dict(draft_id='pd_'+uuid4().hex,owner_id=session.principal.user_id,
            principal_id=session.principal.principal_id,revision=1,status='pending',
            created_at=stamp.isoformat(),expires_at=(stamp+timedelta(minutes=5)).isoformat(),payload=p)
        with db() as c:
            # Bound total retention as well as live drafts. Cleanup affects only
            # this owner's non-authorizing context, never tasks or receipts.
            c.execute('DELETE FROM planning_drafts WHERE owner_id=? AND principal_id=? AND expires_at<?',
                (row['owner_id'],row['principal_id'],(stamp-timedelta(days=1)).isoformat()))
            # Bound active storage; expired rows contain no reusable parameters.
            count=c.execute("SELECT count(*) AS n FROM planning_drafts WHERE owner_id=? AND principal_id=? AND status IN ('pending','resumed') AND expires_at>?",
                (row['owner_id'],row['principal_id'],stamp.isoformat())).fetchone()['n']
            if count>=8:raise TaskError('draft_limit','未完成草稿过多，请先取消不需要的草稿。')
            c.execute('''INSERT INTO planning_drafts
                (draft_id,owner_id,principal_id,revision,status,created_at,expires_at,snapshot,checksum)
                VALUES(?,?,?,?,?,?,?,?,?)''',tuple(row[k] for k in ['draft_id','owner_id','principal_id','revision','status','created_at','expires_at'])+
                (json.dumps(p,ensure_ascii=False),seal(row,p)))
        return row

    def update(self,principal,row,*,status=None,payload=None):
        status=status or row['status'];p=payload or row['payload']
        if status not in STATES:raise TaskError('invalid_draft')
        validate_flow(p['flow'])
        updated_row=dict(row,revision=row['revision']+1,status=status,payload=deepcopy(p))
        with db() as c:
            updated=c.execute('''UPDATE planning_drafts SET revision=?,status=?,snapshot=?,checksum=?
                WHERE draft_id=? AND owner_id=? AND principal_id=? AND revision=? AND status=?''',
                (row['revision']+1,status,json.dumps(p,ensure_ascii=False),seal(updated_row,p),row['draft_id'],
                 principal.user_id,principal.principal_id,row['revision'],row['status']))
            if updated.rowcount!=1:raise TaskError('stale_draft_revision','草稿已被其他会话修改，请刷新并明确重新选择。')
        return updated_row


def attach(session,row):
    from agent.schemas import PendingClarification
    flow=deepcopy(row['payload']['flow'])
    session.planning_draft_id=row['draft_id'];session.planning_draft_revision=row['revision']
    session.context.reset_scope()
    session.context.original_request_date=flow['bound_date']
    session.context.query_requirements=dict(flow=flow,goals=[],months=[],conditions=[])
    session.messages=[]
    session.pending_clarification=PendingClarification(kind='workflow_plan',tool_name='create_task_plan',
        missing_fields=row['payload']['missing_fields'] or ['complete_plan'],requires_full_replan=True)
    session.clarification_expires_at=datetime.fromisoformat(row['expires_at'])


def sync(session):
    if not session.principal or session.protocol!='v2':return None
    flow=(session.context.query_requirements or {}).get('flow')
    pending=session.pending_clarification
    if not flow or flow.get('error') or not pending or pending.kind!='workflow_plan':return None
    fields=missing(flow) or list(pending.missing_fields)
    store=DraftStore()
    if session.planning_draft_id:
        row=store.load(session.principal,session.planning_draft_id)
        if row['revision']!=session.planning_draft_revision or row['status'] not in ACTIVE:
            raise TaskError('stale_draft_revision','草稿已失效或发生修订，请重新选择。')
        p=dict(row['payload'],flow=validate_flow(flow),missing_fields=fields)
        row=store.update(session.principal,row,payload=p)
    else:row=store.create(session,flow,fields)
    session.planning_draft_id=row['draft_id'];session.planning_draft_revision=row['revision']
    session.clarification_expires_at=datetime.fromisoformat(row['expires_at'])
    return public(row)


def consume(session):
    if not session.planning_draft_id:return
    store=DraftStore();row=store.load(session.principal,session.planning_draft_id)
    if (row['revision']!=session.planning_draft_revision or row['status'] not in ACTIVE or
            row['payload']['resume_binding']!=binding(session.session_id)):
        raise TaskError('stale_draft_revision','草稿已失效或由其他会话接续，请重新选择。')
    store.update(session.principal,row,status='consumed')
    session.planning_draft_id=None;session.planning_draft_revision=None
