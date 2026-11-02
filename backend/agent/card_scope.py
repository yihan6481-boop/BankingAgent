"""Trusted projection of owner-scoped card reads within the existing task.

Scopes originate only from the independently checked goal inventory. No model
field or tool text can create a scope or supply a balance. Funding references
use current task arguments so revisions cannot retain another card's balance.
"""
from copy import deepcopy


def scopes_for(session,task):
    getter=getattr(session,'legacy_card_inventory',None)
    wanted=getter() if getter else None
    if not wanted or wanted.get('error') or len(wanted['goals'])!=len(task.steps):return {}
    scopes={}
    for i,(goal,node) in enumerate(zip(wanted['goals'],task.steps)):
        if goal['kind']!='get_cards' or node.tool_name!='get_cards':continue
        ids=goal.get('card_ids',[])
        writes=[s for s in task.steps[:i] if s.tool_name in {'transfer_money','buy_wealth_product'}]
        source=writes[-1] if writes else None
        if ids:
            source=next((s for s in task.steps[i+1:] if s.tool_name in {'transfer_money','buy_wealth_product'} and
                [s.arguments.get('from_card_id' if s.tool_name=='transfer_money' else 'card_id')]==ids),None)
        if source:scopes[node.step_id]={'funding_step_id':source.step_id}
        elif ids:scopes[node.step_id]={'card_ids':ids}
    return scopes


def project(session,step,result):
    from lifecycle.service import active_scope
    scope=active_scope.get()
    record=scope.record if scope else None
    if not record or step.step_id not in record.card_read_scopes:return result
    restriction=record.card_read_scopes[step.step_id]
    ids=restriction.card_ids
    if restriction.funding_step_id:
        source=next(s for s in session.current_task.steps if s.step_id==restriction.funding_step_id)
        ids=[source.arguments.get('from_card_id' if source.tool_name=='transfer_money' else 'card_id')]
    rows=result.data
    if not isinstance(rows,list) or not ids or None in ids or not set(ids).issubset({r.get('card_id') for r in rows}):
        from .schemas import ToolResult
        return ToolResult(status='error',error_code='query_scope_mismatch',message='指定卡片当前不可读取，后续步骤未执行。')
    selected=deepcopy([r for r in rows if r.get('card_id') in ids])
    return result.model_copy(update={'data':selected,'message':'已核验指定银行卡查询结果。'})
