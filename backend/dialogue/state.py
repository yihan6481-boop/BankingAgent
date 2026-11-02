"""Conversation-scoped read-through state. References are not authorization."""
from datetime import datetime,timezone
from foundation.snapshot import resolve,TERMINAL
from foundation.drafts import GoalStore,public
from .projection import project
from .store import DialogueStore


def for_model(state,max_bytes=12000):
    """UI keeps complete state; the model sees bounded read-through pointers."""
    import json
    from .context import ContextError
    state=dict(state);items=state.get('items',[]);focus=(state.get('focus') or {}).get('reference')
    # Historical grants are discovery pointers, not bank facts for every new
    # question. Read the exact owned source when the current goal needs it.
    state['authorized_queries']=[dict(turn_id=r['turn_id'],call_id=r['call_id'],tool_name=r['tool_name'],
        queried_at=(r.get('data') or {}).get('queried_at'),historical=True,status=r.get('status'),
        scope='original query snapshot available via call_result_read; not a new query, global grant or current goal') for r in state.get('authorized_queries',[])]
    active=[x for x in items if x.get('kind')!='result' and x.get('status') not in TERMINAL|{'closed','snapshot','linked'}]
    preferred=sorted(active,key=lambda x:x.get('reference')==focus,reverse=True)
    chosen=[];used=0
    for item in preferred:
        n=len(json.dumps(item,ensure_ascii=False).encode())
        if len(chosen)<8 and used+n<=max_bytes:chosen.append(item);used+=n
    # Recent terminal objects are discovery hints only, never live facts.
    ids={x['reference'] for x in chosen}
    index=[{k:x[k] for k in ['reference','kind','status','revision'] if k in x} for x in items if x.get('reference') not in ids][-20:]
    state.update(items=chosen,object_index=index,omitted_objects=len(items)-len(chosen),truncated=len(chosen)<len(items),
        unfinished=[{k:x[k] for k in ['reference','kind','status','revision'] if k in x} for x in chosen],
        read_through='省略不代表不存在；使用draft_list/task_list/draft_read/task_read定位并核实目标，历史快照不是当前授权。')
    if len(json.dumps(state,ensure_ascii=False).encode())>max_bytes+6000:raise ContextError('required_context_exceeds_budget')
    return state


def snapshot(p,cid):
    from .tools import task_allowed
    s=DialogueStore();s.load(p,cid)
    items=[];focus=None;at=datetime.now(timezone.utc).isoformat()
    for link in s.links(p,cid):
        ref=link['reference']
        if link['kind']=='task':task_allowed(p,ref)
        value=resolve(p,ref)
        if value.get('status')=='preparing':
            pending=GoalStore().load(p,ref)['fields'].get('_preparation',{})
            value=dict(value,verification_reference=pending.get('task_id'),next_actions=['task.recover'],execution_outcome='unknown')
        source='lifecycle_tasks' if link['kind']=='task' else 'tool_goals/verified_query' if link['kind']=='result' else 'tool_goals'
        if link['kind']=='result':
            stored=GoalStore().load(p,ref);f=stored['fields']
            # Trusted aggregates copied verbatim. No arithmetic or inferred zero.
            results=[]
            for entry in f['result'].get('results',[]):
                current=entry.get('current',{})
                comparison=entry.get('comparison')
                if comparison:
                    comparison=dict(comparison,base={k:v for k,v in comparison['base'].items() if k not in {'details','groups'}})
                results.append({k:v for k,v in entry.items() if k not in {'current','plan'}} | {
                    'comparison':comparison,
                    'current':{k:v for k,v in current.items() if k not in {'details','groups'}},
                    'truncated':bool(current.get('details') or current.get('groups'))})
            value=dict(value,fields={k:v for k,v in f.items() if k!='result'} | {'result':{'results':results}},
                       scope='immutable historical query; aggregates from verified_query; use result_read for rows')
        context=dict(link).get('context') or {}
        payment=context.get('payment') if isinstance(context,dict) else None
        extra={}
        if link['kind']=='task' and isinstance(payment,dict):
            from .payment_tools import payment_target
            target=payment_target(p,payment)
            if target:extra['payment_target']=dict(target,scope='server-current payment target; task is authoritative; not authorization')
        value=project(dict(value,source=source,observed_at=at,
            authority_scope='source association only; current task is authoritative' if value.get('status')=='linked' else 'current saved business state' if link['kind']!='result' else 'historical query only',
            association=dict(source_turn=link['source_turn'],source_call=link['source_call'],associated_at=link['associated_at'].isoformat()),
            associated_at=link['associated_at'].isoformat(),**extra))
        items.append(value)
        if link['focused']:focus={'reference':ref,'kind':value['kind'],'scope':'understanding hint only; not authorization or forced pronoun binding'}
    from .read_results import checked_result
    authorized=[dict(turn_id=r['turn_id'],call_id=r['call_id'],tool_name=r['name'],**result) for r in s.authorized_reads(p,cid) if (result:=checked_result(r,p,cid,r['turn_id'])) is not None]
    return dict(protocol='r2-context-2',dialogue_id=cid,focus=focus,items=items,authorized_queries=project(authorized),
        unfinished=[dict(reference=v['reference'],kind=v['kind'],status=v.get('status'),revision=v.get('revision')) for v in items
                    if v['kind']!='result' and v.get('status') not in TERMINAL|{'closed','snapshot','linked'}],
        truncated=False,server_time=at,business_timezone='Asia/Shanghai',
        scope='conversation associations only; live business storage is authoritative; login security is separately revalidated')
