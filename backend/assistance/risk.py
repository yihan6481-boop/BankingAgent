"""Explainable advisory signals, never financial authorization or a refusal."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from hashlib import sha256
from statistics import median,quantiles
from zoneinfo import ZoneInfo
import json
from typing import Literal
from pydantic import Field,StrictStr
from agent.schemas import StrictModel
from lifecycle.store import TaskStore,TaskError
from dialogue.store import transaction

VERSION='advisory-1.0'
TZ=ZoneInfo('Asia/Shanghai')
LABELS={'impersonation':'对方身份尚需独立核实','urgency':'对方催促尽快付款','secrecy':'对方要求对付款保密','rebate':'对方承诺刷单或返利','remote_control':'对方要求共享屏幕或远程操作'}

class Clue(StrictModel):
    kind:Literal['impersonation','urgency','secrecy','rebate','remote_control']
    quote:StrictStr=Field(min_length=2,max_length=180,description='当前用户原话中的连续原文，不能引用历史/OCR/工具或自行补写。')
class SemanticReport(StrictModel):
    clues:list[Clue]=Field(max_length=5)
    reference:StrictStr|None=Field(None,max_length=100,description='已存在的本笔任务引用；没有任务则省略。仅提醒，不改变确认或授权。')

class PresentedClue(Clue):
    label:StrictStr
    source:Literal['user_statement']
class SemanticView(StrictModel):
    kind:Literal['advisory_risk']
    state:Literal['attention','no_semantic_clues_reported']
    clues:list[PresentedClue]=Field(max_length=5)
    message:StrictStr
    rule_version:Literal['advisory-1.0']

def report(args,p,sid):
    from dialogue.links import binding
    from dialogue.store import DialogueStore
    from psycopg.types.json import Jsonb
    scope=binding.get()
    if not scope:raise TaskError('risk_trusted_turn_required',status=403)
    cid,tid,call_id=scope
    parsed=SemanticReport.model_validate(args)
    if parsed.reference:
        TaskStore().load(parsed.reference,p)
        if parsed.reference not in {r['reference'] for r in DialogueStore().links(p,cid)}:raise TaskError('risk_task_not_in_dialogue',status=403)
    DialogueStore().load(p,cid)
    with transaction() as c:
        turn=c.execute('SELECT user_text FROM pg02.dialogue_turns WHERE id=%s AND dialogue_id=%s',(tid,cid)).fetchone()
        if not turn or any(clue.quote not in turn['user_text'] for clue in parsed.clues):raise TaskError('risk_quote_not_in_current_message',status=422)
        clues=[dict(kind=x.kind,quote=x.quote,label=LABELS[x.kind],source='user_statement') for x in parsed.clues]
        c.execute('INSERT INTO assistance.semantic_clues(session_id,turn_id,target_reference,clues) VALUES(%s,%s,%s,%s) ON CONFLICT(owner_uuid,session_id,turn_id) DO UPDATE SET clues=excluded.clues,target_reference=excluded.target_reference',(sid,tid,parsed.reference,Jsonb(clues)))
    return dict(kind='advisory_risk',state='attention' if clues else 'no_semantic_clues_reported',clues=clues,
                message='这些是用户描述中的待核实线索，不代表已确认诈骗。仍可继续核对和付款。',rule_version=VERSION)

def semantic(sid,task_id=None):
    with transaction() as c:
        if task_id:
            rows=c.execute('''SELECT s.clues FROM assistance.semantic_clues s WHERE s.session_id=%s
              AND s.created_at>now()-interval '30 minutes'
              AND NOT EXISTS(SELECT 1 FROM assistance.semantic_clues newer WHERE newer.session_id=s.session_id AND newer.clues='[]'::jsonb AND newer.target_reference IS NULL AND newer.created_at>s.created_at)
              AND (s.target_reference=%s OR EXISTS(
              SELECT 1 FROM pg02.dialogue_links l WHERE l.reference=%s AND (l.source_turn=s.turn_id OR l.context->'risk_sources' ? s.turn_id)))
              ORDER BY s.created_at DESC LIMIT 1''',(sid,task_id,task_id)).fetchall()
        else:rows=c.execute("SELECT clues FROM assistance.semantic_clues WHERE session_id=%s AND created_at>now()-interval '30 minutes' ORDER BY created_at DESC LIMIT 1",(sid,)).fetchall()
    return rows[0]['clues'] if rows else []


def recent_context(sid):
    import os
    if not os.getenv('BA_SUPPORT_CREDENTIAL_FILE'):return []
    with transaction() as c:
        rows=c.execute("""SELECT s.turn_id,s.clues FROM assistance.semantic_clues s WHERE s.session_id=%s AND s.clues!='[]'::jsonb
          AND s.created_at>now()-interval '30 minutes' AND NOT EXISTS(SELECT 1 FROM assistance.semantic_clues n WHERE n.session_id=s.session_id AND n.clues='[]'::jsonb AND n.target_reference IS NULL AND n.created_at>s.created_at)
          ORDER BY s.created_at DESC LIMIT 5""",(sid,)).fetchall()
    return [dict(source_turn=r['turn_id'],clues=r['clues']) for r in rows]


def bind_review(p,sid,cid,tid,name,args,result,verdict):
    import os
    if not os.getenv('BA_SUPPORT_CREDENTIAL_FILE') or result.get('status')!='completed':return
    from psycopg.types.json import Jsonb
    if verdict.risk_reset:
        with transaction() as c:c.execute("INSERT INTO assistance.semantic_clues(session_id,turn_id,clues) VALUES(%s,%s,'[]') ON CONFLICT(owner_uuid,session_id,turn_id) DO UPDATE SET clues='[]',target_reference=NULL",(sid,tid))
    # Only fund-preparation capabilities can carry these explicit model-selected
    # sources. Querying in the same session never implicitly binds old advice.
    if name not in {'transfer_create','transfer_patch','task_prepare','create_task_plan','task_modify'}:return
    allowed={r['source_turn'] for r in recent_context(sid)}
    selected=[s for s in verdict.risk_source_turns if s in allowed]
    from dialogue.links import references
    with transaction() as c:
        for ref in references(name,args,result):
            c.execute("UPDATE pg02.dialogue_links SET context=context||%s WHERE dialogue_id=%s AND reference=%s",(Jsonb(dict(risk_sources=selected)),cid,ref))

def statistics(rows,amount,recipient,at):
    """Independent test oracle uses explicit timestamps/amounts, never this output."""
    at=at.astimezone(timezone.utc)
    history=[r for r in rows if r['at']<=at and r['amount']>0]
    signals=[];limits=[];n=len(history)
    amounts=[r['amount'] for r in history]
    if n>=8:
        center=median(amounts);mad=median(abs(x-center) for x in amounts)
        q95=quantiles(amounts,n=20,method='inclusive')[18]
        if mad>0:
            threshold=max(q95,center+6*mad)
            if amount>threshold:signals.append(dict(code='amount_deviation',label='金额高于这张卡的常用转账范围',actual_cents=amount,median_cents=center,mad_cents=mad,threshold_cents=threshold,sample_size=n))
        else:
            limits.append('历史金额变化不足，MAD为零；仅参考分位数，不计算标准化偏离。')
            if amount>max(q95*3,center+10000):signals.append(dict(code='amount_quantile',label='金额明显高于历史转账分位数',actual_cents=amount,threshold_cents=max(q95*3,center+10000),sample_size=n))
    else:limits.append(f'仅有{n}笔历史转账，金额基线不足，未计算金额异常。')
    if history and not any(r['recipient']==recipient for r in history):signals.append(dict(code='new_recipient',label='这张卡的现有记录中首次向该收款人转账',sample_size=n))
    elif not history:limits.append('没有历史记录，无法判断收款对象是否常用。')
    timed=[r for r in history if r.get('time_source')!='demo_rule_v2']
    hour=at.astimezone(TZ).hour
    if len(timed)>=12:
        bucket=hour//4;count=sum(r['at'].astimezone(TZ).hour//4==bucket for r in timed)
        if count/len(timed)<.1:signals.append(dict(code='unusual_time',label='本次时段在已记录转账中较少使用',hour_start=bucket*4,hour_end=bucket*4+4,historical_count=count,sample_size=len(timed)))
    else:limits.append('可靠秒级转账时间样本不足，未推断常用时段。')
    recent=[r for r in history if at-timedelta(minutes=10)<=r['at']<=at]
    if len(recent)>=3:signals.append(dict(code='recent_frequency',label='过去10分钟已有多笔转账',count=len(recent),window_minutes=10))
    return dict(signals=signals,limitations=limits,sample_size=n,timezone='Asia/Shanghai',as_of=at.isoformat(),scope='本张付款卡已完成转账；不含当前待付款、未来记录。演示记录仅说明规则效果，不证明反诈准确率。')

def evaluate(p,sid,task_id):
    from psycopg.types.json import Jsonb
    record=TaskStore().load(task_id,p)
    transfers=[s for s in record.task.steps if s.tool_name=='transfer_money' and s.status!='completed']
    if not transfers:return dict(state='not_applicable',signals=[],semantic_clues=[],limitations=[],choice=None)
    # Multi-step transfers are evaluated for the current waiting transfer only.
    step=next((s for s in transfers if s.step_id==record.task.current_step_id),transfers[0])
    args=step.arguments
    from security.service import get_security_services
    at=get_security_services().now_provider().astimezone(timezone.utc)
    amount=int(Decimal(str(args['amount']))*100);recipient=args.get('recipient_id')
    with transaction() as c:
        if recipient is None and args.get('recipient_name'):
            candidates=c.execute("SELECT recipient_id FROM pg02.recipients WHERE name=%s AND status='active'",(args['recipient_name'],)).fetchall()
            if len(candidates)==1:recipient=candidates[0]['recipient_id']
        rows=c.execute('''SELECT amount_cents AS amount,recipient_id AS recipient,completed_at::timestamptz AS at,
          'transfer_receipt' AS time_source FROM pg02.transfer_orders WHERE from_card_id=%s
          AND completed_at::timestamptz<=%s AND NOT(idempotency_key=ANY(%s::text[]))
          ORDER BY completed_at::timestamptz DESC LIMIT 2000''',(args['from_card_id'],at,[o.key for o in record.operations.values()])).fetchall()
    stats=statistics(rows,amount,recipient,at)
    stats['scope']='本张付款卡最近最多2000笔已完成转账；不含当前待付款、未来记录。演示记录仅说明规则效果，不证明反诈准确率。'
    if recipient is None:
        stats['signals']=[s for s in stats['signals'] if s['code']!='new_recipient']
        stats['limitations'].append('收款人未唯一解析，未推断是否首次转账。')
    clues=semantic(sid,task_id)
    # Stable risk version for this exact task revision and evidence. No timestamp
    # noise: repeated reads never resurrect a dismissed same-version reminder.
    evidence_key=dict(rule=VERSION,revision=record.revision,args=args,history=[(r['amount'],r['recipient'],r['at'].isoformat()) for r in rows],clues=clues,signals=stats['signals'])
    version=sha256(json.dumps(evidence_key,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()[:24]
    value=dict(state='attention' if stats['signals'] or clues else 'no_signals',rule_version=VERSION,version=version,
               task_id=task_id,revision=record.revision,**stats,semantic_clues=clues,choice=None,
               message='提醒仅供核实，不代表交易一定有风险；付款仍需完成原支付验证。')
    with transaction() as c:
        c.execute('INSERT INTO assistance.risk_reviews(task_id,revision,version,evidence) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(task_id,record.revision,version,Jsonb(value)))
        row=c.execute('SELECT choice FROM assistance.risk_reviews WHERE task_id=%s AND revision=%s AND version=%s',(task_id,record.revision,version)).fetchone()
    value['choice']=row['choice'];return value

def choose(p,sid,task_id,version,choice):
    current=evaluate(p,sid,task_id)
    if current.get('version')!=version:raise TaskError('risk_review_changed',status=409)
    with transaction() as c:c.execute('UPDATE assistance.risk_reviews SET choice=%s,chosen_at=now() WHERE task_id=%s AND revision=%s AND version=%s',(choice,task_id,current['revision'],version))
    return dict(choice=choice,version=version,blocking=False)
