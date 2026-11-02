"""Reject contradictions to a small set of explicit, single-goal literals.

This never chooses a tool or approves a request. Complex/negative/quoted/history
clauses stay with semantic review; a successful check is not proof of intent.
Only the current authenticated turn and the proposed structured input are used.
"""
from decimal import Decimal, InvalidOperation
import re

_COMPLEX=re.compile(r'不|别|排除|除了|并非|上次|刚才|之前|曾|说|例如|假设|如果|或者|同时|分别|对比|比较|[“”「」\"\n]')
_MONTH=re.compile(r'(?<!\d)(1[0-2]|0?[1-9]|十一|十二|十|[一二三四五六七八九])月')
_NUM={v:i for i,v in enumerate(['一','二','三','四','五','六','七','八','九','十','十一','十二'],1)}

def date_projection(text,p):
    """Remove whole catalogued object tokens, not date substrings guessed by a model.

    Existence only disambiguates a literal name; it grants no authority and does
    not prove the request. The original text still enters semantic review.
    Bare calendar tokens remain ambiguous without explicit quoting.
    """
    if p is None or not (_MONTH.search(text) or re.search(r'\d{4}年',text)):return text
    from .store import transaction
    from lifecycle.store import TaskError
    try:
        with transaction() as c:
            rows=c.execute('''SELECT literal FROM (
                SELECT merchant AS literal FROM pg02.transactions
                UNION SELECT alias FROM pg02.merchant_aliases
                UNION SELECT value->>'merchant' FROM billvision.records
                ) known WHERE literal IS NOT NULL AND length(literal)>1
                AND position(literal in %s::text)>0
                ORDER BY length(literal) DESC,literal LIMIT 65''',(text,)).fetchall()
    except Exception:raise TaskError('storage_unavailable') from None
    if len(rows)>64:raise TaskError('semantic_intent_uncertain')
    for row in rows:
        name=row['literal']
        if _MONTH.fullmatch(name) or re.fullmatch(r'\d{4}年(?:[一二三四五六七八九十\d]+月)?',name):continue
        text=text.replace(name,' ')
    return text

def contradiction(text,name,args,p=None):
    if not isinstance(text,str) or _COMPLEX.search(text):return None
    periods=[];amounts=[]
    if name=='query_create':periods=[args.get('fields',{}).get('selection',{}).get('period') or {}]
    elif name in {'query_patch','query_execute'} and p is not None:
        from foundation.drafts import GoalStore,merge
        target=GoalStore().load(p,args['reference'])
        if target['kind']=='query':
            fields=merge(target['fields'],args.get('fields',{})) if name=='query_patch' else target['fields']
            from data_query import QueryPlan
            fields=QueryPlan.model_validate(fields).model_dump(mode='json')
            periods=[fields.get('selection',{}).get('period') or {}]
    elif name=='query_data':periods=[q.get('selection',{}).get('period') or {} for q in args.get('plans',[])]
    elif name=='query_financial_records':periods=[dict(start=args.get('start'),end_exclusive=args.get('end_exclusive'))]
    elif name in {'get_monthly_spending','get_transactions_by_month','get_category_spending','get_monthly_cash_flow_report','get_expense_clues'}:
        periods=[dict(start=args['month']+'-01')]
    elif name=='get_annual_cash_flow_report':periods=[dict(start=str(args['year'])+'-01-01',year_only=True)]
    elif name in {'transfer_create','transfer_patch'}:
        fields=args.get('fields',{})
        if name=='transfer_patch' and p is not None:
            from foundation.drafts import GoalStore,merge
            target=GoalStore().load(p,args['reference'])
            if target['kind']=='transfer':fields=merge(target['fields'],fields)
        if fields.get('amount') is not None:amounts=[fields['amount']]
    elif name=='transfer_money' and args.get('amount') is not None:amounts=[args['amount']]
    elif name=='task_modify' and args.get('changes',{}).get('amount') is not None:amounts=[args['changes']['amount']]
    elif name=='create_task_plan':
        # Multiple steps have distinct semantic roles; do not cross-bind them.
        steps=args.get('steps',[])
        if len(steps)==1:
            step=steps[0];child_name=step.get('tool_name');child_args=step.get('arguments',{})
            from .business import ALLOWED,registry
            if child_name in ALLOWED:child_args=registry().get(child_name).argument_model.model_validate(child_args).model_dump(mode='json')
            return contradiction(text,child_name,child_args,p)
    if len(periods)==1:
        date_text=date_projection(text,p)
        start=periods[0].get('start') or '';years=set(re.findall(r'(?<!\d)(\d{4})年',date_text));months={_NUM.get(m,int(m) if m.isdecimal() else 0) for m in _MONTH.findall(date_text)}
        if (years or months) and not start:return 'request_scope_conflict'
        if len(years)==1 and start[:4]!=next(iter(years)):return 'request_scope_conflict'
        if not periods[0].get('year_only') and len(months)==1 and int(start[5:7])!=next(iter(months)):return 'request_scope_conflict'
    # Only an explicit single amount followed by currency, attached to a
    # transfer/amount clause; unrelated product prices are not amount bindings.
    anchors=re.findall(r'(?:转(?:账)?|汇(?:款)?|金额(?:为|是)?|改(?:成|为)?)\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元',text)
    if len(anchors)==1 and not amounts and name in {'transfer_create','transfer_money'}:return 'request_scope_conflict'
    if len(anchors)==1 and len(amounts)==1:
        try:
            if Decimal(anchors[0])!=Decimal(str(amounts[0])):return 'request_scope_conflict'
        except InvalidOperation:return None
    return None
