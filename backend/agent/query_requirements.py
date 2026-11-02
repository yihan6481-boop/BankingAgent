"""Bounded text requirements checked against existing deterministic capabilities.

Unknown fragments require clarification. Model contracts never supply slots.
Public schemas and authorization are unchanged.
"""
import re
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

BILL_TOOLS = {'get_monthly_spending', 'get_category_spending',
    'get_transactions_by_month', 'get_monthly_cash_flow_report', 'get_expense_clues'}
TIME = re.compile(r'(?<!\d)([0-9]{4})[-年](0?[1-9]|1[0-2])(?:月)?(?!\d)|'
    r'(?<![\d年-])(0?[1-9]|1[0-2])月|本月|这个月|上个月|上月')
TIME_TOKEN = re.sub(r'\((?!\?)', '(?:', TIME.pattern)
METRIC = re.compile(r'净现金流|现金流|收支|收入|流入|总支出|支出|消费|花了|花费|花|流出|交易流水|流水|交易明细|明细|分类|报告|原因|线索')
ACTION = re.compile(r'(?:(?:请|麻烦|帮我|帮忙|先|我|仅基于|查询|查查|查|查看|看看|看|分析|一下|改查|改为|改|按原来的?|按原的?|统计|新任务|开始新任务|换个话题)|\s)+')
CARD = re.compile(r'(?<!\d)([1-9][0-9]*)号卡|(?:银行卡|卡)\s*([1-9][0-9]*)(?!\d)')
NUMBER = r'[0-9]+|[一二两三四五六七八九十百]+'
COUNT = re.compile(r'(?:前|top\s*)('+NUMBER+r')(?:名|个|家|项)?|('+NUMBER+r')(?:个|家)(?:商户|商家)?', re.I)
NO_COMPARISON = re.compile(r'(?:不(?:做|用|需要)?|取消)(?:比较|对比|环比)')
VOCAB = re.compile(r'全部商户|所有商户|全商户|按商户|按商家|按分类|按类别|按|商户|商家|类别|'
    r'最多|最高|最少|最低|降序|升序|排行|排名|分组|汇总|总额|总|金额|笔数|数量|'
    r'环比|同比|比上个月|比上月|比较|对比|相比|与|和|以及|及|并且|并|跟|比|的|在|'
    r'多少钱|多少|是多少|怎么|这么多|实际|已记录|记录|交易|统计|查询|查查|查|看看|看|一下|'
    r'请|帮我|麻烦|我|分析|报告|找找|找|可能|相关|需核查|识别|原因|线索|订阅|'
    r'继续|再|刚才|中的|商户文字|有什么|值得注意|'
    r'仅基于|基于|本|这个|上|月度|月|收|净|流出|流入|支出|收入|消费|收支|现金流|流水|明细|分类|花了|花费|花|'
    r'[\s，。？！；,;?：:、！]+')
VOCAB = re.compile('|'.join(sorted(VOCAB.pattern.split('|'), key=len, reverse=True)))


def _known(text):
    return not VOCAB.sub('', text).strip()


from business_calendar import month_before

def _number(text):
    if text.isdigit():
        return int(text)
    digits = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if text in digits:
        return digits[text]
    if re.fullmatch(r'[一二三四五六七八九]?十[一二三四五六七八九]?', text):
        left, right = text.split('十')
        return digits.get(left,1)*10 + digits.get(right,0)
    return None


def _months(text, bound_date):
    current = date.fromisoformat(bound_date)
    values = []
    for m in TIME.finditer(CARD.sub('',text)):
        value = (f'{int(m[1]):04d}-{int(m[2]):02d}' if m[1] else
            f'{current.year:04d}-{int(m[3]):02d}' if m[3] else current.strftime('%Y-%m'))
        if m[0] in {'上月','上个月'}:
            try:
                value = month_before(value)
            except ValueError:
                continue
        if value not in values:
            values.append(value)
    return values


def _scope(text, bound_date):
    months = _months(text, bound_date)
    comparison = bool(re.search(r'比较|对比|相比|环比|同比|比上个?月',text))
    kind = 'yoy' if '同比' in text else 'mom' if re.search(r'环比|比上个?月',text) else 'explicit' if comparison else None
    unsupported_period = bool(re.search(r'最近|近[一二三四五六七八九十\d]+个月|季度|周|天|日到|区间|至|~|去年|今年|昨天|今天',text))
    if kind == 'mom' and len(months) == 1:
        try:
            months.append(month_before(months[0]))
        except ValueError:
            unsupported_period = True
    metrics = []
    metric_text = re.sub(r'基于(?:(?:'+TIME_TOKEN+r')|的)*(?:交易)?流水','',text)
    for label, pattern in (('expense',r'支出|消费|花|流出'),('income',r'收入|流入'),
        ('cash_flow',r'收支|报告|净现金流|现金流'),('transactions',r'流水|明细'),
        ('categories',r'分类|类别'),('analysis',r'分析|原因|线索|值得注意')):
        if re.search(pattern,metric_text):
            metrics.append(label)
    if not metrics and '看看' in text:
        metrics = ['analysis']
    source_analysis = bool(re.search(r'(?:仅)?基于.*(?:分析|原因)|分析.*(?:'+METRIC.pattern+r')|(?:分析|找找?|查找|识别).*原因|怎么.*花|值得注意',text))
    explicit_outputs = bool(re.search(r'(?:^|[ ，；])(?:请|麻烦|帮我|先|我)*(?:查询|查看|查|统计|汇总|给出|列出)|总额|多少钱|多少|(?:和|及|并|再)(?:给出|列出|分析)|(?:支出|收入)(?:和|及)(?:支出|收入)',text))
    if 'analysis' in metrics and source_analysis and not explicit_outputs:
        # "基于流水分析支出/找原因" names the source, not extra detail/total
        # outputs. Explicit conjoined output requests still retain all metrics.
        metrics = ['analysis']
    count = COUNT.search(text)
    limit = _number(count[1] or count[2]) if count else None
    limits = {_number(m[1] or m[2]) for m in COUNT.finditer(text)}
    ranking = bool(re.search(r'最多|最高|最少|最低|排行|排名|前(?:'+NUMBER+r')|top\s*\d',text,re.I))
    grouping = 'merchant' if re.search(r'商户|商家',text) and (ranking or '分组' in text or '按商' in text) else 'category' if re.search(r'分类|类别',text) else None
    sort = 'asc' if re.search(r'最少|最低|升序',text) else 'desc' if re.search(r'最多|最高|降序|排行|排名|前(?:'+NUMBER+r')|top\s*\d',text,re.I) else None
    output = 'ranking' if ranking else 'grouped' if grouping else 'details' if 'transactions' in metrics else 'summary'
    card = CARD.search(text)
    cards = {int(m[1] or m[2]) for m in CARD.finditer(text)}
    merchant = None
    working = text
    marked = re.search(r'(?:在|商户[：:\s]+)((?:(?!'+TIME_TOKEN+r'|[，。？！；,;?\s]).){1,40}?)(?:的)?(?='+METRIC.pattern+r')',text)
    if marked:
        merchant = marked[1].removesuffix('的')
        working = working[:marked.start()] + working[marked.end():]
    else:
        temporal = list(TIME.finditer(text))
        if temporal:
            prefix = text[:temporal[0].start()].strip()
            prefix = ACTION.sub('',prefix,count=1).strip().strip('的')
            prefix = CARD.sub('',prefix).strip('的 ')
            if prefix and not _known(prefix):
                if not re.search(r'查|看|按|排除|不含|除外|不要',prefix):
                    merchant = prefix.removeprefix('在').removeprefix('商户：').removeprefix('商户:')
                    working = working.replace(text[:temporal[0].start()],'',1)
            if not merchant:
                tail = text[temporal[0].end():]
                metric = METRIC.search(tail)
                if metric:
                    candidate = tail[:metric.start()].strip()
                    candidate = TIME.sub('', candidate).strip()
                    candidate = ACTION.sub('',candidate,count=1).strip()
                    candidate = CARD.sub('',candidate).strip('的 ')
                    if candidate and not _known(candidate) and not candidate.startswith('按'):
                        merchant = candidate.removeprefix('在').removesuffix('的')
                        working = working.replace(tail[:metric.start()],'',1)
                between = re.match(r'(?:的)?([^，。？！；,;?\s]{1,30}?)(?:与|和|跟)(?='+TIME.pattern+r')',tail)
                if not merchant and between and not _known(between[1]):
                    merchant = between[1]
                    working = working.replace(merchant,'',1)
    working = re.sub(r'按原(?:来)?(?:需求|要求)(?:重新规划|重试|重新查询)?','',working)
    working = ACTION.sub('',working,count=1)
    working = TIME.sub('',working)
    working = CARD.sub('',working)
    working = COUNT.sub('',working)
    working = re.sub(r'按原(?:来)?(?:需求|要求)(?:重新规划|重试|重新查询)?','',working)
    working = re.sub(r'按原条件办理','',working)
    working = re.sub(r'(?:补全|保留|完成|重试|重新规划)(?:原始|原来|原)?的?(?:全部|所有|完整)?(?:目标|需求|任务)','',working)
    residual = ACTION.sub('',VOCAB.sub('',working))
    unsupported_filter = bool(re.search(r'排除|不含|除外|除了|不要|仅(?!基于)|只|大于|小于|超过|低于|高于|不超过|不少于|退款|净消费|净支出|净收入|扣除',text))
    return dict(months=months,merchant=merchant,comparison=comparison,comparison_kind=kind,
        card_id=int(card[1] or card[2]) if card else None,metric=metrics[0] if metrics else None,
        metrics=metrics,requires_clues='analysis' in metrics and bool(re.search(r'原因|线索|怎么.*花',text)),
        output=output,group_by=grouping,sort=sort,limit=limit,
        consumption=bool(re.search(r'消费|购买|购物',text)),
        ambiguous_scope=bool(residual.strip()) or bool('排序' in text and sort is None) or len(cards)>1 or len(limits)>1,
        unsupported_period=unsupported_period,unsupported_filter=unsupported_filter,
        has_calendar=bool(TIME.search(text)),bound_date=bound_date)


WRITE_TOOLS = {'transfer_money','set_daily_limit','report_card_lost','cancel_subscription',
    'buy_wealth_product','redeem_wealth_product'}
OPERATORS = {'不超过':'lte','不少于':'gte','大于':'gt','超过':'gt','高于':'gt',
    '不足':'lt','少于':'lt','小于':'lt','低于':'lt','至少':'gte'}
MONEY_TOKEN = r'[0-9]+(?:\.[0-9]{1,2})?'
CARD_TOKEN = r'(?:[1-9][0-9]*号卡|(?:银行卡|卡)\s*[1-9][0-9]*)'


def _condition_action(text):
    """Bind explicit action identity; unsupported/ambiguous syntax fails closed."""
    text = re.sub(r'^(?:然后|再|接着|随后)','',text.strip())
    m = re.fullmatch(r'(?:将|把)?('+CARD_TOKEN+r')的?(?:每日|每天|日)?限额'
        r'(?:调整为|设置为|改为|调为|设为)('+MONEY_TOKEN+r')(?:元)?',text)
    if m:
        card = CARD.search(m[1])
        return dict(tool_name='set_daily_limit',arguments=dict(card_id=int(card[1] or card[2]),daily_limit=m[2]))
    m = re.fullmatch(r'(?:(?:从|用)?('+CARD_TOKEN+r'))?(?:给|向)([^，；,;]{1,40}?)'
        r'(?:转账|转)('+MONEY_TOKEN+r')(?:元)?',text)
    if m:
        args = dict(recipient_name=m[2],amount=m[3])
        if m[1]:
            card = CARD.search(m[1]);args['from_card_id']=int(card[1] or card[2])
        return dict(tool_name='transfer_money',arguments=args)
    return None


def _same_action(wanted, step):
    if not wanted or step.tool_name != wanted['tool_name']:
        return False
    for key,value in wanted['arguments'].items():
        actual = step.arguments.get(key)
        if key in {'amount','daily_limit'}:
            try:
                if isinstance(actual,bool) or Decimal(str(actual)) != Decimal(value):
                    return False
            except (InvalidOperation, TypeError, ValueError):
                return False
        elif actual != value or isinstance(actual,bool):
            return False
    return True


def presentation_preferences(text):
    """Remove only bounded output clauses, never arbitrary negative filters."""
    patterns = {
        'refund_separate': r'退款(?:请)?(?:单独(?:告诉我|说|列出|列)?|另外(?:说|列出|列)|另列|单列)',
        'brief': r'(?:别|不要|不用|不)(?:列出|列|展开|显示|展示)(?:全部|所有|每笔|逐笔)?(?:交易)?(?:流水|明细|记录)|(?:只|仅)(?:给|说|报|显示|展示)?(?:我)?(?:合计|总数|总额|汇总)(?:即可|就好)?',
    }
    preferences = {}
    for key, pattern in patterns.items():
        preferences[key] = bool(re.search(pattern, text))
        text = re.sub(pattern, '', text)
    return text, preferences


def query_followup(text):
    return bool(re.fullmatch(r'\s*(?:那|那么)([^，。？！；,;?\s]{1,40}?)呢[？?。]?\s*', text) or
        re.search(r'商户(?:改为|改成|换成)|其他要求保留|按原(?:来)?(?:需求|要求)|口径(?:改为|改成)', text))


def complete_other_goal(text):
    """Bounded complete goals, not a verb-based topic classifier.

    Used only for unattached clarification drafts; live writes stay protected.
    """
    return bool(re.search(
        r'(?:从|用)[^，。；;]{1,30}(?:给|向)[^，。；;]{1,20}?转(?:账)?[0-9]+(?:\.[0-9]{1,2})?元', text))


def _financial_scope(text, bound_date, previous=None):
    """Bounded summary grammar. Residual text is a constraint, never ignored.

    Separate from legacy monthly capabilities to preserve their semantics.
    This deterministic contract is checked against model parameters, including
    every query in a DAG. The model cannot provide evidence for its own scope.
    """
    from database import merchant_catalog
    MERCHANT_ALIASES = merchant_catalog()
    from analysis_models import QueryArgs
    prior = previous.get('financial') if previous else None
    original = text
    text, preferences = presentation_preferences(text)
    preferences = {k: v or bool(prior and prior.get('presentation', {}).get(k)) for k,v in preferences.items()}
    text = text.replace('买东西', '购买')
    followup = re.fullmatch(r'\s*(?:那|那么)([^，。？！；,;?\s]{1,40}?)呢[？?。]?\s*', text)
    correction = re.search(r'商户(?:改为|改成|换成)([^，。？！；,;?\s]{1,40})', text)
    corrected_merchant = (followup[1] if followup else correction[1] if correction else None)
    if corrected_merchant:
        text = text.replace(followup[0] if followup else correction[0], '')
    text = re.sub(r'其他要求保留|口径(?:改为|改成)', '', text)
    residual = ''
    cancel = bool(NO_COMPARISON.search(text))
    text = NO_COMPARISON.sub('',text)
    unsupported_tokens = re.findall(r'净消费|净支出|净额|扣除|排除|不含|除外|除了|不要|仅|只|同比|最近|季度|排行|排名|最多|最高|最低|最少|大于|小于|超过|低于|高于|如果|若|收入|流入|明细|流水|分类|分组|原因|线索|余额',text)
    unsupported = bool(unsupported_tokens)
    mixed_metric=bool(re.search(r'购买|购物|消费|花',text) and re.search(r'现金流出|已记录支出|已记录流出',text))
    unsupported |= mixed_metric
    if mixed_metric:unsupported_tokens.append('mixed_metric')
    # Only named, controlled aliases. Matching text never creates an alias.
    names = ([m[0] for m in re.finditer('|'.join(map(re.escape,sorted(MERCHANT_ALIASES,key=len,reverse=True))),text)]
             if MERCHANT_ALIASES else [])
    keys = {MERCHANT_ALIASES[n] for n in names}
    merchant_probe = CARD.sub('',re.sub(r'购买|购物','消费',text).replace('已记录',''))
    if re.search(r'\d{4}[-年]\d{1,2}[-月]\d{1,2}',merchant_probe):
        merchant_probe = re.sub(r'\d{4}[-年]\d{1,2}[-月]\d{1,2}(?:日)?','',merchant_probe)
        merchant_probe = bound_date[:7] + re.sub(r'从|至|到|~|～','',merchant_probe)
    parsed_merchant = _scope(merchant_probe,bound_date).get('merchant')
    merchant = corrected_merchant or parsed_merchant or (names[0] if names else (prior.get('merchant') if prior else None))
    ambiguous = len(keys)>1
    working = text.replace(parsed_merchant,'',1) if parsed_merchant else text
    working = re.sub('|'.join(map(re.escape,sorted(MERCHANT_ALIASES,key=len,reverse=True))),'',working)
    metric = ('cash_outflow' if re.search(r'现金流出|现金流出口径|已记录(?:现金)?流出|已记录支出',text) else
              'purchase_expense' if re.search(r'购买|购物|消费|花',text) else
              prior.get('metric') if prior else None)
    months = _months(text,bound_date)
    dates = re.findall(r'(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日)?(?!\d)',CARD.sub('',text))
    start = end = None
    date_months = []
    try:
        if dates:
            if len(dates)!=2 or not re.search(r'至|到|~|～',text):
                raise ValueError('ambiguous_dates')
            parsed = [date(*map(int,d)) for d in dates]
            start,end = parsed[0].isoformat(),(parsed[1]+timedelta(days=1)).isoformat()
            working = re.sub(r'(?<!\d)\d{4}[-年]\d{1,2}[-月]\d{1,2}(?:日)?(?!\d)','',working)
            date_months = _months(working,bound_date)
            months = []
        elif months:
            from analysis_service import _month_range
            span = _month_range(max(months),None)
            start,end = span['start'],span['end_exclusive']
        elif prior:
            start,end = prior.get('start'),prior.get('end_exclusive')
        comparison = bool(re.search(r'比较|对比|相比|环比|比上个?月',text)) or bool(prior and prior.get('compare_previous') and not cancel)
        if cancel:
            comparison = False
        if len(months)>1 and not comparison:
            ambiguous = True
        if dates:
            if date_months and not comparison:
                ambiguous = True
            elif comparison:
                if date_months and date_months!=[month_before(start[:7])]:
                    ambiguous = True
                if not date_months and not re.search(r'环比|比上个?月',text):
                    ambiguous = True
        if comparison and months:
            if len(months)==1 and re.search(r'环比|比上个?月',text):
                months.append(month_before(months[0]))
            if len(months)>2:
                ambiguous = True
            if len(months)==2 and set(months)!={max(months),month_before(max(months))}:
                unsupported = True
            elif len(months)==1 and not (prior and prior.get('compare_previous')):
                ambiguous = True
        cards = {int(m[1] or m[2]) for m in CARD.finditer(text)}
        ambiguous |= len(cards)>1
        card = next(iter(cards)) if cards else (prior.get('card_id') if prior else None)
        working = CARD.sub('',TIME.sub('',working))
        # Recognized unsupported metric syntax remains a refusal reason, while
        # its text is not an eternal unknown after an explicit metric repair.
        working = re.sub(r'(?:扣除?|减去)退款(?:后)?(?:算|计算)?(?:净额|净消费|净支出)?|净消费|净支出|净额','',working)
        working = re.sub(r'现金流出口径|现金流出|购买支出总额|购买支出|购买|购物|从|至|到|~|～|改成|改为|纠正|按原需求|重新规划|重试|保留|可以|能否|吗|谢谢|统计|总额|口径','',working)
        residual = ACTION.sub('',VOCAB.sub('',working)).strip()
        ambiguous |= bool(residual) or merchant is None and bool(names)
        candidate = dict(start=start,end_exclusive=end,merchant=merchant,card_id=card,
                         metric=metric,compare_previous=comparison)
        QueryArgs.model_validate(candidate)
    except (ValueError,TypeError,OverflowError):
        ambiguous = True
        candidate = dict(start=start,end_exclusive=end,merchant=merchant,metric=metric)
    # A merchant correction replaces its bad value, not the independent time,
    # metric, comparison or unsupported business filter. Explicit metric repair
    # can retract an old invalid metric; a new scope does not inherit this state.
    metric_repair = bool(re.search(r'(?:口径)?(?:改为|改成)(?:购买支出|现金流出)', original))
    prior_tokens = prior.get('unsupported_tokens',[]) if prior else []
    retained_tokens = [t for t in prior_tokens if not (metric_repair and t in {'净消费','净支出','净额','收入','流入','mixed_metric'})]
    unsupported |= bool(retained_tokens) or bool(prior and prior.get('unsupported') and not prior_tokens)
    old_residual = prior.get('unresolved_text','') if prior else ''
    if corrected_merchant and old_residual == (prior or {}).get('merchant'):
        old_residual = ''
    unresolved = old_residual or residual
    ambiguous |= bool(unresolved)
    return dict(**candidate,unclear=ambiguous,unsupported=unsupported,unresolved_text=unresolved,
                source_text=original,presentation=preferences,unsupported_tokens=list(dict.fromkeys(unsupported_tokens+retained_tokens)))


def extract(text, bound_date, previous=None):
    from storage.config import pg02_enabled
    # Explicitly selected planning drafts use the same independent inventory on
    # SQLite. The legacy parser's empty goals list cannot represent that flow;
    # dropping it would lose the original conditions or fail during a slot turn.
    if not pg02_enabled() and previous and previous.get('flow'):
        from .flow_requirements import extract as flow_extract
        flow=flow_extract(text,bound_date,previous['flow'])
        if flow is not None:
            return dict(flow=flow,goals=[],months=[],conditions=[])
    if pg02_enabled():
        from .flow_requirements import extract as flow_extract
        flow=flow_extract(text,bound_date,(previous or {}).get('flow'))
        if flow is not None:
            return dict(flow=flow,goals=[],months=[],conditions=[])
        if previous and previous.get('flow'):
            previous=None  # An explicit independent goal must not inherit an empty legacy scope.
        from .wealth_requirements import extract as wealth_extract
        wealth,base=wealth_extract(text,previous,bound_date=bound_date)
        if wealth is None:
            text=base
            if previous and previous.get('wealth') and not previous.get('goals'): previous=None
        if wealth is not None:
            # Parse bill goals independently; wealth "购买" is not bill consumption.
            if base.strip():
                base_previous=dict(previous or {});base_previous.pop('wealth',None)
                value=extract(base,bound_date,base_previous if base_previous.get('goals') else None)
            else:
                value=dict(months=[],goals=[],conditions=[],condition_scope_unclear=False,financial={},annual=None)
            value['wealth']=wealth
            return value
    # A fully explicit transfer clause is a separate goal, never a bill filter.
    # Anything outside this bounded form remains in the query and fails closed.
    transfer_pattern = re.compile(r'(?:然后|之后|再)(?:请|帮我)?从([1-9][0-9]*)号卡(?:给|向)([^，。；,;]{1,20}?)转(?:账)?([0-9]+(?:\.[0-9]{1,2})?)元(?=$|[，。；,;])')
    transfers = [dict(from_card_id=int(m[1]),recipient_name=m[2],amount=str(Decimal(m[3])))
                 for m in transfer_pattern.finditer(text)]
    financial = _financial_scope(transfer_pattern.sub('',text),bound_date,previous)
    text = re.sub(r'^(?:新任务|开始新任务|换个话题)[，,:：\s]*','',text)
    cancel_comparison = bool(NO_COMPARISON.search(text))
    text = NO_COMPARISON.sub('',text)
    parts = re.split(r'[，。？！；,;?]|(?:和|以及|并且)(?='+TIME_TOKEN+r')|(?:并且|然后|并|且|和)(?=如果|若)',text)
    query_parts = []
    prefix_parts = []
    conditions = []
    pending_condition = None
    condition_scope_unclear = False
    for p in parts:
        p = p.strip()
        is_conditional = bool(re.search(r'如果|^(?:若)|[（(]若',p) or re.search(
            r'(?:'+ '|'.join(OPERATORS) +r').*时',p))
        if is_conditional:
            if pending_condition is not None:
                condition_scope_unclear = True
            m = re.fullmatch(r'(?:如果|若)?(.*?)('+ '|'.join(OPERATORS) +r')('
                +MONEY_TOKEN+r')(?:元)?(?:时|的话)?(.*)',p)
            if not m:
                condition_scope_unclear = True
                pending_condition = None
                continue
            source_text = m[1] or (query_parts[-1] if query_parts else '')
            source = _scope(source_text,bound_date)
            if not source['months'] and source['metric'] in {'expense','income','cash_flow'}:
                candidates = {}
                for earlier in query_parts:
                    prior = _scope(earlier,bound_date)
                    if (source['metric'] in prior['metrics'] and len(prior['months']) == 1 and
                            not prior['ambiguous_scope'] and not prior['unsupported_period'] and
                            not prior['merchant'] and not prior['unsupported_filter'] and
                            (source['card_id'] is None or source['card_id'] == prior['card_id'])):
                        candidates[(tuple(prior['months']),prior['card_id'])] = prior
                if len(candidates) == 1:
                    prior = next(iter(candidates.values()))
                    source['months'],source['card_id'] = prior['months'],prior['card_id']
                    # Preserve the inherited source as a typed goal rather than
                    # re-parsing a monthless fragment later.
                    source_text = (prior['months'][0] + ('卡'+str(prior['card_id']) if prior['card_id'] else '') +
                        {'expense':'支出','income':'收入','cash_flow':'净现金流'}[source['metric']])
            if (len(source['months']) != 1 or len(source['metrics']) != 1 or
                    source['metric'] not in {'expense','income','cash_flow'} or
                    source['ambiguous_scope'] or source['unsupported_filter'] or
                    source['unsupported_period'] or source['merchant'] or source['comparison']):
                condition_scope_unclear = True
            wanted = dict(months=source['months'],card_id=source['card_id'],metric=source['metric'],
                operator=OPERATORS[m[2]],value=m[3],action=None)
            conditions.append(wanted)
            query_parts.append(source_text)
            pending_condition = wanted
            if m[4].strip():
                wanted['action'] = _condition_action(m[4])
                condition_scope_unclear |= wanted['action'] is None
                pending_condition = None
            continue
        if pending_condition is not None and p:
            pending_condition['action'] = _condition_action(p)
            condition_scope_unclear |= pending_condition['action'] is None
            pending_condition = None
            continue
        # Negative/future notices for separate subscription actions are not bill
        # filters and grant no permission. Protected reads retain their gate.
        notice = (not METRIC.search(p) and not TIME.search(p) and (
            re.match(r'^(?:本轮|也)?不(?:查询|取消)(?:有效订阅|订阅|自动扣款授权)',p) or
            re.match(r'^(?:稍后|之后|以后).*授权.*(?:查询|选择)',p)))
        if notice:
            continue
        if re.fullmatch(r'(?:之后|然后|再|完成后)?(?:请|帮我)?(?:查询|查看|查)(?:余额|银行卡|卡片)',p.strip()):
            continue  # Separate card-read goal, not an unrecognized bill filter.
        if not p.strip() or re.search(r'(?:给|向).{1,20}转|付款卡|限额',p):
            continue
        if query_parts and not TIME.search(p):
            # Includes trailing filters/sort/count/unknown fragments. A comma
            # is not permission to discard a user constraint.
            query_parts[-1] += ' ' + p
        elif METRIC.search(p):
            query_parts.append(' '.join([*prefix_parts,p]))
            prefix_parts.clear()
        elif query_parts:
            query_parts[-1] += ' ' + p
        else:
            prefix_parts.append(p)
    scopes = [_scope(p,bound_date) for p in query_parts] or [_scope(text,bound_date)]
    if previous:
        old = previous.get('goals',[previous])
        explicit_all = bool(re.search(r'(?:全部|所有|全)商户',text))
        if not query_parts:
            # Typed write-parameter clarification is outside bill scope. The
            # original text still reaches the existing modification kernel.
            correction_text = ' '.join(p for p in parts if not re.search(
                r'付款卡|限额|(?:给|向).{1,20}转',p))
            correction = _scope(correction_text,bound_date)
            scopes = deepcopy(old)
            new_months = _months(text,bound_date)
            for g in scopes:
                if new_months:
                    g['months'] = new_months
                    g['unsupported_period'] = correction['unsupported_period']
                    if g.get('comparison_kind') == 'mom' and len(new_months) == 1:
                        try:
                            g['months'] = new_months + [month_before(new_months[0])]
                        except ValueError:
                            g['unsupported_period'] = True
                if explicit_all:
                    g['merchant'] = None
                g['unsupported_filter'] |= correction['unsupported_filter']
                g['unsupported_period'] |= correction['unsupported_period']
                g['ambiguous_scope'] |= correction['ambiguous_scope']
                if cancel_comparison:
                    g['comparison'],g['comparison_kind'] = False,None
                    g['months'] = [max(g['months'])] if g['months'] else []
        elif len(old) == len(scopes):
            for g,prior in zip(scopes,old):
                g['months'] = g['months'] or prior['months']
                for k in ('merchant','card_id'):
                    if g[k] is None:
                        g[k] = prior.get(k)
                if explicit_all:
                    g['merchant'] = None
                if not g['comparison'] and prior['comparison'] and not cancel_comparison:
                    g['comparison'] = True
                    g['comparison_kind'] = prior.get('comparison_kind')
                    if g['comparison_kind'] == 'mom' and len(g['months']) == 1:
                        try:
                            g['months'].append(month_before(g['months'][0]))
                        except ValueError:
                            g['unsupported_period'] = True
                if (g['output'] == 'summary' and prior.get('output') in {'ranking','grouped','details'} and
                        not re.search(r'总支出|总额|汇总',text) and g['metrics'] != ['analysis']):
                    for k in ('output','group_by','sort','limit'):
                        g[k] = prior.get(k)
                g['unsupported_filter'] |= prior.get('unsupported_filter',False)
                if not g.get('has_calendar'):
                    g['unsupported_period'] |= prior.get('unsupported_period',False)
                if cancel_comparison:
                    g['comparison'],g['comparison_kind'] = False,None
                    g['months'] = [max(g['months'])] if g['months'] else []
        else:
            scopes = [dict(g,ambiguous_scope=True) for g in old]
    value = dict(scopes[0])
    value['months'] = list(dict.fromkeys(m for g in scopes for m in g['months']))
    value['goals'] = scopes
    value['conditions'] = conditions or (deepcopy(previous.get('conditions',[])) if previous else [])
    value['condition_scope_unclear'] = (condition_scope_unclear or pending_condition is not None or
        bool(previous and previous.get('condition_scope_unclear') and not conditions))
    value['financial'] = financial
    value['explicit_query_transfers'] = transfers or (deepcopy(previous.get('explicit_query_transfers',[])) if previous else [])
    annual=re.fullmatch(r'(?:请|麻烦|帮我)?(?:分析|查询|查看|查)?\s*([0-9]{4})年(?:年度|全年)?(?:([1-9][0-9]*)号卡)?(?:收支|现金流)(?:报告)?[。？?]?',text.strip())
    value['annual'] = (dict(year=int(annual[1]),card_id=int(annual[2]) if annual[2] else None)
                       if annual else dict(unclear=True) if re.search(r'年度|全年',text) else None)
    return value


CAPABILITIES = {
    'get_monthly_spending': {'expense'},
    'get_category_spending': {'expense','categories'},
    'get_transactions_by_month': {'transactions'},
    'get_monthly_cash_flow_report': {'expense','income','cash_flow','categories','analysis'},
    'get_expense_clues': {'analysis'},
}


def _failure(code, message):
    return code, message + '尚未执行查询。'


def _explicit_transfer_matches(name, actual, wanted):
    try:
        return (name=='transfer_money' and actual.get('from_card_id')==wanted['from_card_id'] and
                actual.get('recipient_name')==wanted['recipient_name'] and
                Decimal(str(actual.get('amount',0)))==Decimal(wanted['amount']) and
                actual.get('remark') in (None,'') and actual.get('recipient_id') is None)
    except (ValueError,TypeError,InvalidOperation):
        return False


def check(requirements, steps, *, plan_steps=None):
    if requirements is None:
        return None
    if requirements.get('flow'):
        from .flow_requirements import check as flow_check
        return flow_check(requirements['flow'],steps,plan_steps,
            requirements.get('verified_step_queries'))
    if requirements.get('data_query'):
        from data_query import QueryBatch
        queries=[a for n,a in steps if n=='query_data']
        try:
            if len(queries)!=1 or QueryBatch.model_validate(queries[0]).model_dump(mode='json')!=requirements['data_query']:
                return _failure('query_scope_mismatch','完整查询目标发生变化；')
        except ValueError:return _failure('query_scope_mismatch','查询计划无效；')
        if any(s.tool_name=='query_data' and s.condition for s in plan_steps or []):
            return _failure('query_scope_mismatch','查询不能被条件跳过；')
        writes=[(n,a) for n,a in steps if n in WRITE_TOOLS]
        allowed=requirements.get('explicit_query_transfers',[])
        if len(writes)!=len(allowed) or any(not _explicit_transfer_matches(n,a,w) for (n,a),w in zip(writes,allowed)):
            return _failure('query_scope_mismatch','混合查询未保留明确转账对象和参数；')
        if writes and (not plan_steps or any(s.tool_name=='transfer_money' and (s.condition is not None or not any(p.tool_name=='query_data' and p.step_id in s.depends_on for p in plan_steps)) for s in plan_steps)):
            return _failure('query_scope_mismatch','先查后转缺少查询依赖；')
        if any(n not in {'query_data','transfer_money','get_cards'} for n,_ in steps):
            return _failure('query_combination_unsupported','该查询组合尚未支持；')
        return None
    from storage.config import pg02_enabled,PG04_WEALTH_TOOLS
    if pg02_enabled() and (requirements.get('wealth') or any(n in PG04_WEALTH_TOOLS for n,_ in steps)):
        from .wealth_requirements import check as wealth_check
        wanted=requirements.get('wealth')
        if not wanted:
            return _failure('query_scope_mismatch','计划夹带未请求的理财动作；')
        failure=wealth_check(wanted,steps,plan_steps)
        if failure: return failure
        other=[(n,a) for n,a in steps if n not in PG04_WEALTH_TOOLS]
        if not other: return None
        parts=wanted.get('base_parts',[])
        if len(other)!=len(parts): return _failure('query_scope_mismatch','计划未保留全部查询/动作阶段；')
        other_steps=[s for s in plan_steps or [] if s.tool_name not in PG04_WEALTH_TOOLS]
        for index,((name,args),text) in enumerate(zip(other,parts)):
            selected=other_steps[index:index+1]
            if name in WRITE_TOOLS:
                expected=_condition_action(text)
                if not expected or not selected or not _same_action(expected,selected[0]) or args.get('remark') not in (None,''):
                    return _failure('query_scope_mismatch','混合计划未保留明确动作参数；')
            elif name=='get_cards':
                if not re.fullmatch(r'(?:查询|查看|查)(?:余额|银行卡|卡片)',text) or args:
                    return _failure('query_scope_mismatch','计划添加了未请求的卡查询；')
            elif name=='query_data':
                from data_query import QueryBatch
                approved=requirements.get('verified_step_queries',{}).get(selected[0].step_id) if selected else None
                if not approved or QueryBatch.model_validate(args).model_dump(mode='json')!=approved:
                    return _failure('query_scope_mismatch','该查询节点尚未通过独立语义绑定；')
            else:
                base=extract(text,wanted['bound_date'])
                failure=check(base,[(name,args)],plan_steps=selected)
                if failure: return failure
        return None
    if requirements.get('condition_scope_unclear'):
        return _failure('query_scope_unclear','条件、动作或作用域未能明确解析，请逐项明确条件及对应动作参数；')
    annual=requirements.get('annual')
    if annual or any(n=='get_annual_cash_flow_report' for n,_ in steps):
        if not annual or annual.get('unclear'):
            return _failure('query_scope_unclear','请明确年度收支报告的年份和账户；')
        if (len(steps)!=1 or steps[0][0]!='get_annual_cash_flow_report' or
                any(s.condition is not None for s in plan_steps or [])):
            return _failure('query_scope_mismatch','年度报告不能被其他范围或附加步骤替代；')
        from analysis_models import YearReportArgs
        try: actual=YearReportArgs.model_validate(steps[0][1]).model_dump()
        except (ValueError,TypeError):
            return _failure('query_scope_mismatch','年度报告参数无效；')
        if any(actual[k]!=v for k,v in annual.items()):
            return _failure('query_scope_mismatch','年度报告未保留年份或账户；')
        return None
    queries = [(n,a) for n,a in steps if n=='query_financial_records']
    if queries:
        from analysis_models import QueryArgs
        from database import merchant_catalog
        MERCHANT_ALIASES = merchant_catalog()
        wanted = requirements.get('financial',{})
        if wanted.get('unsupported'):
            return _failure('query_scope_unsupported','本轮只支持购买支出或已记录现金流出汇总及相邻整月比较；')
        if wanted.get('unclear') or not wanted:
            return _failure('query_scope_unclear','请明确受控商户别名、期间、账户和购买/现金流出口径；')
        # A legacy bill query is not a substitute or an extra broader result.
        writes = [(n,a) for n,a in steps if n!='query_financial_records']
        allowed = requirements.get('explicit_query_transfers',[])
        if (len(queries)!=1 or len(writes)!=len(allowed) or
                any(not _explicit_transfer_matches(n,a,w) for (n,a),w in zip(writes,allowed))):
            return _failure('query_scope_mismatch','查询计划夹带其他口径或写操作；')
        if writes and (not plan_steps or any(
                s.tool_name=='transfer_money' and ('query_financial_records' not in {
                    p.tool_name for p in plan_steps if p.step_id in s.depends_on} or s.condition is not None)
                for s in plan_steps)):
            return _failure('query_scope_mismatch','明确先查后转要求查询依赖，且不得附加未请求的条件；')
        if any(s.tool_name=='query_financial_records' and s.condition is not None for s in plan_steps or []):
            return _failure('query_scope_mismatch','查询不能附加条件后被跳过；')
        expected = {k:wanted.get(k) for k in QueryArgs.model_fields}
        for _,args in queries:
            try:
                actual = QueryArgs.model_validate(args).model_dump()
            except (ValueError,TypeError):
                return _failure('query_scope_mismatch','查询参数或日期区间无效；')
            for field,value in expected.items():
                a = actual[field]
                if field=='merchant' and value is not None and a is not None:
                    value,a = MERCHANT_ALIASES.get(value,value),MERCHANT_ALIASES.get(a,a)
                if a!=value:
                    return _failure('query_scope_mismatch','计划未保留商户、日期、账户、口径或比较目标；')
        return None
    bill = [(name,args) for name,args in steps if name in BILL_TOOLS]
    if any(s.tool_name in BILL_TOOLS and s.condition is not None for s in plan_steps or []):
        return _failure('query_scope_mismatch','当前条件只绑定明确的写动作；查询输出不能挂附加条件或被跳过后冒充完成；')
    goals = requirements.get('goals',[requirements])
    if not bill:
        if any(g.get('merchant') or g.get('months') and g.get('metric') for g in goals):
            return _failure('query_scope_mismatch','计划未兑现所要求的账单查询范围；')
        return None
    for _,args in bill:
        month = args.get('month')
        try:
            if not isinstance(month,str) or re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])',month) is None:
                raise ValueError('invalid_month')
            date.fromisoformat(month+'-01')
        except (TypeError,ValueError):
            return _failure('query_scope_mismatch','计划月份无效，无法核实时间范围；')
    for g in goals:
        if g.get('merchant'):
            return _failure('query_scope_unsupported','当前工具不支持指定商户过滤；请明确是否改查全商户月度数据。')
        if g.get('consumption') and g.get('output')=='summary' and not requirements.get('financial',{}).get('unclear'):
            return _failure('query_scope_mismatch','购买消费需要明确类型的查询工具，旧月报现金流出不能替代；')
        if g.get('unsupported_filter'):
            return _failure('query_scope_unsupported','当前工具不支持该排除、金额条件或交易口径过滤；')
        if g.get('ambiguous_scope') or g.get('metric') is None:
            return _failure('query_scope_unclear','存在未识别或有歧义的查询条件，请明确月份、对象、指标和输出目标；')
        if g.get('unsupported_period') or g.get('comparison_kind') == 'yoy':
            return _failure('query_scope_unsupported','当前月度工具不支持该区间或同比；请明确受支持的月份及相邻月比较。')
        if g.get('sort') and g.get('output') != 'ranking':
            return _failure('query_scope_unsupported','当前工具不支持该明细或分组的指定排序；')
        if g.get('limit') is not None and g.get('output') != 'ranking':
            return _failure('query_scope_unsupported','当前工具不支持该明细或汇总的指定数量，请明确输出目标；')
        if g.get('comparison') and (g.get('output') in {'ranking','grouped','details'} or
                not set(g.get('metrics',[])).issubset({'expense','income','cash_flow'})):
            return _failure('query_scope_unsupported','现有相邻月比较仅覆盖月度收入、支出和净现金流汇总，不支持该分组、排行或明细比较；')
        months = g['months']
        if not months:
            return _failure('query_scope_unclear','请明确查询月份；')
        matching = [(n,a) for n,a in bill if a.get('card_id') == g.get('card_id')]
        if g['comparison']:
            if len(months) != 2:
                return _failure('query_comparison_incomplete','需要明确两个比较期间；')
            target = max(months)
            try:
                pair = {target,month_before(target)}
            except ValueError:
                pair = set()
            reports = [(n,a) for n,a in matching if n == 'get_monthly_cash_flow_report' and a.get('month') == target]
            metrics = g.get('metrics',[g['metric']])
            if set(months) != pair or not reports or not set(metrics).issubset(CAPABILITIES['get_monthly_cash_flow_report']):
                return _failure('query_comparison_incomplete','计划没有兑现同一指标、账户及两个期间的相邻月比较；')
            target_months = [target]
        else:
            target_months = months
        if g.get('output') == 'ranking' or g.get('group_by') == 'merchant':
            if g.get('consumption'):
                return _failure('query_scope_unsupported','现有报告按已记录支出统计，可能包含转账，不能保证购买消费排行口径；请明确是否改查已记录支出排行。')
            if (g.get('group_by') != 'merchant' or g.get('sort') != 'desc' or
                not isinstance(g.get('limit'),int) or not 1 <= g['limit'] <= 20 or
                g.get('metrics') != ['expense']):
                return _failure('query_scope_unsupported','当前仅支持已记录支出按商户降序的有限排行（1至20项），不支持该分组、指标、排序或数量目标；')
            for month in target_months:
                if not any(n == 'get_monthly_cash_flow_report' and a.get('month') == month and a.get('top_n',10) == g['limit'] for n,a in matching):
                    return _failure('query_scope_mismatch','计划未保留同一月份的商户排行、降序及指定数量；')
        for month in target_months:
            available = set().union(*(CAPABILITIES[n] for n,a in matching if a.get('month') == month))
            if not set(g.get('metrics',[g['metric']])).issubset(available):
                return _failure('query_scope_mismatch','计划未保留同一月份、银行卡范围的全部统计指标或明细；')
            if g.get('requires_clues') and not any(n == 'get_expense_clues' and a.get('month') == month for n,a in matching):
                return _failure('query_scope_mismatch','计划未保留同一期间及账户的原因线索分析；月度统计摘要不能替代原因线索。')
    if any(not any(a.get('month') in g['months'] and a.get('card_id') == g.get('card_id') for g in goals) for _,a in bill):
        return _failure('query_scope_mismatch','计划夹带请求范围外的月份或银行卡；')
    by_id = {s.step_id:s for s in plan_steps or []}
    used_actions = set()
    for wanted in requirements.get('conditions',[]):
        covered = False
        for step in plan_steps or []:
            c = step.condition
            source = by_id.get(c.source_step_id) if c else None
            fields = ({'expense':'data.total_spending'} if source and source.tool_name == 'get_monthly_spending' else
                {'expense':'data.totals.expense','income':'data.totals.income','cash_flow':'data.totals.net_cash_flow'}
                if source and source.tool_name == 'get_monthly_cash_flow_report' else {})
            if (source and step.step_id not in used_actions and _same_action(wanted.get('action'),step) and
                source.arguments.get('month') in wanted['months'] and
                source.arguments.get('card_id') == wanted['card_id'] and
                c.field_path == fields.get(wanted['metric']) and c.operator == wanted['operator'] and
                isinstance(c.value,(int,float)) and not isinstance(c.value,bool) and
                Decimal(str(c.value)) == Decimal(wanted['value'])):
                covered = True
                used_actions.add(step.step_id)
                break
        if not covered:
            return _failure('query_scope_mismatch','计划未保留对应动作、对象、参数及其条件来源、指标、比较符或阈值；')
    if requirements.get('conditions') and any(s.tool_name in WRITE_TOOLS and s.step_id not in used_actions for s in plan_steps or []):
        return _failure('query_scope_mismatch','条件任务计划夹带未绑定条件的动作或重复动作；')
    return None
