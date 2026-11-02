"""Bounded PG04 request grammar. Unparsed wealth constraints fail closed."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

from storage.config import PG04_WEALTH_TOOLS

MONEY=r'[0-9]+(?:\.[0-9]{1,2})?'


def extract(text, previous=None, *, bound_date):
    prior=previous.get('wealth') if previous else None
    probe=re.sub(r'[“"「].*?[”"」]','',text)
    probe=re.sub(r'备注(?:为|是|：|:).+?(?=[，。；;]|$)','',probe)
    if not re.search(r'理财|产品|持仓|风险等级|赎回',probe) and not prior:
        return None,text
    # Parameter corrections retain the rest of the original request contract.
    correction=re.fullmatch(r'(?:请)?(?:把)?(金额|本金|产品|持仓|付款卡|扣款卡)(?:改为|改成|调整为)([0-9]+(?:\.[0-9]{1,2})?)(?:元|号卡)?[。]?',text.strip())
    if prior and correction:
        value=deepcopy(prior); field={'金额':'amount','本金':'amount','产品':'product_id','持仓':'holding_id','付款卡':'card_id','扣款卡':'card_id'}[correction[1]]
        writes=[a for n,a in value['steps'] if n in {'buy_wealth_product','redeem_wealth_product'}]
        if len(writes)!=1 or field not in writes[0]: value['unclear']=True
        elif field!='amount' and re.fullmatch(r'[1-9][0-9]*',correction[2]) is None: value['unclear']=True
        else: writes[0][field]=correction[2] if field=='amount' else int(correction[2])
        return value,value.get('base_text','')
    if prior and re.search(r'金额|本金|付款卡|扣款卡',text) and re.search(r'改|调整',text):
        value=deepcopy(prior);value['unclear']=True
        return value,value.get('base_text','')
    if not re.search(r'理财|产品|持仓|风险等级|赎回',probe):
        return None,text
    steps=[]; base=[]; order=[]; unclear=False;conditions=[];pending=None
    parts=re.split(r'[，,；;。]|然后|完成后|之后|再(?=查|购|买|赎)',text)
    for part in parts:
        p=re.sub(r'^(?:请|麻烦|帮我|先|再)+','',part.strip()).strip()
        if not p: continue
        original=p
        if re.match(r'^(?:本轮|这轮|也)?(?:不需要|不要|不用|不必|不)(?:购买|申购|买入|买|赎回|查询|查看|查|比较|对比|解释)',p):
            continue
        p=re.sub(r'查一下','查询',p)
        conditional=re.fullmatch(r'(?:如果|若)(.*?)(不超过|不少于|大于|超过|高于|不足|少于|小于|低于|至少)('+MONEY+r')(?:元)?(?:时|的话)?',p)
        if conditional:
            from .query_requirements import _scope,OPERATORS
            source=_scope(conditional[1],bound_date)
            if (pending is not None or len(source['months'])!=1 or len(source['metrics'])!=1 or
                source['metric'] not in {'expense','income','cash_flow'} or source['merchant'] or source['ambiguous_scope'] or
                source['unsupported_filter'] or source['unsupported_period'] or source['comparison']):
                unclear=True
            pending=dict(months=source['months'],card_id=source['card_id'],metric=source['metric'],operator=OPERATORS[conditional[2]],value=conditional[3])
            base.append(conditional[1]);order.append('other')
            continue
        item=None
        if re.fullmatch(r'(?:查询|查看|查)(?:我的|个人|用户)?风险等级',p):
            item=('get_risk_profile',{})
        elif re.fullmatch(r'(?:查询|查看|查)(?:我的|个人|理财)?持仓',p):
            item=('get_wealth_holdings',{})
        elif (m:=re.fullmatch(r'(?:查询|查看|查)(R[1-5])?(适合我|适配)?(在售|停用)?(?:理财)?产品',p)):
            item=('get_wealth_products',dict(risk_level=m[1],suitable_only=bool(m[2]),status={'在售':'active','停用':'inactive'}.get(m[3])))
        elif re.fullmatch(r'(?:比较|对比)(?:产品[1-9][0-9]*)(?:(?:和|与|、)产品[1-9][0-9]*){1,4}',p):
            item=('compare_wealth_products',dict(product_ids=[int(x) for x in re.findall(r'产品([1-9][0-9]*)',p)]))
        elif (m:=re.fullmatch(r'(?:解释|查询|查看)(?:用([1-9][0-9]*)号卡)?产品([1-9][0-9]*)(?:购买('+MONEY+r')元)?(?:的)?适当性',p)):
            item=('explain_wealth_suitability',dict(product_id=int(m[2]),card_id=int(m[1]) if m[1] else None,amount=m[3]))
        elif (m:=re.fullmatch(r'(?:从|用)([1-9][0-9]*)号卡(?:购买|买入|申购)(?:理财)?产品([1-9][0-9]*)\s*('+MONEY+r')元',p)):
            item=('buy_wealth_product',dict(card_id=int(m[1]),product_id=int(m[2]),amount=m[3]))
        elif (m:=re.fullmatch(r'赎回(?:理财)?持仓([1-9][0-9]*)\s*('+MONEY+r')元',p)):
            item=('redeem_wealth_product',dict(holding_id=int(m[1]),amount=m[2]))
        if item:
            if pending:
                if item[0] not in {'buy_wealth_product','redeem_wealth_product'}: unclear=True
                conditions.append(dict(pending,index=len(steps)));pending=None
            steps.append(item); order.append('wealth')
        elif re.search(r'理财|产品|持仓|风险等级|赎回',original):
            from .query_requirements import TIME,METRIC
            if TIME.search(original) and METRIC.search(original):
                # A recorded merchant may share a product name. The bill parser
                # must resolve it from its own ledger, never from this catalog.
                base.append(original);order.append('other')
            else: unclear=True
        else:
            base.append(original); order.append('other')
    if not steps and not unclear: return None,'，'.join(base)
    return dict(steps=steps,unclear=unclear or not steps or pending is not None,base_text='，'.join(base),base_parts=base,
                bound_date=bound_date,order=order,conditions=conditions), '，'.join(base)


def check(wanted, steps, plan_steps):
    if wanted.get('unclear'):
        return 'query_scope_unclear','请明确产品/持仓编号、付款卡、金额及步骤；当前表达或条件未覆盖，尚未执行。'
    actual=[(n,a) for n,a in steps if n in PG04_WEALTH_TOOLS]
    if len(actual)!=len(wanted['steps']):
        return 'query_scope_mismatch','计划遗漏理财目标或夹带未请求的理财动作，尚未执行。'
    for (name,args),(expected,values) in zip(actual,wanted['steps']):
        if name!=expected: return 'query_scope_mismatch','理财动作与用户请求不符，尚未执行。'
        defaults={'get_wealth_products':dict(risk_level=None,status=None,suitable_only=False),
                  'explain_wealth_suitability':dict(card_id=None,amount=None)}.get(name,{})
        normalized=dict(defaults,**args)
        if set(normalized)!=set(values): return 'query_scope_mismatch','理财参数范围与请求不符，尚未执行。'
        for key,value in values.items():
            a=normalized[key]
            if key=='amount' and value is not None:
                try: equal=not isinstance(a,bool) and Decimal(str(a))==Decimal(value)
                except (InvalidOperation,ValueError,TypeError): equal=False
            else: equal=a==value and not (isinstance(a,bool) and not isinstance(value,bool))
            if not equal: return 'query_scope_mismatch','计划未保留产品、持仓、卡、金额或筛选条件，尚未执行。'
    if not plan_steps:
        if len(steps)!=1 or wanted.get('base_parts') or wanted.get('conditions'):
            return 'query_scope_mismatch','多个目标和条件必须形成完整依赖计划。'
        return None
    wealth_steps=[s for s in plan_steps if s.tool_name in PG04_WEALTH_TOOLS]
    expected_conditions={c['index']:c for c in wanted.get('conditions',[])}
    by_id={s.step_id:s for s in plan_steps}
    for index,step in enumerate(wealth_steps):
        c=step.condition; expected=expected_conditions.get(index)
        if (c is None)!=(expected is None):
            return 'query_scope_mismatch','理财步骤遗漏或添加了条件，尚未执行。'
        if c is None: continue
        source=by_id.get(c.source_step_id)
        fields=({'expense':'data.total_spending'} if source and source.tool_name=='get_monthly_spending' else
            {'expense':'data.totals.expense','income':'data.totals.income','cash_flow':'data.totals.net_cash_flow'}
            if source and source.tool_name=='get_monthly_cash_flow_report' else {})
        if (not source or source.arguments.get('month') not in expected['months'] or
            source.arguments.get('card_id')!=expected['card_id'] or c.field_path!=fields.get(expected['metric']) or
            c.operator!=expected['operator'] or isinstance(c.value,bool) or not isinstance(c.value,(int,float)) or
            Decimal(str(c.value))!=Decimal(expected['value'])):
            return 'query_scope_mismatch','理财条件的期间、卡、指标、比较符或阈值未保留，尚未执行。'
    if any(s.condition is not None for s in plan_steps if s.tool_name not in PG04_WEALTH_TOOLS):
        return 'query_scope_mismatch','查询不能被附加条件后跳过，尚未执行。'
    # Preserve every explicit sequential phase, including pre/post bill reads.
    if ['wealth' if s.tool_name in PG04_WEALTH_TOOLS else 'other' for s in plan_steps]!=wanted['order']:
        return 'query_scope_mismatch','计划未保留请求步骤顺序，尚未执行。'
    parents={s.step_id:set(s.depends_on) for s in plan_steps}
    def ancestors(sid):
        seen=set(); pending=list(parents[sid])
        while pending:
            parent=pending.pop()
            if parent not in seen:
                seen.add(parent);pending.extend(parents.get(parent,set())-seen)
        return seen
    for before,after in zip(plan_steps,plan_steps[1:]):
        if before.step_id not in ancestors(after.step_id):
            return 'query_scope_mismatch','先查后交易及交易后查询必须保留步骤依赖，尚未执行。'
    return None
