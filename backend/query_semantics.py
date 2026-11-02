"""Server-derived, bounded query requirements; never model evidence or authority.

This is a coverage gate over the existing DSL, not a natural-language compiler.
It reads only the utterance, owner-scoped vocabulary and live QueryPatch state.
Candidate conflicts are distinct from ambiguity and unsupported user goals.
"""
from dataclasses import dataclass, field, asdict
from decimal import Decimal
import re

from data_query import QueryBatch, Selection, problem


@dataclass
class QueryRequirement:
    goals: list = field(default_factory=list)
    certainty: str = 'VALID'
    message: str = ''
    comparison: bool = False
    inherited: bool = False

    @property
    def goal_count(self):return len(self.goals)

    def diagnostic(self):return dict(**asdict(self),goal_count=self.goal_count)


AMOUNT_WORDS=r'大于等于|小于等于|不超过|不高于|不少于|不低于|至少|至多|超过|高于|大于|低于|小于|少于|等于'
AMOUNT_OP={'大于等于':'gte','小于等于':'lte','不超过':'lte','不高于':'lte','不少于':'gte','不低于':'gte',
    '至少':'gte','至多':'lte','超过':'gt','高于':'gt','大于':'gt','低于':'lt','小于':'lt','少于':'lt','等于':'eq'}

def amounts(text):
    result=[]
    for m in re.finditer('('+AMOUNT_WORDS+r')\s*(\d+(?:\.\d{1,2})?)(?![\d.eE,千万亿])\s*(?:元|块)?',text):
        result.append(dict(operator=AMOUNT_OP[m[1]],cents=int(Decimal(m[2])*100)))
    for m in re.finditer(r'(\d+(?:\.\d{1,2})?)\s*元?\s*(?:到|至|~|～)\s*(\d+(?:\.\d{1,2})?)\s*元',text):
        result.extend([dict(operator='gte',cents=int(Decimal(m[1])*100)),dict(operator='lte',cents=int(Decimal(m[2])*100))])
    return result


def presentation(text,catalog):
    # All recognized data names are masked, even if a candidate omitted them.
    from conversation import _mentioned
    for value in _mentioned(text,catalog['merchants']+[a['alias'] for a in catalog['aliases']]+catalog['categories']):
        text=text.replace(value,'指定对象')
    return text


def catalog_subjects(text,catalog):
    """Longest controlled merchant literals protect embedded category words."""
    from conversation import _mentioned
    names=_mentioned(text,catalog['merchants']+[a['alias'] for a in catalog['aliases']])
    category_text=text
    for name in names:
        if name not in catalog['categories']:category_text=category_text.replace(name,' ')
    cats=_mentioned(category_text,catalog['categories'])
    for name in set(names)&set(cats):
        merchant=bool(re.search(r'(?:商户|商家)[：:\s]*'+re.escape(name),text))
        category=bool(re.search(r'(?:分类|类别)[：:\s]*'+re.escape(name)+'|'+re.escape(name)+r'类',text))
        if merchant and not category:cats.remove(name)
        elif category and not merchant:names.remove(name)
    return names,cats


def display(text):
    """Explicit display requirements only; absent differs from CLEAR (None)."""
    out={};metrics=[]
    negative=bool(re.search(r'不要明细|不看明细|不(?:用|要)每(?:一)?笔|别列|不要列|别展开',text))
    detail_text=re.sub(r'平均每(?:一)?笔|平均单笔','均值',text)
    detail=not negative and bool(re.search(r'明细|每(?:一)?笔|逐笔|全部.*(?:流水|记录)|具体买了什么|商品名称|(?:前\s*\d+|最高|最低)(?:的)?\s*(?:\d+|[一二两三四五六七八九十])?\s*笔|列.*(?:消费|记录|超过|高于|低于)',detail_text))
    if detail:metrics.append('details')
    if re.search(r'花(?:了)?多少(?:钱)?|消费(?:是)?多少|支出(?:有)?多少|总额|总金额|合计|总共多少|一共多少',text) and not re.search(r'不要只?给?总额|别汇总|不要总额|总金额改成',text):metrics.append('sum')
    if re.search(r'几笔|多少笔|笔数|消费次数',text):metrics.append('count')
    if re.search(r'平均|均价|单笔均',text):metrics.append('average')
    if re.search(r'净额|(?:购买|消费)(?:额)?减(?:去)?退款|扣除退款',text):metrics=['net']+[m for m in metrics if m not in {'sum','net'}]
    groups=[]
    for pat,name in [(r'每个?月|逐月|按月(?:份)?|月份分别','month'),(r'每天|逐日|按天|按日(?!期)','day'),
        (r'按(?:商户|商家)|(?:排列|排名|排行).*商[户家]|商[户家].*(?:排名|排行)|前\s*\d+\s*(?:个|家)商[户家]','merchant'),
        (r'按(?:分类|类别)|(?:分类|类别).*(?:排名|排行)','category')]:
        if re.search(pat,text):groups.append(name)
    if len(set(groups))>1:out['ambiguous']='你想按月份、日期、商户还是分类分组？当前一次分组只能选择一个维度。'
    elif groups:out['group_by']=groups[0]
    if re.search(r'不(?:要|用)?分组|取消分组',text):out['group_by']=None
    sort_context=bool(re.search(r'排序|排列|排名|排行|升序|降序|最高|最低|最多|最少|前\s*\d+|按(?:消费)?(?:金额|笔数|次数|平均|日期|时间)',text))
    sorts=[]
    if sort_context:
        if re.search(r'平均|均价',text):sorts.append('average')
        if re.search(r'笔数|次数|几笔',text):sorts.append('count')
        if re.search(r'金额|消费额',text) and not re.search(r'平均(?:每笔)?(?:消费)?金额',text):sorts.append('amount')
        if re.search(r'按日期|按时间|从早到晚|从晚到早',text):sorts.append('date')
    if len(set(sorts))>1:out['ambiguous']='你想优先按金额、消费笔数还是平均每笔金额排序？请指定一种排序依据。'
    elif sorts:out['sort_by']=sorts[0]
    if sort_context and not detail and re.search(r'商户|商家',text) and 'group_by' not in out:out['group_by']='merchant'
    if 'sort_by' in out and out['sort_by'] in {'count','average'} and out.get('group_by'):
        metric={'count':'count','average':'average'}[out['sort_by']]
        if metric not in metrics:metrics.append(metric)
    elif out.get('sort_by')=='amount' and out.get('group_by') and not metrics:metrics.append('sum')
    if re.search(r'升序|(?:从)?低到高|(?:从)?小到大|(?:从)?少到多|(?:从)?早到晚|最低|最少',text):out['order']='asc'
    if re.search(r'降序|(?:从)?高到低|(?:从)?大到小|(?:从)?多到少|(?:从)?晚到早|最高|最多',text):
        if 'order' in out:out['ambiguous']='你想升序还是降序排列？请指定一个方向。'
        out['order']='desc'
    m=re.search(r'(?:前|Top\s*|(?:最高|最低)(?:的)?)\s*(\d+|[一二两三四五六七八九十百]+)',text,re.I) or re.search(r'(\d+|[一二两三四五六七八九十百]+)\s*(?:个|家)商[户家]',text)
    if m:
        from agent.query_requirements import _number
        limit=100 if m[1]=='一百' else _number(m[1])
        if limit is None:out['unsupported']='无法确认展示数量，请使用1至100的整数。本次未执行。'
        else:out['limit']=limit
        if re.search(r'(?:个|家)商[户家]',text):out.setdefault('group_by','merchant')
    elif re.search(r'(?:最高|最低)(?:的)?一笔',text):out['limit']=1;out['sort_by']='amount'
    if metrics:out['metrics']=list(dict.fromkeys(metrics))
    if detail and out.get('sort_by')=='amount':out['time_mode']='period'
    if re.search(r'退款多少',text) and not metrics:out['metrics']=['sum']
    if negative:out['no_details']=True
    return out


def extract(text,session,catalog,today,continuation=False,compare_recent=False):
    from conversation import _mentioned,_periods
    from dialogue_state import correction_text
    req=QueryRequirement(inherited=continuation)
    text=correction_text(text)
    pending=getattr(session,'conversation_question',None) or {}
    previous=getattr(session,'conversation_query',None) or pending.get('query')
    if continuation and not previous and pending.get('request'):text=pending['request']+'，'+text
    old=QueryBatch.model_validate(previous) if previous and continuation else None
    if compare_recent and re.search(r'哪个更|哪家更|两家|环比|比较|对比|相比',presentation(text,catalog)):
        recent=getattr(session,'conversation_queries',[])
        if recent:old=QueryBatch.model_validate(recent[-1])
    pt=presentation(text,catalog)
    if re.search(r'偏移|跳过\s*(?:\d+|[一二两三四五六七八九十百]+)\s*(?:笔|条)|第\s*(?:\d+|[一二两三四五六七八九十百]+)\s*页',pt):
        req.certainty='UNSUPPORTED';req.message='当前聊天支持按原范围逐页查看，请先查明细再说“下一页”。本次没有猜测页号或偏移，也未执行查询。';return req
    malformed_money='(?:'+AMOUNT_WORDS+r')\s*(?:[-+−负]\s*\d|\d+(?:\.\d+)?\s*[千万亿]|\d+[eE][+-]?\d+|\d+,\d+|\d+\.\d{3,})'
    unit_money=r'(?:\d+\.\d{3,}|\d+[eE][+-]?\d+|\d+,\d+)\s*(?:元|块|到|至|~|～)'
    if re.search(malformed_money,pt) or re.search(unit_money,pt) or re.search(r'美元|美金|欧元|港元|港币|日元|\b(?:USD|EUR|HKD|JPY)\b|[$€]',pt,re.I):
        req.certainty='UNSUPPORTED';req.message='当前金额条件只支持人民币普通数字、最多两位小数，不支持负数、科学计数、千位分隔、万/亿单位或外币。本次整条未执行，请改写为准确的元金额。';return req
    if re.search('('+AMOUNT_WORDS+r')\s*[一二两三四五六七八九十百千万元]+',pt):
        req.certainty='UNSUPPORTED';req.message='当前金额边界需要阿拉伯数字，可以说“超过50元”。本次未执行，也没有忽略金额条件。';return req
    if re.search(r'银行',pt) and re.search(r'上传',pt) and re.search(r'合并|一起算|合起来|加起来',pt):
        req.certainty='UNSUPPORTED';req.message='银行流水和上传账单尚无可靠的跨来源去重依据，可以分别查询，不能相加作为完整消费。';return req
    unsupported=r'中位数|百分位|分位数|每(?:个|家)?商户.*(?:前\d+笔|最高.*笔)|(?:分组|各组)内|嵌套|(?:或者|或是|或)(?:单笔|金额)?(?:超过|大于|低于|小于)|按(?:地区|城市|小时|商品)(?:分组|统计|汇总)?|(?:最近|最新)\s*(?:\d+|[一二三四五六七八九十]+)\s*笔.*(?:合计|总额|总共|平均)|未知类型'
    if re.search(unsupported,pt):
        req.certainty='UNSUPPORTED';req.message='当前查询不支持中位数、分组内排名、嵌套条件、未知类型统计或最近N笔的汇总。本次整条未执行；可以指定期间查询明细或支持的汇总指标。';return req
    names,cats=catalog_subjects(text,catalog)
    if not cats and not names:
        # Bounded subject syntax only. An unresolved object in this supported
        # form is not permission to silently query every merchant/category.
        subject=re.search(r'(?:\d{1,2}月|本月|上月|这个月|上个月)(?:在)?([^，,。；;？！?\d]{2,24}?)(?:的)?(?:花(?:了)?多少|消费|支出)',text)
        if subject and not re.search(r'按|每|逐|所有|全部|银行|上传|账单|总|平均|合计|卡|购买|退款|转账|理财|分类|日期|金额|最近|最新',subject[1]):
            req.certainty='AMBIGUOUS';req.message='没有在受控商户或分类目录中确认“'+subject[1]+'”，请核对对象名称或说明要查商户还是分类。本次未扩大范围。';return req
    for cat in cats:
        if re.search(re.escape(cat)+r'(?:类)?(?:以外|之外)|(?:排除|不含|除了)\s*'+re.escape(cat),text):
            req.certainty='UNSUPPORTED';req.message='当前查询不支持排除分类。本次整条未执行；可以指定要包含的分类分别查询。';return req
        if cat in names and (text.count(cat)==1 or not re.search(r'分类|类别|'+re.escape(cat)+'类',text)):
            req.certainty='AMBIGUOUS';req.message=f'“{cat}”同时是商户和分类，你想查哪一种？';return req
    disp=display(pt)
    if re.search(r'上传|这张图|这张账单',pt) and not re.search(r'银行(?:流水|账单)',pt) and (cats or disp.get('group_by')=='category' or re.search(r'尾号|银行卡|\d+号卡',pt)):
        req.certainty='UNSUPPORTED';req.message='上传账单没有可核实的银行分类或银行卡字段，不能按这些条件筛选或分组。本次整条未执行；可以按商户、日期或金额查询。';return req
    if disp.get('unsupported'):
        req.certainty='UNSUPPORTED';req.message=disp['unsupported'];return req
    recent=re.search(r'(最近|最新|最后)(?:的)?\s*(\d+|[一二两三四五六七八九十百]+)?\s*笔',pt)
    if recent:
        from agent.query_requirements import _number
        n=100 if recent[2]=='一百' else _number(recent[2]) if recent[2] else 1
        if n is None:
            req.certainty='AMBIGUOUS';req.message='想看最近多少笔？请使用1至100的整数。';return req
        if disp.get('group_by') or disp.get('sort_by') not in {None,'date'} or n>1 and set(disp.get('metrics',[])) & {'sum','count','average','net'}:
            req.certainty='UNSUPPORTED';req.message='最近N笔目前仅支持按日期查看交易明细，不能对最近集合再汇总、分组或按金额重排。本次整条未执行；可以指定期间后查询汇总或金额排名。';return req
        disp.update(time_mode='latest' if n==1 else 'recent_records',metrics=['details'],sort_by='date',order='desc',limit=n)
    elif re.search(r'最近.*(?:买|消费|购物).*(?:什么|哪些|看看)',pt):disp.update(time_mode='recent_records',metrics=['details'],sort_by='date',order='desc')
    if disp.get('ambiguous'):
        req.certainty='AMBIGUOUS';req.message=disp['ambiguous'];return req
    if disp.get('limit',1)>100:
        req.certainty='UNSUPPORTED';req.message='单次最多展示100条；本次未执行，可以每页100条继续翻页。';return req
    if disp.get('group_by')=='merchant' and names and re.search(r'排序|排列|排名|排行|最多|最高|前\s*\d+|降序|升序',pt) and not re.search(r'所有|全部|继续只看原商户',text):
        req.certainty='AMBIGUOUS';req.message='你想继续只看'+ '、'.join(names)+'，还是查看该期间所有商户的消费排行？';return req
    periods=_periods(text,today)
    if continuation and re.search(r'(?:以|之)后',pt):
        from datetime import date,timedelta
        from data_query import Period
        anchor=(getattr(session,'conversation_observation',None) or {}).get('day')
        if anchor:
            day=date.fromisoformat(anchor);m=re.search(r'(\d{1,2})(?:号|日)(?:以|之)后',pt)
            if m:day=day.replace(day=int(m[1]))
            business=getattr(session,'conversation_business_date',today)
            if day<business:
                periods=[Period(start=day+timedelta(days=1),end_exclusive=business+timedelta(days=1))]
                disp.update(time_mode='period',sort_by='date')
    if any(p.start>getattr(session,'conversation_business_date',today) for p in periods):
        req.certainty='UNSUPPORTED';req.message='所选期间尚未发生，本次未执行；可以查询截至今天已发生的记录。';return req
    req.comparison=bool(re.search(r'哪个更|哪家更|两家|环比|比较|对比|相比',pt)) and not disp.get('group_by')
    if req.comparison and re.search(r'环比|比上月|与上月|和上月',pt):
        from datetime import timedelta
        from data_query import Period
        current_periods=_periods(re.sub(r'上(?:个)?月','',text),today)
        current=current_periods[0] if current_periods else periods[0] if periods else old.plans[0].selection.period if old else None
        if current:
            last=(current.start.replace(day=1)-timedelta(days=1)).replace(day=1)
            periods=[current,Period(start=last,end_exclusive=current.start.replace(day=1))]
    # Independent subject clauses. Amounts are bound by subject positions, not
    # membership in a batch-wide bag of literals. Trailing shared metrics apply
    # to all goals only with an explicit distributive expression.
    exclusions=[n for n in names if re.search(r'(?:排除|不含|不要|除了|除去|不算)\s*'+re.escape(n),text)]
    includes=[n for n in names if n not in exclusions]
    separate=bool(re.search(r'分别|各自|各(?:花|有|多少)|再查|消费多少[，,]|花.*[，,].*花',pt))
    subjects=[('merchants',n) for n in includes]
    if cats:subjects += [('categories',c) for c in cats if c not in includes or re.search(r'分类|类别|'+re.escape(c)+'类',text)]
    if len(subjects)>1 and (separate or req.comparison):
        ordered=sorted(subjects,key=lambda x:text.index(x[1]))
    else:ordered=[None]
    source_separate=bool(re.search(r'银行',text) and re.search(r'上传',text) and re.search(r'分别|各自',text))
    if source_separate:ordered=[('source','bank'),('source','uploaded')]
    if len(periods)>1 and req.comparison and len(ordered)==1:ordered=[('period',p.model_dump(mode='json')) for p in periods]
    for i,sub in enumerate(ordered):
        base=old.plans[i].selection.model_dump(mode='json') if old and i<len(old.plans) else Selection().model_dump(mode='json')
        explicit=[]
        source='uploaded' if re.search(r'上传|这张图|这张账单|识别.*账单|删除.*账单|删除.*那张',text) else 'bank'
        if not old or re.search(r'上传|银行',text):base['source']=source;explicit.append('source')
        if source_separate:base['source']=sub[1]
        if base['source']=='uploaded':
            refs=getattr(session,'billvision_documents',[])
            ids=re.findall(r'bv_[0-9a-f]{32}',text)
            if ids:
                if len(set(ids))!=1:
                    req.certainty='AMBIGUOUS';req.message='请明确要查询哪一份上传账单。本次未执行。';return req
                base.update(document_id=ids[0],current_document=False);explicit+=['document_id','current_document']
            elif re.search(r'这张|那张|刚.*上传',text):
                base.update(current_document=True,document_id=refs[0] if len(refs)==1 else None);explicit+=['document_id','current_document']
            elif base['current_document'] and base['document_id'] is None and len(refs)==1:
                base['document_id']=refs[0]
        if periods:base['period']=periods[0].model_dump(mode='json');explicit.append('period')
        if sub and sub[0]=='period':base['period']=sub[1]
        if includes:base['merchants']=includes;explicit.append('merchants')
        if exclusions:base['exclude_merchants']=exclusions;explicit.append('exclude_merchants')
        if re.search(r'(?:所有|全部)(?:的)?商[户家]',text):base['merchants']=[];explicit.append('merchants')
        if cats:base['categories']=cats;explicit.append('categories')
        if sub and sub[0] in {'merchants','categories'}:base[sub[0]]=[sub[1]]
        clause=text
        if sub and sub[0] in {'merchants','categories'} and len(ordered)>1:
            start=text.index(sub[1]);end=text.index(ordered[i+1][1]) if i+1<len(ordered) else len(text)
            clause=text[start:end]
        amt=amounts(clause)
        if amt:base['amounts']=amt;explicit.append('amounts')
        elif re.search(r'取消.*金额.*限制|不.*金额.*限制',text):base['amounts']=[];explicit.append('amounts')
        card=re.search(r'(?:尾号|末四位)\s*(\d{4})',text)
        if card:base.update(card_tail=card[1],card_id=None);explicit+=['card_tail','card_id']
        card_id=re.search(r'(?<!\d)(\d+)\s*号(?:银行卡|卡)',text)
        if card_id and not re.search(r'(?:给|向).*(?:转账|转)',text):base.update(card_id=int(card_id[1]),card_tail=None);explicit+=['card_id','card_tail']
        if re.search(r'退款',pt) and not re.search(r'减|净额|扣除|分开|另|单列|单独',pt) and not re.search(r'购买|消费|花',pt):base.update(kinds=['refund'],direction=None);explicit.append('kinds')
        elif re.search(r'净额|减(?:去)?退款|扣除退款',pt):base.update(kinds=[],direction=None);explicit+=['kinds','direction']
        elif re.search(r'购买|消费|购物|花',pt) and not re.search(r'现金流|总流出|所有支出|转账|理财',pt):base.update(kinds=[],direction=None);explicit+=['kinds','direction']
        for pat,kind in [(r'转账(?:记录|流水|多少|明细)','transfer'),(r'理财买入|理财申购','wealth_buy'),(r'理财赎回','wealth_redeem')]:
            if re.search(pat,pt) and not re.search(r'(?:给|向).*(?:转账|转)',pt):base.update(kinds=[kind],direction=None);explicit.append('kinds')
        if re.search(r'现金流出|总流出|所有支出',pt):
            base.update(kinds=['purchase','transfer','wealth_buy'],direction='expense');explicit+=['kinds','direction']
        if re.search(r'收入',pt) and not re.search(r'退款|理财',pt):base['direction']='income';explicit.append('direction')
        for pat,status in [(r'成功|已完成','success'),(r'待处理|处理中','pending'),(r'失败','failed'),(r'关闭','closed')]:
            if re.search(pat,pt):base['statuses']=[status];explicit.append('statuses')
        view=dict(disp)
        view.setdefault('time_mode',old.plans[i].time_mode if old and i<len(old.plans) else 'period')
        if view.get('group_by'):view['time_mode']='period'
        if old and i<len(old.plans):
            p=old.plans[i]
            for key in ['metrics','group_by','sort_by','order','limit']:
                if key not in view:view[key]=getattr(p,key)
            if disp.get('group_by') in {'day','month'} and disp['group_by']!=p.group_by:
                for key in ('sort_by','order'):
                    if key not in disp:view.pop(key,None)
            if continuation and re.search(r'(?:以|之)后',pt) and p.time_mode!='period' and 'limit' not in disp:
                view.pop('limit',None)
            if 'details' in disp.get('metrics',[]) and not disp.get('group_by'):
                view['group_by']=None
                from dialogue_state import detail_goal
                if 'sort_by' not in disp and detail_goal(pt):view['sort_by']='date'
                if 'details' not in p.metrics:
                    if 'limit' not in disp:view.pop('limit',None)
                    if 'order' not in disp:view['order']='desc'
        # Scoped metrics override global cues unless explicitly shared.
        if sub and sub[0] in {'merchants','categories'} and not re.search(r'各自|分别',pt):
            local=display(presentation(clause,catalog))
            if local.get('metrics'):view['metrics']=local['metrics']
        req.goals.append(dict(selection=base,display=view,explicit=explicit))
    if len(req.goals)>4:
        req.certainty='UNSUPPORTED';req.message='一次最多支持4个查询计划；本次整条未执行，请缩小目标数量。'
    return req


def validate(req,batch,catalog,*,compare_recent=False,raw_ledger=False):
    """Return field conflicts, without relabelling candidate errors unsupported."""
    from conversation import _same_merchant
    if req.certainty!='VALID':return []
    actual=[(p,p.selection) for p in batch.plans]
    actual += [(p,p.comparison) for p in batch.plans if p.comparison]
    conflicts=[];used=set()
    if not req.comparison and (compare_recent or any(p.comparison for p in batch.plans)):
        conflicts.append(dict(field='comparison',expected=False))
    for i,goal in enumerate(req.goals):
        expected=goal['selection'];view=goal['display']
        matches=[];differences=[]
        for n,(p,s) in enumerate(actual):
            if n in used:continue
            missing=[]
            for key in ['source','period','merchants','exclude_merchants','categories','amounts','card_id','card_tail','direction','kinds','statuses']:
                a=s.model_dump(mode='json')[key];b=expected[key]
                if raw_ledger and key in {'direction','kinds'}:continue
                if compare_recent and key=='merchants':continue
                if key in {'merchants','exclude_merchants'}:
                    equal=_same_merchant(a,b,catalog)
                elif key=='kinds' and set(a)<= {'purchase'} and not b:equal=True
                elif key=='direction' and a=='expense' and b is None and s.kinds==['purchase'] and not expected['kinds']:equal=True
                elif key=='amounts':equal=sorted(a,key=lambda v:(v['operator'],v['cents']))==sorted(b,key=lambda v:(v['operator'],v['cents']))
                elif isinstance(a,list):equal=set(a)==set(b)
                else:equal=a==b
                if not equal:missing.append(key)
            if expected['current_document']:
                wanted=expected['document_id']
                reference=(s.current_document and s.document_id in {None,wanted}) or (wanted is not None and s.document_id==wanted)
            else:reference=not s.current_document and s.document_id==expected['document_id']
            if not reference:missing.append('document_id')
            differences.append(missing)
            if not missing:matches.append(n)
        if not matches:
            fields=min(differences,key=len) if differences else ['goal_count']
            conflicts.extend(dict(goal=i,field='selection.'+key,expected=expected.get(key)) for key in fields)
            continue
        n=matches[0];used.add(n);p,_=actual[n]
        for key in ['metrics','group_by','sort_by','order','limit','time_mode']:
            if key not in view:continue
            value=getattr(p,key);wanted=view[key]
            equal=set(wanted)<=set(value) if key=='metrics' else value==wanted
            if not equal:conflicts.append(dict(goal=i,field=key,expected=wanted))
        if view.get('no_details') and 'details' in p.metrics:conflicts.append(dict(goal=i,field='metrics',expected='no_details'))
        if 'net' in p.metrics and 'net' not in view.get('metrics',[]):conflicts.append(dict(goal=i,field='metrics',expected='no_unrequested_net'))
        if not raw_ledger and not compare_recent and 'group_by' not in view and p.group_by is not None:conflicts.append(dict(goal=i,field='group_by',expected=None))
    # Comparisons carry two selections for one logical goal. The existing literal
    # guard verifies previous-period and recent-merchant references precisely.
    if req.comparison or compare_recent:
        if not any(p.comparison for p in batch.plans):conflicts.append(dict(field='comparison',expected=True))
        if not compare_recent and len(actual)!=max(2,len(req.goals)):conflicts.append(dict(field='goal_count',expected=max(2,len(req.goals))))
    elif len(actual)!=len(req.goals):conflicts.append(dict(field='goal_count',expected=len(req.goals)))
    return conflicts


def enforce(text,control,session,catalog,today,raw_ledger=False):
    req=capture(text,control,session,catalog,today)
    conflicts=validate(req,control.query,catalog,compare_recent=control.compare_recent_merchants,raw_ledger=raw_ledger)
    session.conversation_metrics['semantic_validation']=dict(status='CONFLICT' if conflicts else 'VALID',
        conflicts=[dict(goal=c.get('goal'),field=c['field']) for c in conflicts])
    if conflicts:
        labels={'metrics':'统计指标','sort_by':'排序依据','order':'排序方向','group_by':'分组方式','limit':'展示数量',
            'goal_count':'查询目标数量','comparison':'比较目标','source':'数据来源','period':'期间','merchants':'商户',
            'exclude_merchants':'排除商户','categories':'分类','amounts':'金额边界','card_id':'银行卡','card_tail':'银行卡',
            'direction':'收支方向','kinds':'交易口径','statuses':'记录状态','time_mode':'时间模式'}
        fields='、'.join(dict.fromkeys(labels.get(c['field'].split('.')[-1],'筛选范围') for c in conflicts))
        session.conversation_metrics['replan_requirements']=session.conversation_metrics['requirement']['goals']
        problem('query_scope_mismatch','这次查询未能准确保留'+fields+'，尚未执行，也没有改查其他范围。请重试原问题。')
    return req


def capture(text,control,session,catalog,today):
    """Extract before QueryPatch too, so rejected candidates retain requirements."""
    req=extract(text,session,catalog,today,control.continuation,control.compare_recent_merchants)
    explicit_deleted=bool(re.search(r'(?:刚才|刚|已经|已)?删除.*(?:账单|那张|这张)',text))
    ref=getattr(session,'billvision_deleted_reference' if explicit_deleted else 'billvision_reference',None) or {}
    ids=re.findall(r'bv_[0-9a-f]{32}',text)
    bound=(ref.get('session_id')==getattr(session,'session_id',None) and
        ref.get('principal_id')==getattr(getattr(session,'principal',None),'principal_id',None))
    retired=bool(bound and ref.get('retired') and ref.get('object_id') and
        (not ids or ids==[ref['object_id']]) and
        (explicit_deleted or not getattr(session,'billvision_documents',[]) and
         re.search(r'这张|那张|刚.*上传',text) and re.search(r'上传|账单',text)))
    if retired:
        req.certainty='UNSUPPORTED';req.message='该账单已删除、引用失效或不属于当前账户，请重新选择。'
    diagnostic=req.diagnostic()
    # Diagnostics use the same redaction as candidate plans; never log card refs.
    from dialogue_state import diagnostic_plan
    diagnostic['goals']=[dict(g,selection=diagnostic_plan(dict(plans=[dict(selection=g['selection'])]))['plans'][0]['selection']) for g in diagnostic['goals']]
    session.conversation_metrics['requirement']=diagnostic
    if req.certainty!='VALID':
        session.conversation_metrics['semantic_validation']=dict(status=req.certainty,conflicts=[])
        if req.certainty=='AMBIGUOUS' and '继续只看' in req.message:
            import time
            from conversation import _mentioned
            session.conversation_question=dict(kind='merchant_rank',
                target_revision=getattr(session,'conversation_active_revision',None),created_at=time.time(),
                request=text,question=req.message,query=None,
                explicit_subjects=catalog_subjects(text,catalog)[0])
        code='document_unavailable' if retired else 'sources_cannot_merge' if '跨来源去重' in req.message else 'query_requirement_'+('ambiguous' if req.certainty=='AMBIGUOUS' else 'unsupported')
        problem(code,req.message)
    return req
