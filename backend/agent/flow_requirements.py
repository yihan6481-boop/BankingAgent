"""NL_FLOW_01: small independent goal inventory over existing business tools.

This is a bounded grammar, not an authorization mechanism. Unknown clauses fail
closed. Bill clauses still go through NL_CORE_02 and the canonical query guard.
No model-provided span, contract, or completeness assertion supplies this inventory.
"""
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation

MONEY = r'[0-9]+(?:\.[0-9]{1,2})?'
PREFIX = re.compile(r'^(?:新任务|开始新任务|换个话题|请|麻烦|帮我|帮忙|先|再|然后|之后|最后|接着|完成后|买完|买完后|转完后|告诉我|一下|等等|就|准备|如果|若|，|\s)+')
READ_WORD = r'(?:查询|查看|查一下|查查|看看|查|看|统计)'
OPS = {'少于':'lt','低于':'lt','小于':'lt','不超过':'lte','至多':'lte',
       '超过':'gt','大于':'gt','高于':'gt','不少于':'gte','至少':'gte','达到':'gte','等于':'eq','不等于':'ne'}
SPLIT = re.compile(r'[，,。；;]|(?:然后|之后|最后|接着|完成后|买完后|买完|转完后)(?=.)|再(?=查|看)')
WRITES = {'transfer_money','buy_wealth_product','redeem_wealth_product'}
BILLS = {'query_data','query_financial_records','get_monthly_spending','get_category_spending',
         'get_transactions_by_month','get_monthly_cash_flow_report','get_expense_clues'}


def extract(text, bound_date, previous=None):
    """Return None for legacy single goals; never silently drop an unknown phase."""
    probe=re.sub(r'备注(?:为|是|：|:)?[“"「].*?[”"」]','',text)
    parts = [PREFIX.sub('', p.strip()).strip('？！!? ') for p in SPLIT.split(probe) if p.strip()]
    parts = [p for p in parts if p]
    merged=[]
    for part in parts:
        if merged and re.fullmatch(r'(?:与|和|跟).*(?:月|年).*(?:比较|对比)',part):
            merged[-1]+='，'+part
        else:merged.append(part)
    parts=merged
    active_parts=[p for p in parts if not re.fullmatch(r'(?:本轮|也)?不(?:需要|用)?(?:购买理财|查询产品|查询持仓)',p)]
    # Existing single-tool/query protocols retain their own stricter grammars.
    if len(active_parts) < 2 or not re.search(r'转账|转给|转\d|(?:给|向).+转|(?:购买|买入|申购)(?:理财)?产品|赎回', probe):
        if previous and not previous.get('error'):
            # Only an entirely consumed slot answer can continue an old draft.
            # A new business goal never inherits the old draft's parameters.
            m=re.fullmatch(r'(?:请|那就|就)?(?:从|用)([1-9][0-9]*)号卡(?:付款|扣款|转账|转|支付)?[。！!]?',text.strip())
            amount=re.fullmatch(r'(?:金额(?:是|为)?|转)?('+MONEY+r')元[。！!]?',text.strip())
            recipient=re.fullmatch(r'(?:收款人(?:是|为)?|转给|给)([^\s\d，。；,;]{1,20})[。！!]?',text.strip())
            order=re.fullmatch(r'(?:请)?(先查询(?:理财产品)?(?:再|然后)(?:准备)?转账|按原(?:条件|顺序)继续|按顺序(?:执行|办理)?|先查(?:询)?再转(?:账)?|互不依赖(?:的两项操作)?|分别(?:执行|办理))[。！!]?',text.strip())
            if order:
                updated=deepcopy(previous)
                updated['ordering']='independent' if re.search('互不依赖|分别',order[1]) else 'explicit'
                # An explicit conditional/post-write requirement cannot be discarded.
                if updated['ordering']=='independent' and any(g.get('condition') for g in updated['goals']):
                    return dict(updated,error='条件任务不能改为互不依赖，请确认保留原条件顺序。')
                return updated
            if m or amount or recipient:
                updated=deepcopy(previous)
                pending=[g for g in updated['goals'] if g['kind'] in WRITES and
                    g.get('args',{}).get('card_id' if g['kind']=='buy_wealth_product' else 'from_card_id') is None] if m else [
                    g for g in updated['goals'] if g['kind'] in WRITES and g['args'].get('amount') is None] if amount else [
                    g for g in updated['goals'] if g['kind']=='transfer_money' and g['args'].get('recipient_name') is None]
                if len(pending)==1:
                    key=('card_id' if pending[0]['kind']=='buy_wealth_product' else 'from_card_id') if m else 'amount' if amount else 'recipient_name'
                    pending[0]['args'][key]=int(m[1]) if m else amount[1] if amount else recipient[1]
                    return updated
        return None
    goals = []
    pending = None
    error = None
    for raw in parts:
        p = raw
        cm = re.search('('+'|'.join(sorted(OPS,key=len,reverse=True))+')('+MONEY+r')(?:元)?(?:的话|时)?', p)
        condition = None
        if cm:
            before, after = p[:cm.start()], p[cm.end():]
            if not after or re.match(r'^(?:就|则|准备|从|用|转|购买)', after):
                condition = dict(operator=OPS[cm[1]], value=cm[2])
                p = before or ''
                if not p and goals:
                    condition['source'] = len(goals)-1
                elif p and re.search(r'支出|消费|现金流|收入',p):
                    goals.append(dict(kind='bill', request=p))
                    condition['source'] = len(goals)-1
                else:
                    error = '当前条件只支持已声明的月度收支字段；请明确查询指标和条件来源。'
                pending = condition
                p = PREFIX.sub('', after)
                if not p:
                    continue
        # "够的话" means an explicit request for the existing transfer precheck,
        # never a synthesized condition or an authorization from an old balance.
        precheck = bool(re.match(r'^(?:够(?:转'+MONEY+r'元)?|余额足够)(?:的话)?',p))
        if precheck:
            p = re.sub(r'^(?:够(?:转'+MONEY+r'元)?|余额足够)(?:的话)?', '', p)
            p = PREFIX.sub('',p)
        g = None
        if re.search(r'转(?:账|给|'+MONEY+r')',p):
            card = re.search(r'(?:从|用)([1-9][0-9]*)号卡',p)
            amount = re.search('('+MONEY+r')元',p)
            if precheck and not amount:
                amount = re.search('('+MONEY+r')元',raw)
            who = re.search(r'(?:给|向)([^\s\d，。；,;]{1,20}?)(?=转|'+MONEY+r'元|$)',p)
            # A name immediately after 转给 is also captured by the same rule.
            g = dict(kind='transfer_money',args=dict(from_card_id=int(card[1]) if card else None,
                amount=amount[1] if amount else None,recipient_name=who[1] if who else None))
            if g['args']['recipient_name'] in {'他','她','对方','那个人'}:
                g['args']['recipient_name']=None
        elif re.search(r'消费|支出|账单|现金流|收支|花|收入|流水|明细|购买金额|购买总额|购买(?:了)?多少',p):
            g=dict(kind='bill',request=p)
        elif re.search(r'购买|买入|申购',p):
            card=re.search(r'(?:从|用)([1-9][0-9]*)号卡',p)
            product=re.search(r'产品\s*([1-9][0-9]*)',p)
            amount=re.search('('+MONEY+r')元',p)
            g=dict(kind='buy_wealth_product',args=dict(card_id=int(card[1]) if card else None,
                product_id=int(product[1]) if product else None,amount=amount[1] if amount else None))
        elif re.search(r'赎回',p):
            holding=re.search(r'持仓\s*([1-9][0-9]*)',p);amount=re.search('('+MONEY+r')元',p)
            g=dict(kind='redeem_wealth_product',args=dict(holding_id=int(holding[1]) if holding else None,
                amount=amount[1] if amount else None))
        elif re.search(r'持仓',p) and re.search(READ_WORD,p):
            if re.sub(READ_WORD+r'|我的?|最新|当前|理财|持仓|\s','',p):
                error='持仓查询包含当前组合不支持的筛选；整条未执行。'
            g=dict(kind='get_wealth_holdings',args={})
        elif re.search(r'产品',p) and re.search(READ_WORD,p):
            if re.search(r'产品\s*\d|比较|对比',p):
                error='请明确产品查询或比较的范围；当前组合未执行。'
            levels=re.findall(r'R[1-5]',p)
            statuses=re.findall(r'在售|停用',p)
            rest=re.sub(READ_WORD,'',p)
            rest=re.sub(r'适合我的?|适配的?|风险等级|R[1-5]|在售|停用|理财|产品|的|\s','',rest)
            if rest or len(set(levels))>1 or len(set(statuses))>1:
                error='产品筛选含未支持或有歧义的条件；整条未执行，请明确风险等级、在售状态和适当性范围。'
            g=dict(kind='get_wealth_products',args=dict(suitable_only='适合我' in p or '适配' in p,
                risk_level=levels[0] if levels else None,status={'在售':'active','停用':'inactive'}.get(statuses[0]) if statuses else None))
        elif re.search(r'风险等级',p):
            if re.sub(READ_WORD+r'|我的?|当前|风险等级|\s','',p):
                error='风险资料查询包含未支持的范围；整条未执行。'
            g=dict(kind='get_risk_profile',args={})
        elif re.search(r'余额|银行卡|卡片|还剩多少',p):
            from .query_requirements import CARD
            remainder=CARD.sub('',p)
            if re.sub(READ_WORD+r'|我的?|当前|最新|银行卡|卡片|余额|还剩多少|\s','',remainder):
                error='余额查询需明确卡片编号；当前别名或筛选无法可靠解析，整条未执行。'
            g=dict(kind='get_cards',args={},card_ids=list(dict.fromkeys(int(m[1] or m[2]) for m in CARD.finditer(p))))
        elif re.search(r'消费|支出|账单|现金流|收支|花|收入|流水|明细',p):
            g=dict(kind='bill',request=p)
        else:
            error='存在未识别或不支持的业务阶段，请明确每个查询、办理目标及参数；整条未执行。'
        if g:
            if g['kind'] in WRITES:
                # Consume all public write syntax. Anything left (alternatives,
                # negation, hidden extra conditions, extra amounts) is ambiguous.
                rest=re.sub(r'(?:从|用)[1-9][0-9]*号卡|产品\s*[1-9][0-9]*|持仓\s*[1-9][0-9]*|'+MONEY+r'元','',p)
                if g['kind']=='transfer_money' and g['args']['recipient_name']:
                    rest=rest.replace(g['args']['recipient_name'],'',1)
                rest=re.sub(r'转账|转给|转|给|向|购买|买入|申购|赎回|理财|[\s]','',rest)
                # Unresolved aliases/pronouns are missing public fields, never IDs.
                rest=re.sub(r'(?:从|用)(?:工资卡|常用卡|储蓄卡)|他|她|对方|那个人','',rest)
                if rest:error='办理阶段包含未覆盖的条件、歧义或额外参数，请明确后再规划；整条未执行。'
            if pending:
                if g['kind'] not in WRITES or 'source' not in pending:
                    error='条件必须绑定明确的可信查询及后续办理步骤。'
                g['condition']=pending;pending=None
            goals.append(g)
    if pending: error='条件缺少对应的办理步骤。'
    if len(goals)>8 or sum(g['kind'] in WRITES for g in goals)>3:
        error='任务超过最多8步、3个写步骤的限制；整条未执行。'
    if re.search(r'上传|图片|截图|附件',text):
        error='上传账单可以单独查询，不复制到持久银行任务；本次组合未执行。'
    explicit=bool(re.search(r'先|然后|之后|接着|再|完成后|转完后|买完|如果|若',text))
    return dict(goals=goals,error=error,bound_date=bound_date,
        ordering='explicit' if explicit else 'ambiguous')


def verify_card_references(wanted,router,session):
    """Use the original authenticated read tool, never model-selected account IDs.

    get_cards is an unfiltered owner-scoped list. A missing requested card must
    stop the whole plan rather than let a legal transfer hide that missing goal.
    This read supplies no transaction authorization and is not retained as a
    current balance or a condition value for the eventual transfer.
    """
    ids={card for g in wanted['goals'] for card in g.get('card_ids',[])}
    if not ids or wanted.get('error'):return
    dispatch=router.dispatch('get_cards',{},principal=session.principal,session=session)
    result=dispatch.result
    if not result.success or not isinstance(result.data,list):
        wanted['error']='无法核验指定银行卡，整条任务尚未执行，请稍后重新核对。'
    elif not ids.issubset({row.get('card_id') for row in result.data if isinstance(row,dict)}):
        wanted['error']='所查询的银行卡不存在或不属于当前用户；整条任务未执行，请核对卡片编号。'


def check(wanted, steps, plan_steps=None, approved=None, verify_bills=True):
    if wanted['error']:return 'query_scope_unclear',wanted['error']
    goals=wanted['goals']
    if any(any(v is None for k,v in g.get('args',{}).items() if k not in {'risk_level','status'}) for g in goals):
        return 'query_scope_unclear','请补充明确的付款卡、金额和收款人或产品；未开始执行。'
    if len(steps)!=len(goals):return 'query_scope_mismatch','计划遗漏目标或增加了未经请求的业务；整条未执行。'
    if len(goals)>1 and not plan_steps:return 'query_scope_mismatch','多个目标必须使用完整依赖计划。'
    approved=approved or {}
    for i, (g,(name,args)) in enumerate(zip(goals,steps)):
        if (name not in BILLS if g['kind']=='bill' else name!=g['kind']):
            return 'query_scope_mismatch','业务阶段与用户目标不一致；整条未执行。'
        if g['kind']=='bill' and re.search(r'购买支出|购买总额|购买金额',g['request']) and name not in {'query_data','query_financial_records'}:
            return 'query_scope_mismatch','购买支出必须使用购买口径查询，不能用包含转账或理财的总支出代替。'
        if g['kind']!='bill':
            expected=g['args']
            if any(v is None for k,v in expected.items() if k not in {'risk_level','status'}):
                return 'query_scope_unclear','请补充明确的付款卡、金额和收款人或产品；未开始执行。'
            actual={'risk_level':None,'status':None,'suitable_only':False,**args} if name=='get_wealth_products' else args
            normalized={k:v for k,v in actual.items() if v not in (None,'')}
            defaults={k:v for k,v in expected.items() if v not in (None,'')}
            try:
                for k in ('amount',):
                    if k in defaults:
                        normalized[k]=Decimal(str(normalized.get(k)));defaults[k]=Decimal(str(defaults[k]))
            except (InvalidOperation,ValueError,TypeError):return 'query_scope_mismatch','交易金额未保留。'
            if normalized!=defaults:return 'query_scope_mismatch','账户、对象、金额或产品参数与对应目标不一致。'
        elif not verify_bills:
            pass
        elif plan_steps and plan_steps[i].step_id in approved:
            from data_query import QueryBatch
            from dialogue_state import legacy_query
            from agent.tool_registry import build_default_registry
            actual=legacy_query(name,args,build_default_registry(include_analysis=True))
            if QueryBatch.model_validate(actual).model_dump(mode='json')!=approved[plan_steps[i].step_id]:
                return 'query_scope_mismatch','查询范围与已核验目标不一致。'
        elif g['kind']=='bill':
            # Reuse the independent old requirements for legacy bill tools.
            from .query_requirements import extract as legacy_extract,check as legacy_check
            if name=='query_data':return 'query_scope_mismatch','该查询阶段尚未通过独立语义核验。'
            failure=legacy_check(legacy_extract(g['request'],wanted['bound_date']),[(name,args)])
            if failure:return failure
        if plan_steps:
            s=plan_steps[i]
            # Ordered language phases are a conservative chain. Parallel planning
            # must not move a post-write observation before the write succeeds.
            if wanted.get('ordering')=='ambiguous':
                return 'dependency_order_unclear','你希望按列出的顺序先查询再准备办理，还是将它们作为互不依赖的操作？请回复“按顺序办理”或“互不依赖”。'
            if wanted.get('ordering')=='independent':
                if s.depends_on:return 'dependency_mismatch','计划增加了未经确认的依赖；请重新确认操作顺序。'
            elif i and plan_steps[i-1].step_id not in s.depends_on:
                return 'query_scope_mismatch','步骤顺序没有保留用户要求的前后依赖。'
            c=g.get('condition')
            if bool(s.condition)!=bool(c):return 'query_scope_mismatch','条件被遗漏或添加。'
            if c:
                source=plan_steps[c['source']]
                fields={'get_monthly_spending':'data.total_spending','query_financial_records':'data.current.amount_cents'}
                if source.tool_name=='get_monthly_cash_flow_report':
                    from .query_requirements import _scope
                    metric=_scope(goals[c['source']]['request'],wanted['bound_date'])['metric']
                    fields[source.tool_name]={'income':'data.totals.income','expense':'data.totals.expense',
                        'cash_flow':'data.totals.net_cash_flow'}.get(metric)
                threshold=Decimal(c['value'])*(100 if source.tool_name=='query_financial_records' else 1)
                if (source.tool_name not in fields or s.condition.source_step_id!=source.step_id or
                    s.condition.field_path!=fields[source.tool_name] or s.condition.operator!=c['operator'] or
                    isinstance(s.condition.value,bool) or Decimal(str(s.condition.value))!=threshold):
                    return 'query_scope_mismatch','条件来源、字段、比较符或阈值没有保留。'
    return None
