"""Exact structured provenance from executed tools; never language interpretation."""
from contextlib import contextmanager
from contextvars import ContextVar

binding=ContextVar('dialogue_tool_binding',default=None)
LISTS={'draft_list','task_list','context_snapshot'}
MUTATIONS={'transfer_create','query_create','transfer_patch','query_patch','query_execute',
           'draft_close','task_prepare','task_modify','task_pause','task_cancel','task_resume',
           'report_card_lost','set_daily_limit','cancel_subscription','buy_wealth_product','redeem_wealth_product',
           'create_task_plan','task_schedule','task_takeover','task_return','task_note','task_rollback'}


@contextmanager
def tool_scope(cid,tid,call_id):
    token=binding.set((cid,tid,call_id))
    try:yield
    finally:binding.reset(token)


def references(name,args,result):
    """Only documented response locations, never a recursive arbitrary-key scan."""
    from .payment_tools import PAYMENT_TOOL_NAMES, references as payment_references
    if name in PAYMENT_TOOL_NAMES:return payment_references(name,args,result)
    from recall_bridge.dialogue import DESCRIPTIONS as memory_descriptions
    if name in memory_descriptions:return []  # R4 references are never bank tasks/goals.
    if name in LISTS or not isinstance(result,dict):return []
    data=result.get('data')
    if not isinstance(data,dict):return []
    refs=[]
    draft=data.get('draft')
    if isinstance(draft,dict) and draft.get('reference'):
        refs.append(draft['reference'])
        if draft.get('status')=='linked' and draft.get('fields',{}).get('task_id'):refs.append(draft['fields']['task_id'])
    if data.get('kind') in {'query','transfer','result'} and data.get('reference'):refs.append(data['reference'])
    for task in [data,data.get('prepared',{}),data.get('task',{})]:
        if not isinstance(task,dict):continue
        t=task.get('task',{})
        if isinstance(t,dict) and t.get('task_id'):refs.append(t['task_id'])
        elif isinstance(t,dict) and isinstance(t.get('task'),dict) and t['task'].get('task_id'):refs.append(t['task']['task_id'])
    if result.get('status')=='completed' and args.get('reference'):refs.append(args['reference'])
    return list(dict.fromkeys(refs))


def payment_context(name,args,result):
    from .payment_tools import PAYMENT_TOOL_NAMES, context_for
    if name not in PAYMENT_TOOL_NAMES:return None
    return context_for(name,args,result)


def remember(p,cid,tid,call_id,name,args,result):
    from .store import DialogueStore
    s=DialogueStore();context=payment_context(name,args,result)
    for ref in references(name,args,result):
        s.associate(p,cid,ref,tid,call_id,
                    context=context if context and ref==context.get('task_id') else None)
