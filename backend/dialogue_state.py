"""Server-owned query transitions. This state never contains payment authority.

Full QueryBatch values remain the wire format. Internal patches describe changes
authorized by the current utterance, independently of model defaults/annotations.
"""
from enum import Enum
import re
import time
from dataclasses import dataclass, field
from uuid import uuid4

from data_query import QueryBatch, Selection, problem


class Operation(str, Enum):
    NEW = 'NEW_QUERY'
    CONTINUE = 'CONTINUE_QUERY'
    MODIFY = 'MODIFY_QUERY'
    CLARIFY = 'CLARIFY_QUERY'
    CANCEL = 'CANCEL_QUERY'
    DIALOGUE = 'DIALOGUE'


QUERY_FIELDS = ('conversation_query','conversation_queries','conversation_question',
                'conversation_query_date','conversation_observation','conversation_state_at',
                'conversation_query_trace','conversation_legacy_requirements','conversation_legacy_semantics')


def clear(session, history=False):
    session.conversation_retired_revision = getattr(session,'conversation_active_revision',None)
    session.conversation_active_revision = None
    session.conversation_query_status = 'inactive'
    for name in QUERY_FIELDS:
        setattr(session, name, [] if name=='conversation_queries' else None)
    session.conversation_predecision = None
    session.conversation_data_requirement = None
    session.conversation_legacy_pending = None
    if history: session.conversation_turns = []
    # pending_action/challenge/current_task and write drafts belong to the kernel.


def expire(session, now=None):
    observed = getattr(session, 'conversation_observation', None) or {}
    stamps = [v for v in (getattr(session, 'conversation_state_at', None), observed.get('observed_at')) if v]
    stamp = min(stamps) if stamps else None
    if stamp and (now if now is not None else time.time())-stamp > 1800:
        clear(session, history=True)


def smalltalk(text):
    plain = re.sub(r'[，,。.!！?？\s]', '', text)
    return bool(re.fullmatch(r'(?:(?:谢谢|多谢|感谢)(?:你|您)?|好的|好|好啊|嗯|明白了|知道了|你好|您好|早上好|晚上好|'
                             r'你(?:可以|能)帮我做什么|你(?:能|会)做什么|有什么功能)', plain))


def cancelled(text):
    return bool(re.fullmatch(r'\s*(?:请|麻烦)?\s*(?:(?:取消|停止|结束)(?:刚才|当前|这次|这个|上次)?(?:的)?(?:只读)?查询(?:任务)?|不查了|不用查了)[。.!！\s]*', text))


def detail_goal(text):
    """A positive record-level target, not the mere occurrence of a negation."""
    text=re.sub(r'平均每(?:一)?笔|平均单笔','均值',text)
    return bool(re.search(r'明细|每(?:一)?笔|逐笔|每条(?:记录|消费)|全部.*(?:流水|记录)|具体买了什么|(?:前\s*\d+|最高|最低)(?:的)?\s*(?:\d+|[一二两三四五六七八九十])?\s*笔',text) and
                not re.search(r'不要明细|不看明细|不(?:用|要)每(?:一)?笔|别列|不要列',text))


def rank_goal(text):
    return bool(re.search(r'商户|商家',text) and re.search(r'排|升序|降序|从多到少|从少到多',text))


def correction_text(text):
    # Bounded correction syntax, independent of merchant vocabulary.
    text=re.sub(r'不是按[^，,。；;]+[，,]\s*(?:改按|而是按)','改按',text)
    text=re.sub(r'(?:我)?(?:想看的)?不是[^，,。；;]+[，,]\s*(?:我说的|我说|而|我想看|我要)?(?:的)?(?:是)?', '改成', text)
    return text


def relation(text, session):
    """Only explicit discourse links permit inheritance; brevity is irrelevant."""
    if smalltalk(text): return Operation.DIALOGUE
    if cancelled(text): return Operation.CANCEL
    if re.search(r'新(?:的)?查询|新任务|换个话题|重新查|另外查|不看这个了.*查', text): return Operation.NEW
    if re.search(r'下一页|后面一页|翻页|再来一页', text): return Operation.CONTINUE
    live=getattr(session,'conversation_query',None)
    if live and re.search(r'^(?:请|麻烦|劳驾)?(?:逐月|每个?月|逐日|每天)',text.strip()) and not re.search(r'\d{4}年|\d{1,2}月|本月|这个月|上月|最近|最新|上传|银行',text):return Operation.MODIFY
    if live and re.search(r'不是按[^，,]+[，,].*(?:改按|而是按)',text):return Operation.MODIFY
    if live and (detail_goal(text) or rank_goal(text)) and not re.search(r'\d{4}年|\d{1,2}月|本月|这个月|上月|最近|最新|上传|银行',text):
        return Operation.MODIFY
    correction=re.search(r'不是[^，,。；;]+[，,]\s*(?:我说的|我说|而|我想看|我要)?(?:的)?是',text)
    if live and (correction or re.search(r'^不对[，,].*(?:查|看|明细|消费|商户|记录)',text.strip())):return Operation.MODIFY
    if re.search(r'^(?:请|麻烦|劳驾)?(?:改成|改查|换成|只看|只查|只要|取消.*限制|不.*限制)', text.strip()):
        return Operation.MODIFY
    if getattr(session,'conversation_query',None) and re.search(r'^(?:请|麻烦)?(?:列|显示|按|超过|高于|低于)',text.strip()) and not re.search(r'\d{4}年|\d{1,2}月|上传|银行',text):
        return Operation.MODIFY
    if re.search(r'^(?:(?:请|麻烦|劳驾)[，,\s]*)?(?:那.+呢|这个|这笔|那笔|这一天|那天|\d{1,2}号.*以后)',text.strip()):
        return Operation.CONTINUE
    if re.search(r'呢[？?。\s]*$',text) and not re.search(r'明细|消费|购买|花|支出|收入|上传|银行',text): return Operation.CONTINUE
    pending = getattr(session,'conversation_question',None)
    if pending and not re.search(r'查|列|消费|花|支出|收入|购买|最近|最新',text):
        # A bare answer may supply missing scope; complete goals are new tasks.
        if re.search(r'月|年|银行|上传|尾号',text): return Operation.CONTINUE
    return Operation.NEW


def prepare(text, control, session):
    operation = relation(text, session)
    if control.intent in {'dialogue','help','review','business'}:
        session.conversation_operation = operation.value
        return operation
    previous = getattr(session,'conversation_query',None) or (getattr(session,'conversation_question',None) or {}).get('query')
    if operation in {Operation.CONTINUE, Operation.MODIFY}:
        if not previous and not getattr(session,'conversation_question',None):
            problem('query_context_missing','当前没有有效的查询上下文，请说明要接续的来源、时间和对象。')
        control.continuation = True
        if re.search(r'(?:那|这|上次)(?:个|些)(?:呢|怎么样)|上次那个',text) and not re.search(r'月|日|号|商户|金额|明细',text):
            problem('query_state_ambiguous','请明确要接续哪个对象或修改哪个条件，当前查询范围保持不变。')
    elif operation == Operation.NEW:
        # Model flags cannot turn a complete independent request into a follow-up.
        control.continuation = False
        control.topic_change = False
    session.conversation_operation = operation.value
    return operation


def resolve_question(text,session):
    """Only a live, revision-bound choice can continue an ambiguity draft."""
    draft=getattr(session,'conversation_question',None) or {}
    if draft.get('kind')!='merchant_rank':return text
    if draft.get('target_revision')!=getattr(session,'conversation_active_revision',None) or time.time()-draft.get('created_at',0)>1800:
        session.conversation_question=None
        return text
    choice=re.sub(r'[，,。！!？?\s]','',text)
    if re.fullmatch(r'(?:看|查|按)?(?:所有|全部)(?:的)?商[户家](?:吧)?',choice):
        request=draft['request']
        for name in draft.get('explicit_subjects',[]):request=request.replace(name,'')
        return request+'，所有商户'
    if re.fullmatch(r'(?:还是|继续)?只(?:看|查)(?:原来|之前|刚才)?(?:的)?(?:商户|那家|这家)?',choice):
        return draft['request']+'，继续只看原商户'
    return text


@dataclass
class QueryPatch:
    """Server-derived SET/CLEAR/KEEP. Never accepts model changed_fields."""
    sets: dict = field(default_factory=dict)
    clears: set = field(default_factory=set)
    keep: set = field(default_factory=set)


def reconcile(text,control,session,catalog,today):
    """Complete omissions only from a live target; non-neutral conflicts fail.

    No value is interpreted as CLEAR because it equals a Pydantic default.
    Empty/missing candidate values are proposals without deletion authority.
    Explicit contradictory values cannot acquire authority via merging.
    """
    from conversation import CHANGES, _periods, _mentioned, _same_merchant, permitted_change
    if not control.query:return
    from query_semantics import capture
    requirement=capture(text,control,session,catalog,today)
    # Restore a full requested period only for the exact fixed business cutoff.
    # SQL excludes future rows in either representation; arbitrary shorter
    # periods are never repaired. This does not authorize new selection filters.
    from datetime import timedelta
    from data_query import Period
    business=getattr(session,'conversation_business_date',today)
    for p in control.query.plans:
        for s in (p.selection,p.comparison):
            if s is None:continue
            expected_categories={v for g in requirement.goals for v in g['selection']['categories']}
            normalized=[]
            for value in s.categories:
                canonical=value[:-1] if value.endswith('类') else None
                if value not in catalog['categories'] and canonical in expected_categories and canonical in catalog['categories'] and value in text:
                    normalized.append(canonical)
                    session.conversation_metrics.setdefault('canonical_defaults',[]).append('categories: verified_class_suffix')
                else:normalized.append(value)
            s.categories=normalized
            if s.period is None:continue
            for goal in requirement.goals:
                expected=goal['selection']['period']
                if not expected:continue
                wanted=Period.model_validate(expected)
                if s.period.start==wanted.start and wanted.start<=business<wanted.end_exclusive and s.period.end_exclusive==business+timedelta(days=1):
                    s.period=wanted
                    session.conversation_metrics.setdefault('canonical_defaults',[]).append('period: fixed_business_cutoff')
    previous=getattr(session,'conversation_query',None) or (getattr(session,'conversation_question',None) or {}).get('query')
    old=QueryBatch.model_validate(previous) if previous and control.continuation else None
    if old and re.search(r'下一页|后面一页|翻页|再来一页',text):
        # Paging is a server-owned operation. Check candidate narrowing before
        # normalize_followup rebuilds the exact previous page selection.
        if len(old.plans)==len(control.query.plans)==1:
            a=old.plans[0].selection;b=control.query.plans[0].selection
            for n in Selection.model_fields:
                actual=getattr(b,n);default=Selection.model_fields[n].get_default(call_default_factory=True)
                if actual!=getattr(a,n) and actual!=default:
                    problem('query_context_mismatch','翻页时附带了新的筛选条件，尚未执行；请明确要翻页还是修改范围。')
        return
    text=correction_text(text)
    from query_semantics import display,presentation,catalog_subjects
    display_text=presentation(text,catalog)
    requested_display=display(display_text)
    names,_=catalog_subjects(text,catalog)
    all_merchants=bool(re.search(r'(?:所有|全部)(?:的)?商[户家]',text))
    ranking=rank_goal(display_text)
    details=detail_goal(display_text)
    keep_merchant=bool(re.search(r'继续只看原商户',text))
    if old and ranking and any(p.selection.merchants for p in old.plans) and not all_merchants and not names and not keep_merchant:
        if len(old.plans)!=1:problem('query_target_ambiguous','请说明要修改哪一项查询的商户排名。')
        p=old.plans[0];period=p.selection.period
        scope=(f'{period.start.month}月' if period else '原范围')
        merchant='、'.join(p.selection.merchants)
        session.conversation_question=dict(kind='merchant_rank',target_revision=getattr(session,'conversation_active_revision',None),
            created_at=time.time(),request=text,question=f'你想按{scope}所有商户的消费笔数排名，还是继续只看{merchant}？',query=None)
        problem('query_rank_scope_ambiguous',session.conversation_question['question'])
    if old and len(old.plans)!=len(control.query.plans):
        problem('query_context_mismatch','这次涉及多个查询目标，请说明要修改哪一项；尚未执行。')
    resolved=[];patches=[]
    for i,p in enumerate(control.query.plans):
        before=old.plans[i] if old else None
        patch=QueryPatch()
        if before:
            patch.keep={n for n in CHANGES if not permitted_change(n,text,catalog,control.query.plans)}
            # An amount filter or consumption word alone does not change the
            # statistic/display, even though the legacy guard recognizes it.
            if not re.search(r'几笔|笔数|多少笔|平均|均价|净额|多少钱|总额|总金额|花了多少|花多少|明细|每(?:一)?笔|逐笔|最近|最新|列',text):
                patch.keep.add('metrics')
            if requested_display.get('group_by') in {'day','month'}:
                for n in ('sort_by','order'):
                    if requested_display['group_by']==before.group_by and n not in requested_display:patch.keep.add(n)
                    elif n not in requested_display:patch.keep.discard(n)
        # Positive display goals authorize related changes as one operation.
        if details and not ranking:
            patch.sets.update(metrics=requested_display.get('metrics',['details']),sort_by=requested_display.get('sort_by','date'))
            # Scalar/ranking limits are not record-page limits. A display
            # correction starts with the server's bounded page size; an existing
            # detail page keeps its size. Never interpret a model's 100 as an
            # extra user constraint, or reject an otherwise exact correction.
            if before and 'limit' not in requested_display and not re.search(r'(?:前|Top\s*)\d+|最近.*笔|最新.*笔',text,re.I):
                patch.sets['limit']=before.limit if before and 'details' in before.metrics else QueryPlanDefault('limit')
            if before and not re.search(r'升序|降序|高到低|低到高|大到小|小到大|多到少|少到多',text):
                patch.sets['order']=before.order if before and 'details' in before.metrics else QueryPlanDefault('order')
            patch.clears.update({'group_by','comparison','offset'})
        if all_merchants:patch.clears.add('merchants')
        if ranking:
            patch.sets['group_by']='merchant'
            if re.search(r'笔数|多少笔|几笔',text):patch.sets.update(metrics=['count'],sort_by='count')
            if re.search(r'从多到少|降序|从高到低|从大到小',text):patch.sets['order']='desc'
            elif re.search(r'从少到多|升序|从低到高|从小到大',text):patch.sets['order']='asc'
            patch.clears.update({'comparison','offset'})
        if before and (details or ranking):patch.clears.add('offset')
        if before and re.search(r'取消.*金额.*限制|不.*金额.*限制',text):patch.clears.add('amounts')
        patch.keep-=set(patch.sets)|patch.clears
        value=p.model_dump(mode='json')
        provenance={}
        for n in CHANGES:
            candidate=p.selection if n in Selection.model_fields else p
            prior=(before.selection if n in Selection.model_fields else before) if before else None
            dest=value['selection'] if n in Selection.model_fields else value
            actual=getattr(candidate,n)
            default=Selection.model_fields[n].get_default(call_default_factory=True) if n in Selection.model_fields else QueryPlanDefault(n)
            if n in patch.clears:
                # CLEAR is from the utterance or mode change, never the model.
                display_clear=details and n in {'group_by','offset'}
                if not display_clear and actual!=default and (prior is None or actual!=getattr(prior,n)):
                    session.conversation_metrics['conflict_field']=n
                    problem('query_scope_mismatch',f'本次展示调整包含冲突条件，尚未执行；请明确是否还需要该筛选。')
                dest[n]=default;provenance[n]='clear'
            elif n in patch.sets:
                expected=patch.sets[n]
                # Detail conversion can correct a scalar/default metric. Other
                # conflicting metrics/order are rejected rather than relabelled.
                compatible=actual==expected or n not in candidate.model_fields_set or (prior is not None and actual==getattr(prior,n))
                if details and n in {'metrics','sort_by','limit','order'}:compatible=True
                if ranking and n=='metrics' and 'count' in expected:
                    compatible=compatible or ('count' in actual and set(actual)<={'sum','count'})
                if not compatible:problem('query_scope_mismatch','统计指标或排序与本次要求不一致，尚未执行；请明确要按金额还是笔数排序。')
                dest[n]=expected;provenance[n]='derived'
            elif n in patch.keep and prior is not None:
                wanted=getattr(prior,n)
                neutral=actual==default or actual is None or actual==[]
                if n=='metrics' and set(actual)<=set(wanted):neutral=True
                if n=='kinds' and actual==['purchase'] and wanted==[] and candidate.direction in {None,'expense'}:neutral=True
                if n=='direction' and actual=='expense' and wanted is None and candidate.kinds==['purchase']:neutral=True
                if n=='metrics' and re.search(r'有没有|没有.*(?:消费|记录)|是否',text) and 'details' in wanted and 'details' in actual and set(actual)<={'details','sum','count'}:neutral=True
                if actual!=wanted and not neutral and n in candidate.model_fields_set:
                    session.conversation_metrics['conflict_field']=n
                    problem('query_context_mismatch','本次计划夹带了未要求的筛选或展示变更，尚未执行；请明确要修改的条件。')
                dest[n]=prior.model_dump(mode='json')[n];provenance[n]='inherited'
            else:provenance[n]='explicit' if permitted_change(n,text,catalog,control.query.plans) else 'default'
        resolved.append(value);patches.append(dict(set=patch.sets,clear=sorted(patch.clears),keep=sorted(patch.keep),sources=provenance))
    control.query=QueryBatch.model_validate(dict(plans=resolved))
    session.conversation_metrics['patches']=patches
    session.conversation_metrics['merged_plan']=diagnostic_plan(control.query)


def QueryPlanDefault(name):
    from data_query import QueryPlan
    return QueryPlan.model_fields[name].get_default(call_default_factory=True)


def commit(session):
    """Called only after canonical execution/legacy verified success."""
    session.conversation_active_revision=uuid4().hex
    session.conversation_query_status='active'
    session.conversation_last_success=session.conversation_query
    session.conversation_question=None


def diagnostic_plan(value):
    """Test-local diagnostics: plans only, no rows, account numbers or factors."""
    from security.redaction import redact
    if value is None:return None
    if hasattr(value,'model_dump'):value=value.model_dump(mode='json')
    import copy
    value=copy.deepcopy(value)
    for p in value.get('plans',[]):
        for s in (p.get('selection'),p.get('comparison')):
            if s:
                for n in ('card_id','card_tail','document_id'):
                    if s.get(n) is not None:s[n]='[reference]'
    return redact(value)


def validation_date(text, session, today, continuation):
    from datetime import date
    original=getattr(session,'conversation_query_date',None) if continuation else None
    anchor=date.fromisoformat(original) if original else today
    if continuation and re.search(r'(?<!\d)\d{1,2}月',text) and not re.search(r'\d{4}[-年]|本月|上月|这个月|上个月',text):
        previous=getattr(session,'conversation_query',None) or (getattr(session,'conversation_question',None) or {}).get('query')
        years={p.selection.period.start.year for p in QueryBatch.model_validate(previous).plans if p.selection.period} if previous else set()
        if len(years)>1:problem('query_year_ambiguous','前轮涉及多个年份，请说明本次月份属于哪一年。')
        if len(years)==1:anchor=anchor.replace(year=next(iter(years)),day=1)
    return anchor


def validate_new(text, control, catalog, today):
    """Reject copied filters absent from an independent request.

    Existing literal validation checks omissions. Here we check the reverse:
    unrequested narrowing, including stale period/source/account constraints.
    """
    from conversation import _periods, permitted_change
    periods = _periods(text,today)
    selections = [s for p in control.query.plans for s in (p.selection,p.comparison) if s]
    upload = bool(re.search(r'上传|这张图|这张账单|那张账单',text))
    for s in selections:
        if s.source=='uploaded' and not upload:
            problem('query_scope_mismatch','本轮未指定上传来源，计划不能复制旧上传对象。')
        if s.period and s.period not in periods and not control.compare_recent_merchants:
            # Relative comparison scope is checked by the existing comparison gate.
            if not (any(p.comparison is s for p in control.query.plans) and re.search(r'环比|上月|比较|对比|相比',text)):
                problem('query_scope_mismatch','本轮未要求此期间，计划不能继承旧查询时间。')
        defaults = Selection(source=s.source)
        for name in ('card_id','card_tail','document_id','current_document','amounts','categories','direction','kinds','statuses','exclude_merchants'):
            if name=='kinds' and s.kinds==['purchase']: continue
            if name=='direction' and s.direction=='expense' and s.kinds==['purchase']:continue
            if getattr(s,name)!=getattr(defaults,name) and not permitted_change(name,text,catalog,control.query.plans):
                problem('query_scope_mismatch','本轮计划包含未明确要求的账户、来源或筛选条件，请说明完整范围。')


def validate_filters(text, control, session, catalog):
    """Reverse checks: an explicit lower bound never licenses any upper bound.

    Values may come from this request or an unchanged live selection. Vocabulary
    cues alone do not justify arbitrary additional constraints.
    """
    from decimal import Decimal
    from conversation import _mentioned, _same_merchant
    previous=getattr(session,'conversation_query',None) or (getattr(session,'conversation_question',None) or {}).get('query')
    old=QueryBatch.model_validate(previous).plans if previous and control.continuation else []
    literal_amounts=set()
    from query_semantics import amounts
    literal_amounts={(v['operator'],v['cents']) for v in amounts(text)}
    from query_semantics import catalog_subjects
    names,_=catalog_subjects(text,catalog)
    replacement=control.continuation and len(control.query.plans)==1 and names and not re.search(r'比较|对比|相比|排除|不含|不要|除了|除去|不算|加上|也算|合并|一起|和|及',text)
    for i,p in enumerate(control.query.plans):
        for slot,s in [('selection',p.selection),('comparison',p.comparison)]:
            if s is None:continue
            before=getattr(old[i],slot) if i<len(old) else None
            inherited=before.amounts if before else []
            for a in s.amounts:
                if (a.operator,a.cents) not in literal_amounts and a not in inherited:
                    problem('query_scope_mismatch','计划增加了未要求的金额边界，未执行。')
            for category in s.categories:
                if category not in text and not (before and category in before.categories):
                    problem('query_scope_mismatch','计划增加了未明确指定的分类筛选，未执行。')
            labels={'success':r'成功|已完成','pending':r'待处理|处理中','failed':r'失败','closed':r'关闭'}
            for status in s.statuses:
                if not re.search(labels[status],text) and not (before and status in before.statuses):
                    problem('query_scope_mismatch','计划增加或改变了未要求的记录状态筛选，未执行。')
            # A transfer source is not an account filter on the preceding query.
            card_text=re.sub(r'(?:从)?\d+\s*号(?:银行卡|卡)(?:给|向)[^，。；;]*(?:转账|转)[^，。；;]*','',text)
            card=re.search(r'(?<!\d)(\d+)\s*(?:号卡|号银行卡)',card_text)
            if card and s.card_id!=int(card[1]):
                problem('query_scope_mismatch','计划遗漏或改变了银行卡编号，未执行。')
            if s.card_id is not None and not card and not (before and s.card_id==before.card_id):
                problem('query_scope_mismatch','不能用未明确提供的银行卡编号限制查询。')
        if replacement and not p.comparison and not _same_merchant(p.selection.merchants,names,catalog):
            problem('query_scope_mismatch','替换商户时不能同时保留原商户或遗漏新商户，未执行。')
        if p.offset and not control.continuation and not re.search(r'偏移|跳过|第.+页',text):
            problem('query_scope_mismatch','独立查询不能复制旧分页位置，未执行。')


LEGACY_BILLS = {'get_monthly_spending','get_transactions_by_month','get_category_spending',
                'query_financial_records','query_uploaded_bills'}
REPORTS = {'get_monthly_cash_flow_report','get_annual_cash_flow_report','get_expense_clues'}


def legacy_query(name, args, registry):
    """Adapt only standalone bill reads; never rewrite a DAG or its conditions."""
    from datetime import date, timedelta
    from data_query import QueryPlan, Period
    if name=='query_data': return QueryBatch.model_validate(args)
    if name not in LEGACY_BILLS: return None
    model = registry.get(name).argument_model.model_validate(args)
    if name=='query_uploaded_bills':
        period=Period(start=model.start,end_exclusive=model.end_exclusive) if model.start and model.end_exclusive else None
        return QueryBatch(plans=[QueryPlan(selection=Selection(source='uploaded',document_id=model.document_id,
            merchants=[model.merchant] if model.merchant else [],period=period))])
    if name=='query_financial_records':
        s=Selection(period=Period(start=model.start,end_exclusive=model.end_exclusive),card_id=model.card_id,
                    merchants=[model.merchant] if model.merchant else [])
        if model.metric=='cash_outflow':
            s.direction='expense';s.kinds=['purchase','transfer','wealth_buy']
        comparison=None
        if model.compare_previous:
            start=s.period.start
            last=start-timedelta(days=1)
            comparison=s.model_copy(deep=True)
            comparison.period=Period(start=last.replace(day=1),end_exclusive=start)
        return QueryBatch(plans=[QueryPlan(selection=s,comparison=comparison)])
    start=date.fromisoformat(model.month+'-01')
    period=Period(start=start,end_exclusive=(start.replace(day=28)+timedelta(days=4)).replace(day=1))
    q=QueryPlan(selection=Selection(period=period,card_id=model.card_id))
    if name=='get_transactions_by_month':q.metrics=['details'];q.sort_by='date'
    if name=='get_category_spending':q.group_by='category'
    return QueryBatch(plans=[q])


def trace(text, control, session, operation, catalog):
    """Field provenance for diagnostics, never a self-reported model proof."""
    from conversation import CHANGES, permitted_change
    previous = getattr(session,'conversation_query',None) or (getattr(session,'conversation_question',None) or {}).get('query')
    old = QueryBatch.model_validate(previous).plans if previous and control.continuation else []
    fields = []
    for i, plan in enumerate(control.query.plans):
        values = {}
        for name in CHANGES:
            target = plan.selection if name in Selection.model_fields else plan
            prior = (old[i].selection if name in Selection.model_fields else old[i]) if i<len(old) else None
            if permitted_change(name,text,catalog,control.query.plans): source='explicit'
            elif prior is not None and getattr(target,name)==getattr(prior,name): source='inherited'
            else: source='default'
            values[name]=source
        fields.append(values)
    return dict(operation=operation.value,fields=fields,validated_plan=control.query.model_dump(mode='json'))


def touch(session):
    session.conversation_state_at = time.time()
