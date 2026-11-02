"""Typed LLM tools map to the R1 gateway; no business implementation here."""
from datetime import datetime, timezone
import json
from typing import Literal
from pydantic import Field, create_model
from agent.schemas import StrictModel, TransferMoneyArguments
from data_query import QueryPlan, Selection
from foundation.drafts import TransferDraft, GoalStore, merge
from foundation.contracts import Call, feedback
from foundation.gateway import run
from lifecycle.store import TaskStore, TaskError
from .projection import bounded_result, project


def partial(model,name):
    return create_model(name,__base__=StrictModel,**{key:(f.rebuild_annotation(),Field(default=None,description=f.description)) for key,f in model.model_fields.items()})


SelectionPatch=partial(Selection,'SelectionPatch')
QueryPatch=create_model('QueryPatch',__base__=partial(QueryPlan,'QueryOptional'),selection=(SelectionPatch|None,None),comparison=(SelectionPatch|None,None))
class TransferChanges(partial(TransferMoneyArguments,'TransferOptional')):
    # The legacy amount contract is reused, but JSON booleans are never card
    # or candidate IDs in the new model boundary (Pydantic otherwise coerces).
    from_card_id:int|None=Field(None,gt=0,strict=True,description=TransferMoneyArguments.model_fields['from_card_id'].description)
    recipient_id:int|None=Field(None,gt=0,strict=True,description=TransferMoneyArguments.model_fields['recipient_id'].description)

class Empty(StrictModel): pass
class Ref(StrictModel): reference: str=Field(min_length=1,max_length=100)
class Version(Ref): revision:int=Field(ge=1,strict=True)
class TransferCreate(StrictModel): fields:TransferDraft
class TransferPatch(Version): fields:TransferDraft
class QueryCreate(StrictModel): fields:QueryPlan
class QueryUpdate(Version): fields:QueryPatch
class TaskModify(Version):
    step_id:str=Field(min_length=1,max_length=64)
    changes:dict=Field(min_length=1,description='仅修改目标步骤原参数模型中的字段；金额元、ID整数。先读取真实步骤及当前版本。')
class Focus(StrictModel): reference:str|None=Field(None,max_length=100)
class ListArgs(StrictModel): limit:int=Field(8,ge=1,le=30,strict=True)
class DraftListArgs(ListArgs):offset:int=Field(0,ge=0,le=10000,strict=True)
class ResultRead(Ref):
    offset:int=Field(0,ge=0,le=100,strict=True)
    limit:int=Field(10,ge=1,le=20,strict=True)
class CallRead(StrictModel):
    turn_id:str=Field(pattern=r'^dt_[a-f0-9]{32}$')
    call_id:str=Field(min_length=1,max_length=200)
    offset:int=Field(0,ge=0,le=10000,strict=True)
    limit:int=Field(10,ge=1,le=30,strict=True)

class SourceRead(StrictModel):
    turn_id:str|None=Field(None,min_length=1,max_length=100)
    after_number:int=Field(0,ge=0,strict=True)
    offset:int=Field(0,ge=0,le=16000,strict=True)

class CalendarRead(StrictModel):
    date:str|None=Field(None,pattern=r'^\d{4}-\d{2}-\d{2}$',description='可选的明确日期；省略使用服务器今天。')
    month_offset:int=Field(0,ge=-120,le=120,strict=True,description='相对锚点的自然月偏移；由模型理解原话。')
    day:int|None=Field(None,ge=1,le=31,strict=True,description='可选的目标月份日号，不自动把不存在的日期向月末截断。')
    offset_days:int=Field(0,ge=-366,le=366,strict=True,description='在目标日期上加减天数，用于提前一周等日历计算。')

DESCRIPTIONS={
 'calendar_read':(CalendarRead,'读取Asia/Shanghai服务器今天、计算明确目标日期及星期。由你理解相对月/日和提前天数，日期工具负责运算；不保存资料、不创建提醒或保证通知。'),
 'cards_balance':(Empty,'只读查询本人各卡余额、每日配置限额、当日已完成转账已用额/日限额剩余额、单笔限制（均为元）及业务日期/查询时间。问限额不能只答余额；日限额剩余不保证可转，仍受余额/单笔/卡状态和支付检查限制。多卡分别展示，不擅自加总。'),
 'recipient_candidates':(Empty,'读取本人可用收款人及候选ID。同名必须用户明确选择，列出候选不等于选择。'),
 'query_create':(QueryCreate,'创建银行账单查询草稿。selection为筛选；period.start含当日/end_exclusive不含当日。merchants用目录名；金额cents单位分。普通消费用kinds=[purchase]，净支出用metrics=[net]且不设置kinds/direction。最近交易用time_mode=latest、metrics=[details]、sort_by=date、limit=1，不限定类型时kinds留空，内核保留包括未分类的全部类型；指定月份必须保留period，不能去掉条件。最近N笔用recent_records；上一笔可在同期间latest取前两笔并按真实事件时间解释第二条，日内精度不明必须说明。period模式不支持unknown类型金额统计，不可静默丢弃条件。只支持bank。'),
 'query_patch':(QueryUpdate,'修改查询草稿，省略保留，null清除；继承明确相关条件，改时间/商户不清空其他条件。'),
 'query_execute':(Version,'执行完整查询草稿与版本；统计由可信工具计算，无需用户额外确认，返回历史结果引用和口径。'),
 'result_read':(ResultRead,'按引用读取已保存查询结果；明细/分组分页，offset从0起；不会查询实时余额。'),
 'human_service_request':(Empty,'用户明确要求转人工时申请客服并暂停AI新办理。按state说明等待/无人在线/接入，不猜测接通。保留已提交任务核实原结果。'),
 'transfer_create':(TransferCreate,'保存用户明确提出的转账目标。购物愿望或预算讨论先澄清分析还是付款，不据此建立转账补参。amount字符串单位元正数最多2位小数，recipient_label是用户称呼，recipient_id是候选明确选定ID。创建和保存不会转账。'),
 'transfer_patch':(TransferPatch,'修改尚未关联任务的转账草稿；省略保留，null清空。称呼或ID换绑清除旧选择。linked草稿必须使用task_modify修改真实任务。'),
 'draft_read':(Ref,'读取草稿字段、版本、缺项、候选、任务关联。'),
 'draft_validate':(Ref,'校验已保存草稿，不执行资金操作。'),
 'draft_close':(Version,'明确放弃时关闭未关联草稿。先看看/先放着可以仅保留，不必关闭或永久取消。'),
 'draft_list':(DraftListArgs,'按归属列出有界query/transfer草稿，offset从0起分页，用于找回快照外目标，不会改变关注点。'),
 'task_prepare':(Version,'从完整转账草稿准备任务并预览；本会话最多一个活跃任务，不自动取消原任务。仅独立UI确认/TOTP能够执行。'),
 'task_recover':(Version,'核实preparing/linked草稿预留的原任务UUID，不重提未知请求。'),
 'task_read':(Ref,'读取转账任务实时状态与版本，不生成授权。'),
 'task_verify':(Ref,'核实转账任务状态，不重提交未知资金操作。'),
 'task_list':(ListArgs,'读取有限本人转账任务。'),
 'task_modify':(TaskModify,'修改明确目标的已准备转账任务step_id；changes沿用转账Schema（amount元，from_card_id，recipient_id或recipient_name）。旧确认失效，需要重新预览；已完成任务不能修改。'),
 'task_pause':(Version,'暂停明确转账任务，保留目标。'),
 'task_cancel':(Version,'用户明确取消时取消转账任务；不撤销已完成交易。'),
 'task_resume':(Version,'恢复明确任务并按内核重新预览；不代表确认执行。'),
 'context_snapshot':(ListArgs,'重读当前关注点、未完成目标、草稿与任务权威状态；聊天与历史结果不能覆盖。'),
 'context_focus':(Focus,'选择明确目标为关注点，null清空；不会授权、取消其他目标或锁定下一条消息。'),
}
OPS={k:k.replace('_','.',1) for k in DESCRIPTIONS if k.startswith(('task_','draft_','context_'))}
OPS.update(transfer_create='draft.create',transfer_patch='draft.patch',query_create='draft.create',query_patch='draft.patch',query_execute='query.execute')

# Reuse each registered argument model and execution path. No synonym tools.
from .business import READS,WRITES,ALLOWED,ACTIONS
from lifecycle.service import registry
from agent.schemas import TaskPlanProposal
from lifecycle.models import ScheduleRequest,NoteRequest,RollbackRequest
for _name in sorted(READS|WRITES):
    _spec=registry().get(_name)
    DESCRIPTIONS[_name]=(partial(_spec.argument_model,_name+'Inputs') if _name in WRITES else _spec.argument_model,
        _spec.description+(' 仅准备原任务预览；缺项不保存新草稿，返回后由模型继续收集。执行必须原安全界面确认。' if _name in WRITES else ' 只读当前数据库，不必建草稿。')+
        (' Workflow条件字段：'+ '; '.join(path+' ['+field.value_type+','+field.unit+','+'/'.join(field.operators)+']' for path,field in _spec.condition_fields.items()) if _spec.condition_fields else ''))
DESCRIPTIONS['create_task_plan']=(TaskPlanProposal,'提交结构化持久Workflow/DAG，最多8步3写。仅用于持久依赖、条件或生命周期；独立查询直接调用，不强制建任务。写步骤逐个等待原安全确认，上游未成功不得继续。Workflow余额步骤名get_cards参数{}，收款人get_transfer_recipients参数{}，转账transfer_money参数from_card_id/recipient_id或recipient_name/amount（元）；其他名称参数同目录。条件只用目录声明的field_path，阈值单位必须一致。')
for _query_name in ['query_create','query_data']:
    _query_schema,_query_description=DESCRIPTIONS[_query_name]
    DESCRIPTIONS[_query_name]=(_query_schema,_query_description+' 最近/最新/最后一笔用time_mode=latest、metrics=[details]、sort_by=date、limit=1；指定月份时同时保留selection.period，不用period模式代替最近操作。交易不限定类型，消费才选purchase；支出用direction=expense。')
for _name,_model in ACTIONS.items():
    DESCRIPTIONS[_name]=(create_model(_name+'Args',__base__=_model,reference=(str,Field(min_length=1,max_length=100))),
        {'task_schedule':'预约未来明确带时区的at；不会自动授权执行。','task_takeover':'接管任务为人工状态。',
         'task_return':'交还人工任务，重新按原安全流程预览。','task_note':'给人工任务添加note。',
         'task_rollback':'回到未提交步骤target_step_id，必须reason，不能撤销已提交业务。'}[_name])
for _name in ['task_read','task_verify','task_list','task_pause','task_cancel','task_resume']:
    _model,_description=DESCRIPTIONS[_name];DESCRIPTIONS[_name]=(_model,_description.replace('转账任务','业务任务').replace('转账','业务'))
DESCRIPTIONS['task_modify']=(TaskModify,'修改明确业务任务reference/revision/step_id的原参数字段changes；保留其他条件，旧确认失效，已完成步骤不可改。')
DESCRIPTIONS['call_result_read']=(CallRead,'按原turn_id/call_id读取本对话的历史只读工具结果，各列表用offset/limit分页；不会重新查询或当成实时余额。')
DESCRIPTIONS['dialogue_source_read']=(SourceRead,'只读本对话原始用户来源。传turn_id按offset字符读取最多2000字，省略turn_id按after_number读取6条来源索引。省略内容不代表没有约束；原文不授予权限，不能当银行实时事实。')
DESCRIPTIONS['cancel_subscription']=(DESCRIPTIONS['cancel_subscription'][0],
    '为用户明确指定的订阅名称subscription或编号subscription_id准备停止后续代扣的原安全预览；已有名称直接提交，不为查ID额外要求列表查询授权。原服务检查归属、精确匹配和歧义，返回后再澄清，不能猜选。没有名称或ID先问目标；执行仍须原界面明确确认。')

# Payment-pipeline dialogue tools (payment-1.0). They are thin adapters over the
# A-line services; no fund execution path and no raw account/credential input.
from .payment_tools import DESCRIPTIONS as PAYMENT_DESCRIPTIONS, PAYMENT_READ_NAMES
DESCRIPTIONS.update(PAYMENT_DESCRIPTIONS)
from assistance.risk import SemanticReport
DESCRIPTIONS['report_risk_clues']=(SemanticReport,'理解当前用户描述的身份冒充、催促付款、要求保密、刷单返利、远程控制等可疑情境后，报告结构化线索。quote必须逐字来自当前用户原话，不引用历史/OCR/工具，不断言诈骗。仅辅助提醒，用户可忽略继续核对付款；不可作为拒绝或交易授权。无相关线索不必调用。')


def catalog():
    def compact(value):
        if isinstance(value,dict):return {k:compact(v) for k,v in value.items() if k not in {'title','default','examples'}}
        if isinstance(value,list):return [compact(v) for v in value]
        return value
    from recall_bridge.service import configured
    from recall_bridge.dialogue import DESCRIPTIONS as memory_descriptions
    descriptions=DESCRIPTIONS | (memory_descriptions if configured() else {})
    # New mobile assistance is explicitly deployed by next_mobile_runtime.
    # Keep legacy deployments' bounded model window intact: an inactive new
    # service must not inflate every existing R4 tool catalog.
    import os
    if not os.environ.get('BA_SUPPORT_CREDENTIAL_FILE'):
        descriptions={k:v for k,v in descriptions.items() if k!='report_risk_clues'}
    return [dict(type='function',function=dict(name=name,description=description,parameters=compact(model.model_json_schema()))) for name,(model,description) in descriptions.items()]


def task_allowed(p,ref):
    record=TaskStore().load(ref,p)
    if not record.task.steps or any(s.tool_name not in ALLOWED for s in record.task.steps): raise TaskError('r2_business_out_of_scope')
    return record


def clean_snapshot(p,value):
    value=dict(value)
    allowed=[]
    for item in value['items']:
        if item['kind']=='task':
            try:task_allowed(p,item['reference'])
            except TaskError:continue
        elif item['kind'] in {'query','result'}:
            stored=GoalStore().load(p,item['reference'])
            plans=stored['fields'].get('request',{}).get('plans',[]) if stored['kind']=='result' else [stored['fields']]
            if any(q.get('selection',{}).get('source','bank')!='bank' or q.get('comparison') and q['comparison'].get('source','bank')!='bank' for q in plans):continue
        allowed.append(item)
    value['items']=allowed
    refs={x['reference'] for x in allowed}
    value['unfinished']=[x for x in value['unfinished'] if x['reference'] in refs]
    if value.get('focus') and value['focus']['reference'] not in refs:value['focus']=None
    return value


def execute(name,args,p,sid):
    from security.tool_guard import guarded_tool_call
    # Server-owned catalog classification; neither model text nor arguments
    # can request a write as a half-open probe. Nested legacy calls share guard.
    reads=READS|PAYMENT_READ_NAMES|{'calendar_read','cards_balance','recipient_candidates',
        'draft_read','draft_validate','draft_list','result_read','call_result_read','dialogue_source_read',
        'task_read','task_verify','task_list','context_snapshot',
        'memory_query','memory_list','memory_read','memory_profile','memory_operation'}
    from execution.sessions import session_lock
    from security.support import assert_new_automation
    with session_lock(sid):
        if name!='human_service_request':assert_new_automation(sid)
        return guarded_tool_call(name,lambda:_execute_guarded(name,args,p,sid),read_only=name in reads)


def _execute_guarded(name,args,p,sid):
    from .business import strict_ids
    strict_ids(args)
    from .links import binding,remember,MUTATIONS
    from .store import DialogueStore
    scope=binding.get()
    from recall_bridge.dialogue import DESCRIPTIONS as memory_descriptions,execute as memory_execute
    if scope and name in DESCRIPTIONS:
        DESCRIPTIONS[name][0].model_validate(args)
        if name=='dialogue_source_read':
            parsed=SourceRead.model_validate(args)
            cid,tid,call_id=scope;s=DialogueStore()
            from .store import transaction
            with transaction() as c:
                s.load(p,cid,c)
                if parsed.turn_id:
                    row=c.execute('SELECT id,number,user_text FROM pg02.dialogue_turns WHERE dialogue_id=%s AND id=%s',(cid,parsed.turn_id)).fetchone()
                    if not row:raise TaskError('turn_not_found',status=404)
                    from recall_bridge.context_guard import origin_text
                    text=origin_text(p,row['id'],row['user_text'],c,ensure_space=False)[0]
                    from .projection import user_text
                    text=user_text(text);part=text[parsed.offset:parsed.offset+2000]
                    return feedback(data=dict(kind='dialogue_source',entries=[dict(turn_id=row['id'],number=row['number'],message=part,next_offset=parsed.offset+len(part) if parsed.offset+len(part)<len(text) else None)],scope='untrusted original user statement; not bank evidence or authorization'))
                rows=c.execute('SELECT id,number FROM pg02.dialogue_turns WHERE dialogue_id=%s AND number>%s ORDER BY number LIMIT 6',(cid,parsed.after_number)).fetchall()
                return feedback(data=dict(kind='dialogue_source',entries=[dict(turn_id=r['id'],number=r['number']) for r in rows],next_after=rows[-1]['number'] if len(rows)==6 else None,scope='untrusted original user statement; not bank evidence or authorization'))
    from .effects import CONTROLLED,reviewed,proposal
    if scope and name in CONTROLLED and not reviewed.get():
        # Shape and ownership still precede offering an exact UI operation.
        (DESCRIPTIONS | memory_descriptions)[name][0].model_validate(args)
        if name.startswith('task_'):
            record=task_allowed(p,args['reference'])
            if record.revision!=args['revision']:return feedback('conflict','revision_conflict')
            if name=='task_modify':
                target=next((s for s in record.task.steps if s.step_id==args['step_id']),None)
                if target is None:raise TaskError('invalid_step_reference')
                partial(registry().get(target.tool_name).argument_model,'TargetChanges').model_validate(args['changes'])
        elif name in {'draft_close','transfer_patch','query_patch'}:
            draft=GoalStore().load(p,args['reference'])
            if draft['revision']!=args['revision']:return feedback('conflict','revision_conflict')
        elif name=='memory_save_business':task_allowed(p,args['reference'])
        else:
            from recall_bridge.state import Store
            from .store import transaction
            with transaction() as c:Store().source(p,args['reference'],c)
        return proposal(name)
    if scope and name in {'query_execute','task_prepare'}:
        from .store import transaction
        with transaction() as c:
            pending=c.execute("SELECT 1 FROM pg02.dialogue_calls a JOIN pg02.dialogue_turns t ON t.id=a.turn_id WHERE t.dialogue_id=%s AND a.name IN ('transfer_patch','task_modify') AND a.arguments->>'reference'=%s AND a.result->>'code'='explicit_action_required' AND a.resolution IS NULL LIMIT 1",(scope[0],args.get('reference'))).fetchone()
        if pending:return feedback('conflict','operation_review_required',next_actions=['review_exact_operation_in_ui'])
    if name in memory_descriptions:
        if not scope:raise TaskError('memory_trusted_turn_required',status=403)
        cid,tid,call_id=scope
        DialogueStore().call_read(p,cid,tid,call_id)
        return memory_execute(name,args,p,tid)
    if scope:
        cid,tid,call_id=scope;s=DialogueStore()
        if name not in DESCRIPTIONS:return feedback('unsupported','unknown_tool')
        DESCRIPTIONS[name][0].model_validate(args)
        if name=='call_result_read':
            parsed=CallRead.model_validate(args);row=s.call_read(p,cid,parsed.turn_id,parsed.call_id)
            if row['name'] not in READS|{'cards_balance','recipient_candidates'}|PAYMENT_READ_NAMES:raise TaskError('invalid_reference_kind')
            from .read_results import effective
            value=effective(row,p,cid,parsed.turn_id)['result']
            pagination=[]
            def page(v,path='result',rows=False):
                if isinstance(v,list):
                    chosen=v[parsed.offset:parsed.offset+parsed.limit] if rows else v
                    if rows:pagination.append(dict(field=path,recorded_count=len(v),count=len(chosen),truncated=parsed.offset+len(chosen)<len(v)))
                    return [page(x,path+'[]') for x in chosen]
                if isinstance(v,dict):return {k:page(x,path+'.'+k,k in {'data','details','groups','evidence','clues','products','subscriptions'}) for k,x in v.items()}
                return v
            return feedback(data=dict(result=page(value),source_tool=row['name'],historical=True,offset=parsed.offset,limit=parsed.limit,
                pagination=pagination,scope='row lists independently paginated; original request and aggregates preserved'))
        unknown=s.unresolved(p,cid)
        for old in unknown:
            if old['turn_id']==tid and old['call_id']==call_id:continue
            if name in MUTATIONS and old['operation'] in MUTATIONS:
                ref=old['arguments'].get('reference')
                related={ref}
                if ref and ref.startswith('tg_'):
                    saved=GoalStore().load(p,ref)
                    related.add(saved['fields'].get('task_id'))
                    related.add(saved['fields'].get('_preparation',{}).get('task_id'))
                if not ref or args.get('reference') in related:
                    return feedback('conflict','unresolved_original_call',data={'reference':ref},next_actions=old['verification'])
        if name in {'context_snapshot','context_focus'}:
            if name=='context_focus':
                ref=args.get('reference')
                if ref:
                    # Explicit reference still passes the same R2 ownership/scope checks.
                    _execute('draft_read' if ref.startswith('tg_') else 'task_read',{'reference':ref},p,sid)
                    s.associate(p,cid,ref,tid,call_id)
                s.focus(p,cid,ref)
            from .state import snapshot
            return feedback(data=snapshot(p,cid))
    result=_execute(name,args,p,sid)
    if scope:
        try:remember(p,cid,tid,call_id,name,args,result)
        except Exception:
            # Preserve the actual business return when only provenance failed.
            result=dict(result,context_association_status='unavailable')
        if name=='task_recover':s.resolve_preparation(p,cid,args['reference'],result,tid,call_id)
        if name in {'draft_read','result_read'} and result.get('status') in {'completed','saved_incomplete','ambiguous','invalid_parameters'}:
            s.resolve_created_reference(p,cid,args['reference'],tid,call_id)
        if name in {'task_read','task_verify'} and result.get('status')=='completed':
            s.resolve_task_preparation(p,cid,args['reference'],tid,call_id)
    return result


def _execute(name,args,p,sid):
    if name not in DESCRIPTIONS:return feedback('unsupported','unknown_tool')
    model=DESCRIPTIONS[name][0]
    parsed=model.model_validate(args)
    values=parsed.model_dump(mode='json',exclude_unset=True)
    if name=='calendar_read':
        from datetime import date,timedelta
        from security.config import BUSINESS_TIMEZONE
        from security.service import get_security_services
        now=get_security_services().now_provider().astimezone(BUSINESS_TIMEZONE)
        try:
            anchor=date.fromisoformat(parsed.date) if parsed.date else now.date()
            ordinal=anchor.year*12+anchor.month-1+parsed.month_offset
            target=date(ordinal//12,ordinal%12+1,parsed.day or anchor.day)+timedelta(days=parsed.offset_days)
        except (ValueError,OverflowError):raise TaskError('invalid_calendar_date') from None
        return feedback(data=dict(kind='server_calendar',today=now.date().isoformat(),today_weekday='星期'+'一二三四五六日'[now.weekday()],date=target.isoformat(),weekday='星期'+'一二三四五六日'[target.weekday()],timezone='Asia/Shanghai',as_of=now.isoformat(),reminder_created=False,notification_deliverable=False))
    if name=='human_service_request':
        from security.support import request
        return feedback(data=request(sid),effects=['new_automation_paused'],next_actions=['打开人工客服页面查看实际连接状态','核查已有任务与回执'])
    if name=='report_risk_clues':
        from assistance.risk import report
        return feedback(data=report(values,p,sid))
    if name in {'transfer_create','task_prepare','create_task_plan','report_card_lost','set_daily_limit','cancel_subscription','buy_wealth_product','redeem_wealth_product'}:
        from security.support import assert_new_automation
        assert_new_automation(sid)
    if name in PAYMENT_DESCRIPTIONS:
        from .payment_tools import execute as payment_execute
        return payment_execute(name,values,p,sid)
    if name in {'cards_balance','recipient_candidates'}:
        response=run(Call(operation='candidates'),p,sid)
        if response['status']=='completed':
            key='cards' if name=='cards_balance' else 'recipients'
            if name=='cards_balance':
                from .card_limits import enrich
                response['data'][key]=enrich(response['data'][key])
            response['data']={key:response['data'][key], 'queried_at':datetime.now(timezone.utc).isoformat(),'historical':True}
        return bounded_result(response)
    if name in READS|WRITES|set(ACTIONS)|{'create_task_plan'}:
        from .business import execute as business_execute
        return business_execute(name,values,p,sid)
    if name=='draft_list':
        from .store import transaction
        with transaction() as c:
            rows=c.execute("SELECT reference FROM pg02.tool_goals WHERE owner_id=%s AND principal_id=%s AND kind IN ('query','transfer') ORDER BY reference LIMIT %s OFFSET %s",(p.user_id,p.principal_id,parsed.limit+1,parsed.offset)).fetchall()
        items=[]
        for r in rows[:parsed.limit]:
            try:items.append(_execute('draft_read',{'reference':r['reference']},p,sid)['data'])
            except TaskError:continue
        return bounded_result(feedback(data={'items':items,'truncated':len(rows)>parsed.limit,'next_offset':parsed.offset+parsed.limit if len(rows)>parsed.limit else None}))
    if name=='task_list':
        records=[r for r in TaskStore().list(p,limit=30) if r.task.steps and all(s.tool_name in ALLOWED for s in r.task.steps)][:parsed.limit]
        from lifecycle.service import public
        return bounded_result(feedback(data=[public(r) for r in records]))
    ref=values.get('reference')
    if name.startswith('task_') and name not in {'task_prepare','task_recover'}:task_allowed(p,ref)
    if name in {'context_focus','draft_read','draft_validate','draft_close','transfer_patch','query_patch','query_execute','task_prepare','task_recover'} and ref:
        if ref.startswith('tg_'):
            draft=GoalStore().load(p,ref)
            if draft['kind'] not in {'transfer','query'}:raise TaskError('r2_business_out_of_scope')
            if draft['kind']=='query' and (draft['fields']['selection'].get('source','bank')!='bank' or draft['fields'].get('comparison') and draft['fields']['comparison'].get('source','bank')!='bank'):raise TaskError('r2_business_out_of_scope')
            if name.startswith('transfer_') and draft['kind']!='transfer' or name.startswith('query_') and draft['kind']!='query':raise TaskError('invalid_reference_kind')
            if draft['fields'].get('task_id'):task_allowed(p,draft['fields']['task_id'])
        elif name=='context_focus':task_allowed(p,ref)
    if name=='result_read':
        value=GoalStore().load(p,ref)
        if value['kind']!='result':raise TaskError('invalid_reference_kind')
        f=value['fields']
        if any(q['selection']['source']!='bank' for q in f['request']['plans']):raise TaskError('r2_business_out_of_scope')
        from copy import deepcopy
        value=deepcopy(value)
        for entry in value['fields']['result'].get('results',[]):
            for key in ['details','groups']:
                rows=entry['current'][key]; entry['current'][key]=rows[parsed.offset:parsed.offset+parsed.limit]
                if len(rows)>parsed.offset+parsed.limit:entry['current']['truncated']=True
        from foundation.drafts import public
        return bounded_result(feedback(data=public(value)))
    if name.startswith('query_'):
        fields=values.get('fields',{})
        effective=merge(draft['fields'],fields) if name=='query_patch' else draft['fields'] if name=='query_execute' else fields
        q=QueryPlan.model_validate(effective)
        if q.selection.source!='bank' or q.comparison and q.comparison.source!='bank':raise TaskError('r2_business_out_of_scope')
    if name=='task_modify':
        target=next((s for s in task_allowed(p,ref).task.steps if s.step_id==values['step_id']),None)
        if target is None:raise TaskError('invalid_step_reference')
        partial(registry().get(target.tool_name).argument_model,'TargetChanges').model_validate(values['changes'])
        values['fields']={'step_id':values.pop('step_id'),'changes':values.pop('changes')}
        if not values['fields']['changes']:raise TaskError('invalid_empty_changes')
    if name in {'transfer_create','query_create'}:values['kind']='transfer' if name=='transfer_create' else 'query'
    response=run(Call(operation=OPS[name],**values),p,sid)
    if name=='context_snapshot' and response['status']=='completed':response['data']=clean_snapshot(p,response['data'])
    return bounded_result(response)
