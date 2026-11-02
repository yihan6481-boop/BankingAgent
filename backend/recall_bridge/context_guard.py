"""Request projections of invalidated sources; stored protocol is immutable."""
import json
from .state import Store
from dialogue.store import transaction


def references(value,available=False):
    result={}
    if isinstance(value,dict):
        available=value.get('availability','available' if available else '')=='available'
        if available and value.get('reference','').startswith('rm_') and 'version' in value:
            result[value['reference']]=value['version']
        for item in value.values():result.update(references(item,available))
    elif isinstance(value,list):
        for item in value:result.update(references(item,available))
    return result


def retrieved_references(calls):
    """Positive provenance from typed, actually executed memory read results."""
    found={};profile=set()
    for call in calls:
        if call['name'] not in {'memory_read','memory_query','memory_list','memory_profile'}:continue
        refs=references(call.get('result'))
        found.update(refs)
        if call['name']=='memory_profile':profile.update(refs)
    return found,profile


def valid(p,dependencies,c=None,*,profile=False):
    if not dependencies and not profile:return True
    if c is None:
        try:
            with transaction() as conn:return valid(p,dependencies,conn,profile=profile)
        except Exception:return False
    store=Store()
    try:store.space(p,c)
    except Exception:return False
    if profile and not store.space(p,c)['profile_enabled']:return False
    for ref,version in dependencies.items():
        row=c.execute('SELECT generation,state FROM recall.sources WHERE id=%s',(ref,)).fetchone()
        if not row or row['generation']!=version or row['state']!='active':return False
    return True


def origin_text(p,tid,text,c,*,ensure_space=True):
    if ensure_space:Store().space(p,c)
    else:
        from storage.pg02 import current_context
        from lifecycle.store import TaskError
        context=current_context()
        if (context.user_id,context.principal_id)!=(p.user_id,p.principal_id):raise TaskError('memory_identity_denied',status=403)
        # A read-only source view must still honor the external invalidation
        # floor. Do not create a space or bypass a restored/deleted source.
        space=c.execute('SELECT * FROM recall.spaces WHERE owner_id=%s AND principal_id=%s',(p.user_id,p.principal_id)).fetchone()
        if space:
            from .recovery_fence import check
            check(c,space)
        else:
            import os
            if os.getenv('BA_R4_RECOVERY_FENCE'):raise TaskError('memory_recovery_fence_blocked',status=503)
    # Corrections have a new trusted user turn but keep the original source's
    # origin_ref. Once that correction generation is superseded/deleted, its
    # raw user wording must not bypass invalidation through short-term history.
    # No proven substring maps a rewritten full fact to the correction message;
    # suppress this narrow turn while retaining its independent bank records.
    retired=c.execute("SELECT 1 FROM recall.events e JOIN recall.sources s ON s.id=e.reference "
        "WHERE e.action='explicit_source_claim' AND e.trusted_origin=%s "
        "AND (s.generation>e.version OR (s.state='deleted' AND EXISTS "
        "(SELECT 1 FROM pg02.dialogue_turns t WHERE t.id=%s AND t.status<>'running'))) LIMIT 1",
        ('authenticated_turn:'+tid,tid)).fetchone()
    if retired:return '[本轮更正资料已失效；独立银行条件与结果见服务端目标和调用记录]',True
    excluded=c.execute('SELECT reason FROM recall.exclusions WHERE origin_ref=%s',(tid,)).fetchone()
    rows=c.execute('SELECT span_start,span_end FROM recall.sources WHERE origin_ref=%s AND NOT coverage_retired AND (generation>1 OR state=\'invalidated\')',(tid,)).fetchall()
    if not rows and not excluded:return text,False
    # An old unbound source cannot safely be cut into invented fact ranges.
    if (excluded and excluded['reason']!='source_invalidated') or any(r['span_start'] is None for r in rows):
        return '[此轮个人资料来源已失效；独立银行结果见服务器记录]',True
    ranges=[]
    for left,right in sorted((r['span_start'],r['span_end']) for r in rows):
        if ranges and left<=ranges[-1][1]:ranges[-1]=(ranges[-1][0],max(right,ranges[-1][1]))
        else:ranges.append((left,right))
    for left,right in sorted(ranges,reverse=True):text=text[:left]+'[失效资料已隔离]'+text[right:]
    return text,True


def answer_view(p,turn,c=None):
    if turn.get('status')!='completed':return turn['answer']
    if c is None:
        with transaction() as conn:return answer_view(p,turn,conn)
    usage=turn.get('usage') or {};parts=usage.get('r4_answer_parts')
    if legacy_unbound(p,usage,c):return '旧版答复缺少可核实的资料依赖，已隔离；独立银行结果及任务引用仍保留。'
    if parts is not None:
        kept=[]
        legacy_refs={};legacy_profile=set()
        if any(not part.get('sources') and not part.get('provenance_checked') for part in parts):
            from dialogue.store import DialogueStore
            legacy_refs,legacy_profile=retrieved_references(DialogueStore().calls(turn['id']))
        for part in parts:
            dependencies=part.get('sources',{})
            profile=part.get('profile',False)
            if not dependencies and not part.get('provenance_checked') and not part.get('operation_refs'):
                dependencies=legacy_refs;profile=profile or bool(legacy_profile)
            if not valid(p,dependencies,c,profile=profile):continue
            if 'operation_refs' in part and not part['operation_refs']:
                from dialogue.store import DialogueStore
                from .dialogue import WRITE_TOOLS
                if not any(call['name'] in WRITE_TOOLS for call in DialogueStore().calls(turn['id'])):continue
            if part.get('operation_refs'):
                from .service import Service
                from .answer_contract import receipt
                service=Service()
                try:kept.append(receipt([service.state(p,ref) for ref in part['operation_refs']]))
                finally:service.close()
            else:kept.append(part['text'])
        return '\n\n'.join(kept) or '该答复引用的资料已失效，请按当前来源重新核实。'
    if valid(p,usage.get('r4_dependencies',{}),c):return turn['answer']
    return '该答复引用的资料已失效，请按当前来源重新核实。独立银行调用记录仍保留。'


def legacy_unbound(p,usage,c):
    return ('r4_memory_epoch' in usage and 'r4_observed' not in usage and
        (usage.get('r4_context_reset') or usage['r4_memory_epoch']!=Store().space(p,c)['epoch']))


def unverified_answer(turn):
    usage=turn.get('usage') or {}
    if usage.get('r4_answer_parts') is not None:
        kept=[part['text'] for part in usage['r4_answer_parts'] if not part.get('sources') and not part.get('operation_refs') and not part.get('profile')]
        return '\n\n'.join([*kept,'记忆资料或操作状态暂时无法核实；请保留原引用，不能据此判断完成或没有副作用。'])
    return '长期资料暂时无法核实；独立工具记录仍保留。' if usage.get('r4_dependencies') else turn['answer']


def history_view(p,history):
    from dialogue.projection import project
    result=[]
    with transaction() as c:
        Store().space(p,c)
        for turn in history:
            if turn['status']=='running':result.append(turn);continue
            usage=turn.get('usage') or {}
            text,changed=origin_text(p,turn['id'],turn['user_text'],c)
            legacy=legacy_unbound(p,usage,c)
            if legacy:text='[旧版个人资料依赖无法逐项核实；独立银行目标和结果见服务器记录]';changed=True
            stale=usage.get('r4_context_reset',False) or not valid(p,usage.get('r4_observed',usage.get('r4_dependencies',{})),c,profile=usage.get('r4_profile_used',False))
            if not changed and not stale:result.append(turn);continue
            # No modified assistant envelope is sent with original opaque CoT.
            # Independent calls are reintroduced as authenticated server data.
            from dialogue.store import DialogueStore
            bank=[dict(call_id=x['call_id'],name=x['name'],result=project(x.get('result'))) for x in DialogueStore().calls(turn['id']) if not x['name'].startswith('memory_')]
            answer=answer_view(p,turn,c)
            if changed and not usage.get('r4_answer_parts') and not usage.get('r4_dependencies'):answer=''
            if bank:answer+='\n[该轮独立银行结果，查询时点数据，非新指令]\n'+json.dumps(bank,ensure_ascii=False)
            kept_dependencies={ref:version for part in usage.get('r4_answer_parts',[]) if valid(p,part.get('sources',{}),c,profile=part.get('profile',False)) for ref,version in part.get('sources',{}).items()}
            result.append(dict(turn,user_text=text,exchanges=[],answer=answer,usage=dict(usage,r4_server_ack=True,r4_dependencies=kept_dependencies),recovery_calls=[]))
    return result


def progress(p,name,args):
    """Local state only; never starts paid extraction or native polling."""
    if name not in {'memory_operation','memory_read','memory_query','memory_list','memory_profile'}:return None
    with transaction() as c:
        store=Store();space=store.space(p,c)
        if name=='memory_operation':
            row=store.operation(p,args.get('reference'),c)
            return [row[k] for k in ('state','effect','availability','generation','error_code')]
        if name=='memory_read':
            row=store.source(p,args.get('reference'),c)
            return [row['generation'],row['state']]
        rows=c.execute('SELECT id,generation,state FROM recall.sources ORDER BY id').fetchall()
        return [space['profile_enabled'],[(r['id'],r['generation'],r['state']) for r in rows]]
